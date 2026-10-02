"""Routing: the browser session's second layer of egress control (ADR-0026's
2026-10-02 amendment; SECURITY.md §7). Every request and WebSocket a
session's pages start is judged against the run's `EgressPolicy`, the egress
proxy's own allowlist; one the proxy would refuse is aborted before it leaves
and recorded. Routing never sees a redirect's later hops, so the egress proxy
stays the enforcer behind it."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aqa_core.schema import authority
from playwright.async_api import BrowserContext, Route, WebSocketRoute

from aqa_runner.egress import EgressPolicy, Requester

# The scheme whose origins a URL's authority is read as, and how the egress
# proxy sees the connection: Chromium sends a plain http request to it as
# one, and tunnels https, ws and wss through CONNECT (ADR-0026's amendment on
# the egress proxy). The proxy carries no other scheme.
ORIGIN_SCHEMES = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
REQUESTERS: dict[str, Requester] = {
    "http": "request",
    "https": "tunnel",
    "ws": "tunnel",
    "wss": "tunnel",
}


# How many blocked attempts a run keeps, so a page that loops on a blocked
# request can't grow the record without bound; the total counts them all.
KEPT_ATTEMPTS = 1000


@dataclass(frozen=True)
class BlockedAttempt:
    """A request or WebSocket routing refused: what kind (Playwright's
    resource type, `websocket` for a socket) and where it went. Never the
    path, query or user part, which carry an exfiltration's data. `port` is
    None when the URL names none a scheme gives."""

    resource_type: str
    scheme: str
    host: str
    port: int | None


def refused_attempt(
    url: str, policy: EgressPolicy, resource_type: str
) -> BlockedAttempt | None:
    """The attempt to record when the egress proxy would refuse `url`, or
    None when its policy passes it. A scheme the proxy carries nothing for is
    refused too."""
    parts = urlsplit(url)
    blocked = BlockedAttempt(resource_type, parts.scheme, parts.hostname or "", None)
    if parts.scheme not in REQUESTERS:
        return blocked
    # The proxy never sees a URL's user part: Chromium leaves it out of the
    # request line (HttpUtil::SpecForRequest) and a tunnel's CONNECT.
    host_and_port = parts.netloc.rpartition("@")[2]
    try:
        host, port = authority(f"{ORIGIN_SCHEMES[parts.scheme]}://{host_and_port}")
    except ValueError:  # an authority no origin writes, nor Chromium
        return blocked
    if policy.allows(host, port, REQUESTERS[parts.scheme]):
        return None
    return BlockedAttempt(resource_type, parts.scheme, host, port)


@dataclass
class BlockedAttempts:
    """What routing refused in a run: the first `KEPT_ATTEMPTS` attempts, and
    how many there were in all. Evidence for the run's record (#46, #53), as
    the gate's refusals are; the gate's are what the proxy enforced."""

    first: list[BlockedAttempt] = field(default_factory=list)
    total: int = 0

    def add(self, attempt: BlockedAttempt) -> None:
        self.total += 1
        if len(self.first) < KEPT_ATTEMPTS:
            self.first.append(attempt)


async def install_routes(
    context: BrowserContext, policy: EgressPolicy, blocked: BlockedAttempts
) -> None:
    """Route every request and WebSocket of `context`'s pages, popups
    included: one `policy` refuses is aborted and added to `blocked`, and
    any other goes on to the egress proxy. Call it before the context's first
    page exists."""

    async def request(route: Route) -> None:
        attempt = refused_attempt(
            route.request.url, policy, route.request.resource_type
        )
        if attempt is None:
            await route.fallback()
            return
        blocked.add(attempt)
        await route.abort("blockedbyclient")

    async def socket(route: WebSocketRoute) -> None:
        attempt = refused_attempt(route.url, policy, "websocket")
        if attempt is None:
            route.connect_to_server()
            return
        blocked.add(attempt)
        # The page sees a clean close, never an error: Playwright 1.63 has
        # no way to fail a routed socket (ADR-0026 amendment, 2026-10-02).
        await route.close()

    # https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route
    await context.route("**/*", request)
    # https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route-web-socket
    await context.route_web_socket("**/*", socket)
