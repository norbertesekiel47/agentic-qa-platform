"""The scenario `tests/test_peer_connections.py` runs in the packet capture's
child process (`tests/packet_capture.py`), which puts the repository's root
on the path: only there do the runner's test modules import as `packages.…`,
so pytest's own process never imports this module."""

import subprocess
import sys
from pathlib import Path

from aqa_runner.browser_session import open_browser_session
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.test_browser_session import chromium_processes

# mDNS's port and IPv4 group (RFC 6762), as /proc/net writes them: the port
# in hex, the group as a little-endian hex word.
MDNS_PORT = 5353
MDNS_GROUP = "FB0000E0"

# A peer connection with a data channel and an offer, which gathers with no
# ICE server, then negotiates with a second one, which answers, and is handed
# remote candidates whose `.local` names the page chose: a browser that looked
# such a name up itself would send a query naming it. Both stay open while the
# browser's sockets are read. Returns each candidate gathered and how
# gathering ended.
PEER_CONNECTION = """async () => {
    const peer = new RTCPeerConnection();
    peer.createDataChannel("probe");
    const candidates = [];
    peer.onicecandidate = (event) => {
        if (event.candidate) candidates.push(event.candidate.candidate);
    };
    await peer.setLocalDescription(await peer.createOffer());
    await new Promise((done) => {
        setTimeout(done, 5000);
        peer.onicegatheringstatechange = () => {
            if (peer.iceGatheringState === "complete") done();
        };
    });
    const answerer = new RTCPeerConnection();
    await answerer.setRemoteDescription(peer.localDescription);
    await answerer.setLocalDescription(await answerer.createAnswer());
    await peer.setRemoteDescription(answerer.localDescription);
    for (const candidate of [
        "candidate:1 1 udp 2122260223 exfil-fake-secret.local 54321 typ host",
        "candidate:2 1 tcp 1518280447 exfil-fake-secret.local 9 typ host tcptype active",
    ]) {
        await peer.addIceCandidate({candidate, sdpMid: "0", sdpMLineIndex: 0});
    }
    await new Promise((done) => setTimeout(done, 2000));
    window.openPeerConnections = [peer, answerer];
    return {candidates, gathering: peer.iceGatheringState};
}"""


def mdns_sockets(pids: list[int]) -> list[str]:
    """Each UDP socket on mDNS's port, as the OS lists it: on Linux, every
    one in the capture's network, where every socket is the scenario's own
    (`/proc/net`'s local address); on macOS, each that one of the processes
    `pids` holds (`lsof`'s line, which needs no root for one's own
    processes)."""
    if sys.platform == "linux":
        return [
            line.split()[1]
            for name in ("udp", "udp6")
            for line in Path("/proc/net", name).read_text().splitlines()[1:]
            if int(line.split()[1].rsplit(":", 1)[1], 16) == MDNS_PORT
        ]
    listed = subprocess.run(
        ["lsof", "-nP", "-a", f"-iUDP:{MDNS_PORT}", "-p", ",".join(map(str, pids))],
        capture_output=True,
        text=True,
        check=False,  # lsof exits 1 when it finds nothing
    )
    if listed.returncode not in {0, 1}:
        raise RuntimeError(f"lsof failed ({listed.returncode}): {listed.stderr}")
    return listed.stdout.splitlines()[1:]


def interfaces_in_the_mdns_group() -> list[str]:
    """Each interface on which a process in the capture's network joined
    mDNS's IPv4 group (Linux's `/proc/net/igmp`: an interface's line, then
    one indented line per group)."""
    joined: list[str] = []
    interface = ""
    for line in Path("/proc/net/igmp").read_text().splitlines()[1:]:
        if not line.startswith("\t"):
            interface = line.split()[1]
        elif line.split()[0] == MDNS_GROUP:
            joined.append(interface)
    return joined


async def peer_connection() -> dict[str, object]:
    """A session's page makes a peer connection. Returns what it gathered,
    the mDNS sockets the session's Chromium holds and, on Linux, the
    interfaces where the mDNS group was joined."""
    async with (
        async_playwright() as playwright,
        egress_proxy() as egress,
        open_browser_session(playwright.chromium, egress=egress) as session,
    ):
        gathered = await session.page.evaluate(PEER_CONNECTION)
        pids = [pid for _, pid in await chromium_processes(session)]
        return {
            "gathered": gathered,
            "sockets": mdns_sockets(pids),
            "joins": interfaces_in_the_mdns_group()
            if sys.platform == "linux"
            else None,
        }
