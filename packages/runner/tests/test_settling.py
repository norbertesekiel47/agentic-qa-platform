"""Settle windows and settling (#46; ADR-0024, Settling, and its amendment;
ADR-0025, `side_effect`): each action of the browser session returns the
window of the requests it started, and `settle` waits until that window's
requests have finished and the page has been quiet. The browser tests launch
real Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import base64
import contextlib
import hashlib
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from aqa_core.compiled import AriaRole, ByCss, ByRole, Target
from aqa_runner import settling
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.document_origins import PolicyEvent, PolicyEventError
from aqa_runner.locators import Resolved
from aqa_runner.settling import Window
from playwright.async_api import ElementHandle, Error, async_playwright

from packages.runner.tests import document_fixtures
from packages.runner.tests.document_fixtures import Sites, serving_sites, to
from packages.runner.tests.egress_fixtures import egress_proxy

# The fixture site's pages, by name: each says what the test makes it do.
PAGES = {
    # Each action starts a request of its own, to /did/<action>.
    "actions": """<button onclick="fetch('/did/click')">Save</button>
        <label>Name <input oninput="fetch('/did/fill')"></label>
        <label>Size <select onchange="fetch('/did/select')">
            <option>S</option><option>M</option></select></label>
        <script>
            addEventListener("keydown", (event) => {
                if (event.key === "Enter") fetch("/did/press");
            });
        </script>""",
    # A request whose first hop the site holds until the test lets it go.
    "redirect": """<button onclick="fetch('/redirect-held/hop?to=/did/landed')">Go</button>
        <button onclick="fetch('/did/next')">Next</button>""",
    # A fetch the site holds, after which the page writes what it loaded.
    "load": """<button onclick="fetch('/held/data').then(() => {
            document.querySelector('#out').textContent = 'Loaded';
        })">Load</button><p id="out">Empty</p>""",
    # Fifteen changes to the page, 100 ms apart, and when the last was.
    "count": """<button onclick="let n = 0; const tick = setInterval(() => {
            document.querySelector('#out').textContent = ++n;
            if (n === 15) { clearInterval(tick); window.last = performance.now(); }
        }, 100)">Count</button><p id="out">0</p>""",
    # A fetch the site holds until the test lets it go, as a long poll.
    "wait": """<button onclick="fetch('/held/long')">Wait</button>""",
    # A page that takes away what settling watches the page with.
    "breaks": """<button onclick="window.MutationObserver = undefined">Break</button>""",
    # A link to a page that loads something the site holds, then shows it.
    "leave": """<a href="/page/arrive">Go</a>""",
    "arrive": """<p id="out">Empty</p><script>
        setTimeout(() => fetch('/held/arrive').then(() => {
            document.querySelector('#out').textContent = 'Arrived';
        }), 200);
    </script>""",
    # An event stream the page keeps open, and a WebSocket.
    "listen": """<button onclick="window.events = new EventSource('/events/stream')">Listen</button>
        <button onclick="window.socket = new WebSocket(location.origin.replace('http', 'ws') + '/socket')">Open</button>""",
    # A write the page sends 3 s after a click, once settling has said idle.
    "late": """<button onclick="setTimeout(() => fetch('/write/late', {method: 'POST'}), 3000)">Save</button>
        <button onclick="fetch('/did/next')">Next</button>""",
    # A request the page holds open from the start, and a click that sends none.
    "earlier": """<button onclick="document.querySelector('#out').textContent = 'Clicked'">Click</button>
        <p id="out"></p><script>fetch('/held/forever')</script>""",
    # A clock: the page changes every 50 ms, for good.
    "clock": """<button onclick="setInterval(() => {
            document.querySelector('#out').textContent = Date.now();
        }, 50)">Start</button><p id="out"></p>""",
    # A request, then, 0.3 s after it ends, another, and no change to the page.
    "chain": """<button onclick="fetch('/held/first')
            .then(() => new Promise((done) => setTimeout(done, 300)))
            .then(() => fetch('/did/second'))">Chain</button>""",
    # A region around a frame on no origin, which actions refuse.
    "refused": """<div id="around" tabindex="0" style="width: 300px; height: 150px">
            <iframe src="data:text/html,<p>Framed</p>"></iframe>
        </div>""",
}

# What a WebSocket handshake's accept key is made with (RFC 6455, 4.2.2).
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass
class Site:
    """The fixture site's origin, each (method, path) it got, in order, and
    the responses it holds until the test lets them go."""

    origin: str
    seen: list[tuple[str, str]] = field(default_factory=list)
    arrived: threading.Condition = field(default_factory=threading.Condition)
    held: dict[str, threading.Event] = field(default_factory=dict)

    def hold(self, key: str) -> threading.Event:
        with self.arrived:
            return self.held.setdefault(key, threading.Event())

    def release(self, key: str) -> None:
        self.hold(key).set()

    def saw(self, method: str, path: str) -> bool:
        with self.arrived:
            return (method, path) in self.seen


class _Handler(BaseHTTPRequestHandler):
    """Serves the fixture site's pages, `/did/…` and `/write/…` (204),
    `/held/<key>` (204 once released), `/redirect-held/<key>?to=<path>` (302
    once released), `/events/<key>` (an event stream, open until released)
    and `/socket` (a WebSocket, open until the browser closes it)."""

    served: Site

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._answer()

    def do_GET(self) -> None:
        self._answer()

    def _answer(self) -> None:
        target = urlsplit(self.path)
        with self.served.arrived:
            self.served.seen.append((self.command, target.path))
            self.served.arrived.notify_all()
        kind, _, name = target.path.removeprefix("/").partition("/")
        if kind == "page" and name in PAGES:
            self._page(PAGES[name])
        elif kind == "held":
            self.served.hold(name).wait(30)
            self._empty(HTTPStatus.NO_CONTENT)
        elif kind == "redirect-held":
            self.served.hold(name).wait(30)
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", parse_qs(target.query)["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif kind in ("did", "write"):
            self._empty(HTTPStatus.NO_CONTENT)
        elif kind == "socket":
            self._socket()
        elif kind == "events":
            self._events(self.served.hold(name))
        else:
            self._empty(HTTPStatus.NOT_FOUND)

    def _page(self, body: str) -> None:
        data = f"<!doctype html><title>page</title>{body}".encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _events(self, released: threading.Event) -> None:
        """Send one event, and keep the stream open until released."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        with contextlib.suppress(ConnectionError):  # the browser may close it
            self.wfile.write(b"data: one\n\n")
            self.wfile.flush()
            released.wait(30)

    def _socket(self) -> None:
        """Accept a WebSocket and hold it open until the browser closes it."""
        key = self.headers["Sec-WebSocket-Key"] + WEBSOCKET_GUID
        accept = base64.b64encode(
            hashlib.sha1(key.encode(), usedforsecurity=False).digest()
        ).decode()
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        with contextlib.suppress(ConnectionError):  # the browser may reset it
            self.rfile.read(1)

    def log_message(self, *_: object) -> None:
        pass  # the test reads `seen`


@pytest.fixture
def site() -> Iterator[Site]:
    served = Site("")
    handler = type("Handler", (_Handler,), {"served": served})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    served.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield served
    finally:
        with served.arrived:
            keys = list(served.held)
        for key in keys:
            served.release(key)
        server.shutdown()
        thread.join()
        server.server_close()


@asynccontextmanager
async def browsing(site: Site) -> AsyncIterator[BrowserSession]:
    """A session of a run whose start origin is the fixture site."""
    async with (
        async_playwright() as playwright,
        egress_proxy(site.origin) as egress,
        open_browser_session(playwright.chromium, egress=egress) as session,
    ):
        yield session


async def arrives(window: Window, count: int) -> None:
    """Wait up to 5 s until `window` has `count` requests, which Playwright
    reports as the browser sends them."""
    for _ in range(250):
        if window.requests.total >= count:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"the window has {window.requests.total} requests, not {count}")


def paths(window: Window) -> list[tuple[str, str]]:
    """Each request in `window`: its method and its URL's path."""
    return [
        (request.method, urlsplit(request.url).path) for request in window.requests.kept
    ]


async def element(session: BrowserSession, target: Target) -> ElementHandle:
    found = await session.resolve(target, "action")
    assert isinstance(found, Resolved), found
    return found.element


def by_role(role: AriaRole, name: str) -> Target:
    return Target(
        semantic=f"the {name} {role}", locators=(ByRole(role=role, name=name),)
    )


def test_each_action_returns_the_window_of_the_requests_it_started(site: Site) -> None:
    async def scenario() -> list[list[tuple[str, str]]]:
        async with browsing(site) as session:
            windows = [await session.navigate(f"{site.origin}/page/actions")]
            await arrives(windows[-1], 1)
            windows.append(
                await session.click(await element(session, by_role("button", "Save")))
            )
            await arrives(windows[-1], 1)
            name = Target(semantic="the name field", locators=(ByCss(css="input"),))
            windows.append(await session.fill(await element(session, name), "Ada"))
            await arrives(windows[-1], 1)
            size = Target(semantic="the size list", locators=(ByCss(css="select"),))
            windows.append(await session.select(await element(session, size), "M"))
            await arrives(windows[-1], 1)
            windows.append(await session.press("Enter"))
            await arrives(windows[-1], 1)
            windows.append(await session.reload())
            await arrives(windows[-1], 1)
            # Anything still arriving would land in a window by now.
            await asyncio.sleep(0.3)
            return [paths(window) for window in windows]

    assert asyncio.run(scenario()) == [
        [("GET", "/page/actions")],
        [("GET", "/did/click")],
        [("GET", "/did/fill")],
        [("GET", "/did/select")],
        [("GET", "/did/press")],
        [("GET", "/page/actions")],
    ]


def test_a_redirect_hop_stays_in_the_window_of_the_request_it_continues(
    site: Site,
) -> None:
    async def scenario() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/redirect")
            go = await session.click(await element(session, by_role("button", "Go")))
            await arrives(go, 1)
            # The next action starts while the first hop is still held.
            following = await session.click(
                await element(session, by_role("button", "Next"))
            )
            await arrives(following, 1)
            site.release("hop")
            await arrives(go, 2)
            await asyncio.sleep(0.3)
            return paths(go), paths(following)

    go, following = asyncio.run(scenario())

    assert go == [("GET", "/redirect-held/hop"), ("GET", "/did/landed")]
    assert following == [("GET", "/did/next")]


@pytest.mark.parametrize(
    "action", ["navigate", "reload", "click", "fill", "select", "press"]
)
def test_a_refused_action_opens_no_window(site: Site, action: str) -> None:
    around = Target(semantic="the region", locators=(ByCss(css="#around"),))
    later = f"{site.origin}/did/later"

    async def refused(session: BrowserSession) -> None:
        """Make the session refuse `action`: a URL or a page off the allowed
        origins, or an element around a frame on none."""
        match action:
            case "navigate":
                await session.navigate("http://evil.example.test/")
            case "reload":
                await session.page.goto("about:blank")
                await session.reload()
            case "click":
                await session.click(await element(session, around))
            case "fill":
                await session.fill(await element(session, around), "Ada")
            case "select":
                await session.select(await element(session, around), "M")
            case "press":
                await session.page.focus("#around")
                await session.press("a")

    async def scenario() -> list[tuple[str, str]]:
        async with browsing(site) as session:
            loaded = await session.navigate(f"{site.origin}/page/refused")
            with pytest.raises(PolicyEventError):
                await refused(session)
            # What the page sends next is still the navigation's.
            await session.page.evaluate(
                f"() => {{ fetch({later!r}).catch(() => {{}}); }}"
            )
            await arrives(loaded, 2)
            return paths(loaded)

    assert asyncio.run(scenario()) == [("GET", "/page/refused"), ("GET", "/did/later")]


def test_settling_waits_for_the_actions_requests_and_a_quiet_dom(site: Site) -> None:
    async def scenario() -> tuple[str, float, str]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/load")
            loop = asyncio.get_running_loop()
            started = loop.time()
            window = await session.click(
                await element(session, by_role("button", "Load"))
            )
            loop.call_later(1, site.release, "data")
            settled = await session.settle(window)
            elapsed = loop.time() - started
            return settled, elapsed, await session.page.inner_text("#out")

    settled, elapsed, out = asyncio.run(scenario())

    assert settled == "idle"
    # The fetch was held for 1 s, and the page then had to be quiet for 0.5 s.
    assert elapsed >= 1.5
    assert out == "Loaded"


def test_settling_waits_until_the_dom_has_been_quiet_for_half_a_second(
    site: Site,
) -> None:
    async def scenario() -> tuple[str, float]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/count")
            window = await session.click(
                await element(session, by_role("button", "Count"))
            )
            settled = await session.settle(window)
            since = await session.page.evaluate(
                "window.last === undefined ? -1 : performance.now() - window.last"
            )
            return settled, since / 1000

    settled, since_last_change = asyncio.run(scenario())

    assert settled == "idle"
    # No request, so only the page's changes held settling: idle came at
    # least 0.5 s after the last, and not long after.
    assert 0.5 <= since_last_change < 2.5


def test_settling_runs_out_at_ten_seconds_while_the_actions_request_is_open(
    site: Site,
) -> None:
    async def scenario() -> tuple[str, float, str]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/wait")
            window = await session.click(
                await element(session, by_role("button", "Wait"))
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            # Bounded here too, so a settle that never ends fails the test.
            async with asyncio.timeout(15):
                timed_out = await session.settle(window)
            elapsed = loop.time() - started
            site.release("long")
            async with asyncio.timeout(15):
                return timed_out, elapsed, await session.settle(window)

    timed_out, elapsed, then = asyncio.run(scenario())

    assert timed_out == "timeout"
    assert 10 <= elapsed < 12
    # Once the request is let go, the same window settles.
    assert then == "idle"


def test_settling_refuses_a_page_off_the_allowed_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(sites: Sites) -> tuple[PolicyEventError, list[PolicyEvent]]:
        async with document_fixtures.browsing(sites) as session:
            await session.navigate(f"{sites.app}/click?to={to(f'{sites.cdn}/doc')}")
            window = await session.click(await element(session, by_role("link", "Go")))
            with pytest.raises(PolicyEventError) as refused:
                async with asyncio.timeout(15):
                    await session.settle(window)
            return refused.value, session.policy_events.kept

    with serving_sites(monkeypatch) as sites:
        refused, events = asyncio.run(scenario(sites))
        landed = PolicyEvent("document", f"{sites.cdn}/doc", sites.cdn)

    assert refused.event == landed
    assert events == [landed]


def test_a_page_that_breaks_the_dom_probe_never_settles_as_idle(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)

    async def scenario() -> str:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/breaks")
            window = await session.click(
                await element(session, by_role("button", "Break"))
            )
            return await session.settle(window)

    # Whether the page changed can't be told, so it never counts as quiet.
    assert asyncio.run(scenario()) == "timeout"


def test_settling_follows_a_click_into_the_document_it_navigates_to(
    site: Site,
) -> None:
    async def scenario() -> tuple[str, float, str]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/leave")
            loop = asyncio.get_running_loop()
            started = loop.time()
            window = await session.click(await element(session, by_role("link", "Go")))
            loop.call_later(1, site.release, "arrive")
            async with asyncio.timeout(15):
                settled = await session.settle(window)
            elapsed = loop.time() - started
            return settled, elapsed, await session.page.inner_text("#out")

    settled, elapsed, out = asyncio.run(scenario())

    assert settled == "idle"
    assert elapsed >= 1.5
    assert out == "Arrived"


def test_settling_a_closed_page_raises_playwrights_error(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)

    async def scenario() -> None:
        async with browsing(site) as session:
            window = await session.navigate(f"{site.origin}/page/wait")
            await session.page.close()
            # Not a page that keeps changing: there is no page.
            with pytest.raises(Error, match="closed"):
                await session.settle(window)

    asyncio.run(scenario())


def test_an_open_event_stream_is_in_the_window_but_doesnt_hold_settling(
    site: Site,
) -> None:
    async def scenario() -> tuple[str, object, list[tuple[str, str]]]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/listen")
            window = await session.click(
                await element(session, by_role("button", "Listen"))
            )
            async with asyncio.timeout(15):
                settled = await session.settle(window)
            state = await session.page.evaluate("window.events.readyState")
            return settled, state, paths(window)

    settled, state, requests = asyncio.run(scenario())

    assert settled == "idle"
    assert state == 1  # EventSource.OPEN: the stream was still open
    assert requests == [("GET", "/events/stream")]


def test_a_request_belongs_to_the_latest_action_until_the_next_one(site: Site) -> None:
    async def scenario() -> tuple[
        str, bool, list[tuple[str, str]], list[tuple[str, str]]
    ]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/late")
            save = await session.click(
                await element(session, by_role("button", "Save"))
            )
            settled = await session.settle(save)
            written_before = site.saw("POST", "/write/late")
            await arrives(save, 1)
            following = await session.click(
                await element(session, by_role("button", "Next"))
            )
            await arrives(following, 1)
            await asyncio.sleep(0.3)
            return settled, written_before, paths(save), paths(following)

    settled, written_before, save, following = asyncio.run(scenario())

    # Settling ended before the write, which still counts against the click.
    assert settled == "idle"
    assert not written_before
    assert save == [("POST", "/write/late")]
    assert following == [("GET", "/did/next")]


def test_a_request_an_earlier_action_started_doesnt_hold_this_actions_settling(
    site: Site,
) -> None:
    async def scenario() -> tuple[str, list[tuple[str, str]]]:
        async with browsing(site) as session:
            loaded = await session.navigate(f"{site.origin}/page/earlier")
            await arrives(loaded, 2)
            window = await session.click(
                await element(session, by_role("button", "Click"))
            )
            async with asyncio.timeout(15):
                settled = await session.settle(window)
            return settled, paths(loaded)

    settled, loaded = asyncio.run(scenario())

    # The navigation's request is still open, so the page's network never
    # goes idle; the click's own window has none.
    assert loaded == [("GET", "/page/earlier"), ("GET", "/held/forever")]
    assert settled == "idle"


def test_an_open_websocket_doesnt_hold_settling(site: Site) -> None:
    async def scenario() -> tuple[str, object, list[tuple[str, str]]]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/listen")
            window = await session.click(
                await element(session, by_role("button", "Open"))
            )
            async with asyncio.timeout(15):
                settled = await session.settle(window)
            state = await session.page.evaluate("window.socket.readyState")
            return settled, state, paths(window)

    settled, state, requests = asyncio.run(scenario())

    assert settled == "idle"
    assert state == 1  # WebSocket.OPEN: the socket was still open
    # Playwright reports no request for a WebSocket (measured on 1.63).
    assert requests == []


def test_a_page_whose_dom_never_stops_changing_settles_as_a_timeout(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than the 10 s rule, which the held request's test measures.
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)

    async def scenario() -> tuple[str, float]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/clock")
            window = await session.click(
                await element(session, by_role("button", "Start"))
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            settled = await session.settle(window)
            return settled, loop.time() - started

    settled, elapsed = asyncio.run(scenario())

    assert settled == "timeout"
    assert 2 <= elapsed < 4


def test_settling_waits_out_a_pause_between_chained_requests(site: Site) -> None:
    async def scenario() -> tuple[str, list[tuple[str, str]]]:
        async with browsing(site) as session:
            await session.navigate(f"{site.origin}/page/chain")
            window = await session.click(
                await element(session, by_role("button", "Chain"))
            )
            asyncio.get_running_loop().call_later(1, site.release, "first")
            async with asyncio.timeout(15):
                settled = await session.settle(window)
            return settled, paths(window)

    settled, requests = asyncio.run(scenario())

    # The page never changed, and the second request started 0.3 s after the
    # first ended, within the quiet period: settling waited for it too.
    assert settled == "idle"
    assert requests == [("GET", "/held/first"), ("GET", "/did/second")]


def test_settling_raises_a_timeout_that_isnt_its_own() -> None:
    async def look() -> bool:
        raise TimeoutError("the look's own")

    with pytest.raises(TimeoutError, match="the look's own"):
        asyncio.run(settling.settle(Window(), look))


def test_settling_a_crashed_page_raises_playwrights_error(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)

    async def scenario() -> None:
        async with browsing(site) as session:
            window = await session.navigate(f"{site.origin}/page/wait")
            # Chromium's page that crashes the renderer, which never loads.
            with pytest.raises(Error):
                await session.page.goto("chrome://crash")
            # Not a page that keeps changing: the page has crashed.
            with pytest.raises(Error, match="crashed"):
                await session.settle(window)

    asyncio.run(scenario())
