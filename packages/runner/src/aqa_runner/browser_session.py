"""The browser session every run uses, for explore and replay alike: a fresh
browser launched through the sandbox check, with an empty environment, the
run's browser settings, all its traffic through routing and the run's egress
proxy, no service workers, and an accessibility snapshot whose element refs
the agent's tools act on (ADR-0025, ADR-0026 and its 2026-10-01 and
2026-10-02 amendments). It observes and acts only on documents from the
run's allowed origins (#44, ADR-0026's amendment on document origins)."""

import asyncio
import re
from collections.abc import AsyncIterator
from collections.abc import Set as AbstractSet
from contextlib import asynccontextmanager
from typing import Literal, overload

from aqa_core.browser import BrowserSettings
from aqa_core.compiled import Target
from playwright.async_api import ElementHandle, Error, Frame, Page

from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
    PolicyEventKind,
    Popup,
    Records,
    document_origin,
    frame_origin,
    navigable_origin,
)
from aqa_runner.egress import EgressPolicy
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.locators import Absent, Resolved, Unresolved, Use
from aqa_runner.locators import resolve as resolve_target
from aqa_runner.routing import install_routes
from aqa_runner.sandbox import Chromium, launch

# The settings every run uses unless the caller passes its own (ADR-0025).
PINNED_SETTINGS = BrowserSettings()

# Run in every document of the session before its own scripts. Playwright's
# `service_workers="block"` replaces only `navigator.serviceWorker.register`,
# with one that resolves, so a page could still call the prototype's method
# or delete the replacement (ADR-0026 amendment, 2026-10-02). This makes the
# method itself, and the instance's, refuse, and neither can be replaced.
SERVICE_WORKERS_REFUSED = """(() => {
    if (typeof ServiceWorkerContainer === "undefined") return;
    const refuse = () => Promise.reject(
        new DOMException("Service workers are blocked", "SecurityError")
    );
    const lock = (target) => Object.defineProperty(target, "register", {
        value: refuse, writable: false, configurable: false,
    });
    // The prototype first: reading navigator.serviceWorker can throw.
    lock(ServiceWorkerContainer.prototype);
    lock(navigator.serviceWorker);
})();"""

# One line of Playwright's AI snapshot: `- ` and a key, then `:` and a value or
# children, or nothing. Playwright single-quotes a key YAML would misread, so an
# unquoted key ends at the first colon that a space or the line's end follows.
LINE = re.compile(
    r"^(?P<head> *- )?(?P<key>'(?:[^'\n]|'')*'|(?:[^:\n]|:(?=\S))*)(?P<rest>.*)$",
    re.MULTILINE,
)

# An element's own ref ends its key, followed by nothing but `[cursor=pointer]`.
# Page text can't end a key so: a name is JSON-quoted or written /like this/.
ELEMENT_REF = re.compile(r"\[ref=((?:f[0-9]+)?e[0-9]+)\]((?: \[cursor=pointer\])?)\Z")

# A ref this session gives.
SESSION_REF = re.compile(r"e([1-9][0-9]{0,17})")

# What a frame on an origin the run doesn't allow shows in a snapshot, after
# `iframe` and in place of its ref and its content.
LEFT_OUT = "(content from an origin the run doesn't allow, not shown)"


class RefError(LookupError):
    """A ref that names nothing now: one from an older snapshot, one never
    given, or one whose element has left the page."""


class BrowserSession:
    """The browser one attempt or replay of a run uses: a fresh Chromium
    process and its page. Open it with `open_browser_session`, which launches
    it.

    Playwright's own refs can name a different element in a later snapshot
    (LAB_NOTES, 2026-10-01), so the session gives every element ref a number it
    never gives again and resolves only the current snapshot's. Playwright
    resolves a ref against the latest accessibility snapshot of its frame, so
    every accessibility snapshot of the page goes through `snapshot`, and
    snapshots and lookups take turns.

    Each observation first checks that the page is on one of the run's
    allowed origins, from `policy`; otherwise it records a policy event in
    `policy_events` and raises `PolicyEventError`. A snapshot leaves out the
    content of every frame that isn't on one of them. Every page another page
    opens is recorded in `popups` and closed, and is a policy event too when
    it isn't on one of them.

    `page` is public until #53 makes it private. No production code outside
    this module may use it: it observes and acts without the checks."""

    def __init__(self, page: Page, policy: EgressPolicy) -> None:
        self.page = page
        self._policy = policy
        self._refs_given = 0
        self._current: dict[str, str] = {}
        self._turn = asyncio.Lock()
        self.policy_events: Records[PolicyEvent] = Records()
        # Every navigation of any of the page's frames, same-document ones
        # included, and every frame removed from it
        # (https://playwright.dev/python/docs/api/class-page#page-event-frame-navigated).
        self._frame_changes = 0
        page.on("framenavigated", self._count_frame_change)
        page.on("framedetached", self._count_frame_change)
        self.popups: Records[Popup] = Records()
        # Every page opened in the context from now on, popups of popups
        # included (https://playwright.dev/python/docs/api/class-browsercontext#browser-context-event-page).
        page.context.on("page", self._close_popup)

    async def snapshot(self) -> str:
        """The page's accessibility snapshot in Playwright's AI mode
        (https://playwright.dev/python/docs/api/class-page#page-aria-snapshot),
        with this session's refs, unredacted. It retires every earlier
        snapshot's refs, and page text that imitates a ref reads `(ref=…`.
        A frame on an origin the run doesn't allow shows only its iframe's
        line, with no ref (`LEFT_OUT`).

        If any frame navigates or is removed between the page's check and the
        last frame's, the snapshot could hold a document no check saw, so it
        is discarded and `DocumentChangedError` raised: the caller takes
        another. With no change, the page is the document its check saw."""
        async with self._turn:
            # Retired before the call: Playwright may store this snapshot, and
            # resolve refs against it, even if the call never returns.
            self._current = {}
            # Counted from before the page's check, so a navigation during it
            # counts too.
            changes = self._frame_changes
            await self._require_allowed_page()
            taken = await self.page.aria_snapshot(mode="ai")
            try:
                left_out = await self._frames_left_out(taken)
            except Error as error:
                # Playwright's general error type: here, an iframe whose frame
                # was removed with its parent's ("Invalid frame in aria-ref
                # selector"). Any other is raised as it is.
                if self._frame_changes == changes:
                    raise
                raise DocumentChangedError from error
            # Playwright reports a navigation before the result of anything
            # that ran in the new document, so a snapshot of it shows here.
            if self._frame_changes != changes:
                raise DocumentChangedError
            text, self._current = renumber(
                taken, first=self._refs_given + 1, left_out=left_out
            )
            self._refs_given += len(self._current)
            return text

    async def locate(self, ref: str) -> ElementHandle:
        """The element `ref` names in the current snapshot, held: it never
        becomes another element, and acting on it fails once it has left the
        page. Any other ref raises `RefError`, so no string but a current ref
        reaches a selector. The element's frame must be on one of the run's
        allowed origins too, or it is a policy event."""
        async with self._turn:
            await self._require_allowed_page()
            playwright_ref = self._current.get(ref)
            if playwright_ref is None:
                given = SESSION_REF.fullmatch(ref)
                if given is not None and int(given[1]) <= self._refs_given:
                    raise RefError(
                        f"{ref!r} is from an older snapshot: a ref acts only on "
                        "the snapshot that gave it, so take a new snapshot"
                    )
                raise RefError(f"{ref[:40]!r} isn't a ref in the current snapshot")
            gone = RefError(f"{ref!r} names an element that has left the page")
            try:
                # Playwright's built-in aria-ref selector engine. It isn't in
                # Playwright's public docs; the session's tests pin its
                # behavior on 1.63.
                found = await self.page.query_selector(f"aria-ref={playwright_ref}")
            except Error as error:
                if self.page.is_closed():
                    raise  # the page itself has gone, not the ref's element
                # Playwright's general error type: here, a ref whose frame has
                # gone ("Invalid frame in aria-ref selector"), as the main
                # frame's does when it leaves a page that isn't about:blank.
                raise gone from error
            if found is None:
                raise gone
            frame = await found.owner_frame()
            if frame is None:  # an element of a document that is in no frame
                raise gone
            await self._require_allowed(frame, "frame")
        return found

    async def navigate(self, url: str) -> None:
        """Load `url`, an absolute URL on one of the run's allowed origins. Any
        other is a policy event (kind `navigation`), refused before anything
        is requested. The current page needn't be on an allowed origin:
        navigating is the way back after a policy event. The page it lands
        on, after any redirects, is checked as every page is. A network
        failure raises Playwright's `Error` as it is; the egress gate's
        records tell an egress block from an infrastructure failure."""
        async with self._turn:
            origin = navigable_origin(url)
            if origin not in self._policy.allowed_origins:
                raise self._refuse(PolicyEvent("navigation", url, origin))
            # https://playwright.dev/python/docs/api/class-page#page-goto
            await self.page.goto(url)
            await self._require_allowed_page()

    async def reload(self) -> None:
        """Reload the page, which must be on one of the run's allowed origins,
        and check the page it lands on."""
        async with self._turn:
            await self._require_allowed_page()
            # https://playwright.dev/python/docs/api/class-page#page-reload
            await self.page.reload()
            await self._require_allowed_page()

    async def click(self, element: ElementHandle) -> None:
        """Click `element`, from `locate` or `resolve`, once the page and the
        element's frame are checked."""
        async with self._turn:
            await self._require_actionable(element)
            # https://playwright.dev/python/docs/api/class-elementhandle#element-handle-click
            await element.click()

    async def fill(self, element: ElementHandle, value: str) -> None:
        """Fill `element` with `value`, once the page and the element's frame
        are checked."""
        async with self._turn:
            await self._require_actionable(element)
            # https://playwright.dev/python/docs/api/class-elementhandle#element-handle-fill
            await element.fill(value)

    async def select(self, element: ElementHandle, option: str) -> None:
        """Select the option of `element` whose value or label is `option`,
        once the page and the element's frame are checked."""
        async with self._turn:
            await self._require_actionable(element)
            # A string matches an option's value or its label:
            # https://playwright.dev/python/docs/api/class-elementhandle#element-handle-select-option
            await element.select_option(option)

    async def press(self, key: str) -> None:
        """Press `key` on the keyboard, once the page and the frame whose
        document has the focus, where the key goes, are checked."""
        async with self._turn:
            await self._require_allowed_page()
            await self._require_allowed(await self._focused_frame(), "frame")
            # https://playwright.dev/python/docs/api/class-keyboard#keyboard-press
            await self.page.keyboard.press(key)

    @overload
    async def resolve(
        self, target: Target, use: Literal["action", "assertion"]
    ) -> Resolved | Unresolved: ...

    @overload
    async def resolve(
        self, target: Target, use: Literal["negative_check"]
    ) -> Resolved | Absent | Unresolved: ...

    async def resolve(self, target: Target, use: Use) -> Resolved | Absent | Unresolved:
        """`target`'s element for `use`, as `aqa_runner.locators.resolve`
        finds it on the page: an observation, so the page is checked before
        and after, which also covers a page that navigated meanwhile. The
        caller owns a resolved element's handle."""
        async with self._turn:
            await self._require_allowed_page()
            found = await resolve_target(self.page, target, use)
            try:
                await self._require_allowed_page()
            except PolicyEventError:
                if isinstance(found, Resolved):
                    await found.element.dispose()
                raise
            return found

    async def url(self) -> str:
        """The page's URL, once the page is checked: an observation, as
        `url_matches` makes it."""
        async with self._turn:
            await self._require_allowed_page()
            return self.page.url

    async def _frames_left_out(self, snapshot: str) -> set[str]:
        """Playwright's refs of the iframes in `snapshot` whose frame isn't on
        one of the run's allowed origins. Each resolves, as Playwright's own
        snapshot does to add a frame's content below its iframe, to the
        iframe element and its content frame."""
        left_out = set()
        for ref in iframe_refs(snapshot):
            iframe = await self.page.query_selector(f"aria-ref={ref}")
            if iframe is None:  # the iframe has left the page
                left_out.add(ref)
                continue
            try:
                frame = await iframe.content_frame()
            finally:
                await iframe.dispose()
            if (
                frame is None
                or await frame_origin(frame) not in self._policy.allowed_origins
            ):
                left_out.add(ref)
        return left_out

    def _count_frame_change(self, _: Frame) -> None:
        self._frame_changes += 1

    async def _close_popup(self, popup: Page) -> None:
        """Record a page that another page opened, and close it: the session
        observes and acts on its own page only. Playwright reports the popup
        once it has navigated to its first URL, and its opener even when the
        page asked for none. A blank popup is on no origin the session can
        know: Playwright names the page that opened it, not the frame, which
        may be a subresource host's."""
        url = popup.url
        opener = await popup.opener()
        self.popups.add(Popup(url, None if opener is None else opener.url))
        origin = document_origin(url, None)
        if origin not in self._policy.allowed_origins:
            self.policy_events.add(PolicyEvent("popup", url, origin))
        await popup.close()

    async def _require_actionable(self, element: ElementHandle) -> None:
        """Record and raise a policy event unless the page, and the frame of
        `element`, are on the run's allowed origins. An element in no frame
        is on none."""
        await self._require_allowed_page()
        frame = await element.owner_frame()
        if frame is None:
            raise self._refuse(PolicyEvent("frame", "", None))
        await self._require_allowed(frame, "frame")

    async def _focused_frame(self) -> Frame:
        """The frame whose document has the focus, found from the page down
        through each focused frame element. It looks only into documents on
        the run's allowed origins: the first frame that isn't is where a key
        would go, or the way there."""
        frame = self.page.main_frame
        while await frame_origin(frame) in self._policy.allowed_origins:
            focused = await frame.evaluate_handle("document.activeElement")
            element = focused.as_element()
            child = None if element is None else await element.content_frame()
            await focused.dispose()
            if child is None:
                return frame
            frame = child
        return frame

    async def _require_allowed_page(self) -> None:
        await self._require_allowed(self.page.main_frame, "document")

    async def _require_allowed(self, frame: Frame, kind: PolicyEventKind) -> None:
        """Record and raise a policy event of `kind` unless `frame` is on one
        of the run's allowed origins."""
        origin = await frame_origin(frame)
        if origin not in self._policy.allowed_origins:
            raise self._refuse(PolicyEvent(kind, frame.url, origin))

    def _refuse(self, event: PolicyEvent) -> PolicyEventError:
        """Record `event`, and the error to raise for it."""
        self.policy_events.add(event)
        return PolicyEventError(event)


def renumber(
    snapshot: str, *, first: int, left_out: AbstractSet[str] = frozenset()
) -> tuple[str, dict[str, str]]:
    """`snapshot` with each element's own ref replaced by this session's, from
    `e{first}` on, and every other `[ref=`, which page text wrote, made
    `(ref=`. An element whose Playwright ref is in `left_out`, an iframe,
    keeps its line with `LEFT_OUT` in place of its ref, and loses every line
    below it: its frame's content. Also returns the session's refs, mapped to
    Playwright's."""
    refs: dict[str, str] = {}
    lines: list[str] = []
    # The indentation of the iframe whose content is being left out.
    leaving: int | None = None
    for found in LINE.finditer(snapshot):
        indent = len(found[0]) - len(found[0].lstrip(" "))
        if leaving is not None and indent > leaving:
            continue
        leaving = None
        head, key, rest = found["head"] or "", found["key"], found["rest"]
        quote, inner, own = key_parts(found)
        if own is None:
            lines.append(head + as_text(key) + as_text(rest))
        elif own[1] in left_out:
            lines.append(
                f"{head}{quote}{as_text(inner[: own.start()])}{LEFT_OUT}{quote}"
            )
            leaving = indent
        else:
            ref = f"e{first + len(refs)}"
            refs[ref] = own[1]
            lines.append(
                f"{head}{quote}{as_text(inner[: own.start()])}[ref={ref}]{own[2]}{quote}{as_text(rest)}"
            )
    return "\n".join(lines), refs


def key_parts(found: re.Match[str]) -> tuple[str, str, re.Match[str] | None]:
    """A snapshot line's quote around its key, the key inside the quotes, and
    the element's own ref at the end of the key, if the line has one."""
    key = found["key"]
    quote = "'" if key.startswith("'") else ""
    inner = key[len(quote) : len(key) - len(quote)]
    return quote, inner, ELEMENT_REF.search(inner) if found["head"] else None


def iframe_refs(snapshot: str) -> list[str]:
    """Playwright's refs of the iframes in `snapshot`, below whose lines
    Playwright adds their frames' content. It gives `iframe` elements and
    `frame` elements the role `iframe`, and no name."""
    refs = []
    for found in LINE.finditer(snapshot):
        _, inner, own = key_parts(found)
        if own is not None and inner.split(" ", 1)[0] == "iframe":
            refs.append(own[1])
    return refs


def as_text(text: str) -> str:
    """`text` with every imitation of a ref made unlike one, in linear time."""
    return text.replace("[ref=", "(ref=")


@asynccontextmanager
async def open_browser_session(
    chromium: Chromium,
    *,
    egress: EgressProxy,
    settings: BrowserSettings = PINNED_SETTINGS,
) -> AsyncIterator[BrowserSession]:
    """Launch a fresh browser through the sandbox check, open one page with
    `settings`, downloads and service workers refused, and every request
    through routing and `egress`, the run's egress proxy, and close the
    browser, its temporary profile with it, when the session ends."""
    proxy = egress.url  # an egress proxy that isn't serving fails before a launch
    browser = await launch(chromium)
    try:
        width, height = settings.viewport
        # https://playwright.dev/python/docs/api/class-browser#browser-new-context
        # A new context, never a persistent one. Playwright accepts downloads
        # unless told not to.
        context = await browser.new_context(
            timezone_id=settings.timezone,
            locale=settings.locale,
            viewport={"width": width, "height": height},
            device_scale_factor=settings.device_scale_factor,
            color_scheme=settings.color_scheme,
            accept_downloads=False,
            # The only way out, loopback included. Playwright adds
            # `<-loopback>` itself unless its driver's environment, the
            # runner's, sets PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK;
            # naming it here makes that variable irrelevant (ADR-0026
            # amendment, 2026-10-01).
            # https://playwright.dev/python/docs/api/class-browser#browser-new-context-option-proxy
            proxy={"server": proxy, "bypass": "<-loopback>"},
            # A service worker's requests bypass routing.
            # https://playwright.dev/python/docs/api/class-browser#browser-new-context-option-service-workers
            service_workers="block",
        )
        # https://playwright.dev/python/docs/api/class-browsercontext#browser-context-add-init-script
        await context.add_init_script(SERVICE_WORKERS_REFUSED)
        # Before the first page, so routing sees every request a page makes.
        await install_routes(context, egress.policy, egress.blocked_attempts)
        yield BrowserSession(await context.new_page(), egress.policy)
    finally:
        await browser.close()
