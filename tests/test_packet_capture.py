"""The packet capture the hostile-page suite observes traffic with (ADR-0026
amendment, 2026-10-02; TESTING.md §1): on Linux, a scenario runs in a user
and network namespace of its own where every destination is local, and every
packet in it is captured. These are its controls: traffic that must show up
in the capture does. macOS has no capture without root, so there each
scenario runs as it is, its result is checked, and `flows` is None; only
Linux, as CI runs it, checks packets."""

import sys
from pathlib import Path

from tests.packet_capture import observe

SCENARIOS = Path(__file__).with_name("capture_scenarios.py")

# The scenarios' targets (`capture_scenarios.py`): addresses from TEST-NET-3
# (RFC 5737), which no network routes.
STRAY = ("203.0.113.9", 9)
STUN = ("203.0.113.7", 3478)


def test_the_capture_sees_a_stray_datagram_and_a_dns_query() -> None:
    observation = observe(SCENARIOS, "stray_datagram_and_lookup")

    assert observation.result == "sent"
    if sys.platform != "linux":
        assert observation.flows is None
        return
    assert observation.flows is not None
    assert ("udp", *STRAY) in {
        (each.protocol, each.destination, each.destination_port)
        for each in observation.flows
    }, observation.flows
    # The query went to the resolver's port, wherever the host's resolver is.
    queries = [
        each
        for each in observation.flows
        if each.protocol == "udp" and each.destination_port == 53
    ]
    assert queries, observation.flows
    # And nothing else moved: only the datagram, the queries and the ICMP
    # errors that answer them, back to where they came from. The namespace
    # is quiet but for what a scenario sends.
    sent = [each for each in observation.flows if each.protocol == "udp"]
    assert {(each.destination, each.destination_port) for each in sent} <= {
        STRAY,
        *((each.destination, 53) for each in queries),
    }, observation.flows
    assert {
        each.destination for each in observation.flows if each.protocol == "icmp"
    } <= {each.source for each in sent}, observation.flows


def test_the_capture_sees_a_browser_leak_without_the_switches() -> None:
    observation = observe(SCENARIOS, "webrtc_without_the_switches")

    if sys.platform != "linux":
        # No capture here: what this checks is that the scenario leaks,
        # gathering candidates.
        assert observation.result
        assert observation.flows is None
        return
    assert observation.flows is not None
    assert ("udp", *STUN) in {
        (each.protocol, each.destination, each.destination_port)
        for each in observation.flows
    }, observation.flows
