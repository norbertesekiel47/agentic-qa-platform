"""The egress proxy: an HTTP forward proxy on loopback that is the browser's
only way out (ADR-0026 and its 2026-10-01 amendment on the egress proxy;
SECURITY.md §7). It asks the run's egress gate for every connection, and the
gate's policy makes every allowlist decision.

- A plain request arrives in absolute form, one at a time on a kept-alive
  connection, and each one is checked. A refused or failed one closes the
  connection with nothing sent: a synthesized status would reach the page as
  the app's own response.
- A `CONNECT` carries https and WebSockets. A refused or failed one fails the
  tunnel with a non-2xx status, which Chromium never shows a page. The proxy
  never terminates TLS; a tunnel's bytes pass through unread.

h11 frames HTTP/1.1 on both sides: https://h11.readthedocs.io/en/v0.16.0/api.html
"""

import asyncio
import contextlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import h11
from aqa_core.schema import authority

from aqa_runner.egress import EgressGate, EgressRefusedError, EgressUpstreamError

# Headers that describe one hop and are never forwarded, with any header the
# Connection header names (RFC 9110 §7.6.1). A request goes upstream with
# `Connection: close`, so each upstream connection carries one request while
# the browser's may stay open. `Upgrade` stays behind too: Chromium opens
# WebSockets through CONNECT.
HOP_BY_HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"upgrade",
}

# How much one read takes.
CHUNK = 65536


@dataclass(frozen=True)
class _Peer:
    """One side of a proxied exchange: the browser, or the upstream server."""

    http: h11.Connection
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def next(self) -> h11.Event:
        """The peer's next event, reading as much as it needs. The end of the
        stream is h11's to judge: ConnectionClosed between messages, a
        RemoteProtocolError within one. The proxy reads a peer only when the
        peer owes an event, never while it waits on the proxy (PAUSED)."""
        while True:
            event = self.http.next_event()
            if isinstance(event, h11.Event):
                return event
            if event is h11.PAUSED:
                raise h11.RemoteProtocolError("read while the peer waits on the proxy")
            self.http.receive_data(await self.reader.read(CHUNK))

    async def send(self, event: h11.Event) -> None:
        if data := self.http.send(event):
            self.writer.write(data)
            await self.writer.drain()


class EgressProxy:
    """The run's egress proxy, serving on 127.0.0.1 while it is open (`async
    with`). One serves a whole run, every browser session in it, through the
    run's `EgressGate`, so the gate's pins and records cover them all."""

    def __init__(self, gate: EgressGate) -> None:
        self._gate = gate
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> Self:
        # https://docs.python.org/3.14/library/asyncio-stream.html#asyncio.start_server
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        server = self._serving()
        self._server = None
        server.close()
        # Kept-alive browser connections would hold wait_closed open:
        # https://docs.python.org/3.14/library/asyncio-eventloop.html#asyncio.Server.close_clients
        server.close_clients()
        await server.wait_closed()

    @property
    def url(self) -> str:
        """Where the browser sends its traffic."""
        port = self._serving().sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def _serving(self) -> asyncio.Server:
        if self._server is None:
            raise RuntimeError("the egress proxy serves only inside `async with`")
        return self._server

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One browser connection: plain requests in turn, or one tunnel. A
        request the proxy can't read, a refusal or an upstream failure (both
        recorded by the gate), or a browser that went away ends it, with
        nothing more sent."""
        browser = _Peer(h11.Connection(h11.SERVER), reader, writer)
        with contextlib.suppress(
            h11.RemoteProtocolError,
            EgressRefusedError,
            EgressUpstreamError,
            ConnectionError,
        ):
            while isinstance(request := await browser.next(), h11.Request):
                if request.method == b"CONNECT":
                    await self._tunnel(request, browser)
                    break
                await self._forward(request, browser)
                # Kept alive only when both sides finished their messages.
                if (browser.http.our_state, browser.http.their_state) != (
                    h11.DONE,
                    h11.DONE,
                ):
                    break
                browser.http.start_next_cycle()
        writer.close()

    async def _forward(self, request: h11.Request, browser: _Peer) -> None:
        """Send one plain request upstream, and its response back. A failure
        on the upstream side is an infrastructure event; one on the browser's
        side, such as a closed tab, isn't."""
        target = urlsplit(request.target.decode("ascii"))
        if target.scheme != "http":
            raise h11.RemoteProtocolError("a plain request names an http URL")
        host, port = _authority(f"http://{target.netloc}")
        upstream = _Peer(
            h11.Connection(h11.CLIENT),
            *await self._gate.connect(host, port, "request"),
        )
        try:
            path = (target.path or "/") + (f"?{target.query}" if target.query else "")
            with self._upstream_failures(host, port):
                await upstream.send(
                    h11.Request(
                        method=request.method,
                        target=path,
                        # The target's own Host, whatever the browser's said
                        # (RFC 9112 §3.2.2).
                        headers=[
                            (
                                b"host",
                                (host if port == 80 else f"{host}:{port}").encode(),
                            ),
                            *_end_to_end(request.headers),
                            (b"connection", b"close"),
                        ],
                    )
                )
            # The body, then its EndOfMessage: a body cut short is h11's
            # RemoteProtocolError.
            while True:
                event = await browser.next()
                with self._upstream_failures(host, port):
                    await upstream.send(event)
                if not isinstance(event, h11.Data):
                    break
            while True:
                with self._upstream_failures(host, port):
                    event = await upstream.next()
                if isinstance(event, h11.InformationalResponse | h11.Response):
                    event = type(event)(
                        status_code=event.status_code,
                        reason=event.reason,
                        headers=_end_to_end(event.headers),
                    )
                await browser.send(event)
                if isinstance(event, h11.EndOfMessage):
                    return
        finally:
            upstream.writer.close()

    @contextlib.contextmanager
    def _upstream_failures(self, host: str, port: int) -> Iterator[None]:
        """Record an upstream server that breaks off mid-exchange with the
        gate, as an infrastructure event, and end the exchange."""
        try:
            yield
        except (h11.RemoteProtocolError, ConnectionError) as error:
            raise self._gate.record_failure(
                host, port, f"the exchange broke off: {error}"
            ) from error

    async def _tunnel(self, request: h11.Request, browser: _Peer) -> None:
        """Open a tunnel and pass its bytes both ways until either side ends."""
        target = request.target.decode("ascii")
        if not target.rpartition(":")[2].isdecimal():
            raise h11.RemoteProtocolError("CONNECT names a host and a port")
        host, port = _authority(f"http://{target}")
        await browser.next()  # CONNECT's EndOfMessage
        try:
            upstream_reader, upstream_writer = await self._gate.connect(
                host, port, "tunnel"
            )
        except EgressRefusedError:
            await _fail_tunnel(browser, 403)
            raise
        except EgressUpstreamError:
            await _fail_tunnel(browser, 502)
            raise
        await browser.send(
            h11.Response(status_code=200, reason=b"Connection established", headers=[])
        )
        early, _ = browser.http.trailing_data
        upstream_writer.write(early)
        await asyncio.gather(
            _pipe(browser.reader, upstream_writer),
            _pipe(upstream_reader, browser.writer),
        )


def _authority(origin: str) -> tuple[str, int]:
    """A request's host and port, read as an origin is: a host written any
    other way, with a user or port 0, is a request the proxy can't read."""
    try:
        return authority(origin)
    except ValueError as error:
        raise h11.RemoteProtocolError(str(error)) from error


def _end_to_end(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """`headers` without Host, the hop-by-hop ones, and any the Connection
    header names."""
    pairs = [(name.lower(), value) for name, value in headers]
    named = {
        token.strip().lower()
        for name, value in pairs
        if name == b"connection"
        for token in value.split(b",")
    }
    dropped = HOP_BY_HOP | named | {b"host"}
    return [(name, value) for name, value in pairs if name not in dropped]


async def _fail_tunnel(browser: _Peer, status: int) -> None:
    await browser.send(
        h11.Response(status_code=status, headers=[(b"content-length", b"0")])
    )
    await browser.send(h11.EndOfMessage())


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes until the reader's side ends, then close the writer, which
    ends the other direction too."""
    with contextlib.suppress(ConnectionError):  # a side reset: the tunnel ends
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    writer.close()
