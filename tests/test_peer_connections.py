"""A page's peer connection opens no way out: Chromium's WebRTC mDNS
responder, which a page starts by making an `RTCPeerConnection`, is off at
launch (ADR-0026 amendment on WebRTC's mDNS responder, 2026-10-02). With it
on, Chromium binds UDP 5353 and joins the mDNS group on every interface, even
when WebRTC sends no UDP of its own, and the join is a packet that bypasses
the egress proxy.

The scenario runs in the packet capture's child (`tests/packet_capture.py`).
On every OS no ICE candidate is gathered and the session's Chromium holds no
socket on mDNS's port. On Linux, as CI runs it, nothing joined the mDNS group
and the capture holds no packet but TCP. macOS has no capture, and its own
mDNSResponder is in the mDNS group on every interface, so there only the
candidates and Chromium's sockets are checked."""

import sys
from pathlib import Path

from tests.packet_capture import observe

SCENARIOS = Path(__file__).with_name("peer_connection_scenarios.py")


def test_a_peer_connection_opens_no_multicast_socket() -> None:
    observation = observe(SCENARIOS, "peer_connection")

    result = observation.result
    assert isinstance(result, dict)
    assert result["candidates"] == []
    if sys.platform != "linux":
        assert result["sockets"] == []
        assert observation.flows is None
        return
    assert observation.flows is not None
    # The socket, the group's join and the packet that announces it.
    not_tcp = [each for each in observation.flows if each.protocol != "tcp"]
    assert (result["sockets"], result["joins"], not_tcp) == ([], [], [])
