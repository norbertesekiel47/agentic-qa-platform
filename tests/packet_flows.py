"""Reading captured frames as flows, for the packet capture
(`tests/packet_capture.py`): which protocol, from which address and port to
which. Pure parsing, so it runs and is tested on every OS."""

import socket
import struct
from dataclasses import dataclass

# An Ethernet header, which `lo`'s frames have too, and where its type sits.
ETHERNET_HEADER = 14
ETHERTYPE_AT = 12
ETHERTYPES = {0x0800: socket.AF_INET, 0x86DD: socket.AF_INET6}

# Transport protocols by number (IANA's protocol numbers).
PROTOCOLS = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmpv6"}
WITH_PORTS = {"tcp", "udp"}

# IPv6 extension headers that come before a transport header, each with its
# next header first and its length in 8-octet units, less one, second; and
# the fragment header, always 8 octets (RFC 8200 §4).
EXTENSIONS = {0, 43, 60}
FRAGMENT = 44
IPV6_HEADER = 40


@dataclass(frozen=True)
class Flow:
    """Packets of one protocol from one address and port to another. Ports
    are None for a protocol that has none, and for a packet whose transport
    header the frame doesn't hold: a later fragment, or a frame cut short.
    `protocol` is the transport protocol's name, or its number when it has
    none here, so no packet is left out for its protocol."""

    protocol: str
    source: str
    source_port: int | None
    destination: str
    destination_port: int | None


def flow_of(frame: bytes) -> Flow | None:
    """The flow an Ethernet frame belongs to, or None for a frame that
    carries neither IPv4 nor IPv6 (ARP, say) or is too short to name its
    addresses."""
    if len(frame) < ETHERNET_HEADER:
        return None
    (ethertype,) = struct.unpack_from("!H", frame, ETHERTYPE_AT)
    family = ETHERTYPES.get(ethertype)
    packet = frame[ETHERNET_HEADER:]
    if family == socket.AF_INET and len(packet) >= 20:
        return _ipv4_flow(packet)
    if family == socket.AF_INET6 and len(packet) >= IPV6_HEADER:
        return _ipv6_flow(packet)
    return None


def _ipv4_flow(packet: bytes) -> Flow:
    """RFC 791 §3.1: the header's length in its first byte, in 32-bit
    words; a fragment offset other than zero means no transport header."""
    header = (packet[0] & 0x0F) * 4
    (fragment,) = struct.unpack_from("!H", packet, 6)
    later_fragment = fragment & 0x1FFF != 0
    return _flow(
        socket.AF_INET,
        packet[9],
        packet[12:16],
        packet[16:20],
        None if later_fragment else packet[header:],
    )


def _ipv6_flow(packet: bytes) -> Flow:
    """RFC 8200 §§ 3 and 4: past the extension headers to the transport
    header."""
    number, at = packet[6], IPV6_HEADER
    transport: bytes | None = None
    while True:
        if number in EXTENSIONS and len(packet) >= at + 2:
            number, at = packet[at], at + (packet[at + 1] + 1) * 8
        elif number == FRAGMENT and len(packet) >= at + 8:
            (offset,) = struct.unpack_from("!H", packet, at + 2)
            later_fragment = offset & 0xFFF8 != 0
            number, at = packet[at], at + 8
            if later_fragment:
                break
        else:
            transport = packet[at:]
            break
    return _flow(socket.AF_INET6, number, packet[8:24], packet[24:40], transport)


def _flow(
    family: int, number: int, source: bytes, destination: bytes, transport: bytes | None
) -> Flow:
    protocol = PROTOCOLS.get(number, str(number))
    ports: tuple[int | None, int | None] = (None, None)
    if protocol in WITH_PORTS and transport is not None and len(transport) >= 4:
        ports = struct.unpack_from("!HH", transport)
    return Flow(
        protocol,
        socket.inet_ntop(family, source),
        ports[0],
        socket.inet_ntop(family, destination),
        ports[1],
    )


def flows_of(frames: list[bytes]) -> list[Flow]:
    """Each distinct flow, in the order its first packet was seen."""
    return list(dict.fromkeys(flow for flow in map(flow_of, frames) if flow))
