"""Scenarios the packet capture's controls run in its child process
(`tests/packet_capture.py`), which puts the repository's root on the path:
only there do the runner's test modules import as `packages.…`, so pytest's
own process never imports this module."""

import asyncio
import contextlib
import socket

from aqa_runner.browser_session import open_browser_session
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.test_transports import GATHER, WithoutTheSwitches

# Addresses from TEST-NET-3 (RFC 5737), which no network routes: only the
# capture sees what is sent there.
STRAY = ("203.0.113.9", 9)
# And from IPv6's documentation prefix (RFC 3849).
STRAY_V6 = ("2001:db8::9", 9)
STUN = ("203.0.113.7", 3478)
NAME = "canary.example.test"


async def stray_datagram_and_lookup() -> dict[str, object]:
    """A datagram to an IPv4 and an IPv6 address nothing routes, and a name
    looked up the way the system resolver does it. Returns where the
    datagrams went."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stray:
        stray.sendto(b"stray", STRAY)
    with (
        socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as stray,
        # A host without IPv6 routes none of it; the capture's namespace
        # routes every address, where the control requires the packet.
        contextlib.suppress(OSError),
    ):
        stray.sendto(b"stray", STRAY_V6)
    with contextlib.suppress(socket.gaierror):  # nothing answers it, by design
        await asyncio.get_running_loop().getaddrinfo(NAME, 80)
    return {"stray": [list(STRAY), list(STRAY_V6)]}


async def webrtc_without_the_switches() -> dict[str, object]:
    """ICE gathering, with a STUN server, in a session launched without the
    transport switches and the launch-level proxy (`test_transports.py`'s
    control). Returns the STUN server and the candidates gathered."""
    async with (
        async_playwright() as playwright,
        egress_proxy() as egress,
        open_browser_session(
            WithoutTheSwitches(playwright.chromium), egress=egress
        ) as session,
    ):
        candidates: list[str] = await session.page.evaluate(
            GATHER, f"stun:{STUN[0]}:{STUN[1]}"
        )
        return {"stun": list(STUN), "candidates": candidates}


# How much a loaded scenario moves over loopback, and how many marker
# datagrams it interleaves with it, each to a port of its own.
LOAD_BYTES = 20 << 20
MARKERS = range(10000, 10200)


async def markers_under_load() -> dict[str, object]:
    """A loopback transfer of `LOAD_BYTES`, as a browser's traffic through
    the egress proxy is, with a marker datagram sent after each slice of it.
    Returns where the markers went."""
    received = 0
    finished = asyncio.Event()

    async def sink(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal received
        while chunk := await reader.read(65536):
            received += len(chunk)
        writer.close()
        finished.set()

    server = await asyncio.start_server(sink, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    slice_bytes = b"x" * (LOAD_BYTES // len(MARKERS))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as markers:
        for marker in MARKERS:
            writer.write(slice_bytes)
            await writer.drain()
            markers.sendto(b"marker", (STRAY[0], marker))
    writer.close()
    await finished.wait()
    server.close()
    sent = len(slice_bytes) * len(MARKERS)
    return {
        "host": STRAY[0],
        "ports": list(MARKERS),
        "sent": sent,
        "received": received,
    }
