"""The network the packet capture's child runs a scenario in
(`tests/packet_capture.py`), Linux only, without root: a user, network and
mount namespace of its own, where every address is local, with a veth pair
for WebRTC, and with the host's resolver daemons out of reach, so that every
lookup through the name services it allows is a packet in the namespace.
Standard library only: rtnetlink and the interface ioctls over sockets,
`mount` through libc."""

import ctypes
import fcntl
import os
import socket
import struct
import sys
from pathlib import Path

# ioctl requests and the interface flag that brings a link up
# (<linux/sockios.h>, <net/if.h>); `struct ifreq` is 40 bytes on Linux.
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

# Where the host's resolver daemons listen on a Unix socket. A lookup through
# one (nss-resolve, nscd, nss-mdns, or D-Bus) would leave from the host's own
# network, past the capture; with these hidden, glibc falls back to DNS from
# inside the namespace.
RESOLVER_SOCKETS = (
    "/run/systemd/resolve",
    "/run/nscd",
    "/run/avahi-daemon",
    "/run/dbus",
)
MS_REC, MS_PRIVATE = 0x4000, 1 << 18

# The host-lookup sources whose daemons the hiding covers, or that need none
# (nsswitch.conf(5)). Any other (sss, ldap, wins, ...) could resolve through
# a daemon left in reach, so the capture refuses to start with it.
KNOWN_HOST_SOURCES = {
    "files",
    "dns",
    "resolve",
    "myhostname",
    "mymachines",
    "mdns",
    "mdns4",
    "mdns6",
    "mdns_minimal",
    "mdns4_minimal",
    "mdns6_minimal",
}
NSSWITCH = Path("/etc/nsswitch.conf")


def enter() -> None:
    """Enter a user, network and mount namespace of this process's own,
    mapped to root inside it, and set the namespace up. The process must have
    no other thread yet. Drop to the process's own uid afterwards with
    `drop_to_own_uid`."""
    if sys.platform != "linux":
        raise RuntimeError(f"packet capture needs Linux namespaces, not {sys.platform}")
    uid, gid = os.getuid(), os.getgid()
    # https://man7.org/linux/man-pages/man7/user_namespaces.7.html
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET | os.CLONE_NEWNS)
    map_ids(inside_uid=0, outside_uid=uid, inside_gid=0, outside_gid=gid)
    hide_host_resolvers()
    bring_up("lo")
    for family in (socket.AF_INET, socket.AF_INET6):
        add_local_route(family)
    add_interface()


def drop_to_own_uid(uid: int, gid: int) -> None:
    """Enter a nested user namespace mapped to `uid` and `gid`, the process's
    own outside: Chromium refuses to sandbox a browser that runs as root.
    What the process opened as root stays open."""
    if sys.platform != "linux":
        raise RuntimeError(f"user namespaces are Linux's, not {sys.platform}'s")
    os.unshare(os.CLONE_NEWUSER)
    map_ids(inside_uid=uid, outside_uid=0, inside_gid=gid, outside_gid=0)


def map_ids(
    *, inside_uid: int, outside_uid: int, inside_gid: int, outside_gid: int
) -> None:
    """Map one uid and one gid of this process's new user namespace to its
    own outside it, the only mapping an unprivileged process may write."""
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"{inside_uid} {outside_uid} 1")
    Path("/proc/self/gid_map").write_text(f"{inside_gid} {outside_gid} 1")


def mount(source: str | None, target: str, kind: str | None, flags: int) -> None:
    """mount(2), through libc: https://man7.org/linux/man-pages/man2/mount.2.html"""
    libc = ctypes.CDLL(None, use_errno=True)
    encoded = [
        None if each is None else each.encode() for each in (source, target, kind)
    ]
    if libc.mount(*encoded, ctypes.c_ulong(flags), None) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"mount {target}: {os.strerror(error)}")


def host_sources(nsswitch: str) -> list[str]:
    """The sources the `hosts:` line of an nsswitch.conf names, in order,
    without its `[STATUS=action]` items."""
    for line in nsswitch.splitlines():
        key, _, sources = line.partition("#")[0].partition(":")
        if key.strip() == "hosts":
            return [each for each in sources.split() if not each.startswith("[")]
    return []


def hide_host_resolvers() -> None:
    """Put an empty tmpfs over each resolver daemon's socket directory, in
    this mount namespace only, which first stops sharing mounts with the
    host's. Refuses a host whose `hosts:` sources it doesn't cover. A daemon
    directory made later, on the host's `/run`, would show here."""
    if NSSWITCH.exists():
        unknown = set(host_sources(NSSWITCH.read_text())) - KNOWN_HOST_SOURCES
        if unknown:
            raise RuntimeError(
                f"the packet capture can't keep lookups through {sorted(unknown)} "
                "inside its network; give the host's nsswitch.conf `hosts: files dns`"
            )
    mount(None, "/", None, MS_REC | MS_PRIVATE)
    for directory in RESOLVER_SOCKETS:
        if Path(directory).is_dir():
            mount("tmpfs", directory, "tmpfs", 0)


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
    """`ip link add aqa0 type veth peer name aqa1`, both up with no IPv6
    address, and `ip addr add 192.0.2.1/24 dev aqa0`: an interface WebRTC
    gathers on."""
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
