"""The browser's other ways out, closed at launch (ADR-0026 amendment,
2026-10-02; SECURITY.md §7): WebRTC sends no UDP, QUIC is off, the browser
resolves no name itself, and a context without the session's proxy has no way
out. Each test also runs a control: the same session launched without the
switches and the launch-level proxy, which shows what the test would see if
they were gone. These tests launch real Chromium on the OS that runs them:
Linux in CI, macOS locally."""

import asyncio
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import pytest
from aqa_runner.browser_session import open_browser_session
from aqa_runner.sandbox import Chromium, Environment
from playwright.async_api import (
    Browser,
    BrowserType,
    Error,
    Page,
    ProxySettings,
    async_playwright,
)

from packages.runner.tests.egress_fixtures import egress_proxy, serving
from packages.runner.tests.test_browser_session import browser_pid, command_line_of


class WithoutTheSwitches:
    """Real Chromium, launched through `launch` and its sandbox check, that
    leaves out the switches and the launch-level proxy `launch` asks for: the
    control. It records what it left out."""

    def __init__(self, chromium: BrowserType) -> None:
        self.chromium = chromium
        self.left_out: list[tuple[Sequence[str], ProxySettings]] = []

    async def launch(
        self,
        *,
        chromium_sandbox: bool,
        env: Environment,
        args: Sequence[str],
        proxy: ProxySettings,
    ) -> Browser:
        self.left_out.append((args, proxy))
        return await self.chromium.launch(chromium_sandbox=chromium_sandbox, env=env)


# Each test runs as `launch` launches, then as the control.
LAUNCHES = ["as-launch-does", "control-without-the-switches"]

# Each switch the browser process must run with, written as Chromium reads
# it: QUIC off, WebRTC's UDP only through a proxy, no lookups of its own, and
# the launch-level proxy (Playwright passes `proxy` as these two switches).
EXPECTED_SWITCHES = [
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ^NOTFOUND, EXCLUDE 127.0.0.1",
    "--proxy-server=http://launch-proxy.invalid:1",
    "--proxy-bypass-list=<-loopback>",
]


def chromium_for(launched: str, chromium: BrowserType) -> Chromium:
    return chromium if launched == "as-launch-does" else WithoutTheSwitches(chromium)


@contextmanager
def udp_canary() -> Iterator[socket.socket]:
    """A UDP socket on loopback that a page has no business reaching."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as canary:
        canary.bind(("127.0.0.1", 0))
        canary.settimeout(0.5)
        yield canary


def datagrams(canary: socket.socket) -> int:
    """How many datagrams reached the canary, waiting for stragglers."""
    count = 0
    while True:
        try:
            canary.recv(65536)
        except TimeoutError:
            return count
        count += 1


# ICE gathering with a STUN server: each candidate the page gathers, once
# gathering completes or after 5 s. A STUN binding request is a UDP datagram.
GATHER = """async (stun) => {
    const peer = new RTCPeerConnection({iceServers: [{urls: stun}]});
    peer.createDataChannel("probe");
    const candidates = [];
    peer.onicecandidate = (event) => {
        if (event.candidate) candidates.push(event.candidate.candidate);
    };
    await peer.setLocalDescription(await peer.createOffer());
    await new Promise((done) => {
        setTimeout(done, 5000);
        peer.onicegatheringstatechange = () => {
            if (peer.iceGatheringState === "complete") done();
        };
    });
    peer.close();
    return candidates;
}"""


@pytest.mark.parametrize("launched", LAUNCHES)
def test_webrtc_gathers_no_candidate_and_sends_no_udp(launched: str) -> None:
    with udp_canary() as canary:
        stun = f"stun:127.0.0.1:{canary.getsockname()[1]}"

        async def scenario() -> list[str]:
            async with (
                async_playwright() as playwright,
                egress_proxy() as egress,
                open_browser_session(
                    chromium_for(launched, playwright.chromium), egress=egress
                ) as session,
            ):
                candidates: list[str] = await session.page.evaluate(GATHER, stun)
                return candidates

        candidates = asyncio.run(scenario())
        sent = datagrams(canary)

    if launched == "as-launch-does":
        assert (candidates, sent) == ([], 0)
    else:
        # Without the switch, WebRTC gathers host candidates and its STUN
        # requests reach the canary, past the proxy.
        assert candidates
        assert sent > 0


async def load(page: Page, url: str) -> int | str:
    """The status a navigation got, or the network error it failed with."""
    try:
        response = await page.goto(url)
    except Error as error:
        return error.message.split()[1]
    assert response is not None
    return response.status


@pytest.mark.parametrize("launched", LAUNCHES)
def test_the_browser_resolves_no_name_itself(launched: str) -> None:
    # Any name the browser looked up itself would leave in a DNS query: DNS
    # prefetch, a STUN server's name. Every name is the egress proxy's to
    # resolve. A context that names its proxy by a name, here localhost,
    # shows the lookup failing; the proxy's own address is spared.
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> tuple[int | str, int | str]:
            async with (
                async_playwright() as playwright,
                egress_proxy(start) as egress,
                open_browser_session(
                    chromium_for(launched, playwright.chromium), egress=egress
                ) as session,
            ):
                browser = session.page.context.browser
                assert browser is not None
                port = egress.url.rsplit(":", 1)[1]
                by_name = await browser.new_context(
                    proxy={"server": f"http://localhost:{port}"}
                )
                return (
                    await load(session.page, f"{start}/"),
                    await load(await by_name.new_page(), f"{start}/"),
                )

        through_the_address, through_the_name = asyncio.run(scenario())

    assert through_the_address == 200
    if launched == "as-launch-does":
        assert through_the_name == "net::ERR_PROXY_CONNECTION_FAILED"
        assert len(origin.seen) == 1
    else:
        assert through_the_name == 200


@pytest.mark.parametrize("launched", LAUNCHES)
def test_a_context_without_the_sessions_proxy_has_no_way_out(launched: str) -> None:
    # Code that opens a context of its own, outside the session, names no
    # proxy. The launch-level proxy is the one it gets.
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> int | str:
            async with (
                async_playwright() as playwright,
                egress_proxy(start) as egress,
                open_browser_session(
                    chromium_for(launched, playwright.chromium), egress=egress
                ) as session,
            ):
                browser = session.page.context.browser
                assert browser is not None
                stray = await browser.new_context()
                return await load(await stray.new_page(), f"{start}/")

        outcome = asyncio.run(scenario())

    if launched == "as-launch-does":
        assert outcome == "net::ERR_PROXY_CONNECTION_FAILED"
        assert origin.seen == []
    else:
        # Without it, the context connects straight to the origin.
        assert outcome == 200
        assert len(origin.seen) == 1


@pytest.mark.parametrize("launched", LAUNCHES)
def test_chromium_runs_with_the_transport_switches(launched: str) -> None:
    async def scenario() -> tuple[str, list[tuple[Sequence[str], ProxySettings]]]:
        async with async_playwright() as playwright, egress_proxy() as egress:
            chromium = WithoutTheSwitches(playwright.chromium)
            used = playwright.chromium if launched == "as-launch-does" else chromium
            async with open_browser_session(used, egress=egress) as session:
                command = command_line_of(await browser_pid(session))
            return command, chromium.left_out

    command, left_out = asyncio.run(scenario())

    found = [switch for switch in EXPECTED_SWITCHES if switch in command]
    if launched == "as-launch-does":
        assert found == EXPECTED_SWITCHES, command
    else:
        # The control leaves out exactly what the launch asked for.
        assert found == []
        [(switches, proxy)] = left_out
        as_switches = [
            *switches,
            f"--proxy-server={proxy['server']}",
            f"--proxy-bypass-list={proxy.get('bypass')}",
        ]
        assert sorted(as_switches) == sorted(EXPECTED_SWITCHES)
