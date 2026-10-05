"""Own loopback accepts before asynchronous stream construction (ADR-0026)."""

import asyncio
import socket
import sys
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from types import TracebackType
from typing import Self

type Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@dataclass(eq=False)
class _Connection:
    raw: socket.socket | None
    writer: asyncio.StreamWriter | None = None
    task: asyncio.Task[None] | None = None
    closing: bool = False
    cleanup_error: BaseException | None = None

    def connected(
        self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writer = writer
        self.raw = None

    def close_raw(self) -> None:
        if self.raw is not None:
            self.raw.close()
            self.raw = None


class _ClosingStream(asyncio.Protocol):
    """A stream protocol whose loss stays observable after its waiter is cancelled."""

    def __init__(self, stream: asyncio.StreamReaderProtocol) -> None:
        self.stream = stream
        self.closed = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.stream.connection_made(transport)

    def data_received(self, data: bytes) -> None:
        self.stream.data_received(data)

    def eof_received(self) -> bool | None:
        return self.stream.eof_received()

    def pause_writing(self) -> None:
        self.stream.pause_writing()

    def resume_writing(self) -> None:
        self.stream.resume_writing()

    def connection_lost(self, exc: Exception | None) -> None:
        try:
            self.stream.connection_lost(exc)
        finally:
            self.closed.set()


class LoopbackServer:
    """A plain IPv4 listener whose context joins every accepted connection."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._socket: socket.socket | None = None
        self._connections: set[_Connection] = set()

    @property
    def port(self) -> int:
        if self._socket is None:
            raise RuntimeError("the loopback server is not listening")
        return int(self._socket.getsockname()[1])

    async def __aenter__(self) -> Self:
        loop = asyncio.get_running_loop()
        with ExitStack() as rollback:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            rollback.callback(listener.close)
            listener.setblocking(False)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            rollback.callback(loop.remove_reader, listener.fileno())
            loop.add_reader(listener.fileno(), self._accept)
            self._socket = listener
            rollback.pop_all()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._stop()
        tasks = [c.task for c in self._connections if c.task is not None]
        joined = asyncio.gather(*tasks, return_exceptions=True)
        cancelled: asyncio.CancelledError | None = None
        # A caller's cancellation waits for the join, so it never abandons a
        # transferred connection before its transport has closed.
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError as cancellation:
                if cancelled is None:
                    cancelled = cancellation
        for connection in tuple(self._connections):
            if connection.task is not None:
                self._finished(connection, connection.task)
        if cancelled is not None:
            raise cancelled

    def _stop(self) -> None:
        if self._socket is not None:
            asyncio.get_running_loop().remove_reader(self._socket.fileno())
            self._socket.close()
            self._socket = None
        for connection in tuple(self._connections):
            if connection.task is None:
                connection.close_raw()
                self._connections.discard(connection)
            elif not connection.closing and not connection.task.cancelling():
                connection.task.cancel()

    def _accept(self) -> None:
        if self._socket is None:
            return
        try:
            raw, _ = self._socket.accept()
        except BlockingIOError, InterruptedError, ConnectionAbortedError:
            return
        except OSError as error:
            self._fail(error)
            return
        except Exception:
            # An unexpected readiness error retires the listener, then reaches
            # the loop's callback error handler unchanged.
            self._stop()
            raise
        try:
            self._submit(raw)
        except (OSError, RuntimeError, ValueError, MemoryError) as error:
            self._fail(error)
        except Exception:
            # Likewise for an unexpected setup error; rollback or _stop closes
            # the accepted socket.
            self._stop()
            raise

    def _fail(self, error: BaseException) -> None:
        self._stop()
        asyncio.get_running_loop().call_exception_handler(
            {"message": "loopback acceptance failed", "exception": error}
        )

    def _submit(self, raw: socket.socket) -> None:
        with ExitStack() as rollback:
            rollback.callback(raw.close)
            raw.setblocking(False)
            connection = _Connection(raw)
            reader = asyncio.StreamReader()
            protocol = _ClosingStream(
                asyncio.StreamReaderProtocol(reader, connection.connected)
            )
            self._connections.add(connection)
            rollback.pop_all()
        coroutine = self._serve(connection, raw, reader, protocol)
        try:
            connection.task = asyncio.create_task(coroutine)
        except BaseException:
            # Close the coroutine no task will run, whatever stopped submission.
            coroutine.close()
            raise
        try:
            connection.task.add_done_callback(
                lambda task: self._finished(connection, task)
            )
        except BaseException:
            # No done callback will release this record: cancel the task and
            # close the socket now.
            connection.task.cancel()
            connection.close_raw()
            raise

    async def _serve(
        self,
        connection: _Connection,
        raw: socket.socket,
        reader: asyncio.StreamReader,
        protocol: _ClosingStream,
    ) -> None:
        try:
            await asyncio.get_running_loop().connect_accepted_socket(
                lambda: protocol, raw
            )
            if self._socket is not None and connection.writer is not None:
                await self._handler(reader, connection.writer)
        finally:
            connection.closing = True
            if connection.writer is not None:
                writer, primary = connection.writer, sys.exception()
                try:
                    try:
                        writer.close()
                        with suppress(ConnectionError):
                            await writer.wait_closed()
                    finally:
                        # The handler may have cancelled wait_closed's shared
                        # waiter; the transport's loss still proves closure.
                        if writer.is_closing():
                            await protocol.closed.wait()
                except BaseException as error:
                    # Keep any cleanup outcome, cancellation included, for
                    # _finished; the handler's own exception still ends the task.
                    connection.cleanup_error = error
                    if primary is not None:
                        raise primary from error
                    raise

    def _finished(self, connection: _Connection, task: asyncio.Task[None]) -> None:
        if connection not in self._connections:
            return
        connection.close_raw()
        error = None if task.cancelled() else task.exception()
        cleanup = connection.cleanup_error
        if cleanup is not None and not isinstance(cleanup, asyncio.CancelledError):
            asyncio.get_running_loop().call_exception_handler(
                {"message": "loopback cleanup failed", "exception": cleanup}
            )
        if error is not None and error is not cleanup:
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": "loopback connection failed",
                    "exception": error,
                    "task": task,
                }
            )
        self._connections.discard(connection)
