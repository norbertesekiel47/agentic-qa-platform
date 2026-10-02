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
AS_LAUNCHED_AND_CONTROL = pytest.mark.parametrize(
    "switched", [True, False], ids=["as-launch-does", "control-without-the-switches"]
)


def chromium_for(chromium: BrowserType, *, switched: bool) -> Chromium:
    """Playwright's Chromium as `launch` launches it, or the control."""
    return chromium if switched else WithoutTheSwitches(chromium)


# Each argument the browser process must run with, written out here rather
# than read from the sandbox module, so a change to what `launch` passes
# fails this test until it is made on purpose. Playwright passes `proxy` as
# the last two.
EXPECTED_SWITCHES = [
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-quic",
    "--host-resolver-rules=MAP * ^NOTFOUND, EXCLUDE 127.0.0.1",
    "--proxy-server=http://launch-proxy.invalid:1",
    "--proxy-bypass-list=<-loopback>",
]


def arguments_in(command: str, expected: list[str]) -> list[str]:
    """Each of `expected` that `command` holds as a whole argument. Linux
    separates a command line's arguments with NUL and macOS's ps with a
    space; no expected argument ends in either."""
    padded = f" {command.replace('\x00', ' ').strip()} "
    return [argument for argument in expected if f" {argument} " in padded]


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


@AS_LAUNCHED_AND_CONTROL
def test_webrtc_gathers_no_candidate_and_sends_no_udp(*, switched: bool) -> None:
    with udp_canary() as canary:
        stun = f"stun:127.0.0.1:{canary.getsockname()[1]}"

        async def scenario() -> list[str]:
            async with (
                async_playwright() as playwright,
                egress_proxy() as egress,
                open_browser_session(
                    chromium_for(playwright.chromium, switched=switched), egress=egress
                ) as session,
            ):
                candidates: list[str] = await session.page.evaluate(GATHER, stun)
                return candidates

        candidates = asyncio.run(scenario())
        sent = datagrams(canary)

    if switched:
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


@AS_LAUNCHED_AND_CONTROL
def test_the_browser_resolves_no_name_itself(*, switched: bool) -> None:
    # The resolver rule fails every lookup a context makes: a context that
    # names its proxy by a name, here localhost, can't reach it. (Chromium
    # answers localhost itself, so this shows the rule at work, not a query
    # that would have left; packets are the hostile-page suite's.) The
    # session's own proxy, at the address `EgressProxy` listens on, must stay
    # reachable: the rule's exclusion has to name that address, and this test
    # fails if the two drift apart.
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> tuple[int | str, int | str]:
            async with (
                async_playwright() as playwright,
                egress_proxy(start) as egress,
                open_browser_session(
                    chromium_for(playwright.chromium, switched=switched), egress=egress
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
    if switched:
        assert through_the_name == "net::ERR_PROXY_CONNECTION_FAILED"
        assert len(origin.seen) == 1
    else:
        assert through_the_name == 200


@AS_LAUNCHED_AND_CONTROL
@pytest.mark.parametrize("playwright_opts_out", [False, True])
def test_a_context_without_the_sessions_proxy_has_no_way_out(
    *, switched: bool, playwright_opts_out: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Code that opens a context of its own, outside the session, names no
    # proxy. The launch-level proxy is the one it gets, loopback included,
    # even where the runner's environment sets the variable that stops
    # Playwright sending loopback through a proxy.
    if playwright_opts_out:
        monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> int | str:
            async with (
                async_playwright() as playwright,
                egress_proxy(start) as egress,
                open_browser_session(
                    chromium_for(playwright.chromium, switched=switched), egress=egress
                ) as session,
            ):
                browser = session.page.context.browser
                assert browser is not None
                stray = await browser.new_context()
                return await load(await stray.new_page(), f"{start}/")

        outcome = asyncio.run(scenario())

    if switched:
        assert outcome == "net::ERR_PROXY_CONNECTION_FAILED"
        assert origin.seen == []
    else:
        # Without it, the context connects straight to the origin.
        assert outcome == 200
        assert len(origin.seen) == 1


@AS_LAUNCHED_AND_CONTROL
def test_chromium_runs_with_the_transport_switches(*, switched: bool) -> None:
    async def scenario() -> tuple[str, list[tuple[Sequence[str], ProxySettings]]]:
        async with async_playwright() as playwright, egress_proxy() as egress:
            chromium = chromium_for(playwright.chromium, switched=switched)
            async with open_browser_session(chromium, egress=egress) as session:
                command = command_line_of(await browser_pid(session))
            if isinstance(chromium, WithoutTheSwitches):
                return command, chromium.left_out
            return command, []

    command, left_out = asyncio.run(scenario())

    found = arguments_in(command, EXPECTED_SWITCHES)
    if switched:
        assert found == EXPECTED_SWITCHES, command
    else:
        assert found == []
        # The control leaves out exactly what the launch asked for.
        [(switches, proxy)] = left_out
        as_arguments = [
            *switches,
            f"--proxy-server={proxy['server']}",
            f"--proxy-bypass-list={proxy.get('bypass')}",
        ]
        assert sorted(as_arguments) == sorted(EXPECTED_SWITCHES)
