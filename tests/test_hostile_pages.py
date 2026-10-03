"""Hostile pages: for each way a page might send data out, a real browser
session runs a page that tries it, and nothing reaches a disallowed host or
leaves except through the egress proxy (ADR-0026 amendment on hostile pages,
2026-10-02; TESTING.md §1; SECURITY.md §7).

Each attempt aims at a canary of its own, a TCP or UDP listener on `127.0.0.1`
or `::1` that counts what reaches it. On every OS each test checks that no
canary heard anything, that routing or the egress proxy recorded exactly the
attempts it should, and that every connection the allowed origin accepted is
one the egress proxy opened. On Linux, as CI runs it, the scenario also runs
in the packet capture's network (`tests/packet_capture.py`), and every packet
must belong to a connection to the proxy or to one the proxy opened: no UDP,
no DNS, no other TCP. macOS has no capture, so there `flows` is None."""

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.hostile_pages import PAGES
from tests.packet_capture import observe
from tests.packet_flows import Flow

SCENARIOS = Path(__file__).with_name("hostile_scenarios.py")

type Endpoint = tuple[str, int]


def unattributed(
    flows: list[Flow], proxy: Endpoint, upstreams: set[Endpoint]
) -> list[Flow]:
    """Each flow that is neither to or from the egress proxy nor one of the
    connections it opened upstream: any packet but TCP, and TCP between any
    other ends. Packets name addresses and ports, not processes, so a
    browser's own connection to an allowed origin looks like the proxy's
    until the proxy's sockets are known."""
    known = {proxy, *upstreams}
    return [
        each
        for each in flows
        if each.protocol != "tcp"
        or not {
            (each.source, each.source_port),
            (each.destination, each.destination_port),
        }
        & known
    ]


PROXY = ("127.0.0.1", 41000)
UPSTREAM = ("127.0.0.1", 52000)
ORIGIN = ("127.0.0.1", 8000)


def tcp(source: Endpoint, destination: Endpoint) -> Flow:
    return Flow("tcp", *source, *destination)


def test_only_the_proxys_connections_are_attributed() -> None:
    to_the_proxy = tcp(("127.0.0.1", 50001), PROXY)
    from_the_proxy = tcp(PROXY, ("127.0.0.1", 50001))
    upstream = tcp(UPSTREAM, ORIGIN)
    answer = tcp(ORIGIN, UPSTREAM)
    # The same origin, from a port the proxy didn't open: the browser's own.
    direct = tcp(("127.0.0.1", 50002), ORIGIN)
    # Only TCP is the proxy's, even at its address and port.
    datagram = Flow("udp", "127.0.0.1", 50003, *PROXY)
    error = Flow("icmp", "127.0.0.1", None, "127.0.0.1", None)

    flows = [to_the_proxy, from_the_proxy, upstream, answer, direct, datagram, error]

    assert unattributed(flows, PROXY, {UPSTREAM}) == [direct, datagram, error]


@dataclass(frozen=True)
class Expected:
    """What a hostile page's attempts must come to: those routing aborts and
    records, as (resource type, scheme, attempt); those the egress proxy
    refuses by host; what the page sees of some; and paths the origin must
    never be asked for."""

    routed: frozenset[tuple[str, str, str]] = frozenset()
    refused: frozenset[str] = frozenset()
    outcomes: Mapping[str, object] = field(default_factory=dict)
    never_served: frozenset[str] = frozenset()


FAILED = "failed: TypeError"

EXPECTED = {
    # Routing aborts the request it sees; the proxy refuses the redirect hop
    # routing never sees.
    "fetch": Expected(
        routed=frozenset({("fetch", "http", "direct")}),
        refused=frozenset({"hop"}),
        outcomes={"direct": FAILED, "hop": FAILED},
    ),
    "xhr": Expected(
        routed=frozenset({("xhr", "http", "direct")}),
        refused=frozenset({"hop"}),
        outcomes={"direct": FAILED, "hop": FAILED},
    ),
    # Into a frame each, so the page stays to report; the hop is a 307, which
    # keeps the POST and its body.
    "form_post": Expected(
        routed=frozenset({("document", "http", "direct")}),
        refused=frozenset({"hop"}),
    ),
    # Routing closes the page's socket; a worker's, and a WebSocketStream,
    # which Playwright doesn't wrap, meet only the proxy.
    "websocket": Expected(
        routed=frozenset({("websocket", "ws", "page")}),
        refused=frozenset({"worker", "stream"}),
        outcomes={
            "page": "closed",
            "worker": "error",
            "stream": "failed: WebSocketError",
        },
    ),
    # No candidate and no datagram; TURN over TCP goes to the proxy, which
    # refuses it.
    "webrtc": Expected(refused=frozenset({"turn_tcp"}), outcomes={"candidates": []}),
    # Nothing at all over UDP; the https origin answers through the proxy.
    "quic": Expected(
        outcomes={
            "webtransport": "failed: WebTransportError",
            **{f"https-{each}": "sent" for each in range(1, 5)},
        }
    ),
    "ipv6": Expected(
        routed=frozenset({("fetch", "http", "loopback"), ("fetch", "http", "mapped")}),
        refused=frozenset({"hop", "worker"}),
        outcomes={
            "loopback": FAILED,
            "mapped": FAILED,
            "hop": FAILED,
            "worker": "error",
            "candidates": [],
        },
    ),
    # Routing aborts the prefetch; preconnect opens a socket to the proxy,
    # never a request, and neither it nor dns-prefetch looks a name up.
    "dns_prefetch": Expected(
        routed=frozenset({("other", "http", "prefetch")}),
        outcomes={"prefetch": "failed"},
    ),
    "service_worker": Expected(
        outcomes={
            "plain": "refused: SecurityError",
            "prototype": "refused: SecurityError",
            "registrations": 0,
        },
        never_served=frozenset({"/sw.js"}),
    ),
    # Chromium refuses each scheme itself, before routing or the proxy could
    # see it.
    "non_http_schemes": Expected(
        outcomes=dict.fromkeys(
            ["ftp", "gopher", "custom", "file", "fileOnAHost", "chrome"], FAILED
        )
    ),
}


def test_every_hostile_page_has_its_expectations() -> None:
    assert set(EXPECTED) == set(PAGES)


# How routing records a host no origin writes: without it (ADR-0026, the
# amendment on routing). An IPv4-mapped address is one.
RECORDED_AS = {"[::ffff:7f00:1]": ("", None)}


@dataclass(frozen=True)
class Run:
    """What a hostile scenario reports (`tests/hostile_scenarios.py`)."""

    proxy: Endpoint
    origins: list[Endpoint]
    upstreams: set[Endpoint]
    requests: list[tuple[Endpoint, str]]
    targets: dict[str, Endpoint]
    received: dict[str, int]
    blocked: set[tuple[str, str, str, int | None]]
    refused: set[tuple[str, int, str]]
    outcomes: dict[str, object]

    @classmethod
    def of(cls, report: object) -> Run:
        assert isinstance(report, dict)
        return cls(
            proxy=(report["proxy"][0], report["proxy"][1]),
            origins=[(host, port) for host, port in report["origins"]],
            upstreams={(host, port) for host, port in report["upstreams"]},
            requests=[
                ((host, port), path) for (host, port), path in report["requests"]
            ],
            targets={
                name: (host, port) for name, (host, port) in report["targets"].items()
            },
            received=report["received"],
            blocked={tuple(each) for each in report["blocked"]},
            refused={tuple(each) for each in report["refused"]},
            outcomes=report["outcomes"],
        )

    def stray_peers(self) -> list[tuple[Endpoint, str]]:
        """Each request an allowed origin got on a connection the egress
        proxy didn't open."""
        return [each for each in self.requests if each[0] not in self.upstreams]


def recorded(run: Run, routed: frozenset[tuple[str, str, str]]) -> set[object]:
    expected: set[object] = set()
    for resource_type, scheme, attempt in routed:
        host, port = run.targets[attempt]
        expected.add((resource_type, scheme, *RECORDED_AS.get(host, (host, port))))
    return expected


@pytest.mark.parametrize("method", EXPECTED, ids=list(EXPECTED))
def test_a_hostile_page_leaves_nothing_outside_the_proxy(method: str) -> None:
    expected = EXPECTED[method]

    observation = observe(SCENARIOS, method)

    run = Run.of(observation.result)
    # No canary heard from any attempt.
    assert run.received == dict.fromkeys(run.targets, 0)
    # Routing and the proxy each recorded exactly the attempts that met them.
    assert run.blocked == recorded(run, expected.routed)
    assert run.refused == {(*run.targets[each], "host") for each in expected.refused}
    assert {name: run.outcomes[name] for name in expected.outcomes} == expected.outcomes
    assert not {path for _, path in run.requests} & expected.never_served
    # Every connection the allowed origin accepted is one the proxy opened.
    assert run.stray_peers() == []
    if sys.platform != "linux":
        assert observation.flows is None
        return
    assert observation.flows is not None
    # Every packet is the proxy's: no UDP, no DNS, no other TCP.
    assert unattributed(observation.flows, run.proxy, run.upstreams) == []


def test_a_direct_connection_to_an_allowed_origin_is_caught() -> None:
    # The control for the checks above: with the protection gone (a context
    # with no proxy, in a browser launched without the launch-level proxy),
    # a page loaded straight from an allowed origin fails them, though its
    # packets look like the proxy's own connection to that origin.
    observation = observe(SCENARIOS, "direct_and_proxied")

    run = Run.of(observation.result)
    attributed = {path for peer, path in run.requests if peer in run.upstreams}
    stray = run.stray_peers()
    assert attributed == {"/via-proxy"}
    assert {path for _, path in stray} == {"/direct"}
    if sys.platform != "linux":
        assert observation.flows is None
        return
    assert observation.flows is not None
    caught = unattributed(observation.flows, run.proxy, run.upstreams)
    [origin] = run.origins
    ends = {
        frozenset(
            {(each.source, each.source_port), (each.destination, each.destination_port)}
        )
        for each in caught
    }
    # What it caught is TCP to or from the origin, and among it is each
    # connection the origin saw come from elsewhere than the proxy.
    assert {each.protocol for each in caught} == {"tcp"}
    assert all(origin in each for each in ends), caught
    assert {frozenset({peer, origin}) for peer, _ in stray} <= ends, caught


def test_the_quic_page_sends_quic_without_the_protections() -> None:
    # The control for the QUIC page: launched with the same trust in the
    # fixture's key and without the protections, the page's WebTransport and
    # its HTTP/3 requests send QUIC to the canaries, so the page's quiet run
    # above means something.
    observation = observe(SCENARIOS, "quic_unprotected")

    run = Run.of(observation.result)
    assert run.received["webtransport"] > 0
    assert run.received["http3"] > 0
    if sys.platform != "linux":
        assert observation.flows is None
        return
    assert observation.flows is not None
    sent = {
        (each.destination, each.destination_port)
        for each in observation.flows
        if each.protocol == "udp"
    }
    assert {
        ("127.0.0.1", run.targets[name][1]) for name in ("webtransport", "http3")
    } <= sent
