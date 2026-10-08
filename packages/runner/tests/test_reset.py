"""The reset hook's request: a POST to the start origin through the run's
egress gate, passed only by a whole 2xx within its bound (ADR-0024 and its
#53 amendment; DATA_MODEL §6). Every origin here listens on loopback."""

import asyncio
import contextlib
import itertools
import time
import tracemalloc
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from aqa_core.spec import Reset
from aqa_runner.egress import EgressGate, InfrastructureEvent
from aqa_runner.loopback_server import LoopbackServer
from aqa_runner.reset import ResetFailedError, reset

from packages.runner.tests.egress_fixtures import gate, raw_upstream, unused_port

HOOK = Reset(http="POST /test-api/reset?fixture=seed")

# A body chunk in chunked framing: 64 KiB of zeros.
CHUNK = b"10000\r\n" + bytes(65536) + b"\r\n"


@dataclass
class Origin:
    port: int = 0
    closed: asyncio.Event = field(default_factory=asyncio.Event)


@asynccontextmanager
async def streaming(head: bytes, chunks: Iterable[bytes] = ()) -> AsyncIterator[Origin]:
    """A loopback origin that answers a request with `head`, then each of
    `chunks`, then waits for the client to go; `closed` is set once it has."""
    origin = Origin()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # The client's leaving ends the stream: then writes fail.
            with contextlib.suppress(ConnectionError):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(head)
                for chunk in chunks:
                    writer.write(chunk)
                    await writer.drain()
                await reader.read()
        finally:
            origin.closed.set()

    async with LoopbackServer(handle) as server:
        origin.port = server.port
        yield origin


async def outcome(
    egress: EgressGate, start: str, seconds: float = 5.0
) -> ResetFailedError | None:
    """None when the reset passed, otherwise its refusal."""
    try:
        await reset(egress, HOOK, start, seconds=seconds)
    except ResetFailedError as failed:
        return failed
    return None


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (200, None),
        (204, None),
        (299, None),
        (300, "the reset hook answered 300"),
        (404, "the reset hook answered 404"),
        (500, "the reset hook answered 500"),
    ],
)
def test_only_a_2xx_reset_passes(status: int, message: str | None) -> None:
    reply = f"HTTP/1.1 {status} Status\r\nContent-Length: 0\r\n\r\n".encode()

    async def scenario() -> ResetFailedError | None:
        async with raw_upstream(reply) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            return await outcome(gate(allowed=(start,)), start)

    failed = asyncio.run(scenario())

    assert (None if failed is None else str(failed)) == message


def test_a_redirect_is_a_failed_reset() -> None:
    reply = b"HTTP/1.1 302 Found\r\nLocation: /seeded\r\nContent-Length: 0\r\n\r\n"

    async def scenario() -> tuple[ResetFailedError | None, bytes]:
        async with raw_upstream(reply) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            failed = await outcome(gate(allowed=(start,)), start)
            return failed, bytes(upstream.received)

    failed, received = asyncio.run(scenario())

    assert str(failed) == "the reset hook answered 302"
    # Never followed: the origin saw the hook's request only.
    assert received.count(b" HTTP/1.1\r\n") == 1
    assert received.startswith(b"POST /test-api/reset?fixture=seed HTTP/1.1\r\n")


def test_a_reset_past_its_bound_fails_and_closes_its_connection() -> None:
    async def scenario() -> tuple[ResetFailedError | None, float, EgressGate]:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            egress = gate(allowed=(start,))
            began = time.monotonic()
            failed = await outcome(egress, start, 0.5)
            took = time.monotonic() - began
            await asyncio.wait_for(silent.closed.wait(), 5)
            return failed, took, egress

    failed, took, egress = asyncio.run(scenario())

    assert str(failed) == "the reset hook didn't answer within 0.5 s"
    assert took < 2
    assert egress.infrastructure_events == []


def test_a_reset_goes_through_the_gate() -> None:
    reply = b"HTTP/1.1 204 No Content\r\n\r\n"

    async def scenario() -> tuple[ResetFailedError | None, EgressGate, bytes]:
        async with raw_upstream(reply) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            # A gate whose run allows another origin only.
            egress = gate(allowed=(f"http://127.0.0.1:{unused_port()}",))
            failed = await outcome(egress, start)
            return failed, egress, bytes(upstream.received)

    failed, egress, received = asyncio.run(scenario())

    assert str(failed) == "the reset hook couldn't be reached"
    assert [refusal.kind for refusal in egress.refusals] == ["host"]
    assert received == b""


def test_a_reset_posts_its_path_and_query_on_the_start_origin() -> None:
    reply = b"HTTP/1.1 204 No Content\r\n\r\n"

    async def scenario() -> tuple[ResetFailedError | None, bytes]:
        async with raw_upstream(reply) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            failed = await outcome(gate(allowed=(start,)), start)
            return failed, bytes(upstream.received)

    failed, received = asyncio.run(scenario())

    assert failed is None
    assert received.startswith(b"POST /test-api/reset?fixture=seed HTTP/1.1\r\n")


def test_an_unreachable_reset_hook_fails_with_an_infrastructure_event() -> None:
    port = unused_port()
    start = f"http://127.0.0.1:{port}"
    egress = gate(allowed=(start,))

    failed = asyncio.run(outcome(egress, start))

    assert str(failed) == "the reset hook couldn't be reached"
    assert [(event.host, event.port) for event in egress.infrastructure_events] == [
        ("127.0.0.1", port)
    ]


def test_an_informational_reply_before_a_2xx_passes() -> None:
    reply = b"HTTP/1.1 199 Hint\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"

    async def scenario() -> ResetFailedError | None:
        async with raw_upstream(reply) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            return await outcome(gate(allowed=(start,)), start)

    assert asyncio.run(scenario()) is None


def test_an_informational_reply_the_origin_closes_after_fails_upstream() -> None:
    async def scenario() -> tuple[ResetFailedError | None, EgressGate, int]:
        async with raw_upstream(b"HTTP/1.1 199 Hint\r\n\r\n") as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            egress = gate(allowed=(start,))
            return await outcome(egress, start), egress, upstream.port

    failed, egress, port = asyncio.run(scenario())

    assert str(failed) == "the reset hook couldn't be reached"
    assert egress.infrastructure_events == [
        InfrastructureEvent(
            "127.0.0.1", port, "the exchange broke off: h11.RemoteProtocolError"
        )
    ]


def test_an_informational_reply_with_no_final_response_fails_at_the_bound() -> None:
    async def scenario() -> tuple[ResetFailedError | None, EgressGate]:
        async with streaming(b"HTTP/1.1 199 Hint\r\n\r\n") as origin:
            start = f"http://127.0.0.1:{origin.port}"
            egress = gate(allowed=(start,))
            failed = await outcome(egress, start, 0.5)
            await asyncio.wait_for(origin.closed.wait(), 5)
            return failed, egress

    failed, egress = asyncio.run(scenario())

    assert str(failed) == "the reset hook didn't answer within 0.5 s"
    assert egress.infrastructure_events == []


def test_a_large_whole_2xx_reset_passes_holding_none_of_its_body() -> None:
    size = 32 * 2**20
    head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % size

    async def scenario() -> tuple[ResetFailedError | None, int]:
        async with streaming(
            head, itertools.repeat(bytes(65536), size // 65536)
        ) as origin:
            start = f"http://127.0.0.1:{origin.port}"
            tracemalloc.start()
            try:
                failed = await outcome(gate(allowed=(start,)), start)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            return failed, peak

    failed, peak = asyncio.run(scenario())

    assert failed is None
    assert peak < 4 * 2**20


def test_an_endless_reset_body_fails_at_its_bound_holding_none_of_it() -> None:
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"

    async def scenario() -> tuple[ResetFailedError | None, int]:
        async with streaming(head, itertools.repeat(CHUNK)) as origin:
            start = f"http://127.0.0.1:{origin.port}"
            tracemalloc.start()
            try:
                failed = await outcome(gate(allowed=(start,)), start, 0.5)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            # The request's connection closed with it: the origin saw it go.
            await asyncio.wait_for(origin.closed.wait(), 5)
            return failed, peak

    failed, peak = asyncio.run(scenario())

    assert str(failed) == "the reset hook didn't answer within 0.5 s"
    assert peak < 4 * 2**20
