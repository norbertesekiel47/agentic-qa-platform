"""The loopback owner's public lifecycle with real accepted TCP sockets."""

import asyncio
import errno
import gc
import socket
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

import pytest
from aqa_runner.loopback_server import LoopbackServer


def test_exit_closes_an_idle_peer_and_joins_its_handler() -> None:
    async def scenario() -> None:
        started, ended = asyncio.Event(), asyncio.Event()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writer.write(b"control")
            started.set()
            try:
                await reader.read()
            finally:
                ended.set()

        writer = None
        async with asyncio.timeout(3):
            try:
                async with LoopbackServer(handle) as server:
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", server.port
                    )
                    await started.wait()
                    assert await reader.readexactly(7) == b"control"
                assert ended.is_set()
                assert await reader.read() == b""
            finally:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


@dataclass
class Observations:
    sockets: list[socket.socket] = field(default_factory=list)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    accepted: asyncio.Event = field(default_factory=asyncio.Event)
    reported: asyncio.Event = field(default_factory=asyncio.Event)
    closed: int = 0
    returned: bool = False


@asynccontextmanager
async def observe(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Observations]:
    seen = Observations()
    loop = asyncio.get_running_loop()
    previous, accept, close = (
        loop.get_exception_handler(),
        socket.socket.accept,
        socket.socket.close,
    )
    convert = loop.connect_accepted_socket

    def accepted(listener: socket.socket) -> tuple[socket.socket, Any]:
        connection, address = accept(listener)
        seen.sockets.append(connection)
        seen.accepted.set()
        return connection, address

    def closed(connection: socket.socket) -> None:
        if connection in seen.sockets:
            seen.closed += 1
        close(connection)

    async def converted(factory: Any, connection: socket.socket) -> Any:
        task = asyncio.current_task()
        assert task is not None
        seen.tasks.append(task)
        result = await convert(factory, connection)
        seen.returned = True
        return result

    def reported(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        seen.errors.append(context)
        seen.reported.set()

    monkeypatch.setattr(socket.socket, "accept", accepted)
    monkeypatch.setattr(socket.socket, "close", closed)
    monkeypatch.setattr(loop, "connect_accepted_socket", converted)
    loop.set_exception_handler(reported)
    try:
        async with asyncio.timeout(3):
            yield seen
    finally:
        for task in seen.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*seen.tasks, return_exceptions=True)
        for connection in seen.sockets:
            if connection.fileno() >= 0:
                close(connection)
        loop.set_exception_handler(previous)


@pytest.mark.parametrize("cancel", [False, True])
def test_real_transport_transfer_precedes_adapter_return(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            open_connection = asyncio.open_connection
            calls: list[tuple[str, int]] = []
            allocated: list[asyncio.BaseTransport] = []

            async def host_port_only(host: str, port: int) -> Any:
                calls.append((host, port))
                return await open_connection(host, port)

            class Protocol(asyncio.StreamReaderProtocol):
                def connection_made(self, transport: asyncio.BaseTransport) -> None:
                    super().connection_made(transport)
                    if seen.tasks and not seen.returned:
                        allocated.append(transport)
                        assert (
                            transport.get_extra_info("socket").fileno()
                            == seen.sockets[0].fileno()
                        )
                        if cancel:
                            seen.tasks[0].cancel()

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                writer.write(b"accepted-control")

            monkeypatch.setattr(asyncio, "open_connection", host_port_only)
            monkeypatch.setattr(asyncio, "StreamReaderProtocol", Protocol)
            async with LoopbackServer(handle) as server:
                port = server.port
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    assert await reader.read() == (
                        b"" if cancel else b"accepted-control"
                    )
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert len(allocated) == 1
            assert seen.returned is not cancel
            assert seen.sockets[0].fileno() == -1
            assert seen.closed == 1
            assert len(seen.tasks) == 1
            assert seen.tasks[0].done()
            assert seen.tasks[0].cancelled() is cancel
            assert seen.errors == []
            assert calls == [("127.0.0.1", port)]

    asyncio.run(scenario())


def test_exit_after_accept_before_task_start_closes_the_raw_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            handled: list[bytes] = []

            async def handle(
                reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                handled.append(await reader.read())

            async with LoopbackServer(handle) as server:
                client = asyncio.create_task(
                    asyncio.open_connection("127.0.0.1", server.port)
                )
                await seen.accepted.wait()
            reader, writer = await client
            try:
                assert seen.sockets[0].fileno() == -1
                assert await reader.read() == b""
                assert handled == []
                assert seen.tasks == []
                assert seen.errors == []
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage", ["setblocking", "bind", "listen", "add_reader", "registered"]
)
def test_entry_failure_closes_allocated_listener(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        listeners: list[socket.socket] = []
        descriptors: list[int] = []
        original = socket.socket.setblocking
        register = loop.add_reader
        error = OSError("entry failed")

        def setblocking(listener: socket.socket, flag: bool) -> None:
            listeners.append(listener)
            descriptors.append(listener.fileno())
            original(listener, flag)
            if stage == "setblocking":
                raise error

        def failed(*_args: Any) -> None:
            raise error

        def registration(fd: int, callback: Any) -> None:
            if stage == "registered":
                register(fd, callback)
            raise error

        async def handle(
            reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
        ) -> None:
            await reader.read()

        monkeypatch.setattr(socket.socket, "setblocking", setblocking)
        if stage in {"bind", "listen"}:
            monkeypatch.setattr(socket.socket, stage, failed)
        elif stage in {"add_reader", "registered"}:
            monkeypatch.setattr(loop, "add_reader", registration)
        with pytest.raises(OSError, match="entry failed") as raised:
            async with LoopbackServer(handle):
                pytest.fail("entry succeeded")
        assert raised.value is error
        assert len(listeners) == 1
        assert listeners[0].fileno() == -1
        assert not loop.remove_reader(descriptors[0])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        BlockingIOError(),
        InterruptedError(),
        ConnectionAbortedError(),
        OSError(errno.EMFILE, "EMFILE"),
        OSError(errno.ENFILE, "ENFILE"),
        OSError(errno.ENOBUFS, "ENOBUFS"),
        OSError(errno.ENOMEM, "ENOMEM"),
        OSError(errno.EIO, "EIO"),
    ],
)
def test_accept_failure_retires_persistent_readiness(
    monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            loop, callbacks, descriptors = asyncio.get_running_loop(), [], []
            register, accept = loop.add_reader, socket.socket.accept

            def registration(fd: int, callback: Any) -> None:
                callbacks.append(callback)
                descriptors.append(fd)
                register(fd, callback)

            def failed(_listener: socket.socket) -> Any:
                raise failure

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                writer.write(b"still serving")

            monkeypatch.setattr(loop, "add_reader", registration)
            async with LoopbackServer(handle) as server:
                monkeypatch.setattr(socket.socket, "accept", failed)
                callbacks[0]()
                transient = isinstance(
                    failure, (BlockingIOError, InterruptedError, ConnectionAbortedError)
                )
                if transient:
                    monkeypatch.setattr(socket.socket, "accept", accept)
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", server.port
                    )
                    try:
                        assert await reader.read() == b"still serving"
                    finally:
                        writer.close()
                        await writer.wait_closed()
                    assert seen.errors == []
                else:
                    assert [item["exception"] for item in seen.errors] == [failure]
                    assert not loop.remove_reader(descriptors[0])
                    callbacks[0]()
                    assert len(seen.errors) == 1
                    with pytest.raises(RuntimeError, match="not listening"):
                        _ = server.port
            callbacks[0]()

    asyncio.run(scenario())


class RegistrationFailure(asyncio.Task[None]):
    def __init__(self, coroutine: Any, error: Exception) -> None:
        super().__init__(coroutine)
        self.error, self.failed = error, False

    def add_done_callback(self, fn: Any, *, context: Any = None) -> None:
        if not self.failed:
            self.failed = True
            raise self.error
        super().add_done_callback(fn, context=context)


def install_allocation_failure(
    monkeypatch: pytest.MonkeyPatch, stage: str, seen: Observations
) -> tuple[Exception, list[Any], list[asyncio.Task[None]]]:
    error = (
        TypeError("allocation failed")
        if stage == "unexpected"
        else RuntimeError("allocation failed")
    )
    coroutines: list[Any] = []
    submitted: list[asyncio.Task[None]] = []
    original, stream = socket.socket.setblocking, asyncio.StreamReader

    def reader_factory(*args: Any, **kwargs: Any) -> asyncio.StreamReader:
        if seen.sockets:
            raise error
        return stream(*args, **kwargs)

    def nonblocking(connection: socket.socket, flag: bool) -> None:
        if connection in seen.sockets:
            raise error
        original(connection, flag)

    def submission(coroutine: Any) -> Any:
        coroutines.append(coroutine)
        if stage == "registration":
            task = RegistrationFailure(coroutine, error)
            submitted.append(task)
            return task
        raise error

    if stage in {"setblocking", "unexpected"}:
        monkeypatch.setattr(socket.socket, "setblocking", nonblocking)
    elif stage == "stream":
        monkeypatch.setattr(asyncio, "StreamReader", reader_factory)
    else:
        monkeypatch.setattr(asyncio, "create_task", submission)
    return error, coroutines, submitted


@pytest.mark.parametrize(
    "stage", ["setblocking", "create_task", "stream", "registration", "unexpected"]
)
def test_failure_after_accept_closes_raw_socket_and_unsubmitted_coroutine(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:

            async def handle(
                reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                await reader.read()

            async with LoopbackServer(handle) as server:
                error, coroutines, submitted = install_allocation_failure(
                    monkeypatch, stage, seen
                )
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                try:
                    await seen.reported.wait()
                    assert await reader.read() == b""
                    assert seen.sockets[0].fileno() == -1
                    assert [item["exception"] for item in seen.errors] == [error]
                    with pytest.raises(RuntimeError, match="not listening"):
                        _ = server.port
                    assert len(coroutines) == (
                        1 if stage in {"create_task", "registration"} else 0
                    )
                finally:
                    writer.close()
                    await writer.wait_closed()

            assert all(task.done() for task in submitted)
            assert all(coroutine.cr_frame is None for coroutine in coroutines)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["active", "shutdown", "body"])
def test_task_errors_are_reported_once_without_replacing_body_errors(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            started = asyncio.Event()
            primary, body = RuntimeError("handler"), LookupError("body")

            async def handle(
                reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                started.set()
                if phase != "active":
                    try:
                        await reader.read()
                    except asyncio.CancelledError:
                        raise primary from None
                raise primary

            writer = None
            expectation = (
                pytest.raises(LookupError, match="body")
                if phase == "body"
                else nullcontext()
            )
            try:
                with expectation as raised:
                    async with LoopbackServer(handle) as server:
                        port = server.port
                        reader, writer = await asyncio.open_connection(
                            "127.0.0.1", port
                        )
                        if phase == "active":
                            await seen.reported.wait()
                            assert server.port == port
                        else:
                            await started.wait()
                        if phase == "body":
                            raise body
                if phase == "body":
                    assert raised is not None
                    assert raised.value is body
                assert await reader.read() == b""
                assert [item["exception"] for item in seen.errors] == [primary]
                assert all(task.done() for task in seen.tasks)
                assert seen.sockets[0].fileno() == -1
                assert started.is_set()
            finally:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["cleanup", "conversion", "disconnect"])
def test_conversion_and_cleanup_failures_keep_the_primary_error(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            primary = RuntimeError("handler")
            cleanup = (
                ConnectionResetError("closed peer")
                if phase == "disconnect"
                else ValueError("cleanup")
            )
            close, started = asyncio.StreamWriter.wait_closed, asyncio.Event()

            async def handle(
                _reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                started.set()
                raise primary

            async def close_failure(writer: asyncio.StreamWriter) -> None:
                await close(writer)
                if writer.transport.get_extra_info("sockname")[1] == port:
                    raise cleanup

            async def conversion_failure(*_args: Any) -> Any:
                raise primary

            async with LoopbackServer(handle) as server:
                port = server.port
                if phase != "conversion":
                    monkeypatch.setattr(
                        asyncio.StreamWriter, "wait_closed", close_failure
                    )
                else:
                    monkeypatch.setattr(
                        asyncio.get_running_loop(),
                        "connect_accepted_socket",
                        conversion_failure,
                    )
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    await seen.reported.wait()
                    assert server.port == port
                    assert await reader.read() == b""
                finally:
                    writer.close()
                    await writer.wait_closed()
            expected = [cleanup, primary] if phase == "cleanup" else [primary]
            assert [item["exception"] for item in seen.errors] == expected
            assert all(task.done() for task in seen.tasks)
            assert seen.sockets[0].fileno() == -1
            assert started.is_set() is (phase != "conversion")

    asyncio.run(scenario())


def test_completed_connections_are_released_while_listener_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                writer.write(b"finished")

            async with LoopbackServer(handle) as server:
                port = server.port
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    assert await reader.read() == b"finished"
                    completed = weakref.ref(seen.tasks[0])
                    seen.tasks.clear()
                    callbacks_done = asyncio.Event()
                    asyncio.get_running_loop().call_soon(callbacks_done.set)
                    await callbacks_done.wait()
                    gc.collect()
                    assert completed() is None
                    assert server.port == port
                    assert seen.errors == []
                finally:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


def test_socket_creation_failure_propagates_from_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        error = OSError("socket unavailable")

        def unavailable(*_args: Any) -> Any:
            raise error

        async def handle(
            reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
        ) -> None:
            await reader.read()

        monkeypatch.setattr(socket, "socket", unavailable)
        with pytest.raises(OSError, match="socket unavailable") as raised:
            async with LoopbackServer(handle):
                pytest.fail("entry succeeded")
        assert raised.value is error

    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_shutdown_joins_cleanup_already_started_by_handler_completion(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            finished, writers = asyncio.Event(), []
            error = RuntimeError("completed handler failed")

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                writers.append(writer)
                finished.set()
                if fails:
                    raise error

            writer = None
            try:
                async with LoopbackServer(handle) as server:
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", server.port
                    )
                    await finished.wait()
                assert seen.sockets[0].fileno() == -1
                assert await reader.read() == b""
                assert [item["exception"] for item in seen.errors] == (
                    [error] if fails else []
                )
                assert all(task.done() for task in seen.tasks)
            finally:
                for server_writer in writers:
                    server_writer.close()
                    await server_writer.wait_closed()
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


async def turns(count: int) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


def test_caller_cancellation_during_exit_is_delivered_after_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            loop = asyncio.get_running_loop()
            finished, ended = asyncio.Event(), asyncio.Event()
            peers: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

            async def handle(
                _reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                finished.set()
                loop.call_soon(owner.cancel)

            async def run() -> None:
                async with LoopbackServer(handle) as server:
                    peers.append(
                        await asyncio.open_connection("127.0.0.1", server.port)
                    )
                    await finished.wait()
                    ended.set()

            owner = asyncio.create_task(run())
            try:
                with pytest.raises(asyncio.CancelledError):
                    await owner
                assert ended.is_set()
                assert seen.sockets[0].fileno() == -1
                assert all(task.done() for task in seen.tasks)
                assert await peers[0][0].read() == b""
                assert seen.errors == []
            finally:
                for _reader, writer in peers:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancellations", [1, 2])
def test_caller_cancellation_waits_for_cleanup_already_in_progress(
    monkeypatch: pytest.MonkeyPatch, cancellations: int
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            started, held, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            interrupted: list[asyncio.CancelledError] = []
            peers: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

            async def handle(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                close = writer.wait_closed

                async def held_close() -> None:
                    await close()
                    held.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError as cancellation:
                        interrupted.append(cancellation)
                        raise

                monkeypatch.setattr(writer, "wait_closed", held_close)
                started.set()
                await reader.read()

            async def run() -> None:
                async with LoopbackServer(handle) as server:
                    peers.append(
                        await asyncio.open_connection("127.0.0.1", server.port)
                    )
                    await started.wait()

            owner = asyncio.create_task(run())
            try:
                await held.wait()
                for number in range(cancellations):
                    owner.cancel(f"caller {number}")
                    await turns(2)
                assert interrupted == []
                assert not owner.done()
                release.set()
                with pytest.raises(asyncio.CancelledError) as raised:
                    await owner
                assert raised.value.args == ("caller 0",)
                assert interrupted == []
                assert seen.sockets[0].fileno() == -1
                assert await peers[0][0].read() == b""
                assert seen.errors == []
            finally:
                release.set()
                for _reader, writer in peers:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


def test_cleanup_closes_the_connection_without_a_new_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            loop, started = asyncio.get_running_loop(), asyncio.Event()
            refused: list[Any] = []

            def no_task(coroutine: Any, **_kwargs: Any) -> Any:
                refused.append(coroutine)
                coroutine.close()
                raise MemoryError("cleanup task refused")

            async def handle(
                reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                started.set()
                await reader.read()

            with monkeypatch.context() as denial:
                async with LoopbackServer(handle) as server:
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", server.port
                    )
                    await started.wait()
                    denial.setattr(loop, "create_task", no_task)
            try:
                assert refused == []
                assert seen.sockets[0].fileno() == -1
                assert await reader.read() == b""
                assert seen.errors == []
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("closes", [False, True])
def test_shutdown_while_the_handler_awaits_closure_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, closes: bool
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            started = asyncio.Event()

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                started.set()
                if closes:
                    writer.close()
                await writer.wait_closed()

            async with LoopbackServer(handle) as server:
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                await started.wait()
            try:
                assert seen.tasks[0].cancelled()
                assert seen.sockets[0].fileno() == -1
                assert await reader.read() == b""
                assert seen.errors == []
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


PAYLOAD, REQUEST = b"0123456789abcdef" * (1024 * 1024), b"half-closed"


@dataclass
class BufferedExchange:
    """A handler's payload queued behind a peer that has stopped reading."""

    wrote: asyncio.Event = field(default_factory=asyncio.Event)
    buffered: list[int] = field(default_factory=list)
    served: list[asyncio.StreamWriter] = field(default_factory=list)
    peers: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = field(
        default_factory=list
    )

    async def serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.served.append(writer)
        assert await reader.read() == REQUEST
        # Bounded so the payload queues behind the paused peer. Linux doubles
        # this and charges unacknowledged bytes to it; with room for only one
        # 64 KiB loopback segment in flight, each segment waits about 40 ms for
        # a delayed ACK and the payload outlasts observe's deadline.
        writer.get_extra_info("socket").setsockopt(
            socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024
        )
        writer.transport.set_write_buffer_limits(high=1024, low=512)
        writer.write(PAYLOAD)
        self.buffered.append(writer.transport.get_write_buffer_size())
        self.wrote.set()

    async def connect(self, port: int) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.peers.append((reader, writer))
        assert isinstance(writer.transport, asyncio.Transport)
        writer.transport.pause_reading()
        writer.write(REQUEST)
        writer.write_eof()
        await self.wrote.wait()

    async def receive(self) -> bytes:
        reader, writer = self.peers[0]
        assert isinstance(writer.transport, asyncio.Transport)
        writer.transport.resume_reading()
        return await reader.read()

    async def release(self, owner: asyncio.Task[None]) -> None:
        for writer in self.served:
            if writer.get_extra_info("socket").fileno() >= 0:
                writer.transport.abort()
        for _reader, writer in self.peers:
            writer.close()
            await writer.wait_closed()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("closes", [False, True])
def test_exit_delivers_buffered_output_after_the_close_waiter_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, closes: bool
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            exchange, cancelled = BufferedExchange(), asyncio.Event()

            async def handle(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await exchange.serve(reader, writer)
                if closes:
                    writer.close()
                try:
                    await writer.wait_closed()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async def run() -> None:
                async with LoopbackServer(handle) as server:
                    await exchange.connect(server.port)

            owner = asyncio.create_task(run())
            try:
                await cancelled.wait()
                await turns(16)
                assert exchange.buffered[0] > 0
                assert not owner.done()
                assert seen.sockets[0].fileno() >= 0
                assert seen.errors == []
                assert await exchange.receive() == PAYLOAD
                await owner
                assert seen.sockets[0].fileno() == -1
                assert seen.errors == []
            finally:
                await exchange.release(owner)

    asyncio.run(scenario())


def test_drain_waits_for_a_paused_peer_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            exchange, drained = BufferedExchange(), asyncio.Event()

            async def handle(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await exchange.serve(reader, writer)
                await writer.drain()
                drained.set()

            async def run() -> None:
                async with LoopbackServer(handle) as server:
                    await exchange.connect(server.port)
                    await drained.wait()

            owner = asyncio.create_task(run())
            try:
                await exchange.wrote.wait()
                assert exchange.buffered[0] > 0
                assert not drained.is_set()
                assert await exchange.receive() == PAYLOAD
                await owner
                assert drained.is_set()
                assert seen.sockets[0].fileno() == -1
                assert seen.errors == []
            finally:
                await exchange.release(owner)

    asyncio.run(scenario())


def test_close_notice_allocation_failure_closes_the_accepted_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            error, event = MemoryError("close notice refused"), asyncio.Event

            def notice() -> asyncio.Event:
                if seen.sockets:
                    raise error
                return event()

            async def handle(
                reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                await reader.read()

            async with LoopbackServer(handle) as server:
                monkeypatch.setattr(asyncio, "Event", notice)
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                try:
                    await seen.reported.wait()
                    assert await reader.read() == b""
                    assert seen.sockets[0].fileno() == -1
                    assert [item["exception"] for item in seen.errors] == [error]
                    assert seen.tasks == []
                    with pytest.raises(RuntimeError, match="not listening"):
                        _ = server.port
                finally:
                    writer.close()
                    await writer.wait_closed()

    asyncio.run(scenario())


def test_unexpected_accept_error_retires_the_listener_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            loop, failure = asyncio.get_running_loop(), ValueError("accept failed")
            register, callbacks, listeners, handled = loop.add_reader, [], [], []

            def registration(fd: int, callback: Any) -> None:
                callbacks.append((fd, callback))
                register(fd, callback)

            def failed(listener: socket.socket) -> Any:
                listeners.append(listener)
                raise failure

            async def handle(
                _reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                handled.append(True)

            monkeypatch.setattr(loop, "add_reader", registration)
            async with LoopbackServer(handle) as server:
                monkeypatch.setattr(socket.socket, "accept", failed)
                fd, callback = callbacks[0]
                loop.call_soon(callback)
                await seen.reported.wait()
                assert listeners[0].fileno() == -1
                assert not loop.remove_reader(fd)
                with pytest.raises(RuntimeError, match="not listening"):
                    _ = server.port
                for _ in range(3):
                    loop.call_soon(callback)
                await turns(3)
                assert len(listeners) == 1
                assert [item["exception"] for item in seen.errors] == [failure]
                assert handled == []

    asyncio.run(scenario())


def test_cleanup_failure_after_a_finished_handler_is_reported_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            cleanup, close = ValueError("cleanup"), asyncio.StreamWriter.wait_closed

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                writer.write(b"served")

            async def close_failure(writer: asyncio.StreamWriter) -> None:
                await close(writer)
                if writer.transport.get_extra_info("sockname")[1] == port:
                    raise cleanup

            async with LoopbackServer(handle) as server:
                port = server.port
                monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", close_failure)
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    assert await reader.read() == b"served"
                    await seen.reported.wait()
                    assert server.port == port
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert [item["exception"] for item in seen.errors] == [cleanup]
            assert seen.sockets[0].fileno() == -1

    asyncio.run(scenario())


def test_a_failing_stream_connection_lost_still_ends_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            error, received = RuntimeError("connection_lost failed"), []

            class Failing(asyncio.StreamReaderProtocol):
                def connection_lost(self, exc: Exception | None) -> None:
                    super().connection_lost(exc)
                    raise error

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                writer.write(b"served")

            async def run() -> None:
                async with LoopbackServer(handle) as server:
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", server.port
                    )
                    received.append(await reader.read())
                    writer.close()
                    await writer.wait_closed()

            monkeypatch.setattr(asyncio, "StreamReaderProtocol", Failing)
            owner = asyncio.create_task(run())
            try:
                assert await asyncio.wait({owner}, timeout=2) == ({owner}, set())
                assert received == [b"served"]
                assert [item["exception"] for item in seen.errors] == [error]
                assert seen.sockets[0].fileno() == -1
            finally:
                for task in seen.tasks:
                    task.cancel()
                await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(scenario())


def test_shutdown_cancels_a_handler_whose_own_deadline_is_expiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            loop = asyncio.get_running_loop()
            waiting, wake = asyncio.Event(), asyncio.Event()
            deadlines: list[asyncio.Timeout] = []
            outcomes: list[str] = []
            peers: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

            async def handle(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                try:
                    async with asyncio.timeout(None) as deadline:
                        deadlines.append(deadline)
                        waiting.set()
                        await reader.read()
                except TimeoutError:
                    outcomes.append("timed out")
                    writer.write(b"forwarded after shutdown")
                    await reader.read()
                except asyncio.CancelledError:
                    outcomes.append("cancelled")
                    raise

            async def run() -> None:
                async with LoopbackServer(handle) as server:
                    peers.append(
                        await asyncio.open_connection("127.0.0.1", server.port)
                    )
                    await waiting.wait()
                    # Timers due at one instant run in the order they were set,
                    # so exit begins in the turn the handler's deadline fires.
                    when = loop.time() + 0.05
                    loop.call_at(when, wake.set)
                    deadlines[0].reschedule(when)
                    await wake.wait()

            owner = asyncio.create_task(run())
            try:
                assert await asyncio.wait({owner}, timeout=2) == ({owner}, set())
                assert outcomes == ["cancelled"]
                assert await peers[0][0].read() == b""
                assert seen.errors == []
            finally:
                for _reader, writer in peers:
                    writer.close()
                    await writer.wait_closed()
                await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(scenario())


def test_an_exception_handled_around_the_loop_is_not_the_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:

            async def handle(
                _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0):
                        await writer.wait_closed()

            async with LoopbackServer(handle) as server:
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                try:
                    assert await reader.read() == b""
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert seen.errors == []
            assert seen.sockets[0].fileno() == -1

    outer = LookupError("handled by the code running the loop")
    try:
        raise outer
    except LookupError:
        asyncio.run(scenario())
    assert outer.__cause__ is None


def test_writer_construction_failure_closes_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            error = MemoryError("writer refused")
            construct = asyncio.streams.StreamWriter
            transports: list[asyncio.WriteTransport] = []
            handled: list[bool] = []

            def refused(transport: asyncio.WriteTransport, *args: Any) -> Any:
                if transport.get_extra_info("sockname")[1] == port:
                    transports.append(transport)
                    raise error
                return construct(transport, *args)

            async def handle(
                _reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
            ) -> None:
                handled.append(True)

            async with LoopbackServer(handle) as server:
                port = server.port
                monkeypatch.setattr(asyncio.streams, "StreamWriter", refused)
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    await seen.reported.wait()
                    assert await reader.read() == b""
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert transports[0].is_closing()
            assert seen.sockets[0].fileno() == -1
            assert [item["exception"] for item in seen.errors] == [error]
            assert handled == []
            assert all(task.done() for task in seen.tasks)

    asyncio.run(scenario())
