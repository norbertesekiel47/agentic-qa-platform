"""Own loopback accepts before asynchronous stream construction (ADR-0026)."""

import asyncio
import socket
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

    def connected(
        self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writer = writer
        self.raw = None

    def close_raw(self) -> None:
        if self.raw is not None:
            self.raw.close()
            self.raw = None


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
        await asyncio.gather(*tasks, return_exceptions=True)
        for connection in tuple(self._connections):
            if connection.task is not None:
                self._finished(connection, connection.task)

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
            self._stop()
            raise
        try:
            self._submit(raw)
        except (OSError, RuntimeError, ValueError, MemoryError) as error:
            self._fail(error)
        except Exception:
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
            protocol = asyncio.StreamReaderProtocol(reader, connection.connected)
            self._connections.add(connection)
            rollback.pop_all()
        coroutine = self._serve(connection, raw, reader, protocol)
        try:
            connection.task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        try:
            connection.task.add_done_callback(
                lambda task: self._finished(connection, task)
            )
        except BaseException:
            connection.task.cancel()
            connection.close_raw()
            raise

    async def _serve(
        self,
        connection: _Connection,
        raw: socket.socket,
        reader: asyncio.StreamReader,
        protocol: asyncio.StreamReaderProtocol,
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
                results = await asyncio.gather(
                    self._close_writer(connection.writer), return_exceptions=True
                )
                if isinstance(results[0], BaseException):
                    asyncio.get_running_loop().call_exception_handler(
                        {"message": "loopback cleanup failed", "exception": results[0]}
                    )

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()

    def _finished(self, connection: _Connection, task: asyncio.Task[None]) -> None:
        if connection not in self._connections:
            return
        connection.close_raw()
        self._connections.discard(connection)
        if not task.cancelled() and (error := task.exception()) is not None:
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": "loopback connection failed",
                    "exception": error,
                    "task": task,
                }
            )
