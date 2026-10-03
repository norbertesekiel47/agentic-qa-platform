"""The four invariants (DATA_MODEL §6; ADR-0024, Settling, and its #47
amendment): what the session's page did, besides what its assertions check,
that keeps a run from passing. They are observed from before the page's
first navigation to the end of the attempt, reloads included, and the
spec's `invariants` decide, once the run is over, which of them count.

- `console_errors`: console messages of type `error`, Chromium's own
  "Failed to load resource" entries included, from every frame of the page
  and its dedicated workers, as Playwright reports them on the page.
- `js_exceptions`: uncaught exceptions and unhandled rejections (`pageerror`),
  from the same places. They are never console errors.
- `http_5xx`: any response with a 5xx status, the page's frames' and
  dedicated workers' alike.
- `broken_images`: an `<img>` whose `error` event fires, in a document on
  one of the run's allowed origins.

A request the run refuses (an egress block, an expected-blocked host's
included) leaves symptoms that are the run's doing, not the app's: Chromium's
console entry for it and a broken image whose load it was never count. They
are matched by the request's URL, read as routing reads it, never by console
or failure text (ADR-0026's #47 amendment). What the page then does without
what it was refused, such as a script that throws, still counts. Popups are
never observed: the session closes them (ADR-0026's amendments)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Self, get_args

from aqa_core.spec import InvariantName, Invariants
from playwright.async_api import ConsoleMessage, Error, Page, Request, Response

from aqa_runner.document_origins import Records
from aqa_runner.egress import EgressPolicy
from aqa_runner.routing import PROXIED_SCHEMES, refused_attempt

# Every invariant, in DATA_MODEL §6's order.
INVARIANTS: tuple[InvariantName, ...] = get_args(InvariantName)

# The most of each thing an invariant saw that it keeps: the page wrote it.
TEXT_CHARS = 200

# The isolated world the broken-image reporter runs in, and the binding it
# reports through, both the page's own CDP session's: the page's scripts
# can't reach either (ADR-0024's #47 amendment).
WORLD = "aqa-invariants"
BINDING = "aqaBrokenImage"

# The most of an image's URL a report carries: the page chooses it, and a
# data: URL can run to megabytes.
URL_CHARS = 2048

# How many URLs whose latest image load was refused at a redirect hop are
# kept at most; the oldest goes first.
PENDING_LOADS = 100

# The broken-image reporter, run in `WORLD` in every document of the page
# before the document's own scripts. Its capture listener on the window is
# the first, so nothing the page adds runs before it, and in its own world
# the binding, `HTMLImageElement` and `currentSrc` are out of the page's
# reach. It reports each trusted `error` event of an <img>: the document's
# origin, a newline, which no serialized origin or URL holds, and the first
# `URL_CHARS` of the URL the image tried. Opening the document anew erases
# every listener on it and its window, and starts no new world, so the
# reporter listens again whenever the document's root is replaced (a
# MutationObserver isn't a listener, and stays). An `error` event isn't
# composed, so an image inside a shadow root is never seen.
BROKEN_IMAGES = f"""(() => {{
    const report = globalThis.{BINDING};
    const reported = (event) => {{
        if (event.isTrusted && event.target instanceof HTMLImageElement) {{
            report(self.origin + "\\n" + event.target.currentSrc.slice(0, {URL_CHARS}));
        }}
    }};
    const listen = () => addEventListener("error", reported, true);
    listen();
    new MutationObserver(listen).observe(document, {{childList: true}});
}})();"""

# What became of an invariant: the page did nothing it counts, the page did,
# or the spec turned it off, whatever the page did.
type InvariantOutcome = Literal["held", "violated", "disabled"]


@dataclass(frozen=True)
class InvariantResult:
    """One invariant once the run is over: its outcome, the first 100 things
    it saw (`Records`), each at most `TEXT_CHARS` characters, and how many
    there were in all."""

    name: InvariantName
    outcome: InvariantOutcome
    seen: tuple[str, ...]
    total: int


class Observers:
    """What each invariant saw on one page, the session's, for a run whose
    egress policy is `policy`. Make them with `watch`, before the page's
    first navigation."""

    def __init__(self, policy: EgressPolicy) -> None:
        self._policy = policy
        self.seen: dict[InvariantName, Records[str]] = {
            name: Records[str]() for name in INVARIANTS
        }
        # The URLs (`_load_key`), each an image's `currentSrc`, whose latest
        # image load the run refused at a redirect hop, oldest first. Blink
        # loads a URL once for all the images that want it at the time
        # (measured), so every one of their errors is that load's; an image
        # error at the URL after a newer load has started is the newer one's.
        self._refused_hops: dict[str, None] = {}

    @classmethod
    async def watch(cls, page: Page, policy: EgressPolicy) -> Self:
        """Observers of `page`, collecting from now on, reloads included."""
        observers = cls(policy)
        # https://playwright.dev/python/docs/api/class-page#page-event-console
        page.on("console", observers._console)
        # https://playwright.dev/python/docs/api/class-page#page-event-page-error
        page.on("pageerror", observers._page_error)
        # Playwright 1.63 reports a dedicated worker's responses on the page.
        # https://playwright.dev/python/docs/api/class-page#page-event-response
        page.on("response", observers._response)
        # https://playwright.dev/python/docs/api/class-page#page-event-request
        page.on("request", observers._started)
        # https://playwright.dev/python/docs/api/class-page#page-event-request-failed
        page.on("requestfailed", observers._failed)
        # The page's own CDP session, for the reporter's world: Playwright's
        # bindings run in the page's world, and call through globals the
        # page's scripts can replace.
        # https://playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session
        # Playwright's connection keeps the session, and its listener, until
        # the page closes, so nothing here needs to hold it.
        session = await page.context.new_cdp_session(page)
        session.on("Runtime.bindingCalled", observers._reported)
        # The CDP methods and their parameters, as Playwright 1.63's
        # protocol.d.ts documents them: without Page.enable no script runs on
        # a new document, and without Runtime.enable no binding call arrives.
        # https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-addBinding
        # https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-addScriptToEvaluateOnNewDocument
        await session.send("Page.enable")
        await session.send("Runtime.enable")
        await session.send(
            "Runtime.addBinding", {"name": BINDING, "executionContextName": WORLD}
        )
        await session.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": BROKEN_IMAGES, "worldName": WORLD},
        )
        return observers

    def _console(self, message: ConsoleMessage) -> None:
        """A console message, counted when it is an error, unless it is
        Chromium's own entry for a load the run refused: an entry with no
        arguments, at the refused request's URL (Playwright 1.63 gives
        Chromium's `Log.entryAdded` entries no arguments, and the entry's
        URL as their location). The page's console calls have arguments."""
        if message.type != "error":
            return
        if not message.args and self._refused(message.location["url"]):
            return
        self._add("console_errors", message.text)

    def _page_error(self, error: Error) -> None:
        self._add("js_exceptions", error.message)

    def _response(self, response: Response) -> None:
        if 500 <= response.status <= 599:
            self._add("http_5xx", f"{response.status} {response.url}")

    def _started(self, request: Request) -> None:
        """A new image load of a URL, not a redirect's next hop: the image
        errors at that URL from now on are this load's, not those of a
        refused load before it."""
        if request.resource_type == "image" and request.redirected_from is None:
            self._refused_hops.pop(_load_key(request.url), None)

    def _failed(self, request: Request) -> None:
        """Note an image load the run refused at a redirect hop, under the
        URL it started at, so the broken images of the images that share
        it are left out. Chromium reports the failed request before an
        image's `error` event, which runs the reporter (measured). A load
        refused straight needs no note: its own URL says so."""
        first = request.redirected_from
        if request.resource_type != "image" or first is None:
            return
        if not self._refused(request.url):
            return
        while first.redirected_from is not None:
            first = first.redirected_from
        key = _load_key(first.url)
        # The newest last, so the oldest is the first to go.
        self._refused_hops.pop(key, None)
        self._refused_hops[key] = None
        if len(self._refused_hops) > PENDING_LOADS:
            del self._refused_hops[next(iter(self._refused_hops))]

    def _reported(self, event: Mapping[str, object]) -> None:
        """A broken image the reporter saw, counted when its document is on
        one of the run's allowed origins (a serialized origin is written as
        an allowed origin is: a subresource host's frame, or a data: frame,
        is on none), unless its load was one the run refused: straight, or
        at a redirect hop, as its URL's latest load."""
        payload = event.get("payload")
        if event.get("name") != BINDING or not isinstance(payload, str):
            return
        origin, _, url = payload.partition("\n")
        if origin not in self._policy.allowed_origins or self._refused(url):
            return
        if _load_key(url) not in self._refused_hops:
            self._add("broken_images", url)

    def _refused(self, url: str) -> bool:
        """Whether a request for `url` is one the run refuses, read as
        routing reads it: routing aborts it, or the proxy refuses it where
        routing can't see, as at a redirect hop. Only the schemes the proxy
        carries: a data: or blob: URL reaches no network, and Chromium
        refuses ftp:, file: and the like itself, so their failures are no
        refusal of the run's and count."""
        return (
            url.partition(":")[0].lower() in PROXIED_SCHEMES
            and refused_attempt(url, self._policy, "") is not None
        )

    def _add(self, name: InvariantName, what: str) -> None:
        self.seen[name].add(what[:TEXT_CHARS])


def _load_key(url: str) -> str:
    """The URL an image's load started at as a report and a failed request
    both carry it: its first `URL_CHARS`, without a fragment."""
    return url[:URL_CHARS].partition("#")[0]


def invariant_results(
    seen: Mapping[InvariantName, Records[str]], settings: Invariants
) -> tuple[InvariantResult, ...]:
    """Each invariant's result from what it `seen`, for a spec whose
    `invariants` are `settings`."""
    return tuple(
        InvariantResult(
            name,
            _outcome(name, seen[name], settings),
            tuple(seen[name].kept),
            seen[name].total,
        )
        for name in INVARIANTS
    )


def _outcome(
    name: InvariantName, seen: Records[str], settings: Invariants
) -> InvariantOutcome:
    if not settings.inherit or name in settings.disable:
        return "disabled"
    return "violated" if seen.total else "held"
