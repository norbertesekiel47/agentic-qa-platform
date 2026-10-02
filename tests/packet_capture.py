"""Packet-level observation for the hostile-page suite (ADR-0026 amendment,
2026-10-02; TESTING.md §1), with the standard library only and no root.

`observe` runs an async scenario in a child process. On Linux the child
first enters a network of its own (`tests/capture_network.py`): a user,
network and mount namespace where every address is local, so a packet to
any address, a DNS server's included, is sent and seen; with a veth pair,
which WebRTC gathers on (never on `lo`); and with the host's resolver
daemons out of reach. There, still root inside, it opens an `AF_PACKET`
socket on every interface with a receive ring the kernel writes each packet
into, then drops to its own uid in a nested user namespace, because Chromium
won't sandbox as root. It runs the scenario, drains the ring, and fails
unless the kernel dropped nothing, so a quiet capture can't be an incomplete
one. It reports each IP flow any interface sent (`tests/packet_flows.py`).

Elsewhere (macOS) nothing can capture without root, so the scenario runs in
the child as it is and the report's `flows` is None.

Run as a module from the repository's root, this is the child: `python -m
tests.packet_capture <scenario file> <async function>`."""

import asyncio
import contextlib
import importlib.util
import json
import mmap
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from tests import capture_network
from tests.packet_flows import Flow, flows_of

# The repository's root: the child runs from it, so `tests.…` and the
# runner's `packages.…` test modules import there.
ROOT = Path(__file__).resolve().parents[1]

# How long the capture keeps reading after the scenario ends, for packets
# still in flight, and how long one wait for a packet lasts.
SETTLE_SECONDS = 0.5
WAIT_MILLISECONDS = 200

# AF_PACKET with a TPACKET_V2 receive ring (<linux/if_packet.h>,
# https://docs.kernel.org/networking/packet_mmap.html): every protocol; the
# copy of a packet an interface sends (each is seen sent and again
# received); 64 MiB of 512-byte frames, which hold a packet's headers, all a
# flow needs, and which a burst of the scenario's traffic doesn't fill.
ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4
SOL_PACKET = 263
PACKET_RX_RING, PACKET_STATISTICS, PACKET_VERSION = 5, 6, 10
TPACKET_V2 = 1
TP_STATUS_KERNEL, TP_STATUS_USER = 0, 1
TPACKET_REQ = struct.Struct("=IIII")
TPACKET2_HDR = struct.Struct("=IIIHHIIHH4x")
TPACKET_STATS = struct.Struct("=II")
# Where the frame's `struct sockaddr_ll` keeps the packet's type: after the
# header, aligned to 16 bytes, at `sll_pkttype`.
PKTTYPE_AT = 32 + 10
RING_BLOCK, RING_BLOCKS, RING_FRAME = 4 << 20, 16, 512
RING_FRAMES = RING_BLOCK * RING_BLOCKS // RING_FRAME


@dataclass(frozen=True)
class Observation:
    """What a scenario returned, and every flow the capture saw: None where
    there is no capture."""

    result: object
    flows: list[Flow] | None


def observe(scenario: Path, name: str, *, timeout: float = 180) -> Observation:
    """Run the async function `name` from the file `scenario` in a child
    process, captured on Linux. Raises `RuntimeError`, with the child's error
    output, if it fails, and kills it and every process it started if it
    outlives `timeout`."""
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.packet_capture", str(scenario), name],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, errors = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise
    lines = output.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise RuntimeError(
            f"the scenario's process failed ({child.returncode}):\n{errors}"
        )
    report = json.loads(lines[-1])
    flows = report["flows"]
    return Observation(
        report["result"], None if flows is None else [Flow(**each) for each in flows]
    )


class Source(Protocol):
    """What `capturing` reads: frames as they arrive, and how many the
    kernel dropped."""

    def wait(self, milliseconds: int) -> None: ...

    def take(self) -> list[bytes]: ...

    def dropped(self) -> tuple[int, int]: ...


class Capture:
    """An `AF_PACKET` socket on every interface with a receive ring. Open it
    while the process can still do so in its network namespace."""

    socket: socket.socket
    ring: mmap.mmap
    next: int

    def __init__(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError(f"AF_PACKET is Linux's, not {sys.platform}'s")
        self.socket = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
        )
        self.socket.setsockopt(SOL_PACKET, PACKET_VERSION, TPACKET_V2)
        self.socket.setsockopt(
            SOL_PACKET,
            PACKET_RX_RING,
            TPACKET_REQ.pack(RING_BLOCK, RING_BLOCKS, RING_FRAME, RING_FRAMES),
        )
        self.ring = mmap.mmap(self.socket.fileno(), RING_BLOCK * RING_BLOCKS)
        self.next = 0

    def wait(self, milliseconds: int) -> None:
        select.select([self.socket], [], [], milliseconds / 1000)

    def take(self) -> list[bytes]:
        """The frames the kernel has filled since the last call, in order,
        each handed back to it; only the ones an interface sent."""
        frames: list[bytes] = []
        while True:
            at = self.next * RING_FRAME
            status, _, length, start, *_ = TPACKET2_HDR.unpack_from(self.ring, at)
            if not status & TP_STATUS_USER:
                return frames
            if self.ring[at + PKTTYPE_AT] == PACKET_OUTGOING:
                frames.append(self.ring[at + start : at + start + length])
            struct.pack_into("=I", self.ring, at, TP_STATUS_KERNEL)
            self.next = (self.next + 1) % RING_FRAMES

    def dropped(self) -> tuple[int, int]:
        """How many packets the kernel dropped, of how many it saw."""
        seen, dropped = TPACKET_STATS.unpack(
            self.socket.getsockopt(SOL_PACKET, PACKET_STATISTICS, TPACKET_STATS.size)
        )
        return dropped, seen

    def close(self) -> None:
        self.ring.close()
        self.socket.close()


@contextlib.contextmanager
def capturing(source: Source) -> Iterator[list[bytes]]:
    """Read every frame `source` gets into the list it yields, on a thread of
    its own, until the block ends and `SETTLE_SECONDS` more have passed, then
    drain what is left. Raises what the reader raised, and raises if the
    kernel dropped a packet: an incomplete capture is never evidence."""
    frames: list[bytes] = []
    failures: list[OSError] = []
    done = threading.Event()

    def read() -> None:
        try:
            while not done.is_set():
                source.wait(WAIT_MILLISECONDS)
                frames.extend(source.take())
            frames.extend(source.take())
        # The capture's own failure, re-raised below: a reader that stopped
        # early must not pass for a quiet one.
        except OSError as error:
            failures.append(error)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        yield frames
    finally:
        time.sleep(SETTLE_SECONDS)
        done.set()
        reader.join()
    if failures:
        raise RuntimeError("the packet capture stopped early") from failures[0]
    dropped, seen = source.dropped()
    if dropped:
        raise RuntimeError(f"the packet capture dropped {dropped} of {seen} packets")


def load(scenario: Path, name: str) -> Callable[[], Awaitable[object]]:
    """The async function `name` in the file `scenario`."""
    spec = importlib.util.spec_from_file_location("capture_scenario", scenario)
    if spec is None or spec.loader is None:
        raise ImportError(f"no scenario module at {scenario}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function: Callable[[], Awaitable[object]] = getattr(module, name)
    return function


def main(scenario: Path, name: str) -> None:
    """The child: run the scenario, captured on Linux, and print a JSON report
    of what it returned and the flows seen as the last line of its output."""
    capture = None
    if sys.platform == "linux":
        uid, gid = os.getuid(), os.getgid()
        # Before anything starts a thread: unshare refuses a threaded process.
        capture_network.enter()
        capture = Capture()
        capture_network.drop_to_own_uid(uid, gid)
    run = load(scenario, name)
    flows = None
    if capture is None:
        result = asyncio.run(run())
    else:
        try:
            with capturing(capture) as frames:
                result = asyncio.run(run())
        finally:
            capture.close()
        flows = [asdict(flow) for flow in flows_of(frames)]
    sys.stdout.write(json.dumps({"result": result, "flows": flows}) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
