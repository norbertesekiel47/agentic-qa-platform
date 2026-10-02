"""The packet capture the hostile-page suite observes traffic with (ADR-0026
amendment, 2026-10-02; TESTING.md §1): on Linux, a scenario runs in a user
and network namespace of its own where every address is local, and every IP
packet any interface sends is captured. These are its controls: traffic
that must show up in the capture does, and nothing else does.

macOS has no capture without root, so there each scenario runs as it is and
`flows` is None: the first control then checks only that its scenario runs,
and the second that its session gathers WebRTC candidates, the leak the
capture must see. Only Linux, as CI runs it, checks packets."""

import sys
from pathlib import Path

from tests.packet_capture import Flow, observe

SCENARIOS = Path(__file__).with_name("capture_scenarios.py")


def destinations(flows: list[Flow], protocol: str) -> set[tuple[str, int | None]]:
    return {
        (each.destination, each.destination_port)
        for each in flows
        if each.protocol == protocol
    }


def test_the_capture_sees_a_stray_datagram_and_a_dns_query() -> None:
    observation = observe(SCENARIOS, "stray_datagram_and_lookup")

    assert isinstance(observation.result, dict)
    stray = tuple(observation.result["stray"])
    if sys.platform != "linux":
        assert observation.flows is None
        return
    assert observation.flows is not None
    sent = destinations(observation.flows, "udp")
    assert stray in sent, observation.flows
    # The query went to the resolver's port, wherever the host's resolver is.
    queries = {each for each in sent if each[1] == 53}
    assert queries, observation.flows
    # And nothing else moved: only the datagram, the queries and the ICMP
    # errors that answer them, back to where they came from. The namespace
    # is quiet but for what a scenario sends.
    assert sent == {stray, *queries}, observation.flows
    senders = {each.source for each in observation.flows if each.protocol == "udp"}
    assert {
        each.destination for each in observation.flows if each.protocol == "icmp"
    } <= senders, observation.flows
    assert {each.protocol for each in observation.flows} <= {"udp", "icmp"}


def test_the_capture_sees_a_browser_leak_without_the_switches() -> None:
    observation = observe(SCENARIOS, "webrtc_without_the_switches")

    assert isinstance(observation.result, dict)
    if sys.platform != "linux":
        # No capture here: what this checks is that the scenario leaks,
        # gathering candidates.
        assert observation.result["candidates"]
        assert observation.flows is None
        return
    assert observation.flows is not None
    assert tuple(observation.result["stun"]) in destinations(
        observation.flows, "udp"
    ), observation.flows
