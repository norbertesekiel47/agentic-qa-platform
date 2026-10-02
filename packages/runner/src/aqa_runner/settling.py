"""Settle windows: which action each request the page sends belongs to
(ADR-0024, Settling, and its amendment; ADR-0025, `side_effect`: "a write
that arrives before the next action counts against the step")."""

import asyncio
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from playwright.async_api import Page, Request

from aqa_runner.document_origins import Records

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

# How settling ended.
type Settled = Literal["idle", "timeout"]

# Whether the document changed since the last look: a mutation observer on the
# whole document, installed at the first look, which counts as a change. It
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


@dataclass(eq=False)
class Window:
    """A settle window: the requests the page sends from one action until the
    next. `requests` keeps the first `RECORD_LIMIT` and counts all; `open`
    holds those still open, streams aside; `changed_at` is when one last
    started or ended (`time.monotonic`)."""

    requests: Records[PageRequest] = field(default_factory=Records[PageRequest])
    open: set[Request] = field(default_factory=set[Request])
    changed_at: float = field(default_factory=time.monotonic)


class Traffic:
    """Puts each request `page` sends into the window of the latest action,
    and a redirect's next hop into the window of the request it continues."""

    def __init__(self, page: Page) -> None:
        self._window = Window()
        self._windows: weakref.WeakKeyDictionary[Request, Window] = (
            weakref.WeakKeyDictionary()
        )
        # https://playwright.dev/python/docs/api/class-page#page-event-request
        page.on("request", self._started)
        page.on("requestfinished", self._ended)
        page.on("requestfailed", self._ended)

    def next_window(self) -> Window:
        """A new window, which every request from now on joins."""
        self._window = Window()
        return self._window

    def _started(self, request: Request) -> None:
        # A redirect finishes its hop and starts the next one as a new
        # request, `redirected_from` the hop:
        # https://playwright.dev/python/docs/api/class-request#request-redirected-from
        hop = request.redirected_from
        window = self._window if hop is None else self._windows[hop]
        self._windows[request] = window
        window.requests.add(PageRequest(request.method, request.url))
        # https://playwright.dev/python/docs/api/class-request#request-resource-type
        if request.resource_type not in STREAMS:
            window.open.add(request)
        window.changed_at = time.monotonic()

    def _ended(self, request: Request) -> None:
        window = self._windows[request]
        window.open.discard(request)
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
