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
STUN = ("203.0.113.7", 3478)
NAME = "canary.example.test"


async def stray_datagram_and_lookup() -> dict[str, object]:
    """A datagram to an address nothing routes, and a name looked up the way
    the system resolver does it. Returns where the datagram went."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stray:
        stray.sendto(b"stray", STRAY)
    with contextlib.suppress(socket.gaierror):  # nothing answers it, by design
        await asyncio.get_running_loop().getaddrinfo(NAME, 80)
    return {"stray": list(STRAY)}


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
