"""A replaying cassette lets nothing reach the network (conftest.py's `cassette`),
even while another thread sends through http.client: vcrpy's http.client stub
holds `force_reset()` open around that send, which once put the real httpx2
transport back for the whole process (LAB_NOTES, 2026-10-16)."""

import asyncio
import http.client
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from typing import Any

import httpx2
import pytest

Cassette = Callable[..., AbstractContextManager[Any]]
# Nothing listens on the discard port, so a request that gets past the cassette
# fails here instead of leaving the machine.
DEAD_PROXY = "http://127.0.0.1:9"
# TEST-NET-1 (RFC 5737): reserved for documentation and routed nowhere.
OFF_THIS_MACHINE = ("192.0.2.1", 80)


def send(port: int) -> None:
    """Sends more than a socket's buffers hold to `port` through http.client,
    until the server resets the connection, however the platform reports it."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    with closing(connection), suppress(OSError):
        connection.request("POST", "/", body=b"x" * (64 << 20))
        connection.getresponse()


@contextmanager
def another_thread_sending() -> Iterator[None]:
    """Another thread's http.client send to a localhost server that accepts it
    and never reads, so the send blocks inside vcr's `force_reset()` until the
    block ends."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(30)
        sender = threading.Thread(target=send, args=(server.getsockname()[1],))
        sender.start()
        try:
            held, _ = server.accept()
            with held:
                yield
        finally:
            # Closed with data unread, a connection is reset, which ends the send.
            server.close()
            sender.join()


async def an_unrecorded_request() -> None:
    async with httpx2.AsyncClient(proxy=DEAD_PROXY, trust_env=False) as client:
        await client.post(
            "https://api.anthropic.com/v1/messages", content=b'{"never": "recorded"}'
        )


def test_another_threads_send_leaves_the_replay_closed_to_an_unrecorded_request(
    cassette: Cassette,
) -> None:
    with (
        pytest.raises(AssertionError, match="has no response for a request"),
        cassette("structured_output", replay_only=True),
        another_thread_sending(),
    ):
        asyncio.run(an_unrecorded_request())


def test_a_replay_refuses_a_connection_off_this_machine_before_it_starts(
    cassette: Cassette,
) -> None:
    with socket.socket() as connection:
        connection.setblocking(False)
        with (
            pytest.raises(
                ConnectionRefusedError,
                match=r"^cassettes/structured_output\.yaml is replaying, so the "
                r"connection to 192\.0\.2\.1 port 80 is refused",
            ),
            cassette("structured_output", replay_only=True),
        ):
            connection.connect(OFF_THIS_MACHINE)

        # A connect binds a local port before its first packet: none was bound.
        assert connection.getsockname()[1] == 0
