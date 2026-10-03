"""Routing: the browser session's second layer of egress control (ADR-0026's
2026-10-02 amendment; SECURITY.md §7). Every request and WebSocket a
session's pages start is judged against the run's `EgressPolicy`, the egress
proxy's own allowlist; one the proxy would refuse is aborted before it leaves
and recorded. Routing never sees a redirect's later hops or a dedicated
worker's sockets, and a hostile page can open a socket around Playwright's
in-page socket routing, so the egress proxy stays the enforcer behind it."""

import re
from urllib.parse import urlsplit

from aqa_core.schema import DEFAULT_PORTS, authority
from playwright.async_api import BrowserContext, Route, WebSocketRoute

from aqa_runner.egress import EgressPolicy, Requester
from aqa_runner.egress_proxy import BlockedAttempt, BlockedAttempts, named_host

# For each scheme the egress proxy carries: the scheme whose origins its
# authority is read as, and how the proxy sees the connection. Chromium sends
# a plain http request to it as one, and tunnels https, ws and wss through
# CONNECT (ADR-0026's amendment on the egress proxy). `test_routing.py`'s
# verdicts pin this against the proxy's rules.
PROXIED_SCHEMES: dict[str, tuple[str, Requester]] = {
    "http": ("http", "request"),
    "https": ("https", "tunnel"),
    "ws": ("http", "tunnel"),
    "wss": ("https", "tunnel"),
}

# The port a WebSocket's URL leaves out, by scheme: WHATWG URL's defaults,
# which Playwright's socket routing sees the URL serialized with.
SOCKET_DEFAULT_PORTS = {"ws": 80, "wss": 443}


def refused_attempt(
    url: str, policy: EgressPolicy, resource_type: str
) -> BlockedAttempt | None:
    """The attempt to record when the egress proxy would refuse `url`, or
    None when its policy passes it. A scheme the proxy carries nothing for,
    or a URL no browser writes, is refused too."""
    try:
        parts = urlsplit(url)
    except ValueError:  # brackets that don't close
        return BlockedAttempt(resource_type, url.partition(":")[0], "", None)
    # Only a host an origin could write is recorded: a page chooses the rest.
    if parts.scheme not in PROXIED_SCHEMES:
        return BlockedAttempt(resource_type, parts.scheme, "", None)
    origin_scheme, requester = PROXIED_SCHEMES[parts.scheme]
    # The proxy never sees a URL's user part: Chromium leaves it out of the
    # request line (HttpUtil::SpecForRequest) and a tunnel's CONNECT.
    host_and_port = parts.netloc.rpartition("@")[2]
    try:
        host, port = authority(f"{origin_scheme}://{host_and_port}")
    except ValueError:  # an authority no origin writes, nor Chromium
        return BlockedAttempt(resource_type, parts.scheme, "", None)
    if policy.allows(host, port, requester):
        return None
    return BlockedAttempt(resource_type, parts.scheme, named_host(host), port)


def refused_sockets(policy: EgressPolicy) -> re.Pattern[str]:
    """A pattern that matches a WebSocket URL, as WHATWG URL serializes it,
    exactly when `policy` refuses its host and port. Playwright routes only
    the sockets it matches, so every other socket stays the page's own:
    Playwright's forwarding of a routed socket can send a Blob after a string
    the page sent later (ADR-0026 amendment, 2026-10-02). Both Python and
    JavaScript read it."""
    candidates = {authority(origin) for origin in policy.allowed_origins} | {
        (host, port)
        for host in policy.subresource_hosts
        for port in DEFAULT_PORTS.values()
    }
    allowed = [
        f"{scheme}://(?:[^/?#@]*@)?{re.escape(host)}"
        + ("" if port == default else f":{port}")
        for scheme, default in SOCKET_DEFAULT_PORTS.items()
        for host, port in sorted(candidates)
        if policy.allows(host, port, PROXIED_SCHEMES[scheme][1])
    ]
    if not allowed:
        return re.compile(r"^wss?://")
    return re.compile(rf"^(?!(?:{'|'.join(allowed)})(?:[/?#]|$))wss?://")


async def install_routes(
    context: BrowserContext, policy: EgressPolicy, blocked: BlockedAttempts
) -> None:
    """Route every request of `context`'s pages, popups included, and every
    WebSocket `policy` refuses: one the policy refuses is aborted and added
    to `blocked`, and any other goes on to the egress proxy. Call it before
    the context's first page exists."""

    async def on_request(route: Route) -> None:
        attempt = refused_attempt(
            route.request.url, policy, route.request.resource_type
        )
        if attempt is None:
            await route.fallback()
            return
        blocked.add(attempt)
        await route.abort("blockedbyclient")

    async def on_websocket(route: WebSocketRoute) -> None:
        attempt = refused_attempt(route.url, policy, "websocket")
        if attempt is None:  # only a socket `refused_sockets` misjudged
            route.connect_to_server()
            return
        blocked.add(attempt)
        # The page sees a clean close, never an error: Playwright 1.63 has
        # no way to fail a routed socket (ADR-0026 amendment, 2026-10-02).
        await route.close()

    # https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route
    await context.route("**/*", on_request)
    # https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route-web-socket
    await context.route_web_socket(refused_sockets(policy), on_websocket)
