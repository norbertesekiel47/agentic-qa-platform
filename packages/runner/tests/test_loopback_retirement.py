"""A retired listener tells its owner (#162; ADR-0026's #53 P8 amendment):
every error that retires the loopback owner's listener reaches the owner's
callback once, after the listener has stopped, and before the loop's
exception handler. A transient one retires nothing. Accepts are driven as
`test_loopback_server.py` drives them."""

import asyncio
import errno
import socket
from typing import Any

import pytest
from aqa_runner.loopback_server import LoopbackServer

from packages.runner.tests.test_loopback_server import (
    install_allocation_failure,
    observe,
    turns,
)


async def unreached(
    _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    writer.write(b"served")


def readiness(
    monkeypatch: pytest.MonkeyPatch, loop: asyncio.AbstractEventLoop
) -> list[Any]:
    """The listener's readiness callbacks, as the loop registers them."""
    callbacks: list[Any] = []
    register = loop.add_reader

    def registration(fd: int, callback: Any) -> None:
        callbacks.append(callback)
        register(fd, callback)

    monkeypatch.setattr(loop, "add_reader", registration)
    return callbacks


@pytest.mark.parametrize(
    "code", [errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM, errno.EIO]
)
def test_a_persistent_accept_failure_tells_the_owner_once_and_still_reaches_the_loop(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    failure = OSError(code, errno.errorcode[code])

    def failed(_listener: socket.socket) -> Any:
        raise failure

    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            callbacks = readiness(monkeypatch, asyncio.get_running_loop())
            retired: list[BaseException] = []
            async with LoopbackServer(unreached, on_retired=retired.append) as server:
                monkeypatch.setattr(socket.socket, "accept", failed)
                callbacks[0]()
                assert retired == [failure]
                assert [item["exception"] for item in seen.errors] == [failure]
                with pytest.raises(RuntimeError, match="not listening"):
                    _ = server.port
                for _ in range(3):
                    callbacks[0]()
                assert retired == [failure]
                assert len(seen.errors) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", [BlockingIOError(), InterruptedError(), ConnectionAbortedError()]
)
def test_a_transient_accept_failure_tells_the_owner_nothing(
    monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    def failed(_listener: socket.socket) -> Any:
        raise failure

    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            callbacks = readiness(monkeypatch, asyncio.get_running_loop())
            accept = socket.socket.accept
            retired: list[BaseException] = []
            async with LoopbackServer(unreached, on_retired=retired.append) as server:
                monkeypatch.setattr(socket.socket, "accept", failed)
                callbacks[0]()
                monkeypatch.setattr(socket.socket, "accept", accept)
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                try:
                    assert await reader.read() == b"served"
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert retired == []
            assert seen.errors == []

    asyncio.run(scenario())


def test_an_unexpected_accept_error_tells_the_owner_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = ValueError("accept failed")

    def failed(_listener: socket.socket) -> Any:
        raise failure

    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            loop = asyncio.get_running_loop()
            callbacks = readiness(monkeypatch, loop)
            retired: list[BaseException] = []
            async with LoopbackServer(unreached, on_retired=retired.append) as server:
                monkeypatch.setattr(socket.socket, "accept", failed)
                loop.call_soon(callbacks[0])
                await seen.reported.wait()
                assert retired == [failure]
                with pytest.raises(RuntimeError, match="not listening"):
                    _ = server.port
                for _ in range(3):
                    loop.call_soon(callbacks[0])
                await turns(3)
                assert retired == [failure]
                assert [item["exception"] for item in seen.errors] == [failure]

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["setblocking", "unexpected"])
def test_a_failure_setting_up_an_accepted_socket_tells_the_owner_once(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    async def scenario() -> None:
        async with observe(monkeypatch) as seen:
            retired: list[BaseException] = []
            async with LoopbackServer(unreached, on_retired=retired.append) as server:
                error, _, _ = install_allocation_failure(monkeypatch, stage, seen)
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                try:
                    await seen.reported.wait()
                    assert retired == [error]
                    assert seen.sockets[0].fileno() == -1
                    assert await reader.read() == b""
                    with pytest.raises(RuntimeError, match="not listening"):
                        _ = server.port
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert retired == [error]

    asyncio.run(scenario())
