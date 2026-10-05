"""Settle windows: which action each request the page sends belongs to
(ADR-0024, Settling, and its amendment; ADR-0025, `side_effect`: "a write
that arrives before the next action counts against the step")."""

import asyncio
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from aqa_core.compiled import NetworkNone, NetworkSeen
from playwright.async_api import Page, Request, Response

from aqa_runner import network
from aqa_runner.document_origins import Records
from aqa_runner.redaction import Redactor

# How long settling may take, how long a window must have been quiet to be
# idle, and how often it is looked at (ADR-0024 and its amendment, Settling).
SETTLE_SECONDS = 10
QUIET_SECONDS = 0.5
POLL_SECONDS = 0.1

# Streams a page keeps open as long as it listens, which never hold a window
# open: an EventSource's request. Playwright reports no request for a
# WebSocket at all (measured on 1.63). What their messages do to the page
# shows as DOM changes.
STREAMS = frozenset({"eventsource"})

# The most of a page-chosen URL a record keeps, counted after its scan, so
# the cut never leaves part of a value the scan would replace (ADR-0026's
# A2 amendment).
URL_CHARS = 2048

# How settling ended.
type Settled = Literal["idle", "timeout"]

# Whether the document changed since the last look: a mutation observer on the
# page's own document, not its frames' or its shadow roots' content, installed
# at the first look, which counts as a change (as a new document's does). It
# runs in the page's world, whose scripts can reach it, so its answer is the
# page's word: a page can look busy or quiet, and tell the run nothing else.
DOM_CHANGED = """() => {
    const key = Symbol.for("aqa.settling");
    const seen = document[key];
    if (seen === undefined) {
        const fresh = {changed: false};
        new MutationObserver(() => { fresh.changed = true; }).observe(document, {
            subtree: true, childList: true, attributes: true, characterData: true,
        });
        Object.defineProperty(document, key, {value: fresh});
        return true;
    }
    const changed = seen.changed;
    seen.changed = false;
    return changed;
}"""


@dataclass(frozen=True)
class PageRequest:
    """A request the page sent, as Playwright reports it."""

    method: str
    url: str


@dataclass(frozen=True)
class Exchange:
    """The page-chosen method and URL of a browser response, and its status."""

    method: str
    url: str
    status: int


@dataclass(eq=False)
class Window:
    """A settle window: the requests the page sends from one action until the
    next. `requests` and `responses` each keep the first `RECORD_LIMIT` and
    count all. `open` holds request identities, streams aside. `changed_at`
    is when one last started or ended (`time.monotonic`). No field retains
    a live Playwright object."""

    requests: Records[PageRequest] = field(default_factory=Records[PageRequest])
    responses: Records[Exchange] = field(default_factory=Records[Exchange])
    open: set[int] = field(default_factory=set[int])
    changed_at: float = field(default_factory=time.monotonic)


def recorded_url(redactor: Redactor, url: str) -> str:
    """`url` scanned with `redactor`, then cut to `URL_CHARS`, as a record's
    plain text."""
    return str(redactor.redact(url, limit=URL_CHARS))


class Traffic:
    """Puts each request `page` sends into the window of the latest action,
    and a redirect's next hop into the window of the request it continues.

    Made before the page sends anything, so every request it hears of has
    started in a window. A handler's error reaches the next Playwright call
    (Playwright 1.63 keeps it for that call) and raises there when the call
    is the session's. When it is the routing's continuation of a request,
    which a routed request's own handler error meets first, Playwright drops
    the error and that request stalls. A redirect hop isn't routed: it
    reaches the server unrecorded (LAB_NOTES, 2026-10-05).

    The windows hold the page-chosen methods and URLs scanned with
    `redactor`, once per request, when it starts. Network checks read each
    kept response's complete method and URL instead, which stay here, so a
    bound value that collides with them can't hide a response (ADR-0026's A2
    amendment)."""

    def __init__(self, page: Page, redactor: Redactor) -> None:
        self.windows: list[Window] = []
        self._redactor = redactor
        self._open_requests: set[Request] = set()
        self._response_requests: dict[Window, list[Request]] = {}
        self._complete_responses: dict[Window, Records[Exchange]] = {}
        self._request_windows: weakref.WeakKeyDictionary[Request, Window] = (
            weakref.WeakKeyDictionary()
        )
        # Each request's complete method and URL, and the scan of them its
        # window records, read once when it starts, for its response too.
        self._sent: weakref.WeakKeyDictionary[
            Request, tuple[PageRequest, PageRequest]
        ] = weakref.WeakKeyDictionary()
        self._window = self.next_window()
        # https://playwright.dev/python/docs/api/class-page#page-event-request
        page.on("request", self._started)
        page.on("response", self._responded)
        page.on("requestfinished", self._ended)
        page.on("requestfailed", self._ended)

    def next_window(self) -> Window:
        """A new window, which every request from now on joins."""
        self._window = Window()
        self.windows.append(self._window)
        self._response_requests[self._window] = []
        self._complete_responses[self._window] = Records()
        return self._window

    async def network_held(self, check: NetworkNone | NetworkSeen) -> bool:
        """Whether every window's complete responses establish `check`
        (`aqa_runner.network.held`)."""
        return await network.held(
            check, [self._complete_responses[window] for window in self.windows]
        )

    def _started(self, request: Request) -> None:
        # A redirect finishes its hop and starts the next one as a new
        # request, `redirected_from` the hop:
        # https://playwright.dev/python/docs/api/class-request#request-redirected-from
        hop = request.redirected_from
        window = self._window if hop is None else self._request_windows[hop]
        complete = PageRequest(request.method, request.url)
        sent = PageRequest(
            str(self._redactor.redact(complete.method)),
            recorded_url(self._redactor, complete.url),
        )
        self._request_windows[request] = window
        self._sent[request] = complete, sent
        window.requests.add(sent)
        # https://playwright.dev/python/docs/api/class-request#request-resource-type
        if request.resource_type not in STREAMS:
            self._open_requests.add(request)
            window.open.add(id(request))
        window.changed_at = time.monotonic()

    def _responded(self, response: Response) -> None:
        request = response.request
        window = self._request_windows[request]
        complete, sent = self._sent[request]
        window.responses.add(Exchange(sent.method, sent.url, response.status))
        self._complete_responses[window].add(
            Exchange(complete.method, complete.url, response.status)
        )
        if window.responses.total == len(window.responses.kept):
            self._response_requests[window].append(request)

    def _ended(self, request: Request) -> None:
        window = self._request_windows[request]
        self._open_requests.discard(request)
        window.open.discard(id(request))
        window.changed_at = time.monotonic()


async def settle(window: Window, dom_changed: Callable[[], Awaitable[bool]]) -> Settled:
    """Wait until `window` has no open request and, for `QUIET_SECONDS`, no
    request has started or ended in it and `dom_changed` has seen no change
    to the page: `"idle"`. After `SETTLE_SECONDS`, `"timeout"`, even while a
    look is under way."""
    quiet_since = time.monotonic()
    limit = asyncio.timeout(SETTLE_SECONDS)
    try:
        async with limit:
            while True:
                if await dom_changed():
                    quiet_since = time.monotonic()
                quiet = time.monotonic() - max(quiet_since, window.changed_at)
                if not window.open and quiet >= QUIET_SECONDS:
                    return "idle"
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        if not limit.expired():
            raise  # not settling's own limit
        return "timeout"
