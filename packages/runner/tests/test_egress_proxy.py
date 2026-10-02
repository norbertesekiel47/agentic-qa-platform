"""The egress proxy: the browser's only way out, asking the run's egress gate
for every connection (ADR-0026 and its 2026-10-01 amendment on the egress
proxy; SECURITY.md §7). Test-first (TESTING.md §2). The browser tests launch
real Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import contextlib
import socket
import struct
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from urllib.parse import parse_qs, urlsplit

import pytest
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.egress import Connection, EgressGate, EgressPolicy, IPAddress
from aqa_runner.egress_proxy import EgressProxy
from playwright.async_api import Error, async_playwright

# Every name a test uses resolves to loopback, so each origin the tests reach
# is declared private, as a project declares its local servers.
LOOPBACK = ["127.0.0.1"]


class Resolver:
    """The local DNS fixture: each name's answer, which a test can change
    mid-run, and how many times each name was looked up."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.lookups: Counter[str] = Counter()

    async def __call__(self, host: str) -> list[IPAddress]:
        self.lookups[host] += 1
        return [ip_address(text) for text in self.answers[host]]


@dataclass(frozen=True)
class Seen:
    """One request a fixture server received."""

    host: str
    path: str
    cookie: str | None
    upgrade: str | None
    body: bytes = b""


@dataclass
class Origin:
    """A fixture server: the port it listens on and each request it saw."""

    port: int
    seen: list[Seen] = field(default_factory=list)

    def hosts(self) -> list[str]:
        return [each.host.rsplit(":", 1)[0] for each in self.seen]


class _Handler(BaseHTTPRequestHandler):
    """Serves a page naming its host and path, `/redirect?to=<url>` (302),
    `/set-cookie`, `/drop` (a response cut short) and anything else as a
    page. A WebSocket upgrade is recorded and answered 400."""

    seen: list[Seen]

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.seen.append(
            Seen(self.headers.get("Host", ""), self.path, None, None, body)
        )
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        self.seen.append(
            Seen(
                self.headers.get("Host", ""),
                self.path,
                self.headers.get("Cookie"),
                self.headers.get("Upgrade"),
            )
        )
        target = urlsplit(self.path)
        if target.path == "/redirect":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", parse_qs(target.query)["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if target.path == "/slow":
            # 1 MB, the second half a moment after the first.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", str(2**20))
            self.end_headers()
            with contextlib.suppress(ConnectionError):  # the reader may be gone
                self.wfile.write(b"x" * 2**19)
                time.sleep(0.3)
                self.wfile.write(b"x" * 2**19)
            return
        if target.path == "/hop":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Connection", "close, X-Hop")
            self.send_header("X-Hop", "only for the proxy")
            self.send_header("X-Kept", "yes")
            self.end_headers()
            return
        if target.path == "/drop":
            # Promises 100 bytes, sends 7, and closes.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"partial")
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if self.headers.get("Upgrade"):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        body = f"<!doctype html><title>{self.headers.get('Host')}{self.path}</title>"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        if target.path == "/set-cookie":
            self.send_header("Set-Cookie", "session=browser-only; Path=/")
        self.end_headers()
        self.wfile.write(body.encode())


class _IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


@contextmanager
def serving(host: str = "127.0.0.1", port: int = 0) -> Iterator[Origin]:
    """A fixture server on `host` and `port` (any free port by default)."""
    seen: list[Seen] = []
    handler = type("Handler", (_Handler,), {"seen": seen})
    server_type = _IPv6Server if ":" in host else ThreadingHTTPServer
    server = server_type((host, port), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield Origin(server.server_address[1], seen)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def gate(
    *,
    allowed: tuple[str, ...],
    private: tuple[str, ...] = (),
    subresource: tuple[str, ...] = (),
    answers: dict[str, list[str]] | None = None,
) -> EgressGate:
    """A run's gate: `allowed[0]` is its start origin, so it may be private
    too, as may `private`."""
    return EgressGate(
        EgressPolicy(
            allowed_origins=allowed,
            subresource_hosts=subresource,
            private_origins=(allowed[0], *private),
        ),
        resolve=Resolver(answers or {}),
    )


async def exchange(proxy: EgressProxy, request: bytes) -> bytes:
    """Everything the proxy sends back to a client that sends `request` and
    then waits until the proxy closes the connection, or drops it."""
    port = int(proxy.url.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    received = b""
    try:
        writer.write(request)
        await writer.drain()
        async with asyncio.timeout(5):
            while chunk := await reader.read(65536):
                received += chunk
    except ConnectionResetError:
        pass  # a dropped connection: what arrived before it is the answer
    writer.close()
    return received


def get(url: str, *, close: bool = False) -> bytes:
    """A plain proxied GET, absolute-form, as Chromium sends it."""
    authority = urlsplit(url).netloc
    connection = "Connection: close" if close else "Proxy-Connection: keep-alive"
    return f"GET {url} HTTP/1.1\r\nHost: {authority}\r\n{connection}\r\n\r\n".encode()


# Plain requests: absolute-form, each one checked.


def test_a_plain_request_to_an_allowed_origin_is_forwarded() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(proxy, get(f"{start}/page", close=True))

        response = asyncio.run(scenario())

    assert response.startswith(b"HTTP/1.1 200 ")
    assert f"127.0.0.1:{origin.port}/page".encode() in response
    # Forwarded in origin-form, with the target's own Host header.
    assert origin.seen == [Seen(f"127.0.0.1:{origin.port}", "/page", None, None)]


def test_a_refused_plain_request_gets_no_response_at_all() -> None:
    # A synthesized 403 would reach the page as the app's own response.
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(
                    proxy, get(f"http://evil.example.test:{origin.port}/", close=True)
                )

        response = asyncio.run(scenario())

    assert response == b""
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("evil.example.test", "host")
    ]
    assert origin.seen == []


def test_a_request_body_is_forwarded() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(
                    proxy,
                    f"POST {start}/form HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    "Content-Length: 9\r\nConnection: close\r\n\r\nname=page".encode(),
                )

        response = asyncio.run(scenario())

    assert response.startswith(b"HTTP/1.1 204 ")
    assert [(each.path, each.body) for each in origin.seen] == [("/form", b"name=page")]


def test_every_request_on_a_kept_alive_connection_is_checked() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(
                    proxy,
                    get(f"{start}/first")
                    + get(f"http://evil.example.test:{origin.port}/second"),
                )

        response = asyncio.run(scenario())

    # The first is answered; the second is refused, and the connection closed.
    assert response.count(b"HTTP/1.1 ") == 1
    assert [each.path for each in origin.seen] == ["/first"]
    assert [r.host for r in egress.refusals] == ["evil.example.test"]


def test_an_unreachable_upstream_drops_a_plain_request() -> None:
    port = unused_port()
    start = f"http://127.0.0.1:{port}"
    egress = gate(allowed=(start,))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, get(f"{start}/", close=True))

    assert asyncio.run(scenario()) == b""
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", port)
    ]
    assert egress.refusals == []


def test_a_response_cut_short_upstream_is_cut_short_downstream() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(proxy, get(f"{start}/drop", close=True))

        response = asyncio.run(scenario())

    # Whatever arrived is passed on, and the connection drops: the page sees a
    # failed load, never a response the proxy completed.
    assert len(response.partition(b"\r\n\r\n")[2]) < 100
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", origin.port)
    ]


# Tunnels: CONNECT, for https and WebSockets.


def connect(authority: str) -> bytes:
    return f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()


def test_a_tunnel_to_an_allowed_origin_carries_its_bytes() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                inner = "GET /inside HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                return await exchange(
                    proxy, connect(f"127.0.0.1:{origin.port}") + inner.encode()
                )

        response = asyncio.run(scenario())

    established, _, inside = response.partition(b"\r\n\r\n")
    assert established.startswith(b"HTTP/1.1 200 ")
    assert inside.startswith(b"HTTP/1.0 200 ")
    assert [each.path for each in origin.seen] == ["/inside"]


def test_a_refused_tunnel_fails_with_no_bytes_through() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(
                    proxy, connect(f"evil.example.test:{origin.port}")
                )

        response = asyncio.run(scenario())

    # A non-2xx answer to CONNECT fails the tunnel; Chromium never shows a
    # page a proxy's CONNECT response.
    assert response.startswith(b"HTTP/1.1 403 ")
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("evil.example.test", "host")
    ]
    assert origin.seen == []


def test_an_unreachable_upstream_fails_the_tunnel() -> None:
    port = unused_port()
    start = f"http://127.0.0.1:{port}"
    egress = gate(allowed=(start,))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, connect(f"127.0.0.1:{port}"))

    assert asyncio.run(scenario()).startswith(b"HTTP/1.1 502 ")
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", port)
    ]


def unused_port() -> int:
    """A port on 127.0.0.1 that nothing listens on, so connecting to it is
    refused at once (LAB_NOTES, 2026-10-01)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_proxy_serves_only_while_open() -> None:
    with pytest.raises(RuntimeError, match="async with"):
        _ = EgressProxy(gate(allowed=("http://127.0.0.1:9",))).url


def test_the_proxy_listens_on_loopback_only() -> None:
    async def scenario() -> str:
        async with EgressProxy(gate(allowed=("http://127.0.0.1:9",))) as proxy:
            return proxy.url

    assert asyncio.run(scenario()).startswith("http://127.0.0.1:")


@pytest.mark.parametrize(
    "request_line",
    [
        b"GET /relative HTTP/1.1",
        b"GET https://a.example.test/ HTTP/1.1",
        b"GET http://a.example.test:99999/ HTTP/1.1",
        b"GET http:///no-host HTTP/1.1",
        # Not as an origin writes it, so not the allowed 127.0.0.1:9.
        b"GET http://user@127.0.0.1:9/ HTTP/1.1",
        b"GET http://127.0.0.1:0/ HTTP/1.1",
        b"CONNECT nonsense HTTP/1.1",
        b"CONNECT 127.0.0.1:0x9 HTTP/1.1",
    ],
)
def test_a_request_the_proxy_cannot_read_is_dropped(request_line: bytes) -> None:
    egress = gate(allowed=("http://127.0.0.1:9",))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, request_line + b"\r\nHost: x\r\n\r\n")

    assert asyncio.run(scenario()) == b""
    # Dropped before the gate: nothing was asked for, refused or dialled.
    assert (egress.refusals, egress.infrastructure_events) == ([], [])


def test_headers_a_connection_header_names_stay_behind() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(proxy, get(f"{start}/hop", close=True))

        response = asyncio.run(scenario()).lower()

    # RFC 9110 §7.6.1: a header the Connection header names is hop-by-hop.
    assert b"x-kept: yes" in response
    assert b"x-hop" not in response


def test_a_browser_that_goes_away_mid_response_is_no_upstream_failure() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> None:
            async with EgressProxy(egress) as proxy:
                port = int(proxy.url.rsplit(":", 1)[1])
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(get(f"{start}/slow", close=True))
                await reader.readuntil(b"\r\n\r\n")
                # Reset, as a closed tab or a cancelled navigation would.
                raw = writer.get_extra_info("socket")
                raw.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                writer.close()
                await asyncio.sleep(1)  # the rest arrives while nobody reads it

        asyncio.run(scenario())

    assert egress.infrastructure_events == []


# Browser sessions: Chromium's every request goes through the proxy.

APP = "app.example.test"
EVIL = "evil.example.test"

# A WebSocket's outcome, as the page sees it.
OPEN_SOCKET = """(url) => new Promise((done) => {
    const socket = new WebSocket(url);
    socket.onopen = () => done("open");
    socket.onerror = () => done("error");
})"""


@asynccontextmanager
async def browsing(egress: EgressGate) -> AsyncIterator[BrowserSession]:
    """A browser session whose traffic goes through a proxy for `egress`."""
    async with (
        async_playwright() as playwright,
        EgressProxy(egress) as proxy,
        open_browser_session(playwright.chromium, egress=proxy) as session,
    ):
        yield session


def app_gate(port: int, **answers: list[str]) -> EgressGate:
    """A run that starts at http://app.example.test:`port`, a local server."""
    return gate(allowed=(f"http://{APP}:{port}",), answers={APP: LOOPBACK, **answers})


async def load(session: BrowserSession, url: str) -> int | str:
    """The status a navigation got, or the network error it failed with."""
    try:
        response = await session.page.goto(url)
    except Error as error:
        return error.message.split()[1]
    assert response is not None
    return response.status


def test_loopback_goes_through_the_proxy_too() -> None:
    # The run's start origin is elsewhere, so a direct connection would load
    # these pages, and the proxy refuses them.
    with serving() as origin:
        egress = gate(allowed=("http://127.0.0.1:9",))

        async def scenario() -> list[int | str]:
            async with browsing(egress) as session:
                return [
                    await load(session, f"http://127.0.0.1:{origin.port}/"),
                    await load(session, f"http://localhost:{origin.port}/"),
                ]

        outcomes = asyncio.run(scenario())

    assert outcomes == ["net::ERR_EMPTY_RESPONSE"] * 2
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("127.0.0.1", "host"),
        ("localhost", "host"),
    ]
    assert origin.seen == []


def test_a_redirect_to_a_disallowed_host_is_refused_at_the_hop() -> None:
    with serving() as origin:
        egress = app_gate(origin.port)
        hop = f"http://{EVIL}:{origin.port}/landed"

        async def scenario() -> tuple[int | str, list[str]]:
            async with browsing(egress) as session:
                responses: list[str] = []
                session.page.on(
                    "response", lambda response: responses.append(response.url)
                )
                outcome = await load(
                    session, f"http://{APP}:{origin.port}/redirect?to={hop}"
                )
                return outcome, responses

        outcome, responses = asyncio.run(scenario())

    # Refused at the hop, with no response the page could take for the app's.
    assert outcome == "net::ERR_EMPTY_RESPONSE"
    assert [url for url in responses if urlsplit(url).hostname == EVIL] == []
    assert origin.hosts() == [APP]
    assert [(r.host, r.port, r.kind) for r in egress.refusals] == [
        (EVIL, origin.port, "host")
    ]


def test_a_redirect_to_a_subresource_host_passes_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A subresource host passes on its scheme's default port only: 80 for
    # http. The gate's dial of port 80 goes to the fixture's port instead.
    with serving() as origin:
        open_connection = asyncio.open_connection

        async def to_fixture(host: str, port: int) -> Connection:
            return await open_connection(host, origin.port if port == 80 else port)

        monkeypatch.setattr(asyncio, "open_connection", to_fixture)
        egress = gate(
            allowed=(f"http://{APP}:{origin.port}",),
            subresource=("cdn.example.test",),
            # Declared private only so it may resolve to loopback here.
            private=("http://cdn.example.test",),
            answers={APP: LOOPBACK, "cdn.example.test": LOOPBACK},
        )
        hop = "http://cdn.example.test/asset"

        async def scenario() -> int | str:
            async with browsing(egress) as session:
                return await load(
                    session, f"http://{APP}:{origin.port}/redirect?to={hop}"
                )

        outcome = asyncio.run(scenario())

    # Which document may use which origin is the session's check (#44).
    assert outcome == HTTPStatus.OK
    assert [(each.host, each.path) for each in origin.seen][-1] == (
        "cdn.example.test",
        "/asset",
    )
    assert egress.refusals == []


def test_a_websocket_to_a_disallowed_host_is_refused_at_connect() -> None:
    with serving() as origin:
        egress = app_gate(origin.port)

        async def scenario() -> list[str]:
            async with browsing(egress) as session:
                await session.page.goto(f"http://{APP}:{origin.port}/")
                return [
                    await session.page.evaluate(
                        OPEN_SOCKET, f"ws://{EVIL}:{origin.port}/ws"
                    ),
                    await session.page.evaluate(
                        OPEN_SOCKET, f"ws://{APP}:{origin.port}/ws"
                    ),
                ]

        outcomes = asyncio.run(scenario())

    # Neither opens: the fixture answers 400 to the upgrade it does receive.
    assert outcomes == ["error", "error"]
    assert [(r.host, r.kind) for r in egress.refusals] == [(EVIL, "host")]
    # The allowed one went through the tunnel; the refused one never left.
    assert [(each.host.rsplit(":", 1)[0], each.upgrade) for each in origin.seen] == [
        (APP, None),
        (APP, "websocket"),
    ]


def test_an_unreachable_upstream_never_becomes_a_response() -> None:
    port = unused_port()
    egress = app_gate(port)

    async def scenario() -> tuple[int | str, str]:
        async with browsing(egress) as session:
            # The blank first page opens the socket; a failed navigation would
            # leave an error page behind.
            socket_outcome = await session.page.evaluate(
                OPEN_SOCKET, f"ws://{APP}:{port}/ws"
            )
            return await load(session, f"http://{APP}:{port}/"), socket_outcome

    outcome, socket_outcome = asyncio.run(scenario())

    # The navigation fails as a network error, never as an HTTP status, and
    # the tunnel fails; each is an infrastructure event.
    assert outcome == "net::ERR_EMPTY_RESPONSE"
    assert socket_outcome == "error"
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        (APP, port),
        (APP, port),
    ]
    assert egress.refusals == []


def test_the_start_origin_and_a_private_origin_load_and_other_loopback_doesnt() -> None:
    with serving() as origin:
        egress = gate(
            allowed=(
                f"http://127.0.0.1:{origin.port}",
                f"http://staging.example.test:{origin.port}",
                f"http://elsewhere.example.test:{origin.port}",
            ),
            private=(f"http://staging.example.test:{origin.port}",),
            answers={
                "staging.example.test": LOOPBACK,
                "elsewhere.example.test": LOOPBACK,
            },
        )

        async def scenario() -> list[int | str]:
            async with browsing(egress) as session:
                return [
                    await load(session, f"http://127.0.0.1:{origin.port}/"),
                    await load(session, f"http://staging.example.test:{origin.port}/"),
                    await load(
                        session, f"http://elsewhere.example.test:{origin.port}/"
                    ),
                ]

        outcomes = asyncio.run(scenario())

    # An allowed origin that isn't declared private may not resolve to one.
    assert outcomes == [HTTPStatus.OK, HTTPStatus.OK, "net::ERR_EMPTY_RESPONSE"]
    assert origin.hosts() == ["127.0.0.1", "staging.example.test"]
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("elsewhere.example.test", "address")
    ]


def test_every_session_of_a_run_keeps_the_pinned_address() -> None:
    with serving() as first, serving("::1", first.port) as rebound:
        resolver = Resolver({APP: LOOPBACK})
        start = f"http://{APP}:{first.port}"
        egress = EgressGate(
            EgressPolicy(
                allowed_origins=(start,), subresource_hosts=(), private_origins=(start,)
            ),
            resolve=resolver,
        )

        async def scenario() -> list[int | str]:
            async with (
                async_playwright() as playwright,
                EgressProxy(egress) as proxy,
            ):
                outcomes = []
                for answer in (["127.0.0.1"], ["::1"]):
                    resolver.answers[APP] = answer  # rebinds after the first
                    async with open_browser_session(
                        playwright.chromium, egress=proxy
                    ) as session:
                        outcomes.append(
                            await load(session, f"http://{APP}:{first.port}/")
                        )
                return outcomes

        outcomes = asyncio.run(scenario())

    assert outcomes == [HTTPStatus.OK, HTTPStatus.OK]
    assert len(first.seen) == 2
    assert rebound.seen == []
    assert resolver.lookups == Counter({APP: 1})


def test_a_closed_proxy_leaves_the_browser_no_way_out() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> int | str:
            async with async_playwright() as playwright:
                proxy = await EgressProxy(gate(allowed=(start,))).__aenter__()
                async with open_browser_session(
                    playwright.chromium, egress=proxy
                ) as session:
                    # The proxy closes while the session still runs.
                    await proxy.__aexit__(None, None, None)
                    return await load(session, f"{start}/")

        outcome = asyncio.run(scenario())

    # Chromium has no other route: it doesn't fall back to a direct connection.
    assert outcome == "net::ERR_PROXY_CONNECTION_FAILED"
    assert origin.seen == []
