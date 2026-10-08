"""The egress proxy: an HTTP forward proxy on loopback that is the browser's
only way out (ADR-0026 and its 2026-10-01 amendment on the egress proxy;
SECURITY.md §7). It asks the run's egress gate for every connection, and the
gate's policy makes every allowlist decision.

- A plain request arrives in absolute form, one per browser connection: the
  response says `Connection: close`, so a failure never lands on a reused
  connection, which Chromium would answer by sending the request again. A
  refused or failed request closes the connection with nothing sent: a
  synthesized status would reach the page as the app's own response.
- A `CONNECT` carries https and WebSockets. A refused or failed one fails the
  tunnel with a non-2xx status, which Chromium never shows a page. The proxy
  never terminates TLS; a tunnel's bytes pass through unread.
- A failure on the upstream side is an infrastructure event, recorded with the
  gate; one on the browser's side, such as a closed tab, isn't.
- Each phase of a run (an attempt, a confirmation) has its own listener on a
  new port. Ending it drops what is still queued, cancels and joins every
  connection it accepted, and counts the exchanges whose effect upstream is
  unknown, so the reset hook runs only after the phase's traffic is certain.

h11 frames HTTP/1.1 on both sides: https://h11.readthedocs.io/en/v0.16.0/api.html
"""

import asyncio
import contextlib
import errno
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from functools import partial
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import h11
from aqa_core.schema import DEFAULT_PORTS, authority

from aqa_runner.egress import (
    EgressGate,
    EgressPolicy,
    EgressRefusedError,
    EgressUpstreamError,
)
from aqa_runner.egress_peers import Peer, Upstream
from aqa_runner.loopback_server import LoopbackServer

# Headers that describe one hop and are never forwarded, with any header the
# Connection header names (RFC 9110 §7.6.1). `Upgrade` stays behind too:
# Chromium opens WebSockets through CONNECT. Content-Length and
# Transfer-Encoding are forwarded on purpose: h11 frames each side's body
# from them.
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

# How many distinct attempts routing's record keeps, so a page can't grow it
# without bound; repeats and the total are counted whatever the bound.
KEPT_ATTEMPTS = 1000

# The longest host a record keeps; a longer one is no DNS name (RFC 1035
# §2.3.4, written out).
MAX_HOST = 253


def named_host(host: str) -> str:
    """`host` as a run's record names a refused host: itself when an origin
    could write it and it is at most `MAX_HOST` characters, otherwise empty.
    A page chooses it, so a long one could carry what the page exfiltrates."""
    if len(host) > MAX_HOST:
        return ""
    try:
        written, _ = authority(f"http://{host}")
    except ValueError:  # a host no origin writes
        return ""
    return host if written == host else ""


@dataclass(frozen=True)
class BlockedAttempt:
    """A request or WebSocket the browser sessions' routing refused before it
    reached the proxy (`aqa_runner.routing`): what kind (Playwright's resource
    type, `websocket` for a socket) and where it went. Never the path, query
    or user part, which a page can fill with what it exfiltrates; the host is
    kept, though a page can choose it too, so redaction must cover it before
    anything is saved (#49). `port` is None when the URL names none a scheme
    gives. `host` is empty when the URL's host is none an origin could
    write."""

    resource_type: str
    scheme: str
    host: str
    port: int | None


@dataclass
class BlockedAttempts:
    """What routing refused in a run: each distinct attempt, the first
    `KEPT_ATTEMPTS` of them, with how many times it was made, and how many
    attempts there were in all, so a page that repeats one attempt can't
    crowd out the next. With the gate's refusals they are the run's egress
    blocks (#47 reads both); the run's record persists them (#46, #53)."""

    counts: dict[BlockedAttempt, int] = field(default_factory=dict)
    total: int = 0

    def add(self, attempt: BlockedAttempt) -> None:
        self.total += 1
        if attempt in self.counts:
            self.counts[attempt] += 1
        elif len(self.counts) < KEPT_ATTEMPTS:
            self.counts[attempt] = 1

    @property
    def overflowed(self) -> bool:
        """Whether some attempts came after the record was full, so it names
        not every refused host: a page chose a thousand distinct attempts
        first. #47 counts that as an egress block."""
        return self.total > sum(self.counts.values())


@dataclass(frozen=True)
class RefusedHost:
    """A host and port an egress block refused, as the run's record names
    it (`named_host`), with no port when the attempt named none a scheme
    gives."""

    host: str
    port: int | None


@dataclass(frozen=True)
class EgressBlocks:
    """A run's egress blocks (ADR-0026's #47 amendment): each host and port
    routing or the gate refused for being no allowed origin or subresource
    host, once, in the order first recorded, the expected-blocked hosts left
    out; and whether routing's record overflowed, and so names not every
    refused host."""

    refused: tuple[RefusedHost, ...]
    overflowed: bool

    @property
    def blocked(self) -> bool:
        """Whether the run has an egress block: a refused host, or an
        overflowed record, which may hide one."""
        return bool(self.refused) or self.overflowed


@dataclass(frozen=True)
class PhaseTraffic:
    """What a phase's browser connections left uncertain when it ended: plain
    requests that sent upstream without their response reaching the browser
    whole, and tunnels that carried any byte upstream, whose requests the
    proxy can't see answered. Either may have changed the app after the
    browser closed."""

    plain_uncertain: int
    tunnel_uncertain: int


@dataclass
class _Exchange:
    """One browser connection's exchange: a tunnel or a plain request,
    whether any byte went upstream (noted before it went, so a partial send
    counts), and whether a plain response reached the browser whole."""

    tunnel: bool = False
    sent: bool = False
    answered: bool = False


@dataclass(eq=False)
class _Phase:
    """One phase's listener, its port, and its uncertain exchanges so far.
    It stays the proxy's until its listener has joined every connection.
    The port is kept here because the listener's own raises once it has
    stopped, and a retirement names it after the stop."""

    listener: LoopbackServer = field(init=False)
    port: int = 0
    plain_uncertain: int = 0
    tunnel_uncertain: int = 0
    ending: bool = False

    def classify(self, exchange: _Exchange) -> None:
        if exchange.sent and exchange.tunnel:
            self.tunnel_uncertain += 1
        elif exchange.sent and not exchange.answered:
            self.plain_uncertain += 1


class EgressProxy:
    """The run's egress proxy, serving on 127.0.0.1 while it is open (`async
    with`). One serves a whole run, every browser session in it, through the
    run's `EgressGate`, so the gate's pins and records cover them all. It
    opens with its first phase; `end_phase` and `begin_phase` give each
    later phase a listener of its own."""

    def __init__(self, gate: EgressGate) -> None:
        self._gate = gate
        # What the browser sessions' routing refused before it reached the
        # proxy (ADR-0026 amendment, 2026-10-02). With the gate's refusals,
        # which are what the proxy itself refused, these are the run's egress
        # blocks.
        self.blocked_attempts = BlockedAttempts()
        self._entered = False
        # Its listener owns every browser connection, so ending the phase or
        # closing the proxy ends them all: none may forward a request after.
        self._phase: _Phase | None = None

    async def __aenter__(self) -> Self:
        await self._open_phase()
        self._entered = True
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._entered = False
        # A phase another task is ending is joined here too: the proxy
        # closes only once every connection has.
        if self._phase is not None:
            await self._end(self._phase, kind, error, trace)

    async def begin_phase(self) -> None:
        """Open a phase: a new listener, on a new port."""
        if not self._entered:
            raise RuntimeError("the egress proxy serves only inside `async with`")
        await self._open_phase()

    async def _open_phase(self) -> None:
        if self._phase is not None:
            raise RuntimeError("the egress proxy has a phase open or ending")
        phase = _Phase()
        listener = LoopbackServer(
            partial(self._serve, phase), on_retired=partial(self._retired, phase)
        )
        await listener.__aenter__()
        phase.listener, phase.port = listener, listener.port
        self._phase = phase

    async def end_phase(self) -> PhaseTraffic:
        """End the open phase: its listener stops, so a connection still
        queued is never accepted, and every connection it accepted is
        cancelled and joined before this returns, a caller's cancellation
        included. Its traffic is what those connections left uncertain."""
        phase = self._phase
        if phase is None or phase.ending:
            raise RuntimeError("the egress proxy has no open phase to end")
        await self._end(phase, None, None, None)
        return PhaseTraffic(phase.plain_uncertain, phase.tunnel_uncertain)

    async def _end(
        self,
        phase: _Phase,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        phase.ending = True
        try:
            # Returns or raises only once every connection is joined.
            await phase.listener.__aexit__(kind, error, trace)
        finally:
            # Another task's end may already have let a new phase open.
            if self._phase is phase:
                self._phase = None

    @property
    def policy(self) -> EgressPolicy:
        """The run's policy. The browser session checks every document it
        observes against its allowed origins (#44), and its routing judges
        every request against it (#43), as the gate does at the proxy."""
        return self._gate.policy

    def egress_blocks(self, expected_blocked: tuple[str, ...]) -> EgressBlocks:
        """The run's egress blocks so far, from routing's record and the
        gate's `host` refusals, which can overlap (ADR-0026's #43 amendment),
        leaving out the hosts in `expected_blocked`, the project config's.
        The gate's `address` refusals are the IP policy's, and no egress
        block."""
        refused = [
            # Routing named its hosts as it recorded them; the gate keeps
            # them as the page wrote them.
            RefusedHost(attempt.host, attempt.port)
            for attempt in self.blocked_attempts.counts
        ] + [
            RefusedHost(named_host(refusal.host), refusal.port)
            for refusal in self._gate.refusals
            if refusal.kind == "host"
        ]
        return EgressBlocks(
            tuple(
                host
                for host in dict.fromkeys(refused)
                if host.host not in expected_blocked
            ),
            self.blocked_attempts.overflowed,
        )

    def _retired(self, phase: _Phase, error: BaseException) -> None:
        """The phase's listener retired after an accept error (#162). Nothing
        reopens it (#141's R2), so the browser can no longer reach the
        proxy: an infrastructure event, kept in the run's gate whatever
        phase comes next. Fixed text and the error's name, never more."""
        name = type(error).__name__
        if isinstance(error, OSError) and error.errno is not None:
            name = errno.errorcode.get(error.errno, name)
        self._gate.record_failure(
            "127.0.0.1",
            phase.port,
            "the egress proxy stopped accepting the browser's connections after "
            f"an accept error ({name})",
        )

    @property
    def url(self) -> str:
        """Where the browser sends its traffic: the open phase's address."""
        if not self._entered:
            raise RuntimeError("the egress proxy serves only inside `async with`")
        if self._phase is None or self._phase.ending:
            raise RuntimeError("the egress proxy is between phases")
        # The live listener's port raises once an accept error has retired
        # it, so no browser is launched toward a port another process could
        # take.
        return f"http://127.0.0.1:{self._phase.listener.port}"

    async def _serve(
        self,
        phase: _Phase,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """One browser connection: one plain request, or one tunnel. A request
        the proxy can't read, a refusal or an upstream failure (both recorded
        by the gate), or a browser that went away ends it, with nothing more
        sent. The listener closes the connection. However it ends, its
        exchange counts in the phase whose listener accepted it."""
        browser = Peer(h11.Connection(h11.SERVER), reader, writer)
        exchange = _Exchange()
        try:
            with contextlib.suppress(
                h11.RemoteProtocolError,
                EgressRefusedError,
                EgressUpstreamError,
                OSError,
            ):
                request = await browser.next()
                if isinstance(request, h11.Request):
                    exchange.tunnel = request.method == b"CONNECT"
                    serve = self._tunnel if exchange.tunnel else self._forward
                    await serve(request, browser, exchange)
        finally:
            phase.classify(exchange)

    async def _forward(
        self, request: h11.Request, browser: Peer, exchange: _Exchange
    ) -> None:
        """Send one plain request upstream, and its response back."""
        host, port, path = _plain_target(request.target)
        reader, writer = await self._gate.connect(host, port, "request")
        upstream = Upstream(
            h11.Connection(h11.CLIENT), reader, writer, self._gate, host, port
        )

        async def answer() -> None:
            await _relay(upstream, browser)
            exchange.answered = True

        try:
            exchange.sent = True
            await upstream.send(
                h11.Request(
                    method=request.method,
                    target=path,
                    headers=[
                        # The target's own Host, whatever the browser's said
                        # (RFC 9112 §3.2.2).
                        (b"host", _host_header(host, port)),
                        *_request_headers(request.headers),
                        (b"connection", b"close"),
                    ],
                )
            )
            await _relay(browser, upstream)  # the body
            await _unless_browser_leaves(answer(), browser)
        finally:
            writer.close()

    async def _tunnel(
        self, request: h11.Request, browser: Peer, exchange: _Exchange
    ) -> None:
        """Open a tunnel and pass its bytes both ways until either side ends."""
        host, port = _host_and_port(request.target.decode("ascii"), default_port=None)
        await browser.next()  # CONNECT's EndOfMessage
        try:
            reader, writer = await self._gate.connect(host, port, "tunnel")
        except EgressRefusedError:
            await _fail_tunnel(browser, 403)
            raise
        except EgressUpstreamError:
            await _fail_tunnel(browser, 502)
            raise
        upstream = Upstream(
            h11.Connection(h11.CLIENT), reader, writer, self._gate, host, port
        )

        async def send_upstream(data: bytes) -> None:
            if data:
                exchange.sent = True
            await upstream.write(data)

        try:
            await browser.send(
                h11.Response(
                    status_code=200, reason=b"Connection established", headers=[]
                )
            )
            early, _ = browser.http.trailing_data
            await send_upstream(early)
            await _first_to_end(
                _carry(browser.read, send_upstream),
                _carry(upstream.read, browser.write),
            )
        finally:
            writer.close()


def _plain_target(target: bytes) -> tuple[str, int, str]:
    """A plain request's host, port and origin-form target, as written. A
    host the policy can't match, written in some other form, is the gate's
    to refuse and record."""
    scheme, separator, rest = target.decode("ascii").partition("://")
    if scheme != "http" or not separator:
        raise h11.RemoteProtocolError("a plain request names an http URL")
    # The authority ends where urlsplit ends it: at the first /, ? or #.
    end = min((at for at in map(rest.find, "/?#") if at >= 0), default=len(rest))
    host, port = _host_and_port(rest[:end], default_port=DEFAULT_PORTS["http"])
    # Never rewritten: the app's URL reaches it as the page wrote it.
    remainder = rest[end:]
    return host, port, remainder if remainder.startswith("/") else f"/{remainder}"


def _host_and_port(authority: str, *, default_port: int | None) -> tuple[str, int]:
    """An authority's host, lowercase with an IPv6 address in brackets, and
    its port. Only an authority no browser writes is one the proxy can't
    read."""
    try:
        parts = urlsplit(f"//{authority}")
        port = parts.port
    except ValueError as error:  # brackets that don't close, a port out of range
        raise h11.RemoteProtocolError(str(error)) from error
    if not parts.hostname or "@" in authority:
        raise h11.RemoteProtocolError(f"'{authority}' names no host alone")
    if port is None:
        if default_port is None:
            raise h11.RemoteProtocolError("CONNECT names a host and a port")
        port = default_port
    host = parts.hostname
    # Brackets hold an IPv6 address only: urlsplit drops them from any other
    # host, such as [v1.app.test], leaving a different one.
    if ("[" in authority) != (":" in host):
        raise h11.RemoteProtocolError(f"'{authority}' brackets a host that isn't IPv6")
    return (f"[{host}]" if ":" in host else host), port


def _host_header(host: str, port: int) -> bytes:
    if port == DEFAULT_PORTS["http"]:
        return host.encode()
    return f"{host}:{port}".encode()


def _request_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """A request's end-to-end headers, framed one way only: with
    Transfer-Encoding, never Content-Length as well (RFC 9112 §6.3)."""
    forwarded = _end_to_end(headers)
    if any(name == b"transfer-encoding" for name, _ in forwarded):
        return [(name, value) for name, value in forwarded if name != b"content-length"]
    return forwarded


def _end_to_end(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """`headers` without Host, the hop-by-hop ones, and any the Connection
    header names."""
    pairs = [(name.lower(), value) for name, value in headers]
    # Framing headers stay whatever Connection names: h11 framed the
    # message from them.
    named = {
        token.strip().lower()
        for name, value in pairs
        if name == b"connection"
        for token in value.split(b",")
    } - {b"content-length", b"transfer-encoding"}
    dropped = HOP_BY_HOP | named | {b"host"}
    return [(name, value) for name, value in pairs if name not in dropped]


async def _relay(source: Peer, sink: Peer) -> None:
    """Pass one message from `source` to `sink`, up to its EndOfMessage. A
    response head loses its hop-by-hop headers, and a final one closes the
    browser's connection after it."""
    while True:
        event = await source.next()
        if isinstance(event, h11.InformationalResponse):
            event = h11.InformationalResponse(
                status_code=event.status_code,
                reason=event.reason,
                headers=_end_to_end(event.headers),
            )
        elif isinstance(event, h11.Response):
            event = h11.Response(
                status_code=event.status_code,
                reason=event.reason,
                headers=[*_end_to_end(event.headers), (b"connection", b"close")],
            )
        await sink.send(event)
        if isinstance(event, h11.EndOfMessage):
            return


async def _unless_browser_leaves(
    exchange: Coroutine[None, None, None], browser: Peer
) -> None:
    """Run `exchange` until it ends, or until the browser closes its side, as
    it does when a page aborts the request: then the upstream is let go,
    rather than held until it answers."""

    async def browser_gone() -> None:
        with contextlib.suppress(OSError):
            while await browser.read():
                pass  # nothing more is expected on this connection

    await _first_to_end(exchange, browser_gone())


async def _first_to_end(*coroutines: Coroutine[None, None, None]) -> None:
    """Run `coroutines` until one ends, cancel the rest, and raise what the
    first to end raised."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        task.result()


async def _carry(
    read: Callable[[], Awaitable[bytes]], write: Callable[[bytes], Awaitable[None]]
) -> None:
    """Copy one direction of a tunnel until it ends. The browser's side
    ending, cleanly or not, just ends it; the upstream's failures are the
    gate's infrastructure events, raised by its `read` and `write`."""
    with contextlib.suppress(OSError):
        while data := await read():
            await write(data)


async def _fail_tunnel(browser: Peer, status: int) -> None:
    await browser.send(
        h11.Response(status_code=status, headers=[(b"content-length", b"0")])
    )
    await browser.send(h11.EndOfMessage())
