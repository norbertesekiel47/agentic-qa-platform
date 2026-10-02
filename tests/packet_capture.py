"""Packet-level observation for the hostile-page suite (ADR-0026 amendment,
2026-10-02; TESTING.md §1), with the standard library only and no root.

`observe` runs an async scenario in a child process. On Linux the child
first enters a user and a network namespace of its own (unprivileged user
namespaces, which CI allows before the browser tests; ADR-0026):

1. Mapped to root in its new user namespace, it brings `lo` up and adds
   "AnyIP" local routes for `0.0.0.0/0` and `::/0`, so every destination is
   local and any packet to any address crosses `lo`, DNS servers included.
   It adds a veth interface with an IPv4 address and no IPv6 one, which
   WebRTC gathers on (never on `lo`); a multicast packet, which no local
   route takes, goes out there.
2. It opens an `AF_PACKET` socket on every interface, then enters a nested
   user namespace mapped to its own uid, because Chromium won't sandbox as
   root. The socket stays open across it.
3. It runs the scenario, capturing every packet any interface sends, and
   reports each flow: protocol, source and destination address and port.

Elsewhere (macOS) nothing can capture without root, so the scenario runs in
the child as it is and the report's `flows` is None.

Run as a script, this module is the child: `python packet_capture.py
<scenario file> <async function>`."""

import asyncio
import contextlib
import fcntl
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# How long the capture keeps reading after the scenario ends, for packets
# still in flight.
SETTLE_SECONDS = 0.5

# ioctl requests and the interface flag that brings `lo` up (<linux/sockios.h>,
# <net/if.h>); `struct ifreq` is 40 bytes on Linux.
SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
IFF_UP = 0x1
IFREQ = struct.Struct("16sh22x")

# rtnetlink: a local route that covers every address, and an interface that
# isn't loopback, with an address (<linux/rtnetlink.h>, <linux/if_link.h>,
# <linux/veth.h>, <linux/if_addr.h>).
NLMSGHDR = struct.Struct("=IHHII")
RTATTR = struct.Struct("=HH")
RTMSG = struct.Struct("=BBBBBBBBI")
IFINFOMSG = struct.Struct("=BxHiII")
IFADDRMSG = struct.Struct("=BBBBI")
RTM_NEWLINK, RTM_SETLINK, RTM_NEWADDR, RTM_NEWROUTE = 16, 19, 20, 24
NLMSG_ERROR = 2
NLM_F_REQUEST, NLM_F_ACK, NLM_F_EXCL, NLM_F_CREATE = 0x1, 0x4, 0x200, 0x400
NLA_F_NESTED = 0x8000
RT_TABLE_LOCAL = 255
RTPROT_BOOT = 3
RT_SCOPE_HOST = 254
RTN_LOCAL = 2
RTA_OIF = 4
IFLA_IFNAME, IFLA_LINKINFO, IFLA_AF_SPEC = 3, 18, 26
IFLA_INFO_KIND, IFLA_INFO_DATA, VETH_INFO_PEER = 1, 2, 1
IFLA_INET6_ADDR_GEN_MODE, IN6_ADDR_GEN_MODE_NONE = 8, 1
IFA_ADDRESS, IFA_LOCAL = 1, 2

# The interface WebRTC gathers on besides `lo`, where it never does: a veth
# pair, one end with an address from TEST-NET-1 (RFC 5737).
INTERFACE, PEER = "aqa0", "aqa1"
INTERFACE_ADDRESS = "192.0.2.1"

# AF_PACKET: every protocol, and the copy of a packet an interface sends
# (each packet is seen sent and again received) (<linux/if_packet.h>).
ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4
ETHERTYPES = {0x0800: socket.AF_INET, 0x86DD: socket.AF_INET6}
PROTOCOLS = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmp"}

# Where a frame's fields sit: the Ethernet header (`lo`'s frames have one
# too), then, per address family, the protocol number, the source and
# destination addresses, and the header's length (IPv4's is in its first
# byte, in 32-bit words) (RFC 791 §3.1, RFC 8200 §3).
ETHERNET_HEADER = 14
ETHERTYPE_AT = 12


@dataclass(frozen=True)
class Layout:
    """Where an IP header keeps its protocol number and its addresses."""

    protocol: int
    source: slice
    destination: slice


IPV4 = Layout(9, slice(12, 16), slice(16, 20))
IPV6 = Layout(6, slice(8, 24), slice(24, 40))
IPV6_HEADER = 40


@dataclass(frozen=True)
class Flow:
    """Packets of one protocol from one address and port to another; ports
    are None for ICMP."""

    protocol: str
    source: str
    source_port: int | None
    destination: str
    destination_port: int | None


@dataclass(frozen=True)
class Observation:
    """What a scenario returned, and every flow the capture saw: None where
    there is no capture."""

    result: object
    flows: list[Flow] | None


def observe(scenario: Path, name: str, *, timeout: float = 180) -> Observation:
    """Run the async function `name` from the file `scenario` in a child
    process, captured on Linux. Raises `RuntimeError`, with the child's
    error output, if it fails."""
    child = subprocess.run(
        [sys.executable, __file__, str(scenario), name],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if child.returncode != 0:
        raise RuntimeError(
            f"the scenario's process failed ({child.returncode}):\n{child.stderr}"
        )
    report = json.loads(child.stdout.strip().splitlines()[-1])
    flows = report["flows"]
    return Observation(
        report["result"], None if flows is None else [Flow(**each) for each in flows]
    )


def enter_own_network() -> socket.socket:
    """Enter a user and network namespace of this process's own, make every
    address local, and return a capture socket on `lo`; then drop to this
    process's own uid in a nested user namespace. The process must have no
    other thread yet."""
    if sys.platform != "linux":
        raise RuntimeError(f"packet capture needs Linux namespaces, not {sys.platform}")
    uid, gid = os.getuid(), os.getgid()
    # https://man7.org/linux/man-pages/man7/user_namespaces.7.html
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
    map_ids(inside_uid=0, outside_uid=uid, inside_gid=0, outside_gid=gid)
    bring_up("lo")
    for family in (socket.AF_INET, socket.AF_INET6):
        add_local_route(family)
    add_interface()
    # Unbound, it captures every interface.
    capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    capture.settimeout(0.2)
    # Chromium refuses to sandbox a browser that runs as root.
    os.unshare(os.CLONE_NEWUSER)
    map_ids(inside_uid=uid, outside_uid=0, inside_gid=gid, outside_gid=0)
    return capture


def map_ids(
    *, inside_uid: int, outside_uid: int, inside_gid: int, outside_gid: int
) -> None:
    """Map one uid and one gid of this process's new user namespace to its
    own outside it, the only mapping an unprivileged process may write."""
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"{inside_uid} {outside_uid} 1")
    Path("/proc/self/gid_map").write_text(f"{inside_gid} {outside_gid} 1")


def bring_up(name: str) -> None:
    """`ip link set <name> up`, over the interface ioctls."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        current = fcntl.ioctl(control, SIOCGIFFLAGS, IFREQ.pack(name.encode(), 0))
        flags = IFREQ.unpack(current)[1]
        fcntl.ioctl(control, SIOCSIFFLAGS, IFREQ.pack(name.encode(), flags | IFF_UP))


def attribute(kind: int, payload: bytes) -> bytes:
    """One rtnetlink attribute, padded to 4 bytes."""
    length = RTATTR.size + len(payload)
    return RTATTR.pack(length, kind) + payload + b"\0" * (-length % 4)


def rtnetlink(kind: int, body: bytes) -> None:
    """Send one request, and raise unless the kernel acknowledges it. A
    request to make something new must make it, never change what exists:
    https://man7.org/linux/man-pages/man7/rtnetlink.7.html"""
    if sys.platform != "linux":
        raise RuntimeError(f"rtnetlink is Linux's, not {sys.platform}'s")
    flags = NLM_F_REQUEST | NLM_F_ACK
    if kind != RTM_SETLINK:
        flags |= NLM_F_CREATE | NLM_F_EXCL
    header = NLMSGHDR.pack(NLMSGHDR.size + len(body), kind, flags, 1, 0)
    with socket.socket(
        socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE
    ) as link:
        link.send(header + body)
        reply = link.recv(65536)
    answer = NLMSGHDR.unpack_from(reply)[1]
    (error,) = struct.unpack_from("=i", reply, NLMSGHDR.size)
    if answer != NLMSG_ERROR or error != 0:
        raise OSError(-error, f"rtnetlink refused request {kind}")


def add_local_route(family: int) -> None:
    """`ip route add local 0.0.0.0/0 dev lo table local`, or `::/0`."""
    route = RTMSG.pack(
        family, 0, 0, 0, RT_TABLE_LOCAL, RTPROT_BOOT, RT_SCOPE_HOST, RTN_LOCAL, 0
    )
    oif = attribute(RTA_OIF, struct.pack("=I", socket.if_nametoindex("lo")))
    rtnetlink(RTM_NEWROUTE, route + oif)


def add_interface() -> None:
    """`ip link add aqa0 type veth peer name aqa1`, both up, and
    `ip addr add 192.0.2.1/24 dev aqa0`: an interface WebRTC gathers on."""
    link = IFINFOMSG.pack(socket.AF_UNSPEC, 0, 0, 0, 0)
    peer = link + attribute(IFLA_IFNAME, PEER.encode() + b"\0")
    info = attribute(IFLA_INFO_KIND, b"veth") + attribute(
        IFLA_INFO_DATA | NLA_F_NESTED, attribute(VETH_INFO_PEER | NLA_F_NESTED, peer)
    )
    rtnetlink(
        RTM_NEWLINK,
        link
        + attribute(IFLA_IFNAME, INTERFACE.encode() + b"\0")
        + attribute(IFLA_LINKINFO | NLA_F_NESTED, info),
    )
    # No IPv6 address on either end, so the kernel sends nothing of its own
    # there (no duplicate-address checks or router solicitations), which
    # would read as the scenario's. IPv6 to any address still crosses `lo`.
    no_ipv6_address = attribute(
        IFLA_AF_SPEC | NLA_F_NESTED,
        attribute(
            socket.AF_INET6 | NLA_F_NESTED,
            attribute(IFLA_INET6_ADDR_GEN_MODE, bytes([IN6_ADDR_GEN_MODE_NONE])),
        ),
    )
    for name in (INTERFACE, PEER):
        index = socket.if_nametoindex(name)
        rtnetlink(
            RTM_SETLINK,
            IFINFOMSG.pack(socket.AF_UNSPEC, 0, index, 0, 0) + no_ipv6_address,
        )
        bring_up(name)
    address = socket.inet_aton(INTERFACE_ADDRESS)
    index = socket.if_nametoindex(INTERFACE)
    rtnetlink(
        RTM_NEWADDR,
        IFADDRMSG.pack(socket.AF_INET, 24, 0, 0, index)
        + attribute(IFA_LOCAL, address)
        + attribute(IFA_ADDRESS, address),
    )


@contextlib.contextmanager
def capturing(capture: socket.socket) -> Iterator[list[bytes]]:
    """Read every packet an interface sends into the list it yields, on a
    thread of its own, until the block ends and `SETTLE_SECONDS` more have
    passed."""
    frames: list[bytes] = []
    done = threading.Event()

    def read() -> None:
        while not done.is_set():
            try:
                frame, address = capture.recvfrom(65535)
            except TimeoutError:
                continue
            if address[2] == PACKET_OUTGOING:
                frames.append(frame)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        yield frames
    finally:
        time.sleep(SETTLE_SECONDS)
        done.set()
        reader.join()


def flow_of(frame: bytes) -> Flow | None:
    """The flow an Ethernet frame belongs to, or None for a frame that
    carries neither IPv4 nor IPv6, such as ARP."""
    (ethertype,) = struct.unpack_from("!H", frame, ETHERTYPE_AT)
    family = ETHERTYPES.get(ethertype)
    if family is None:
        return None
    packet = frame[ETHERNET_HEADER:]
    if family == socket.AF_INET:
        fields, header = IPV4, (packet[0] & 0x0F) * 4
    else:
        fields, header = IPV6, IPV6_HEADER
    number = packet[fields.protocol]
    protocol = PROTOCOLS.get(number, str(number))
    ports: tuple[int | None, int | None] = (None, None)
    if protocol in ("tcp", "udp"):
        ports = struct.unpack_from("!HH", packet, header)
    return Flow(
        protocol,
        socket.inet_ntop(family, packet[fields.source]),
        ports[0],
        socket.inet_ntop(family, packet[fields.destination]),
        ports[1],
    )


def flows_of(frames: list[bytes]) -> list[Flow]:
    """Each distinct flow, in the order its first packet was seen."""
    return list(dict.fromkeys(flow for flow in map(flow_of, frames) if flow))


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
    # Before anything starts a thread: unshare refuses a threaded process.
    capture = enter_own_network() if sys.platform == "linux" else None
    sys.path.insert(0, str(ROOT))
    run = load(scenario, name)
    flows = None
    if capture is None:
        result = asyncio.run(run())
    else:
        with capture, capturing(capture) as frames:
            result = asyncio.run(run())
        flows = [asdict(flow) for flow in flows_of(frames)]
    sys.stdout.write(json.dumps({"result": result, "flows": flows}) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
