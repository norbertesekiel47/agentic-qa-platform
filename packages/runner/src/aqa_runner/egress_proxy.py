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
  never terminates TLS; a tunnel's bytes pass through unread."""

import asyncio
import contextlib
from collections.abc import Iterable
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import h11

from aqa_runner.egress import EgressGate, EgressRefusedError, EgressUpstreamError

# Headers that describe one hop and are never forwarded (RFC 9110 §7.6.1). A
# request goes upstream with `Connection: close`, so each upstream connection
# carries one request while the browser's may stay open. `Upgrade` stays
# behind too: Chromium opens WebSockets through CONNECT.
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


class EgressProxy:
    """The run's egress proxy, serving on 127.0.0.1 while it is open (`async
    with`). One serves a whole run, every browser session in it, through the
    run's `EgressGate`, so the gate's pins and records cover them all."""

    def __init__(self, gate: EgressGate) -> None:
        self.gate = gate
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> Self:
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
        request the proxy can't read, a refusal, an upstream failure (both
        recorded by the gate) or a browser that went away ends it, with
        nothing more sent."""
        browser = h11.Connection(h11.SERVER)
        with contextlib.suppress(
            h11.RemoteProtocolError,
            EgressRefusedError,
            EgressUpstreamError,
            ConnectionError,
        ):
            while isinstance(request := await _next(browser, reader), h11.Request):
                if request.method == b"CONNECT":
                    await self._tunnel(request, browser, reader, writer)
                    break
                await self._forward(request, browser, reader, writer)
                if (
                    browser.our_state is not h11.DONE
                    or browser.their_state is not h11.DONE
                ):
                    break
                browser.start_next_cycle()
        writer.close()

    async def _forward(
        self,
        request: h11.Request,
        browser: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Send one plain request upstream, and its response back."""
        target = urlsplit(request.target.decode("ascii"))
        if target.scheme != "http":
            raise h11.RemoteProtocolError("a plain request names an http URL")
        host, port = _authority(target.netloc)
        upstream_reader, upstream_writer = await self.gate.connect(
            host, port, "request"
        )
        try:
            upstream = h11.Connection(h11.CLIENT)
            path = (target.path or "/") + (f"?{target.query}" if target.query else "")
            await _send(
                upstream,
                upstream_writer,
                h11.Request(
                    method=request.method,
                    target=path,
                    # The target's own Host, whatever the browser's said
                    # (RFC 9112 §3.2.2).
                    headers=[
                        (b"host", target.netloc.encode()),
                        *_end_to_end(request.headers),
                        (b"connection", b"close"),
                    ],
                ),
            )
            # The body, then its EndOfMessage: a body cut short is h11's
            # RemoteProtocolError.
            while isinstance(event := await _next(browser, reader), h11.Data):
                await _send(upstream, upstream_writer, event)
            await _send(upstream, upstream_writer, event)
            try:
                await _relay_response(upstream, upstream_reader, browser, writer)
            except (h11.RemoteProtocolError, ConnectionError) as error:
                # Cut short upstream: the browser's connection drops too, and
                # the proxy never completes a response.
                raise self.gate.fail(
                    host, port, f"the response broke off: {error}"
                ) from error
        finally:
            upstream_writer.close()

    async def _tunnel(
        self,
        request: h11.Request,
        browser: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Open a tunnel and pass its bytes both ways until either side ends."""
        host, _, port = request.target.decode("ascii").rpartition(":")
        if not port.isdecimal():
            raise h11.RemoteProtocolError("CONNECT names a host and a port")
        await _next(browser, reader)  # CONNECT's EndOfMessage
        try:
            upstream_reader, upstream_writer = await self.gate.connect(
                host, int(port), "tunnel"
            )
        except EgressRefusedError:
            await _fail_tunnel(browser, writer, 403)
            raise
        except EgressUpstreamError:
            await _fail_tunnel(browser, writer, 502)
            raise
        await _send(
            browser,
            writer,
            h11.Response(status_code=200, reason=b"Connection established", headers=[]),
        )
        early, _ = browser.trailing_data
        upstream_writer.write(early)
        await asyncio.gather(
            _pipe(reader, upstream_writer), _pipe(upstream_reader, writer)
        )


def _authority(netloc: str) -> tuple[str, int]:
    """The host, as an origin writes it, and the port of a plain request's
    URL. A host written any other way never matches the policy, which refuses
    it."""
    parts = urlsplit(f"http://{netloc}")
    try:
        port = parts.port or 80
    except ValueError as error:  # a port that isn't a number in range
        raise h11.RemoteProtocolError(str(error)) from error
    if not parts.hostname:
        raise h11.RemoteProtocolError("a plain request names a host")
    host = parts.hostname
    return (f"[{host}]" if ":" in host else host), port


def _end_to_end(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """`headers` without the hop-by-hop ones and Host."""
    return [
        (name, value)
        for name, value in headers
        if name.lower() not in HOP_BY_HOP and name.lower() != b"host"
    ]


async def _relay_response(
    upstream: h11.Connection,
    upstream_reader: asyncio.StreamReader,
    browser: h11.Connection,
    writer: asyncio.StreamWriter,
) -> None:
    """Pass the upstream response to the browser as it arrives, up to its
    EndOfMessage. A response cut short is h11's RemoteProtocolError."""
    while True:
        event = await _next(upstream, upstream_reader)
        if isinstance(event, h11.InformationalResponse | h11.Response):
            event = type(event)(
                status_code=event.status_code,
                reason=event.reason,
                headers=_end_to_end(event.headers),
            )
        await _send(browser, writer, event)
        if isinstance(event, h11.EndOfMessage):
            return


async def _next(connection: h11.Connection, reader: asyncio.StreamReader) -> h11.Event:
    """The connection's next event, reading as much as it needs. The end of
    the stream is h11's to judge: ConnectionClosed between messages, a
    RemoteProtocolError within one. The proxy reads a side only when that
    side owes an event, never while it waits on the proxy (PAUSED)."""
    while True:
        event = connection.next_event()
        if isinstance(event, h11.Event):
            return event
        if event is h11.PAUSED:
            raise h11.RemoteProtocolError("read while the peer waits on the proxy")
        connection.receive_data(await reader.read(CHUNK))


async def _send(
    connection: h11.Connection, writer: asyncio.StreamWriter, event: h11.Event
) -> None:
    if data := connection.send(event):
        writer.write(data)
        await writer.drain()


async def _fail_tunnel(
    browser: h11.Connection, writer: asyncio.StreamWriter, status: int
) -> None:
    await _send(
        browser,
        writer,
        h11.Response(status_code=status, headers=[(b"content-length", b"0")]),
    )
    await _send(browser, writer, h11.EndOfMessage())


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes until the reader's side ends, then close the writer, which
    ends the other direction too."""
    with contextlib.suppress(ConnectionError):  # a side reset: the tunnel ends
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    writer.close()
