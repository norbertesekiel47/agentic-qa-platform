"""The run's egress gate: which hosts the run may reach, where they may
resolve, and each name's DNS answer pinned for the run (ADR-0026 and its
2026-10-01 amendment on the egress proxy; SECURITY.md §7)."""

import asyncio
import socket
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Literal

from aqa_core.config import ProjectConfig
from aqa_core.project import allowed_origins
from aqa_core.schema import DEFAULT_PORTS, authority
from aqa_core.spec import Spec

type IPAddress = IPv4Address | IPv6Address

# How the egress gate looks a name up: the system resolver in a run, and a
# scripted one, the local DNS fixture, in tests.
type Resolve = Callable[[str], Awaitable[Sequence[IPAddress]]]

type Connection = tuple[asyncio.StreamReader, asyncio.StreamWriter]

# Who asks for a connection: the egress proxy, for one of the browser's plain
# requests (http's) or for a tunnel (CONNECT, which carries https or wss).
type Requester = Literal["request", "tunnel"]

# The one port a subresource host passes on, by requester: its scheme's
# default.
SUBRESOURCE_PORTS: dict[Requester, int] = {
    "request": DEFAULT_PORTS["http"],
    "tunnel": DEFAULT_PORTS["https"],
}

# Why a connection was refused: its host and port aren't on the allowlist, or
# the IP policy refused an address the host resolves to.
type RefusalKind = Literal["host", "address"]

# Seconds a connection waits for its host's answer, whether it looks the name
# up or waits for another connection's lookup of it, and for one address to
# accept before the next in the answer is tried. A stalled resolver or an
# address that drops packets then ends in an infrastructure event instead of
# the OS's own timeout (over a minute), or none.
RESOLVE_TIMEOUT = 10
CONNECT_TIMEOUT = 10

# Networks refused whatever the project declares, beyond link-local addresses
# (which cover 169.254.169.254 and ECS's 169.254.170.2): cloud metadata and
# credential services, and NAT64's local-use prefix, whose embedded IPv4
# address can't be read back. Networks, so an address with an IPv6 scope
# still matches.
ALWAYS_REFUSED = (
    ip_network("fd00:ec2::/32"),  # AWS: instance metadata, EKS Pod Identity
    ip_network("fd20:ce::254/128"),  # GCP, on IPv6-only VMs
    ip_network("fd00:c1::a9fe:a9fe/128"),  # OCI
    ip_network("100.100.100.200/32"),  # Alibaba Cloud
    ip_network("168.63.129.16/32"),  # Azure's WireServer, a public address
    ip_network("64:ff9b:1::/48"),  # local-use NAT64 (RFC 8215)
)

# IPv6 forms whose last 32 bits are an IPv4 address that the address reaches:
# NAT64's well-known prefix (RFC 6052), IPv4-translated addresses (RFC 2765)
# and the deprecated IPv4-compatible form (RFC 4291). IPv4-mapped and 6to4
# forms have properties of their own.
EMBEDDING_IPV4 = (
    IPv6Network("64:ff9b::/96"),
    IPv6Network("::ffff:0:0:0/96"),
    IPv6Network("::/96"),
)

# The only IPv6 space IANA allocates for public unicast. Python's `is_global`
# also calls site-local fec0::/10 and multicast global.
GLOBAL_UNICAST = IPv6Network("2000::/3")


def _unwrapped(address: IPAddress) -> IPAddress:
    """The address the IP policy judges: the IPv4 address that an IPv6 form
    wraps, or `address` itself. Python calls `::7f00:1`, `::ffff:0:7f00:1`
    and `64:ff9b::a9fe:a9fe` global, though they reach 127.0.0.1 and
    169.254.169.254. `::` and `::1` unwrap to 0.0.0.0 and 0.0.0.1, which the
    policy judges as it judges them: unspecified, and not public."""
    if isinstance(address, IPv4Address):
        return address
    # https://docs.python.org/3.14/library/ipaddress.html#ipaddress.IPv6Address.ipv4_mapped
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    # https://docs.python.org/3.14/library/ipaddress.html#ipaddress.IPv6Address.sixtofour
    if address.sixtofour is not None:
        return address.sixtofour
    if any(address in network for network in EMBEDDING_IPV4):
        return IPv4Address(int(address) & 0xFFFF_FFFF)
    return address


def _is_public(address: IPAddress) -> bool:
    """Whether `address` is public unicast: global by IANA's special-purpose
    registries, not multicast, which Python also calls global, and for IPv6
    inside the space allocated for public unicast."""
    # https://docs.python.org/3.14/library/ipaddress.html#ipaddress.IPv4Address.is_global
    if isinstance(address, IPv6Address) and address not in GLOBAL_UNICAST:
        return False
    return address.is_global and not address.is_multicast


def address_refusal(address: IPAddress, *, private_allowed: bool) -> str | None:
    """Why the IP policy refuses `address`, or None when it passes.
    Link-local, unspecified and `ALWAYS_REFUSED` addresses are always refused.
    Any other address that isn't public passes only when `private_allowed`:
    for the start origin and the project's declared private origins, in local
    and CI runs."""
    unwrapped = _unwrapped(address)
    if (
        unwrapped.is_link_local
        or unwrapped.is_unspecified
        or any(unwrapped in network for network in ALWAYS_REFUSED)
    ):
        return f"{address} is a link-local, unspecified or cloud metadata address"
    if not _is_public(unwrapped) and not private_allowed:
        return (
            f"{address} is not a public address: only the start origin and the "
            "project's declared private origins may resolve to one"
        )
    return None


@dataclass(frozen=True)
class EgressPolicy:
    """Which hosts a run's egress passes, and which of them may resolve to a
    private address, in a local or CI run (ADR-0026). Hosted runs reach public
    addresses only, from M6 (#29).

    Origins pass by host and port alone: a tunnel's CONNECT names no scheme.
    Which document may use which origin is the session's check (#44)."""

    # The start origin, then the spec's.
    allowed_origins: tuple[str, ...]
    # Bare hosts from the project config.
    subresource_hosts: tuple[str, ...]
    # The start origin, then the project config's.
    private_origins: tuple[str, ...]

    def allows(self, host: str, port: int, requester: Requester) -> bool:
        """Whether `requester` may connect to `host` and `port`: an allowed
        origin's host and port, or a subresource host on its scheme's default
        port, 80 for a plain request and 443 for a tunnel."""
        if (host, port) in map(authority, self.allowed_origins):
            return True
        return host in self.subresource_hosts and port == SUBRESOURCE_PORTS[requester]

    def may_be_private(self, host: str, port: int) -> bool:
        """Whether `host` and `port` may resolve to a loopback or private
        address: the private origins' (`egress_policy` puts the run's start
        origin first among them)."""
        return (host, port) in map(authority, self.private_origins)


def egress_policy(spec: Spec, config: ProjectConfig, start: str) -> EgressPolicy:
    """The policy for a run of `spec` from the start origin `start`."""
    return EgressPolicy(
        allowed_origins=allowed_origins(spec, start),
        subresource_hosts=config.egress.subresource_hosts,
        private_origins=tuple(dict.fromkeys((start, *config.egress.private_origins))),
    )


@dataclass(frozen=True)
class Refusal:
    """A connection the egress gate refused, recorded for the run. `host`
    means the host isn't an allowed origin or a subresource host on that port,
    an egress block unless the project expects it (#47). `address` means the
    IP policy refused where the host resolves."""

    host: str
    port: int
    kind: RefusalKind
    detail: str


@dataclass(frozen=True)
class InfrastructureEvent:
    """A connection the egress gate couldn't make: the name didn't resolve,
    or no address accepted. Never the app's response, and never a finding."""

    host: str
    port: int
    cause: str


class EgressRefusedError(Exception):
    """The egress gate refused a connection (ADR-0026)."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(f"{refusal.host}:{refusal.port}: {refusal.detail}")
        self.refusal = refusal


class EgressUpstreamError(Exception):
    """The egress gate couldn't reach an allowed host: an infrastructure
    error (API.md §7, 10+), never a finding (ADR-0026)."""

    def __init__(self, event: InfrastructureEvent) -> None:
        super().__init__(f"{event.host}:{event.port}: {event.cause}")
        self.event = event


async def system_resolve(host: str) -> list[IPAddress]:
    """`host`'s addresses from the system resolver, in the order it gives
    them."""
    # https://docs.python.org/3.14/library/asyncio-eventloop.html#asyncio.loop.getaddrinfo
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return [ip_address(str(info[4][0])) for info in infos]


def _ip_literal(host: str) -> IPAddress | None:
    """`host` as an address, when it is one rather than a name."""
    try:
        return ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return None


class EgressGate:
    """A run's egress gate: its policy, its DNS pins and what it refused or
    couldn't reach (ADR-0026). Every connection the run makes to the outside
    goes through `connect`, and every allowlist decision is the policy's.

    One gate serves a whole run, every browser session and runner-side request
    in it, so each name's pin holds for the run."""

    def __init__(
        self, policy: EgressPolicy, *, resolve: Resolve = system_resolve
    ) -> None:
        self.policy = policy
        self._resolve = resolve
        # Each name's first answer that passed the IP policy. A pinned name is
        # never looked up again, so it can't rebind mid-run.
        self._pins: dict[str, tuple[IPAddress, ...]] = {}
        # One lookup per name at a time, so two first requests pin one answer.
        self._lookups: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.refusals: list[Refusal] = []
        self.infrastructure_events: list[InfrastructureEvent] = []

    async def connect(self, host: str, port: int, requester: Requester) -> Connection:
        """A connection to `host` and `port`, written as an origin writes them
        (lowercase, an IPv6 address in brackets), for `requester`. Refused,
        before any lookup, unless the policy allows it; then refused unless
        every address of the host's pinned answer passes the IP policy for
        that host and port. Raises `EgressRefusedError` or
        `EgressUpstreamError`, and records either."""
        if not self.policy.allows(host, port, requester):
            raise self._refuse(
                host, port, "host", "not an allowed origin or a subresource host"
            )
        return await self._open(host, port, await self._addresses(host, port))

    async def _addresses(self, host: str, port: int) -> tuple[IPAddress, ...]:
        literal = _ip_literal(host)
        if literal is not None:
            return self._checked(host, port, (literal,))
        try:
            # One deadline for the wait behind another lookup and for this
            # one: https://docs.python.org/3.14/library/asyncio-task.html#asyncio.timeout
            async with asyncio.timeout(RESOLVE_TIMEOUT), self._lookups[host]:
                answer = self._pins.get(host) or await self._lookup(host, port)
                # Checked on every connection, pinned or not: whether a private
                # address may pass depends on the port as well as the host.
                self._pins[host] = self._checked(host, port, answer)
        except TimeoutError as error:
            raise self.record_failure(
                host, port, f"{host} doesn't resolve within {RESOLVE_TIMEOUT} s"
            ) from error
        return answer

    async def _lookup(self, host: str, port: int) -> tuple[IPAddress, ...]:
        try:
            answer = tuple(await self._resolve(host))
        except OSError as error:  # socket.gaierror: the name doesn't resolve
            raise self.record_failure(
                host, port, f"{host} doesn't resolve: {error}"
            ) from error
        if not answer:
            raise self.record_failure(host, port, f"{host} resolves to no address")
        return answer

    def _checked(
        self, host: str, port: int, answer: tuple[IPAddress, ...]
    ) -> tuple[IPAddress, ...]:
        """`answer`, once every address in it passes the IP policy. One that
        doesn't refuses the connection, and the answer isn't pinned."""
        private_allowed = self.policy.may_be_private(host, port)
        for address in answer:
            problem = address_refusal(address, private_allowed=private_allowed)
            if problem is not None:
                raise self._refuse(host, port, "address", problem)
        return answer

    async def _open(
        self, host: str, port: int, answer: tuple[IPAddress, ...]
    ) -> Connection:
        """A connection to the first address in `answer` that accepts one
        within `CONNECT_TIMEOUT`. Each is an address, so opening it looks
        nothing up."""
        problems = []
        for address in answer:
            try:
                # https://docs.python.org/3.14/library/asyncio-stream.html#asyncio.open_connection
                return await asyncio.wait_for(
                    asyncio.open_connection(str(address), port), CONNECT_TIMEOUT
                )
            # Refused or unreachable, or TimeoutError, an OSError since 3.11.
            except OSError as error:
                problems.append(f"{address}: {str(error) or type(error).__name__}")
        raise self.record_failure(host, port, "; ".join(problems))

    def _refuse(
        self, host: str, port: int, kind: RefusalKind, detail: str
    ) -> EgressRefusedError:
        refusal = Refusal(host, port, kind, detail)
        self.refusals.append(refusal)
        return EgressRefusedError(refusal)

    def record_failure(self, host: str, port: int, cause: str) -> EgressUpstreamError:
        """Record that a connection to `host` and `port` failed upstream, and
        the error to raise for it: the gate's own failures, and the egress
        proxy's when a response breaks off."""
        event = InfrastructureEvent(host, port, cause)
        self.infrastructure_events.append(event)
        return EgressUpstreamError(event)
