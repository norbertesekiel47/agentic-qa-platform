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

import pytest

from tests.packet_capture import capturing, observe
from tests.packet_flows import Flow

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
    strays = {tuple(each) for each in observation.result["stray"]}
    if sys.platform != "linux":
        assert observation.flows is None
        return
    assert observation.flows is not None
    sent = destinations(observation.flows, "udp")
    # IPv4 and IPv6 alike: the namespace routes every address.
    assert strays <= sent, observation.flows
    # The query went to the resolver's port, wherever the host's resolver is.
    queries = {each for each in sent if each[1] == 53}
    assert queries, observation.flows
    # And nothing else moved: only the datagram, the queries and the ICMP
    # errors that answer them, back to where they came from. The namespace
    # is quiet but for what a scenario sends.
    assert sent == {*strays, *queries}, observation.flows
    senders = {each.source for each in observation.flows if each.protocol == "udp"}
    errors = {
        each.destination
        for each in observation.flows
        if each.protocol in {"icmp", "icmpv6"}
    }
    assert errors <= senders, observation.flows
    assert {each.protocol for each in observation.flows} <= {"udp", "icmp", "icmpv6"}


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


class Script:
    """A capture source that hands out frames from a script, then fails or
    reports drops as told."""

    def __init__(
        self, frames: list[bytes], *, failure: OSError | None = None, drops: int = 0
    ) -> None:
        self.frames = frames
        self.failure = failure
        self.drops = drops

    def wait(self, milliseconds: int) -> None:
        del milliseconds  # a script has nothing to wait for

    def take(self) -> list[bytes]:
        if self.failure is not None:
            raise self.failure
        taken, self.frames = self.frames, []
        return taken

    def dropped(self) -> tuple[int, int]:
        return self.drops, 10


def test_a_capture_gives_every_frame_it_got() -> None:
    with capturing(Script([b"one", b"two"])) as frames:
        pass

    assert frames == [b"one", b"two"]


def test_a_capture_whose_reader_failed_is_no_evidence() -> None:
    with (
        pytest.raises(RuntimeError, match="stopped early"),
        capturing(Script([], failure=OSError("gone"))),
    ):
        pass


def test_a_capture_that_dropped_a_packet_is_no_evidence() -> None:
    with (
        pytest.raises(RuntimeError, match="dropped 1 of 10"),
        capturing(Script([b"one"], drops=1)),
    ):
        pass


def test_the_capture_misses_no_packet_under_load() -> None:
    # Many megabytes over loopback, as a page's traffic through the proxy
    # is, with a marker datagram after each slice: every marker shows up,
    # and the kernel dropped nothing (`observe` would raise).
    observation = observe(SCENARIOS, "markers_under_load")

    assert isinstance(observation.result, dict)
    assert observation.result["received"] == observation.result["sent"]
    if sys.platform != "linux":
        assert observation.flows is None
        return
    assert observation.flows is not None
    host = observation.result["host"]
    expected = {(host, port) for port in observation.result["ports"]}
    assert expected <= destinations(observation.flows, "udp")
