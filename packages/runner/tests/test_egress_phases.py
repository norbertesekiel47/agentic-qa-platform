"""The egress proxy's phases (ADR-0026's #53 P8 amendment): each phase has
its own listener; ending it drops queued connections, cancels and joins
every accepted one, and returns what they left uncertain, so the reset
hook runs only once the phase's traffic is certain. Every origin here
listens on loopback."""

import asyncio
import contextlib
import errno
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressProxy, PhaseTraffic

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import (
    connect,
    exchange,
    gate,
    get,
    proxy_client,
    raw_upstream,
    unused_port,
)
from packages.runner.tests.test_loopback_server import turns
from packages.runner.tests.test_runner_requests import certificate

ANSWER = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


def port_of(proxy: EgressProxy) -> int:
    return int(urlsplit(proxy.url).port or 0)


def post(url: str) -> bytes:
    return (
        f"POST {url} HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n"
        "Connection: close\r\n\r\ncharge"
    ).encode()


@dataclass
class Watch:
    """The connections the proxy's listeners accepted, seen from the loop:
    their sockets and tasks, and the barriers a test can hold them at,
    in stream conversion (`convert`) or in closing (`close`). While `fail`
    is set, an accept on a watched listener raises it; `accepts` counts
    every accept a watched listener tried."""

    ports: set[int] = field(default_factory=set)
    fail: BaseException | None = None
    accepts: int = 0
    peers: set[Any] = field(default_factory=set)
    sockets: list[socket.socket] = field(default_factory=list)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    converting: asyncio.Event = field(default_factory=asyncio.Event)
    convert: asyncio.Event | None = None
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    close: asyncio.Event | None = None

    def watch(self, proxy: EgressProxy) -> int:
        port = port_of(proxy)
        self.ports.add(port)
        return port

    async def released(self, port: int) -> None:
        """Every connection the proxy accepted is closed and its task done,
        and the listener on `port` refuses a new one."""
        assert all(accepted.fileno() == -1 for accepted in self.sockets)
        assert all(task.done() for task in self.tasks)
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", port)


@pytest.fixture
def watch(monkeypatch: pytest.MonkeyPatch) -> Watch:
    seen = Watch()
    accept = socket.socket.accept
    convert = asyncio.BaseEventLoop.connect_accepted_socket
    wait_closed = asyncio.StreamWriter.wait_closed

    def accepted(listener: socket.socket) -> tuple[socket.socket, Any]:
        watched = listener.getsockname()[1] in seen.ports
        seen.accepts += watched
        if watched and seen.fail is not None:
            raise seen.fail
        connection, address = accept(listener)
        if watched:
            seen.sockets.append(connection)
            seen.peers.add(connection.getpeername())
        return connection, address

    async def converted(
        loop: asyncio.BaseEventLoop, factory: Any, connection: socket.socket
    ) -> Any:
        if connection in seen.sockets:
            task = asyncio.current_task()
            assert task is not None
            seen.tasks.append(task)
            if seen.convert is not None:
                seen.converting.set()
                await seen.convert.wait()
        return await convert(loop, factory, connection)

    async def closed(writer: asyncio.StreamWriter) -> None:
        await wait_closed(writer)
        if seen.close is not None and writer.get_extra_info("peername") in seen.peers:
            seen.closing.set()
            await seen.close.wait()

    monkeypatch.setattr(socket.socket, "accept", accepted)
    monkeypatch.setattr(asyncio.BaseEventLoop, "connect_accepted_socket", converted)
    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", closed)
    return seen


def test_a_plain_exchange_answered_in_full_is_complete() -> None:
    async def scenario() -> tuple[bytes, PhaseTraffic]:
        async with raw_upstream(ANSWER) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                received = await exchange(proxy, get(f"{start}/"))
                return received, await proxy.end_phase()

    received, traffic = asyncio.run(scenario())

    assert received.endswith(b"\r\n\r\nok")
    assert traffic == PhaseTraffic(0, 0)


def test_a_plain_exchange_the_browser_left_before_its_response_is_uncertain() -> None:
    async def scenario() -> PhaseTraffic:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                _, writer = await proxy_client(proxy)
                writer.write(get(f"{start}/"))
                await until(lambda: b"\r\n\r\n" in silent.received)
                writer.close()
                await writer.wait_closed()
                # The proxy let the upstream go when the browser left.
                await asyncio.wait_for(silent.closed.wait(), 5)
                return await proxy.end_phase()

    assert asyncio.run(scenario()) == PhaseTraffic(1, 0)


def test_a_plain_exchange_cut_by_an_upstream_error_after_sending_is_uncertain() -> None:
    async def scenario() -> tuple[bytes, int, PhaseTraffic]:
        async with raw_upstream(resets=True) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            egress = gate(allowed=(start,))
            async with EgressProxy(egress) as proxy:
                received = await exchange(proxy, get(f"{start}/"))
                traffic = await proxy.end_phase()
            return received, len(egress.infrastructure_events), traffic

    assert asyncio.run(scenario()) == (b"", 1, PhaseTraffic(1, 0))


def test_a_plain_exchange_cancelled_at_the_phases_end_is_uncertain() -> None:
    async def scenario() -> tuple[PhaseTraffic, bytes]:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                reader, writer = await proxy_client(proxy)
                writer.write(get(f"{start}/"))
                await until(lambda: b"\r\n\r\n" in silent.received)
                traffic = await proxy.end_phase()
                left = await reader.read()
                writer.close()
                return traffic, left

    assert asyncio.run(scenario()) == (PhaseTraffic(1, 0), b"")


class CutWriter:
    """An upstream connection's writer that takes the bytes, then fails as a
    reset does while they drain: a request cut partway upstream."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer

    def write(self, data: bytes) -> None:
        self.writer.write(data)

    async def drain(self) -> None:
        raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")

    def close(self) -> None:
        self.writer.close()


def test_a_plain_exchange_whose_request_fails_partway_upstream_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_connection = asyncio.open_connection

    async def scenario() -> tuple[int, PhaseTraffic]:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"

            async def opening(host: str, port: int, **tls: Any) -> tuple[Any, Any]:
                reader, writer = await open_connection(host, port, **tls)
                return reader, CutWriter(writer) if port == upstream.port else writer

            monkeypatch.setattr(asyncio, "open_connection", opening)
            egress = gate(allowed=(start,))
            async with EgressProxy(egress) as proxy:
                await exchange(proxy, get(f"{start}/"))
                traffic = await proxy.end_phase()
            return len(egress.infrastructure_events), traffic

    assert asyncio.run(scenario()) == (1, PhaseTraffic(1, 0))


@pytest.mark.parametrize("case", ["refused host", "unreachable", "unreadable"])
def test_a_refused_or_unconnected_request_is_clean(case: str) -> None:
    start = f"http://127.0.0.1:{unused_port()}"
    request = {
        "refused host": get("http://evil.example.test/"),
        "unreachable": get(f"{start}/"),
        "unreadable": b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n",
    }[case]

    async def scenario() -> tuple[bytes, PhaseTraffic]:
        async with EgressProxy(gate(allowed=(start,))) as proxy:
            received = await exchange(proxy, request)
            return received, await proxy.end_phase()

    assert asyncio.run(scenario()) == (b"", PhaseTraffic(0, 0))


def test_a_tunnel_that_carried_nothing_upstream_is_clean() -> None:
    async def scenario() -> tuple[bytes, PhaseTraffic]:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                reader, writer = await proxy_client(proxy)
                writer.write(connect(f"127.0.0.1:{upstream.port}"))
                received = await reader.readuntil(b"\r\n\r\n")
                writer.close()
                await asyncio.wait_for(upstream.closed.wait(), 5)
                return received, await proxy.end_phase()

    received, traffic = asyncio.run(scenario())

    assert received.startswith(b"HTTP/1.1 200 ")
    assert traffic == PhaseTraffic(0, 0)


@pytest.mark.parametrize(
    "ending", ["browser first", "upstream first", "upstream error", "open at the end"]
)
def test_a_tunnel_that_carried_bytes_upstream_is_uncertain_however_it_ends(
    ending: str,
) -> None:
    async def scenario() -> PhaseTraffic:
        async with raw_upstream(
            b"bye" if ending == "upstream first" else None,
            resets=ending == "upstream error",
        ) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                reader, writer = await proxy_client(proxy)
                writer.write(connect(f"127.0.0.1:{upstream.port}"))
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"opaque\r\n\r\n")
                await until(lambda: bool(upstream.received))
                if ending == "browser first":
                    writer.close()
                    await asyncio.wait_for(upstream.closed.wait(), 5)
                elif ending != "open at the end":
                    # The upstream ended it: the browser's side sees it end.
                    with contextlib.suppress(ConnectionError):
                        await reader.read()
                traffic = await proxy.end_phase()
                writer.close()
                return traffic

    assert asyncio.run(scenario()) == PhaseTraffic(0, 1)


@dataclass
class TlsApp:
    """An https app's port, the requests it committed, and the event it
    waits for before it commits (or, after answering, before it closes)."""

    port: int
    committed: list[bytes] = field(default_factory=list)
    received: threading.Event = field(default_factory=threading.Event)
    proceed: threading.Event = field(default_factory=threading.Event)


@contextmanager
def tls_app(directory: Path, *, answers: bool) -> Iterator[TlsApp]:
    """An https app on 127.0.0.1 that takes one connection and reads one
    request. When `answers`, it commits it, answers 204 and keeps the
    connection open until `proceed`; otherwise it commits it only once
    `proceed` is set, whether or not its client is still there."""
    tls = certificate(directory, "127.0.0.1")
    listener = socket.create_server(("127.0.0.1", 0))
    app = TlsApp(listener.getsockname()[1])

    def serve() -> None:
        connection, _ = listener.accept()
        with (
            contextlib.suppress(OSError),
            tls.wrap_socket(connection, server_side=True) as private,
        ):
            request = private.recv(65536)
            app.received.set()
            if answers:
                app.committed.append(request)
                private.sendall(b"HTTP/1.1 204 No Content\r\n\r\n")
            app.proceed.wait(5)
            if not answers:
                app.committed.append(request)
        connection.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield app
    finally:
        app.proceed.set()
        thread.join(5)
        listener.close()


async def tls_client(
    proxy: EgressProxy, port: int, directory: Path
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A browser's https connection to the app on `port`, through the
    proxy's tunnel."""
    reader, writer = await proxy_client(proxy)
    writer.write(connect(f"127.0.0.1:{port}"))
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200 ")
    trusted = ssl.create_default_context(cafile=directory / "cert.pem")
    await writer.start_tls(trusted, server_hostname="127.0.0.1")
    return reader, writer


def test_a_tls_request_whose_client_disconnects_before_teardown_while_the_server_commits_afterwards_is_uncertain(
    tmp_path: Path,
) -> None:
    with tls_app(tmp_path, answers=False) as app:
        start = f"https://127.0.0.1:{app.port}"

        async def scenario() -> tuple[PhaseTraffic, int]:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                _, writer = await tls_client(proxy, app.port, tmp_path)
                writer.write(
                    b"POST /charge HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 6\r\n\r\ncharge"
                )
                await until(app.received.is_set)
                # The tab closes: no TLS goodbye, no answer awaited.
                writer.transport.abort()
                traffic = await proxy.end_phase()
                return traffic, len(app.committed)

        traffic, committed_at_the_end = asyncio.run(scenario())
        app.proceed.set()

    assert traffic == PhaseTraffic(0, 1)
    # The app committed only after the phase had ended: the proxy couldn't
    # have seen it, so the phase is uncertain.
    assert committed_at_the_end == 0
    assert len(app.committed) == 1


def test_an_idle_tls_connection_after_a_completed_exchange_is_uncertain(
    tmp_path: Path,
) -> None:
    with tls_app(tmp_path, answers=True) as app:
        start = f"https://127.0.0.1:{app.port}"

        async def scenario() -> tuple[bytes, PhaseTraffic]:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                reader, writer = await tls_client(proxy, app.port, tmp_path)
                writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                answer = await reader.readuntil(b"\r\n\r\n")
                traffic = await proxy.end_phase()
                writer.close()
                return answer, traffic

        answer, traffic = asyncio.run(scenario())

    assert answer.startswith(b"HTTP/1.1 204 ")
    assert traffic == PhaseTraffic(0, 1)


def test_a_buffered_post_paused_in_conversion_when_the_phase_ends_never_reaches_upstream(
    watch: Watch,
) -> None:
    async def scenario() -> tuple[PhaseTraffic, bytes]:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                port = watch.watch(proxy)
                watch.convert = asyncio.Event()
                _, writer = await proxy_client(proxy)
                writer.write(post(f"{start}/charge"))
                writer.close()
                await asyncio.wait_for(watch.converting.wait(), 5)
                traffic = await proxy.end_phase()
                await watch.released(port)
                watch.convert.set()
                await asyncio.sleep(0.2)
            return traffic, bytes(upstream.received)

    assert asyncio.run(scenario()) == (PhaseTraffic(0, 0), b"")


def test_a_post_queued_in_the_listen_backlog_when_the_phase_ends_never_reaches_upstream(
    watch: Watch,
) -> None:
    async def scenario() -> tuple[PhaseTraffic, bytes]:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                port = watch.watch(proxy)
                # No await between connecting and ending: the loop never
                # gets to accept it.
                with socket.create_connection(("127.0.0.1", port)) as browser:
                    browser.sendall(post(f"{start}/charge"))
                traffic = await proxy.end_phase()
                assert watch.sockets == []
                await watch.released(port)
                await asyncio.sleep(0.2)
            return traffic, bytes(upstream.received)

    assert asyncio.run(scenario()) == (PhaseTraffic(0, 0), b"")


def test_no_handler_from_an_ended_phase_starts_after_the_next_phase_begins(
    watch: Watch,
) -> None:
    async def scenario() -> tuple[PhaseTraffic, bytes, bytes]:
        async with raw_upstream(ANSWER) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                first = watch.watch(proxy)
                release = watch.convert = asyncio.Event()
                _, writer = await proxy_client(proxy)
                writer.write(post(f"{start}/charge"))
                await asyncio.wait_for(watch.converting.wait(), 5)
                traffic = await proxy.end_phase()
                await watch.released(first)
                await proxy.begin_phase()
                release.set()
                answer = await exchange(proxy, get(f"{start}/after"))
                writer.close()
            return traffic, bytes(upstream.received), answer

    traffic, received, answer = asyncio.run(scenario())

    assert traffic == PhaseTraffic(0, 0)
    assert answer.endswith(b"\r\n\r\nok")
    # Only the next phase's request reached the upstream.
    assert received.startswith(b"GET /after HTTP/1.1\r\n")
    assert b"charge" not in received


def test_a_released_conversion_in_an_open_phase_forwards_its_buffered_post(
    watch: Watch,
) -> None:
    # The control: the hazard the barrier tests remove is real.
    async def scenario() -> bytes:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                watch.watch(proxy)
                release = watch.convert = asyncio.Event()
                _, writer = await proxy_client(proxy)
                writer.write(post(f"{start}/charge"))
                writer.close()
                await asyncio.wait_for(watch.converting.wait(), 5)
                release.set()
                await until(lambda: upstream.received.endswith(b"charge"))
            return bytes(upstream.received)

    received = asyncio.run(scenario())

    assert received.startswith(b"POST /charge HTTP/1.1\r\n")
    assert received.endswith(b"\r\n\r\ncharge")


def test_each_phase_listens_on_a_new_port_and_the_ended_ones_refuses() -> None:
    async def scenario() -> tuple[int, int, bytes]:
        async with raw_upstream(ANSWER) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                first = port_of(proxy)
                await proxy.end_phase()
                with pytest.raises(RuntimeError, match="between phases"):
                    _ = proxy.url
                with pytest.raises(ConnectionRefusedError):
                    await asyncio.open_connection("127.0.0.1", first)
                await proxy.begin_phase()
                answer = await exchange(proxy, get(f"{start}/"))
                return first, port_of(proxy), answer

    first, second, answer = asyncio.run(scenario())

    assert second != first
    assert answer.endswith(b"\r\n\r\nok")


def test_end_phase_returns_only_that_phases_uncertain_counts() -> None:
    async def scenario() -> tuple[PhaseTraffic, PhaseTraffic]:
        async with raw_upstream(ANSWER) as answering, raw_upstream() as silent:
            answered = f"http://127.0.0.1:{answering.port}"
            unanswered = f"http://127.0.0.1:{silent.port}"
            egress = gate(allowed=(answered, unanswered), private=(unanswered,))
            async with EgressProxy(egress) as proxy:
                _, writer = await proxy_client(proxy)
                writer.write(get(f"{unanswered}/"))
                await until(lambda: b"\r\n\r\n" in silent.received)
                first = await proxy.end_phase()
                writer.close()
                await proxy.begin_phase()
                await exchange(proxy, get(f"{answered}/"))
                return first, await proxy.end_phase()

    assert asyncio.run(scenario()) == (PhaseTraffic(1, 0), PhaseTraffic(0, 0))


def test_a_one_phase_proxy_behaves_as_before() -> None:
    # The control: a proxy no one gives phases serves until it closes.
    async def scenario() -> bytes:
        async with raw_upstream(ANSWER) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(proxy, get(f"{start}/"))

    assert asyncio.run(scenario()).endswith(b"\r\n\r\nok")


def test_leaving_the_proxy_between_phases_closes_cleanly() -> None:
    async def scenario() -> EgressProxy:
        async with EgressProxy(gate(allowed=("http://127.0.0.1:9",))) as proxy:
            await proxy.end_phase()
        return proxy

    proxy = asyncio.run(scenario())

    with pytest.raises(RuntimeError, match="async with"):
        _ = proxy.url


def test_a_phase_cannot_begin_while_one_is_open_or_ending(watch: Watch) -> None:
    async def scenario() -> tuple[PhaseTraffic, int, int]:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                first = watch.watch(proxy)
                with pytest.raises(RuntimeError, match="open or ending"):
                    await proxy.begin_phase()
                # Entering it again leaves the open phase as it was.
                with pytest.raises(RuntimeError, match="open or ending"):
                    await proxy.__aenter__()
                assert port_of(proxy) == first
                release = watch.close = asyncio.Event()
                _, writer = await proxy_client(proxy)
                writer.write(get(f"{start}/"))
                await until(lambda: b"\r\n\r\n" in silent.received)
                ending = asyncio.create_task(proxy.end_phase())
                await asyncio.wait_for(watch.closing.wait(), 5)
                with pytest.raises(RuntimeError, match="open or ending"):
                    await proxy.begin_phase()
                with pytest.raises(RuntimeError, match="between phases"):
                    _ = proxy.url
                with pytest.raises(RuntimeError, match="no open phase"):
                    await proxy.end_phase()
                release.set()
                traffic = await ending
                await watch.released(first)
                await proxy.begin_phase()
                writer.close()
                return traffic, first, port_of(proxy)

    traffic, first, second = asyncio.run(scenario())

    assert traffic == PhaseTraffic(1, 0)
    assert second != first


@pytest.mark.parametrize("cancellations", [1, 2])
def test_end_phase_cancelled_repeatedly_still_joins_before_it_raises(
    watch: Watch, cancellations: int
) -> None:
    async def scenario() -> tuple[int, int]:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                first = watch.watch(proxy)
                release = watch.close = asyncio.Event()
                _, writer = await proxy_client(proxy)
                writer.write(get(f"{start}/"))
                await until(lambda: b"\r\n\r\n" in silent.received)
                ending = asyncio.create_task(proxy.end_phase())
                await asyncio.wait_for(watch.closing.wait(), 5)
                for number in range(cancellations):
                    ending.cancel(f"caller {number}")
                    await turns(2)
                # The caller's next step, a reset, can't start: the phase's
                # cleanup still holds it.
                assert not ending.done()
                release.set()
                with pytest.raises(asyncio.CancelledError) as raised:
                    await ending
                assert raised.value.args == ("caller 0",)
                await watch.released(first)
                with pytest.raises(RuntimeError, match="between phases"):
                    _ = proxy.url
                await proxy.begin_phase()
                writer.close()
                return first, port_of(proxy)

    first, second = asyncio.run(scenario())

    assert second != first


def test_a_keyboard_interrupt_in_the_proxys_body_joins_the_phase_before_it_propagates(
    watch: Watch,
) -> None:
    held: list[bool] = []
    ports: list[int] = []
    proxies: list[EgressProxy] = []

    async def scenario() -> None:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            release = watch.close = asyncio.Event()

            async def release_once_held() -> None:
                await asyncio.wait_for(watch.closing.wait(), 5)
                held.append(not watch.tasks[0].done())
                release.set()

            releasing = asyncio.create_task(release_once_held())
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                proxies.append(proxy)
                ports.append(watch.watch(proxy))
                _, writer = await proxy_client(proxy)
                writer.write(get(f"{start}/"))
                await until(lambda: b"\r\n\r\n" in silent.received)
                writer.close()
                raise KeyboardInterrupt
            await releasing

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(scenario())

    assert held == [True]
    assert all(accepted.fileno() == -1 for accepted in watch.sockets)
    assert all(task.done() for task in watch.tasks)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", ports[0]), timeout=1)
    with pytest.raises(RuntimeError, match="async with"):
        _ = proxies[0].url


def retired(port: int, name: str) -> InfrastructureEvent:
    return InfrastructureEvent(
        "127.0.0.1",
        port,
        "the egress proxy stopped accepting the browser's connections after an "
        f"accept error ({name})",
    )


async def retire(proxy: EgressProxy, watch: Watch, failure: BaseException) -> int:
    """Retire the proxy's open listener with `failure` on its next accept,
    and give its port."""
    port = watch.watch(proxy)
    watch.fail = failure
    # A plain socket: the listener may close under an asyncio client's setup.
    with socket.create_connection(("127.0.0.1", port)):
        await until(lambda: watch.accepts > 0)
        await turns(2)
    return port


@pytest.mark.parametrize(
    ("failure", "name"),
    [
        (OSError(errno.EMFILE, "Too many open files"), "EMFILE"),
        (OSError(errno.ENOBUFS, "No buffer space available"), "ENOBUFS"),
        (ValueError("accept failed"), "ValueError"),
        # An errno no name is known for, or none: the class names it.
        (OSError(99999, "Unknown error"), "OSError"),
        (OSError("accept failed"), "OSError"),
    ],
)
def test_a_retired_listener_is_an_infrastructure_event_with_a_fixed_cause(
    watch: Watch, failure: BaseException, name: str
) -> None:
    egress = gate(allowed=("http://127.0.0.1:9",))

    async def scenario() -> int:
        async with EgressProxy(egress) as proxy:
            return await retire(proxy, watch, failure)

    port = asyncio.run(scenario())

    assert egress.infrastructure_events == [retired(port, name)]


def test_a_retirement_is_kept_when_the_phase_had_no_uncertain_exchange(
    watch: Watch,
) -> None:
    egress = gate(allowed=("http://127.0.0.1:9",))

    async def scenario() -> tuple[int, PhaseTraffic]:
        async with EgressProxy(egress) as proxy:
            port = await retire(proxy, watch, OSError(errno.EMFILE, "EMFILE"))
            return port, await proxy.end_phase()

    port, traffic = asyncio.run(scenario())

    assert traffic == PhaseTraffic(0, 0)
    assert egress.infrastructure_events == [retired(port, "EMFILE")]


def test_a_retirement_in_one_phase_is_still_recorded_after_the_next_phase_begins(
    watch: Watch,
) -> None:
    async def scenario() -> tuple[int, int, bytes, list[InfrastructureEvent]]:
        async with raw_upstream(ANSWER) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            egress = gate(allowed=(start,))
            async with EgressProxy(egress) as proxy:
                first = await retire(proxy, watch, OSError(errno.EMFILE, "EMFILE"))
                await proxy.end_phase()
                watch.fail = None
                await proxy.begin_phase()
                answer = await exchange(proxy, get(f"{start}/"))
                return first, port_of(proxy), answer, egress.infrastructure_events

    first, second, answer, events = asyncio.run(scenario())

    assert second != first
    assert answer.endswith(b"\r\n\r\nok")
    assert events == [retired(first, "EMFILE")]


def test_a_retired_listener_is_never_retried(watch: Watch) -> None:
    egress = gate(allowed=("http://127.0.0.1:9",))

    async def scenario() -> int:
        async with EgressProxy(egress) as proxy:
            port = await retire(proxy, watch, OSError(errno.EMFILE, "EMFILE"))
            watch.fail = None
            # Its socket is closed: nothing listens there any more.
            with pytest.raises(ConnectionRefusedError):
                await asyncio.open_connection("127.0.0.1", port)
            # asyncio.start_server, the listener's predecessor, retried 1 s
            # after a persistent accept error.
            await asyncio.sleep(1.2)
            with pytest.raises(ConnectionRefusedError):
                await asyncio.open_connection("127.0.0.1", port)
            return port

    port = asyncio.run(scenario())

    assert watch.accepts == 1
    assert egress.infrastructure_events == [retired(port, "EMFILE")]


def test_a_retired_phase_gives_no_url(watch: Watch) -> None:
    # No browser may be pointed at a port another process could now take.
    async def scenario() -> PhaseTraffic:
        async with EgressProxy(gate(allowed=("http://127.0.0.1:9",))) as proxy:
            await retire(proxy, watch, OSError(errno.EMFILE, "EMFILE"))
            with pytest.raises(RuntimeError, match="not listening"):
                _ = proxy.url
            return await proxy.end_phase()

    assert asyncio.run(scenario()) == PhaseTraffic(0, 0)


def test_leaving_the_proxy_while_its_phase_is_ending_waits_for_the_join(
    watch: Watch,
) -> None:
    async def scenario() -> tuple[PhaseTraffic, bool]:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            proxy = EgressProxy(gate(allowed=(start,)))
            await proxy.__aenter__()
            port = watch.watch(proxy)
            release = watch.close = asyncio.Event()
            _, writer = await proxy_client(proxy)
            writer.write(get(f"{start}/"))
            await until(lambda: b"\r\n\r\n" in silent.received)
            ending = asyncio.create_task(proxy.end_phase())
            await asyncio.wait_for(watch.closing.wait(), 5)
            leaving = asyncio.create_task(proxy.__aexit__(None, None, None))
            await turns(3)
            held = not leaving.done()
            release.set()
            await leaving
            traffic = await ending
            await watch.released(port)
            writer.close()
            return traffic, held

    assert asyncio.run(scenario()) == (PhaseTraffic(1, 0), True)


def test_a_phase_opened_while_the_proxy_exits_outlives_that_exit(watch: Watch) -> None:
    # A misuse, entering while another task exits, must still leave no
    # listener the proxy has forgotten.
    async def scenario() -> tuple[int, int]:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            proxy = EgressProxy(gate(allowed=(start,)))
            await proxy.__aenter__()
            first = watch.watch(proxy)
            _, writer = await proxy_client(proxy)
            writer.write(get(f"{start}/"))
            await until(lambda: b"\r\n\r\n" in silent.received)

            async def end_then_enter() -> None:
                await proxy.end_phase()
                await proxy.__aenter__()

            entering = asyncio.create_task(end_then_enter())
            await asyncio.sleep(0)
            leaving = asyncio.create_task(proxy.__aexit__(None, None, None))
            await asyncio.gather(entering, leaving)
            second = port_of(proxy)
            await proxy.__aexit__(None, None, None)
            writer.close()
            return first, second

    first, second = asyncio.run(scenario())

    assert second != first
