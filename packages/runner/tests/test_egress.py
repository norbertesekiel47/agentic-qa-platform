"""The run's egress: which hosts it lets the browser reach, the IP policy for
where they resolve, and DNS answers pinned for the whole run (ADR-0026 and its
2026-10-01 amendment on the egress proxy; SECURITY.md §7). Test-first
(TESTING.md §2)."""

import asyncio
import socket
import time
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_address
from pathlib import Path

import pytest
from aqa_core.project import load_config, load_spec
from aqa_core.schema import authority
from aqa_runner import egress as egress_module
from aqa_runner.egress import (
    Connection,
    EgressGate,
    EgressPolicy,
    EgressRefusedError,
    EgressUpstreamError,
    IPAddress,
    Requester,
    address_refusal,
    egress_policy,
    system_resolve,
)


def addresses(*written: str) -> list[IPAddress]:
    return [ip_address(text) for text in written]


# Addresses anyone on the internet can reach, written in each form the policy
# reads: an IPv6 form that embeds an IPv4 address is judged as that address.
PUBLIC = addresses(
    "8.8.8.8",
    "2606:4700:4700::1111",
    "::ffff:8.8.8.8",  # IPv4-mapped
    "64:ff9b::808:808",  # NAT64's well-known prefix
    "2002:808:808::1",  # 6to4
    "::ffff:0:808:808",  # IPv4-translated (RFC 2765)
)

# Loopback, private, unique-local and shared addresses: reachable only for the
# start origin and the project's declared private origins.
NON_PUBLIC = addresses(
    "127.0.0.1",
    "::1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "fc00::1",
    "100.64.0.1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::7f00:1",  # IPv4-compatible 127.0.0.1, which Python calls global
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1, which Python calls global
    "2002:7f00:1::1",  # 6to4 of 127.0.0.1
    "::ffff:0:7f00:1",  # IPv4-translated 127.0.0.1, which Python calls global
    "fec0::1",  # site-local, deprecated but routable, which Python calls global
    "ff0e::1",  # global-scope multicast, not unicast
    "224.0.0.1",  # multicast, which Python calls global
)

# Never reachable, whatever the project declares.
ALWAYS_REFUSED = [
    *addresses(
        "169.254.169.254",  # link-local: AWS, GCP and Azure instance metadata
        "169.254.170.2",  # link-local: ECS task metadata
        "fe80::1",
        "fe80::1%1",  # with a scope, as getaddrinfo can answer
        "::ffff:169.254.169.254",
        "::a9fe:a9fe",
        "64:ff9b::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
        "::ffff:0:a9fe:a9fe",  # IPv4-translated
        # NAT64's local-use prefix (RFC 8215), whose IPv4 can't be read back
        "64:ff9b:1::a9fe:a9fe",
        "64:ff9b:1::1",
        "64:ff9b:1:ffff::1",
        "fd00:ec2::254",  # AWS instance metadata over IPv6
        "fd00:ec2::254%1",  # with a scope
        "fd00:ec2::23",  # EKS Pod Identity's credentials over IPv6
        "fd00:ec2:ffff::1",  # anywhere in AWS's fd00:ec2::/32
        "fd20:ce::254",  # GCP's metadata server on IPv6-only VMs
        "fd00:c1::a9fe:a9fe",  # OCI's instance metadata over IPv6
        "100.100.100.200",  # Alibaba Cloud metadata
        "::ffff:100.100.100.200",
        "168.63.129.16",  # Azure WireServer, a public address
        "::ffff:168.63.129.16",
        "::",
        "::ffff:0.0.0.0",
    ),
    IPv4Address(0),  # 0.0.0.0, the unspecified address
]


@pytest.mark.parametrize("address", PUBLIC, ids=str)
@pytest.mark.parametrize("private_allowed", [False, True])
def test_public_addresses_pass(address: IPAddress, *, private_allowed: bool) -> None:
    assert address_refusal(address, private_allowed=private_allowed) is None


@pytest.mark.parametrize("address", NON_PUBLIC, ids=str)
def test_non_public_addresses_need_permission(
    address: IPAddress,
) -> None:
    assert address_refusal(address, private_allowed=False) is not None
    assert address_refusal(address, private_allowed=True) is None


@pytest.mark.parametrize("address", ALWAYS_REFUSED, ids=str)
@pytest.mark.parametrize("private_allowed", [False, True])
def test_link_local_and_metadata_are_refused_everywhere(
    address: IPAddress, *, private_allowed: bool
) -> None:
    assert address_refusal(address, private_allowed=private_allowed) is not None


# Which hosts pass, by host and port (ADR-0026 amendment, 2026-10-01).

POLICY = EgressPolicy(
    allowed_origins=(
        "http://127.0.0.1:4100",
        "https://pay.example.test",
        "http://[::1]:8080",
    ),
    subresource_hosts=("fonts.example.test", "[2001:db8::5]"),
    private_origins=("http://127.0.0.1:4100", "http://staging.example.test:8080"),
)


@pytest.mark.parametrize(
    ("host", "port"),
    [("127.0.0.1", 4100), ("pay.example.test", 443), ("[::1]", 8080)],
)
@pytest.mark.parametrize("requester", ["request", "tunnel"])
def test_allowed_origins_pass_on_their_own_port(
    host: str, port: int, requester: Requester
) -> None:
    # CONNECT names no scheme, so an origin passes by its host and port alone.
    assert POLICY.allows(host, port, requester)


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("127.0.0.1", 4101),
        ("pay.example.test", 80),
        ("[::1]", 8081),
        ("evil.example.test", 443),
        # A declared private origin may resolve to a private address, but only
        # an allowed origin or a subresource host is reachable at all.
        ("staging.example.test", 8080),
    ],
)
@pytest.mark.parametrize("requester", ["request", "tunnel"])
def test_other_hosts_and_ports_are_refused(
    host: str, port: int, requester: Requester
) -> None:
    assert not POLICY.allows(host, port, requester)


@pytest.mark.parametrize("host", ["fonts.example.test", "[2001:db8::5]"])
def test_subresource_hosts_pass_only_on_their_schemes_default_port(host: str) -> None:
    # A plain request is http's, on port 80; a tunnel carries https or wss, on
    # port 443.
    assert POLICY.allows(host, 80, "request")
    assert POLICY.allows(host, 443, "tunnel")
    assert not POLICY.allows(host, 443, "request")
    assert not POLICY.allows(host, 80, "tunnel")
    assert not POLICY.allows(host, 8443, "tunnel")


def test_only_the_start_and_declared_private_origins_may_be_private() -> None:
    assert POLICY.may_be_private("127.0.0.1", 4100)
    assert POLICY.may_be_private("staging.example.test", 8080)
    assert not POLICY.may_be_private("staging.example.test", 80)
    assert not POLICY.may_be_private("pay.example.test", 443)
    assert not POLICY.may_be_private("[::1]", 8080)


def test_egress_policy_from_spec_config_and_start(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "egress:\n"
        "  subresource_hosts: [fonts.example.test]\n"
        "  expected_blocked: [analytics.example.test]\n"
        "  private_origins: ['http://staging.example.test:8080', 'http://127.0.0.1:4100']\n"
    )
    (tmp_path / "pay.spec.md").write_text(
        "---\n"
        "id: pay\n"
        "goal: A shopper pays.\n"
        "preconditions: { start_url: / }\n"
        "expect: [The order is confirmed]\n"
        "allowed_origins: ['https://pay.example.test']\n"
        "---\n"
    )
    config = load_config(tmp_path / "config.yaml")
    spec = load_spec(tmp_path / "pay.spec.md", config)

    assert egress_policy(spec, config, "http://127.0.0.1:4100") == EgressPolicy(
        allowed_origins=("http://127.0.0.1:4100", "https://pay.example.test"),
        subresource_hosts=("fonts.example.test",),
        private_origins=("http://127.0.0.1:4100", "http://staging.example.test:8080"),
    )


# Connections: the allowlist, DNS pins and the IP policy together. The local
# DNS fixture is ScriptedResolver, injected where the system resolver would
# answer; the servers listen on loopback and say which address was reached.


# What a scripted resolver answers for each name: its addresses, or an error.
type Answers = dict[str, Sequence[str] | OSError]


class ScriptedResolver:
    """Answers each name from a script the test can change mid-run, and
    counts the lookups. While `stalled`, a lookup never answers."""

    def __init__(self, answers: Answers) -> None:
        self.answers = answers
        self.lookups: Counter[str] = Counter()
        self.stalled = False

    async def __call__(self, host: str) -> list[IPAddress]:
        self.lookups[host] += 1
        await asyncio.sleep(0)  # a concurrent lookup could start meanwhile
        if self.stalled:
            await asyncio.Event().wait()
        answer = self.answers[host]
        if isinstance(answer, OSError):
            raise answer
        return [ip_address(text) for text in answer]


@dataclass
class Servers:
    """Loopback servers on one port: one on 127.0.0.1 and, when it listens,
    one on [::1]. Each tells a client which of them it reached."""

    port: int
    accepted: list[str]


async def _tell(label: str, accepted: list[str], writer: asyncio.StreamWriter) -> None:
    accepted.append(label)
    writer.write(label.encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


@asynccontextmanager
async def loopback_servers(*, ipv6_listens: bool = True) -> AsyncIterator[Servers]:
    """Servers on 127.0.0.1 and, if `ipv6_listens`, [::1], sharing one port.
    Without the second, a connection to [::1] on that port is refused."""
    accepted: list[str] = []
    servers = [
        await asyncio.start_server(
            lambda _, writer: _tell("127.0.0.1", accepted, writer), "127.0.0.1", 0
        )
    ]
    port = servers[0].sockets[0].getsockname()[1]
    if ipv6_listens:
        servers.append(
            await asyncio.start_server(
                lambda _, writer: _tell("::1", accepted, writer), "::1", port
            )
        )
    try:
        yield Servers(port, accepted)
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


def unused_port() -> int:
    """A port on 127.0.0.1 that nothing listens on, so connecting to it is
    refused at once. (A bound socket that doesn't listen makes macOS time the
    connection out instead.)"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def reached(egress: EgressGate, host: str, port: int) -> str:
    """Which loopback server a connection to `host` and `port` reaches."""
    reader, writer = await egress.connect(host, port, "request")
    try:
        return (await reader.read()).decode()
    finally:
        writer.close()
        await writer.wait_closed()


def spy_on_dialling(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every address the gate dials from now on, none of which answers."""
    dialled: list[str] = []

    async def dial(host: str, port: int) -> Connection:
        dialled.append(f"{host} {port}")
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "open_connection", dial)
    return dialled


def run_policy(
    port: int, *, private: Sequence[str] = (), other: Sequence[str] = ()
) -> EgressPolicy:
    """A policy whose start origin is http://127.0.0.1:`port`, with `private`
    origins declared private and allowed, and `other` origins allowed only."""
    start = f"http://127.0.0.1:{port}"
    return EgressPolicy(
        allowed_origins=(start, *private, *other),
        subresource_hosts=(),
        private_origins=(start, *private),
    )


APP = "app.example.test"


def app_egress(port: int, answers: Answers) -> tuple[EgressGate, ScriptedResolver]:
    """A run whose start origin is http://127.0.0.1:`port` and which declares
    http://app.example.test:`port` private, with names answered from
    `answers`."""
    resolver = ScriptedResolver(answers)
    policy = run_policy(port, private=[f"http://{APP}:{port}"])
    return EgressGate(policy, resolve=resolver), resolver


def test_a_host_outside_the_allowlist_is_refused_before_any_lookup() -> None:
    async def scenario() -> tuple[EgressGate, ScriptedResolver, EgressRefusedError]:
        resolver = ScriptedResolver({})
        egress = EgressGate(run_policy(4100), resolve=resolver)
        with pytest.raises(EgressRefusedError) as refused:
            await egress.connect("evil.example.test", 443, "tunnel")
        return egress, resolver, refused.value

    egress, resolver, refused = asyncio.run(scenario())

    assert egress.refusals == [refused.refusal]
    assert (refused.refusal.host, refused.refusal.port) == ("evil.example.test", 443)
    assert refused.refusal.kind == "host"
    assert "evil.example.test:443" in str(refused)
    assert resolver.lookups == Counter()
    assert egress.infrastructure_events == []


def test_the_requester_decides_a_subresource_hosts_port() -> None:
    # A subresource host passes on 443 for a tunnel only. Its answer is then
    # judged by the IP policy, which refuses it: proof the allowlist passed.
    async def scenario() -> list[str]:
        egress = EgressGate(
            EgressPolicy(
                allowed_origins=("http://127.0.0.1:4100",),
                subresource_hosts=("cdn.example.test",),
                private_origins=("http://127.0.0.1:4100",),
            ),
            resolve=ScriptedResolver({"cdn.example.test": ["169.254.169.254"]}),
        )
        kinds: list[str] = []
        requesters: tuple[Requester, ...] = ("request", "tunnel")
        for requester in requesters:
            with pytest.raises(EgressRefusedError) as refused:
                await egress.connect("cdn.example.test", 443, requester)
            kinds.append(refused.value.refusal.kind)
        return kinds

    assert asyncio.run(scenario()) == ["host", "address"]


def test_start_origin_on_loopback_and_a_declared_private_origin_are_allowed() -> None:
    async def scenario() -> tuple[list[str], EgressGate, ScriptedResolver]:
        async with loopback_servers() as servers:
            private = f"http://staging.example.test:{servers.port}"
            resolver = ScriptedResolver({"staging.example.test": ["127.0.0.1"]})
            egress = EgressGate(
                run_policy(servers.port, private=[private]), resolve=resolver
            )
            return (
                [
                    await reached(egress, "127.0.0.1", servers.port),
                    await reached(egress, "staging.example.test", servers.port),
                ],
                egress,
                resolver,
            )

    reached_addresses, egress, resolver = asyncio.run(scenario())

    assert reached_addresses == ["127.0.0.1", "127.0.0.1"]
    # The start origin's host is an address: nothing is looked up for it.
    assert resolver.lookups == Counter({"staging.example.test": 1})
    assert egress.refusals == []


@pytest.mark.parametrize(
    ("origin", "answers"),
    [
        ("http://app.example.test:{port}", {"app.example.test": ["127.0.0.1"]}),
        (
            "http://intranet.example.test:{port}",
            {"intranet.example.test": ["10.1.2.3"]},
        ),
        ("http://[::1]:{port}", {}),
    ],
)
def test_a_private_or_loopback_address_behind_an_allowed_origin_is_refused(
    origin: str, answers: Answers, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialled = spy_on_dialling(monkeypatch)
    allowed = origin.format(port=4200)
    host, port = authority(allowed)

    async def scenario() -> tuple[EgressGate, EgressRefusedError]:
        egress = EgressGate(
            run_policy(4100, other=[allowed]), resolve=ScriptedResolver(answers)
        )
        with pytest.raises(EgressRefusedError) as refused:
            await egress.connect(host, port, "request")
        return egress, refused.value

    egress, refused = asyncio.run(scenario())

    assert refused.refusal.kind == "address"
    assert egress.refusals == [refused.refusal]
    assert dialled == []


@pytest.mark.parametrize(
    "answer",
    [["169.254.169.254"], ["::ffff:169.254.169.254"], ["127.0.0.1", "169.254.169.254"]],
    ids=str,
)
def test_a_private_origin_resolving_to_metadata_is_refused_without_connecting(
    answer: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dialled = spy_on_dialling(monkeypatch)

    async def scenario() -> tuple[EgressGate, EgressRefusedError]:
        private = "http://staging.example.test:8080"
        egress = EgressGate(
            run_policy(4100, private=[private]),
            resolve=ScriptedResolver({"staging.example.test": answer}),
        )
        with pytest.raises(EgressRefusedError) as refused:
            await egress.connect("staging.example.test", 8080, "request")
        return egress, refused.value

    egress, refused = asyncio.run(scenario())

    # The whole answer is judged before any address is dialled, even one the
    # policy passes.
    assert dialled == []
    assert refused.refusal.kind == "address"
    assert "169.254.169.254" in refused.refusal.detail
    assert egress.refusals == [refused.refusal]
    assert egress.infrastructure_events == []


def test_a_rebound_name_still_connects_to_its_pinned_address() -> None:
    async def scenario() -> tuple[list[str], ScriptedResolver]:
        async with loopback_servers() as servers:
            egress, resolver = app_egress(servers.port, {APP: ["127.0.0.1"]})
            first = await reached(egress, APP, servers.port)
            # The name rebinds to another address the policy would also pass.
            resolver.answers[APP] = ["::1"]
            second = await reached(egress, APP, servers.port)
            return [first, second], resolver

    reached_addresses, resolver = asyncio.run(scenario())

    assert reached_addresses == ["127.0.0.1", "127.0.0.1"]
    assert resolver.lookups == Counter({APP: 1})


def test_a_refused_answer_is_not_pinned() -> None:
    async def scenario() -> tuple[str, EgressGate, ScriptedResolver]:
        async with loopback_servers() as servers:
            egress, resolver = app_egress(servers.port, {APP: ["169.254.169.254"]})
            with pytest.raises(EgressRefusedError):
                await egress.connect(APP, servers.port, "request")
            resolver.answers[APP] = ["127.0.0.1"]
            return await reached(egress, APP, servers.port), egress, resolver

    address, egress, resolver = asyncio.run(scenario())

    assert address == "127.0.0.1"
    assert resolver.lookups == Counter({APP: 2})
    assert len(egress.refusals) == 1


def test_a_pinned_answer_is_judged_again_for_each_port() -> None:
    async def scenario() -> tuple[str, EgressRefusedError, ScriptedResolver]:
        async with loopback_servers() as servers:
            resolver = ScriptedResolver({"app.example.test": ["127.0.0.1"]})
            egress = EgressGate(
                run_policy(
                    servers.port,
                    private=[f"http://app.example.test:{servers.port}"],
                    other=["http://app.example.test:9"],
                ),
                resolve=resolver,
            )
            address = await reached(egress, "app.example.test", servers.port)
            # The same name on a port that isn't a declared private origin's.
            with pytest.raises(EgressRefusedError) as refused:
                await egress.connect("app.example.test", 9, "request")
            return address, refused.value, resolver

    address, refused, resolver = asyncio.run(scenario())

    assert address == "127.0.0.1"
    assert refused.refusal.kind == "address"
    assert resolver.lookups == Counter({"app.example.test": 1})


def test_concurrent_first_requests_resolve_a_name_once() -> None:
    async def scenario() -> tuple[list[str], ScriptedResolver]:
        async with loopback_servers() as servers:
            egress, resolver = app_egress(servers.port, {APP: ["127.0.0.1"]})
            first, second = await asyncio.gather(
                reached(egress, APP, servers.port), reached(egress, APP, servers.port)
            )
            return [first, second], resolver

    reached_addresses, resolver = asyncio.run(scenario())

    assert reached_addresses == ["127.0.0.1", "127.0.0.1"]
    assert resolver.lookups == Counter({APP: 1})


def test_addresses_are_tried_in_the_answers_order() -> None:
    async def scenario() -> list[str]:
        found = []
        for ipv6_listens in (True, False):
            async with loopback_servers(ipv6_listens=ipv6_listens) as servers:
                egress, _ = app_egress(servers.port, {APP: ["::1", "127.0.0.1"]})
                found.append(await reached(egress, APP, servers.port))
        return found

    # The first address that accepts: [::1], or 127.0.0.1 once [::1] refuses.
    assert asyncio.run(scenario()) == ["::1", "127.0.0.1"]


def test_an_address_that_never_answers_times_out_and_the_next_is_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_connection = asyncio.open_connection

    async def dropping_ipv6(host: str, port: int) -> Connection:
        if host == "::1":
            await asyncio.Event().wait()  # an address that drops every packet
        return await open_connection(host, port)

    monkeypatch.setattr(asyncio, "open_connection", dropping_ipv6)
    monkeypatch.setattr(egress_module, "CONNECT_TIMEOUT", 0.05)

    async def scenario() -> str:
        async with loopback_servers() as servers:
            egress, _ = app_egress(servers.port, {APP: ["::1", "127.0.0.1"]})
            # Bounded: without its own timeout, the egress would wait forever.
            return await asyncio.wait_for(reached(egress, APP, servers.port), 5)

    assert asyncio.run(scenario()) == "127.0.0.1"


def test_a_lookup_that_never_answers_is_an_infrastructure_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(egress_module, "RESOLVE_TIMEOUT", 0.2)

    async def scenario() -> tuple[list[object], float, EgressGate, str]:
        async with loopback_servers() as servers:
            egress, resolver = app_egress(servers.port, {APP: ["127.0.0.1"]})
            resolver.stalled = True
            started = time.monotonic()
            # Bounded: without its own deadline, the gate would wait forever.
            failures = await asyncio.wait_for(
                asyncio.gather(
                    *(egress.connect(APP, servers.port, "request") for _ in range(5)),
                    return_exceptions=True,
                ),
                5,
            )
            waited = time.monotonic() - started
            resolver.stalled = False
            address = await reached(egress, APP, servers.port)
            return list(failures), waited, egress, address

    failures, waited, egress, address = asyncio.run(scenario())

    assert [type(failure) for failure in failures] == [EgressUpstreamError] * 5
    assert [event.host for event in egress.infrastructure_events] == [APP] * 5
    # One deadline for each connection, queued behind the name's lookup or
    # not: five waiting in turn would take five times as long.
    assert waited < 0.5
    assert egress.refusals == []
    # Nothing was pinned while DNS stalled: once it answers, the name connects.
    assert address == "127.0.0.1"


@pytest.mark.parametrize(
    "answer",
    [socket.gaierror(socket.EAI_NONAME, "no such name"), []],
    ids=["no such name", "no address"],
)
def test_an_unresolvable_name_is_an_infrastructure_event(
    answer: Sequence[str] | OSError,
) -> None:
    async def scenario() -> tuple[EgressGate, EgressUpstreamError]:
        private = "http://app.example.test:8080"
        egress = EgressGate(
            run_policy(4100, private=[private]),
            resolve=ScriptedResolver({"app.example.test": answer}),
        )
        with pytest.raises(EgressUpstreamError) as failed:
            await egress.connect("app.example.test", 8080, "request")
        return egress, failed.value

    egress, failed = asyncio.run(scenario())

    assert egress.infrastructure_events == [failed.event]
    assert (failed.event.host, failed.event.port) == ("app.example.test", 8080)
    assert "app.example.test" in failed.event.cause
    assert "app.example.test:8080" in str(failed)
    assert egress.refusals == []


def test_an_unreachable_address_is_an_infrastructure_event() -> None:
    port = unused_port()

    async def scenario() -> tuple[EgressGate, EgressUpstreamError]:
        egress = EgressGate(run_policy(port), resolve=ScriptedResolver({}))
        with pytest.raises(EgressUpstreamError) as failed:
            await egress.connect("127.0.0.1", port, "request")
        return egress, failed.value

    egress, failed = asyncio.run(scenario())

    assert egress.infrastructure_events == [failed.event]
    assert (failed.event.host, failed.event.port) == ("127.0.0.1", port)
    assert "127.0.0.1" in failed.event.cause


def test_the_system_resolver_answers_by_default() -> None:
    async def scenario() -> tuple[list[IPAddress], str]:
        async with loopback_servers() as servers:
            start = f"http://localhost:{servers.port}"
            egress = EgressGate(
                EgressPolicy(
                    allowed_origins=(start,),
                    subresource_hosts=(),
                    private_origins=(start,),
                )
            )
            return (
                await system_resolve("localhost"),
                await reached(egress, "localhost", servers.port),
            )

    answer, address = asyncio.run(scenario())

    assert answer
    assert all(each.is_loopback for each in answer)
    assert address in {"127.0.0.1", "::1"}
