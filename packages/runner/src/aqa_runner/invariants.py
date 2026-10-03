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
from aqa_runner.egress_proxy import KEPT_ATTEMPTS
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

# The broken-image reporter, run in `WORLD` in every document of the page
# before the document's own scripts. Its capture listener on the window is
# the first, so nothing the page adds runs before it, and in its own world
# the binding, `HTMLImageElement` and `currentSrc` are out of the page's
# reach. It reports each trusted `error` event of an <img>: the document's
# origin, a newline, which no serialized origin or URL holds, and the URL the
# image tried. An `error` event isn't composed, so an image inside a shadow
# root is never seen.
BROKEN_IMAGES = f"""(() => {{
    const report = globalThis.{BINDING};
    addEventListener("error", (event) => {{
        if (event.isTrusted && event.target instanceof HTMLImageElement) {{
            report(self.origin + "\\n" + event.target.currentSrc);
        }}
    }}, true);
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
        # The first URL of each image load the run refused, straight or at a
        # later hop, without its fragment, the first `KEPT_ATTEMPTS` of them:
        # the image's `currentSrc` is that URL, not the refused hop's.
        self._refused_loads: set[str] = set()

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

    def _failed(self, request: Request) -> None:
        """Note the first URL of an image load the run refused, straight or
        at a later hop, so the image's broken image is left out. Chromium
        reports the failed request before the image's `error` event, which
        runs the reporter (measured, straight and at a redirect hop)."""
        if request.resource_type != "image" or not self._refused(request.url):
            return
        while request.redirected_from is not None:
            request = request.redirected_from
        if len(self._refused_loads) < KEPT_ATTEMPTS:
            self._refused_loads.add(request.url.partition("#")[0])

    def _reported(self, event: Mapping[str, object]) -> None:
        """A broken image the reporter saw, counted when its document is on
        one of the run's allowed origins (a serialized origin is written as
        an allowed origin is: a subresource host's frame, or a data: frame,
        is on none), unless its load was one the run refused."""
        if event.get("name") != BINDING:
            return
        origin, _, url = str(event.get("payload")).partition("\n")
        if origin not in self._policy.allowed_origins:
            return
        if url.partition("#")[0] not in self._refused_loads:
            self._add("broken_images", url)

    def _refused(self, url: str) -> bool:
        """Whether a request for `url` is one the run refuses, read as
        routing reads it: routing aborts it, or the proxy refuses it where
        routing can't see, as at a redirect hop. A URL that reaches no
        network, such as a data: or blob: URL, or none, is refused by none."""
        return (
            url.partition(":")[0].lower() in PROXIED_SCHEMES
            and refused_attempt(url, self._policy, "") is not None
        )

    def _add(self, name: InvariantName, what: str) -> None:
        self.seen[name].add(what[:TEXT_CHARS])


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
