"""Reading captured frames as flows (`tests/packet_flows.py`), on hand-built
frames: no packet the hostile-page suite judges may be misread, whatever
headers it carries."""

import socket
import struct

import pytest

from tests.packet_flows import Flow, flow_of

ETHERNET_IPV4 = bytes(12) + b"\x08\x00"
ETHERNET_IPV6 = bytes(12) + b"\x86\xdd"
PORTS = struct.pack("!HHHH", 40000, 53, 8, 0)


def ipv4(protocol: int, transport: bytes, *, fragment_offset: int = 0) -> bytes:
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(transport),
        1,
        fragment_offset,
        64,
        protocol,
        0,
        socket.inet_aton("192.0.2.1"),
        socket.inet_aton("203.0.113.9"),
    )
    return ETHERNET_IPV4 + header + transport


def ipv6(next_header: int, rest: bytes) -> bytes:
    header = struct.pack(
        "!IHBB16s16s",
        6 << 28,
        len(rest),
        next_header,
        64,
        socket.inet_pton(socket.AF_INET6, "2001:db8::1"),
        socket.inet_pton(socket.AF_INET6, "2001:db8::9"),
    )
    return ETHERNET_IPV6 + header + rest


def extension(next_header: int) -> bytes:
    """A hop-by-hop or destination-options header of 8 octets."""
    return bytes([next_header, 0]) + bytes(6)


def fragment(next_header: int, offset: int) -> bytes:
    return struct.pack("!BBHI", next_header, 0, offset << 3, 7)


V4 = ("192.0.2.1", "203.0.113.9")
V6 = ("2001:db8::1", "2001:db8::9")

FRAMES = {
    "ipv4-udp": (ipv4(17, PORTS), Flow("udp", V4[0], 40000, V4[1], 53)),
    "ipv4-later-fragment": (
        ipv4(17, PORTS, fragment_offset=185),
        Flow("udp", V4[0], None, V4[1], None),
    ),
    "ipv4-tcp-cut-short": (ipv4(6, b"\x9c"), Flow("tcp", V4[0], None, V4[1], None)),
    "ipv4-unnamed-protocol": (ipv4(132, PORTS), Flow("132", V4[0], None, V4[1], None)),
    "ipv6-udp": (ipv6(17, PORTS), Flow("udp", V6[0], 40000, V6[1], 53)),
    "ipv6-udp-after-extensions": (
        ipv6(0, extension(60) + extension(17) + PORTS),
        Flow("udp", V6[0], 40000, V6[1], 53),
    ),
    "ipv6-first-fragment": (
        ipv6(44, fragment(17, 0) + PORTS),
        Flow("udp", V6[0], 40000, V6[1], 53),
    ),
    "ipv6-later-fragment": (
        ipv6(44, fragment(17, 185) + PORTS),
        Flow("udp", V6[0], None, V6[1], None),
    ),
    "icmpv6": (ipv6(58, bytes(8)), Flow("icmpv6", V6[0], None, V6[1], None)),
    "ipv6-cut-short-in-extensions": (
        ipv6(0, b"\x11"),
        Flow("0", V6[0], None, V6[1], None),
    ),
}


@pytest.mark.parametrize(("frame", "flow"), FRAMES.values(), ids=list(FRAMES))
def test_a_frame_reads_as_its_flow(frame: bytes, flow: Flow) -> None:
    assert flow_of(frame) == flow


NOT_IP = {
    "arp": bytes(12) + b"\x08\x06" + bytes(28),
    "shorter-than-ethernet": bytes(10),
    "ipv4-header-cut-short": ETHERNET_IPV4 + bytes(10),
    "ipv6-header-cut-short": ETHERNET_IPV6 + bytes(30),
}


@pytest.mark.parametrize("frame", NOT_IP.values(), ids=list(NOT_IP))
def test_a_frame_that_names_no_ip_addresses_is_no_flow(frame: bytes) -> None:
    assert flow_of(frame) is None
