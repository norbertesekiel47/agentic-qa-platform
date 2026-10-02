"""Fixtures the egress proxy's tests share: the local DNS fixture, origin
servers that record what reaches them, raw upstreams that misbehave at the
byte level, and a raw client of the proxy (ADR-0026 amendment, 2026-10-01).
Imported by its path, as pytest names the runner's test modules."""

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

from aqa_runner.egress import EgressGate, EgressPolicy, IPAddress
from aqa_runner.egress_proxy import EgressProxy

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
    upgrade: str | None = None
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
    `/drop` (a response cut short) and anything else as a
    page. A WebSocket upgrade is recorded and answered 400."""

    seen: list[Seen]

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.seen.append(Seen(self.headers.get("Host", ""), self.path, body=body))
        if self.path == "/boom":
            return  # a server that crashed mid-request: no response at all
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        self.seen.append(
            Seen(self.headers.get("Host", ""), self.path, self.headers.get("Upgrade"))
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


def connect(authority: str) -> bytes:
    return f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()


def unused_port() -> int:
    """A port on 127.0.0.1 that nothing listens on, so connecting to it is
    refused at once (LAB_NOTES, 2026-10-01)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def header_names(message: bytes) -> set[bytes]:
    """The header names in a message's head, lowercase."""
    head = message.partition(b"\r\n\r\n")[0]
    return {line.partition(b":")[0].lower() for line in head.split(b"\r\n")[1:]}


@dataclass
class RawUpstream:
    """An upstream server's port, the bytes it received, and whether the
    proxy has closed its connection."""

    port: int
    received: bytearray = field(default_factory=bytearray)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


def reset(writer: asyncio.StreamWriter) -> None:
    """Close with a reset, as a crashed server or a closed tab does."""
    raw = writer.get_extra_info("socket")
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()


@asynccontextmanager
async def raw_upstream(
    reply: bytes | None = None, *, resets: bool = False
) -> AsyncIterator[RawUpstream]:
    """An upstream that sends `reply` once a request head arrives and
    closes; that never answers when `reply` is None; or that resets as soon
    as anything arrives when `resets`."""
    upstream = RawUpstream(0)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            if resets:
                upstream.received += await reader.read(65536)
                reset(writer)
                return
            upstream.received += await reader.readuntil(b"\r\n\r\n")
            if reply is not None:
                writer.write(reply)
                await writer.drain()
                writer.close()
                return
            while data := await reader.read(65536):
                upstream.received += data
        upstream.closed.set()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    upstream.port = server.sockets[0].getsockname()[1]
    try:
        yield upstream
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


async def proxy_client(
    proxy: EgressProxy,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", urlsplit(proxy.url).port)
