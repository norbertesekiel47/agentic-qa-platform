"""Closing the egress proxy, or a raw upstream, right after it accepts a
connection closes the accepted socket before closing returns (#141;
ADR-0026's #141 amendments). Test-first (TESTING.md §2)."""

import asyncio
import socket
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import urlsplit

import pytest
from aqa_runner.egress_proxy import EgressProxy

from packages.runner.tests.egress_fixtures import Origin, gate, raw_upstream, serving


async def listen(
    listener: str, origin: Origin, stack: AsyncExitStack
) -> tuple[int, Callable[[], object]]:
    """Open `listener` on `stack`: its port, and what reached past it, which
    is empty unless its handler ran."""
    if listener == "proxy":
        start = f"http://127.0.0.1:{origin.port}"
        proxy = await stack.enter_async_context(EgressProxy(gate(allowed=(start,))))
        port = urlsplit(proxy.url).port
        assert port is not None
        return port, lambda: origin.seen
    upstream = await stack.enter_async_context(
        raw_upstream(b"HTTP/1.1 204 No Content\r\n\r\n")
    )
    return upstream.port, lambda: upstream.received


async def peer_outcome(client: socket.socket) -> str:
    try:
        async with asyncio.timeout(2):
            data = await asyncio.get_running_loop().sock_recv(client, 1)
    except ConnectionResetError:
        return "reset"
    return "eof" if data == b"" else "data"


@pytest.mark.parametrize("listener", ["proxy", "raw_upstream"])
def test_closing_right_after_an_accept_closes_the_accepted_socket(
    monkeypatch: pytest.MonkeyPatch, listener: str
) -> None:
    accepted: list[tuple[socket.socket, Any]] = []
    accept = socket.socket.accept

    async def scenario(origin: Origin) -> tuple[str, list[dict[str, Any]]]:
        errors: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: errors.append(context)
        )
        ready = asyncio.Event()
        stack = AsyncExitStack()
        port, reached = await listen(listener, origin, stack)

        def observed(listening: socket.socket) -> tuple[socket.socket, Any]:
            connection, address = accept(listening)
            if listening.getsockname()[1] == port:
                accepted.append((connection, address))
                ready.set()
            return connection, address

        monkeypatch.setattr(socket.socket, "accept", observed)
        # Sent before the listener accepts: a handler that ran would read it,
        # and the proxy would forward it to the origin.
        client = socket.create_connection(("127.0.0.1", port))
        try:
            client.sendall(
                f"POST http://127.0.0.1:{origin.port}/charge HTTP/1.1\r\n"
                "Host: x\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode()
            )
            client.setblocking(False)
            async with asyncio.timeout(3):
                await ready.wait()
            # The accepting callback set `ready` before it scheduled the
            # connection's setup, so this close comes between the two.
            await stack.aclose()
            [(connection, address)] = accepted
            assert address == client.getsockname()
            assert connection.fileno() == -1, "the accepted socket is still open"
            outcome = await peer_outcome(client)
            assert not reached()
            return outcome, errors
        finally:
            client.close()
            for connection, _ in accepted:
                connection.close()

    with serving() as origin:
        outcome, errors = asyncio.run(scenario(origin), debug=True)

    assert outcome in {"eof", "reset"}
    assert errors == []
