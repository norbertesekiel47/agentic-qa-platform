"""Scenarios the hostile-page suite (`tests/test_hostile_pages.py`) runs in the
packet capture's child process (`tests/packet_capture.py`), which puts the
repository's root on the path: only there do the runner's test modules
import as `packages.…`, so pytest's own process never imports this module.

Each scenario serves a hostile page (`tests/hostile_pages.py`) from an
allowed origin, opens a canary for each attempt it makes, runs it in a real
browser session behind the egress proxy, and returns what every party saw:
the canaries, routing's records, the gate's refusals, the origin's requests
and the connections the proxy opened."""

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import ssl
import subprocess
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from aqa_runner.browser_session import open_browser_session
from aqa_runner.egress import Connection, EgressGate, EgressPolicy, Requester
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.sandbox import Chromium, Environment
from playwright.async_api import (
    Browser,
    BrowserType,
    Page,
    Playwright,
    ProxySettings,
    async_playwright,
)

from packages.runner.tests.test_runner_requests import certificate
from packages.runner.tests.test_transports import WithoutTheSwitches
from tests.hostile_pages import PAGES, PRELUDE, HostilePage, Kind, Target

# Where each kind of canary listens.
LOOPBACK: dict[Kind, str] = {
    "tcp4": "127.0.0.1",
    "tcp6": "::1",
    "udp4": "127.0.0.1",
    "udp6": "::1",
}

type Endpoint = tuple[str, int]


class AttributedGate(EgressGate):
    """The run's egress gate, which also records both ends of every
    connection it opens upstream, so a packet can be told to be the egress
    proxy's: packets don't name the process that sent them."""

    def __init__(self, policy: EgressPolicy) -> None:
        super().__init__(policy)
        self.upstreams: list[tuple[Endpoint, Endpoint]] = []

    async def connect(
        self,
        host: str,
        port: int,
        requester: Requester,
        *,
        tls: ssl.SSLContext | None = None,
    ) -> Connection:
        reader, writer = await super().connect(host, port, requester, tls=tls)
        local = writer.get_extra_info("sockname")[:2]
        remote = writer.get_extra_info("peername")[:2]
        self.upstreams.append(((local[0], local[1]), (remote[0], remote[1])))
        return reader, writer


class TrustingTheFixture:
    """A test double for the QUIC page: real Chromium, launched through
    `launch` and its sandbox check, with everything `launch` asks for plus
    one switch that trusts the fixture's key, so its https origin's
    `Alt-Svc` counts. Without the protections (`protected=False`), it
    launches with that switch alone: the control, which shows what the page
    sends where nothing stops it."""

    def __init__(self, chromium: BrowserType, key: str, *, protected: bool) -> None:
        self.chromium = chromium
        self.trust = f"--ignore-certificate-errors-spki-list={key}"
        self.protected = protected

    async def launch(
        self,
        *,
        chromium_sandbox: bool,
        env: Environment,
        args: Sequence[str],
        proxy: ProxySettings,
    ) -> Browser:
        if self.protected:
            return await self.chromium.launch(
                chromium_sandbox=chromium_sandbox,
                env=env,
                args=[*args, self.trust],
                proxy=proxy,
            )
        return await self.chromium.launch(
            chromium_sandbox=chromium_sandbox, env=env, args=[self.trust]
        )


def spki_hash_of(certificate: Path) -> str:
    """The base64 SHA-256 of a certificate's public key (its
    SubjectPublicKeyInfo), as `--ignore-certificate-errors-spki-list` takes
    it. A PEM public key is that structure in base64."""
    pem = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-pubkey"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    return base64.b64encode(hashlib.sha256(base64.b64decode(body)).digest()).decode()


@dataclass
class Canary:
    """A listener an attempt aims at, and how many connections or datagrams
    reached it."""

    target: Target
    port: int = 0
    received: int = 0

    def written(self) -> Endpoint:
        """Its host as the page writes it, and its port."""
        host = LOOPBACK[self.target.kind]
        written = self.target.host or (f"[{host}]" if ":" in host else host)
        return written, self.port


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, canary: Canary) -> None:
        self.canary = canary

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        del data, addr  # only that one arrived matters
        self.canary.received += 1


@contextlib.asynccontextmanager
async def listening(targets: dict[str, Target]) -> AsyncIterator[dict[str, Canary]]:
    """A canary for each target, listening until the block ends."""
    loop = asyncio.get_running_loop()
    canaries = {name: Canary(target) for name, target in targets.items()}
    async with contextlib.AsyncExitStack() as stack:
        for canary in canaries.values():
            address = LOOPBACK[canary.target.kind]
            if canary.target.kind.startswith("udp"):
                transport, _ = await loop.create_datagram_endpoint(
                    functools.partial(_Datagrams, canary), local_addr=(address, 0)
                )
                stack.callback(transport.close)
                canary.port = transport.get_extra_info("sockname")[1]
                continue

            def accepted(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
                canary: Canary = canary,
            ) -> None:
                del reader  # a connection that arrived is enough
                canary.received += 1
                writer.close()

            server = await asyncio.start_server(accepted, address, 0)
            stack.push_async_callback(server.wait_closed)
            stack.callback(server.close)
            canary.port = server.sockets[0].getsockname()[1]
        yield canaries


@dataclass
class Site:
    """An allowed origin: what it serves, where each connection it accepted
    came from, and each request it got, with where it came from."""

    origin: str
    pages: dict[str, tuple[str, str]]
    alt_svc: str | None
    peers: list[Endpoint] = field(default_factory=list)
    requests: list[tuple[Endpoint, str]] = field(default_factory=list)


class _Handler(BaseHTTPRequestHandler):
    """Serves the site's pages by path, a 307 to any URL at
    `/redirect?to=`, which keeps a POST a POST, and an empty answer to
    anything else. Not `egress_fixtures.serving`'s handler: that one serves
    no page of a test's own, redirects with a 302, which turns a POST into a
    GET, and records no peer, which the attribution check needs."""

    site: Site

    def setup(self) -> None:
        # Once per connection, whether or not a request follows.
        super().setup()
        self.site.peers.append(self.client_address[:2])

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._answer()

    def _answer(self) -> None:
        self.site.requests.append((self.client_address[:2], self.path))
        target = urlsplit(self.path)
        if target.path == "/redirect":
            self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
            self.send_header("Location", parse_qs(target.query)["to"][0])
        elif target.path in self.site.pages:
            kind, body = self.site.pages[target.path]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind)
            encoded = body.encode()
            self.send_header("Content-Length", str(len(encoded)))
            self._alt_svc()
            self.end_headers()
            self.wfile.write(encoded)
            return
        else:
            self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self._alt_svc()
        self.end_headers()

    def _alt_svc(self) -> None:
        if self.site.alt_svc is not None:
            self.send_header("Alt-Svc", self.site.alt_svc)


@contextlib.contextmanager
def hosting(
    pages: dict[str, tuple[str, str]],
    *,
    tls: ssl.SSLContext | None = None,
    alt_svc: str | None = None,
) -> Iterator[Site]:
    """A site on 127.0.0.1, over https with `tls`."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    scheme = "http"
    if tls is not None:
        # The handshake in the handler's thread, after `setup` has recorded
        # the peer, not in the serve loop, which drops a failed one unseen.
        server.socket = tls.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False
        )
        scheme = "https"
    site = Site(f"{scheme}://127.0.0.1:{server.server_port}", pages, alt_svc)
    server.RequestHandlerClass = type("Handler", (_Handler,), {"site": site})
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def page_markup(page: HostilePage, targets: dict[str, str]) -> str:
    """The page: its head, and its script run over its targets, the
    promise of what it saw left on `window.hostile`."""
    return (
        f"<!doctype html><html><head>{page.head}</head><body><script>{PRELUDE}\n"
        f"window.hostile = ({page.script})({json.dumps(targets)});"
        "</script></body></html>"
    )


async def run_page(method: str, *, protected: bool = True) -> dict[str, object]:
    """Run `method`'s hostile page in a browser session and return what each
    party saw. Unprotected, the page runs in a context with no proxy, in a
    browser launched without the switches and the launch-level proxy: the
    control for what the page would send."""
    page = PAGES[method]
    with contextlib.ExitStack() as stack:
        async with listening(page.targets) as canaries:
            targets = {
                name: "{}:{}".format(*canary.written())
                for name, canary in canaries.items()
            }
            key, sites = None, []
            if page.trusts_the_fixture:
                directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                tls = certificate(directory, "127.0.0.1")
                key = spki_hash_of(directory / "cert.pem")
                alt_svc = f'h3=":{canaries["http3"].port}"; ma=3600'
                tls_site = stack.enter_context(hosting({}, tls=tls, alt_svc=alt_svc))
                sites.append(tls_site)
                targets["tls"] = tls_site.origin
            site = stack.enter_context(hosting({}))
            sites.append(site)
            targets["origin"] = site.origin
            site.pages["/"] = ("text/html", page_markup(page, targets))
            for path, script in page.scripts.items():
                site.pages[path] = ("text/javascript", script)
            gate = gate_for(sites)
            async with async_playwright() as playwright, EgressProxy(gate) as egress:
                chromium = chromium_for(playwright, key, protected=protected)
                async with open_browser_session(chromium, egress=egress) as session:
                    tab = session.page if protected else await stray_page(session.page)
                    await tab.goto(f"{site.origin}/")
                    outcomes = await tab.evaluate("window.hostile")
                    # The document each of the page's frames ended on.
                    frames = [each.url for each in tab.frames if each != tab.main_frame]
                    # A round trip through the proxy once every attempt has
                    # settled, so a connection an attempt started has
                    # reached it.
                    after = await session.page.context.request.get(
                        f"{site.origin}/after"
                    )
                    assert after.ok
                return report(
                    egress, gate, sites, canaries, outcomes=outcomes, frames=frames
                )


def chromium_for(
    playwright: Playwright, key: str | None, *, protected: bool
) -> Chromium:
    """The session's Chromium: as `launch` launches it, trusting the
    fixture's key when the page has an https origin (`key`), or, unprotected,
    the control's."""
    if key is not None:
        return TrustingTheFixture(playwright.chromium, key, protected=protected)
    if protected:
        return playwright.chromium
    return WithoutTheSwitches(playwright.chromium)


async def direct_and_proxied() -> dict[str, object]:
    """The attribution control: an allowed origin loaded by the session,
    through the egress proxy, then straight from a context with no proxy, in
    a browser launched without the launch-level proxy, which every session
    has."""
    page = ("text/html", "<!doctype html><title>An allowed origin</title>")
    with hosting({"/via-proxy": page, "/direct": page}) as site:
        gate = gate_for([site])
        async with async_playwright() as playwright, EgressProxy(gate) as egress:
            chromium = WithoutTheSwitches(playwright.chromium)
            async with open_browser_session(chromium, egress=egress) as session:
                await session.page.goto(f"{site.origin}/via-proxy")
                await (await stray_page(session.page)).goto(f"{site.origin}/direct")
            return report(egress, gate, [site], {}, outcomes={}, frames=[])


def gate_for(sites: list[Site]) -> AttributedGate:
    """A run's gate that allows the sites' origins, private as they are."""
    origins = tuple(each.origin for each in sites)
    return AttributedGate(EgressPolicy(origins, (), origins))


def report(
    egress: EgressProxy,
    gate: AttributedGate,
    sites: list[Site],
    canaries: dict[str, Canary],
    *,
    outcomes: object,
    frames: list[str],
) -> dict[str, object]:
    """What each party saw, as JSON, while `egress` serves."""
    proxy = urlsplit(egress.url)
    return {
        "proxy": [proxy.hostname, proxy.port],
        "origins": [
            [urlsplit(each.origin).hostname, urlsplit(each.origin).port]
            for each in sites
        ],
        "upstreams": gate.upstreams,
        # Each accepted connection's peer, with the origin it reached.
        "peers": [
            [peer, [urlsplit(each.origin).hostname, urlsplit(each.origin).port]]
            for each in sites
            for peer in each.peers
        ],
        "requests": [request for each in sites for request in each.requests],
        "targets": {name: canary.written() for name, canary in canaries.items()},
        "received": {name: canary.received for name, canary in canaries.items()},
        "blocked": [
            [each.resource_type, each.scheme, each.host, each.port]
            for each in egress.blocked_attempts.counts
        ],
        "refused": [[each.host, each.port, each.kind] for each in gate.refusals],
        "outcomes": outcomes,
        "frames": frames,
    }


async def stray_page(page: Page) -> Page:
    """A page in a new context of `page`'s browser, which names no proxy."""
    browser = page.context.browser
    assert browser is not None
    return await (await browser.new_context()).new_page()


async def fetch() -> dict[str, object]:
    return await run_page("fetch")


async def xhr() -> dict[str, object]:
    return await run_page("xhr")


async def form_post() -> dict[str, object]:
    return await run_page("form_post")


async def websocket() -> dict[str, object]:
    return await run_page("websocket")


async def webrtc() -> dict[str, object]:
    return await run_page("webrtc")


async def webrtc_remote() -> dict[str, object]:
    return await run_page("webrtc_remote")


async def quic() -> dict[str, object]:
    return await run_page("quic")


async def quic_unprotected() -> dict[str, object]:
    return await run_page("quic", protected=False)


async def ipv6() -> dict[str, object]:
    return await run_page("ipv6")


async def dns_prefetch() -> dict[str, object]:
    return await run_page("dns_prefetch")


async def service_worker() -> dict[str, object]:
    return await run_page("service_worker")


async def non_http_schemes() -> dict[str, object]:
    return await run_page("non_http_schemes")
