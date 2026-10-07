"""The egress proxy and the raw upstream on the loopback owner: closing
either right after it accepts a connection closes the accepted socket before
closing returns, and the proxy's refusals and failures are no errors for the
event loop (#141; ADR-0026's #141 amendments). Test-first (TESTING.md §2)."""

import asyncio
import socket
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import urlsplit

import pytest
from aqa_runner.egress_proxy import EgressProxy

from packages.runner.tests.egress_fixtures import (
    Origin,
    exchange,
    gate,
    raw_upstream,
    serving,
    unused_port,
)


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
            await stack.aclose()
            client.close()
            for connection, _ in accepted:
                connection.close()

    with serving() as origin:
        outcome, errors = asyncio.run(scenario(origin), debug=True)

    assert outcome in {"eof", "reset"}
    assert errors == []


@pytest.mark.parametrize(
    ("request_text", "status_line", "recorded"),
    [
        ("GET http://evil.example.test/ HTTP/1.1\r\nHost: x\r\n\r\n", b"", (1, 0)),
        (
            "CONNECT evil.example.test:443 HTTP/1.1\r\nHost: evil.example.test\r\n\r\n",
            b"HTTP/1.1 403 ",
            (1, 0),
        ),
        ("GET /relative HTTP/1.1\r\nHost: x\r\n\r\n", b"", (0, 0)),
        ("GET {start}/ HTTP/1.1\r\nHost: x\r\n\r\n", b"", (0, 1)),
    ],
    ids=["refused", "refused-tunnel", "unreadable", "upstream-failed"],
)
def test_a_refusal_or_failure_is_no_error_for_the_loop(
    request_text: str, status_line: bytes, recorded: tuple[int, int]
) -> None:
    # The listener reports a handler's unexpected exception to the loop; the
    # proxy's expected outcomes stay the gate's records.
    start = f"http://127.0.0.1:{unused_port()}"
    egress = gate(allowed=(start,))

    async def scenario() -> tuple[bytes, list[dict[str, Any]]]:
        errors: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: errors.append(context)
        )
        async with EgressProxy(egress) as proxy:
            answer = await exchange(proxy, request_text.format(start=start).encode())
        return answer, errors

    answer, errors = asyncio.run(scenario())

    assert answer.partition(b"\r\n")[0] == status_line
    assert (len(egress.refusals), len(egress.infrastructure_events)) == recorded
    assert errors == []


def test_a_closed_proxy_names_no_url() -> None:
    async def scenario() -> EgressProxy:
        async with EgressProxy(gate(allowed=("http://127.0.0.1:9",))) as proxy:
            pass
        return proxy

    proxy = asyncio.run(scenario())
    with pytest.raises(RuntimeError, match="async with"):
        _ = proxy.url
