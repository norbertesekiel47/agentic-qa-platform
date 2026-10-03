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

from aqa_runner import settling
from aqa_runner.bound_secrets import (
    BoundSecret,
    SecretRefusedError,
    described,
    field_matches,
)
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
    reaches,
)
from aqa_runner.egress import EgressPolicy
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.locators import Absent, Resolved, Unresolved, Use, rendered_text
from aqa_runner.locators import resolve as resolve_target
from aqa_runner.routing import install_routes
from aqa_runner.sandbox import Chromium, launch
from aqa_runner.settling import Settled, Traffic, Window

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

# Whether `owner`, a frame's element, is inside what a click on `element`
# lands in, run in the element's own frame, where both are. That is the
# element Playwright's click targets, whose descendants its hit check
# accepts: the closest button or link, unless the element is a field (1.63's
# injected `retarget(node, "button-link")`). It climbs both trees from
# `owner`: the DOM's, through shadow roots to their hosts, and the rendered
# one through each slot a node is assigned to, which the hit check follows
# (1.63's `expectHitTarget`: `assignedSlot ?? parentElementOrShadowHost`).
CONTAINS = """(element, owner) => {
    const target =
        element.matches("input, textarea, select") || element.isContentEditable
            ? element
            : element.closest("button, [role=button], a, [role=link]") ?? element;
    const seen = new Set();
    const nodes = [owner];
    while (nodes.length > 0) {
        const node = nodes.pop();
        if (!node || seen.has(node)) continue;
        if (node === target) return true;
        seen.add(node);
        nodes.push(node.assignedSlot, node instanceof ShadowRoot ? node.host : node.parentNode);
    }
    return false;
}"""

# Whether a frame's element, or one around it, is transformed, which stops
# Playwright checking what a click in the frame hits (1.63's
# `describeIFrameStyle`, which climbs `parentElementOrShadowHost`).
TRANSFORMED = """(owner) => {
    for (let node = owner; node; ) {
        if (getComputedStyle(node).transform !== "none") return true;
        const parent = node.parentNode;
        node = parent instanceof ShadowRoot ? parent.host : node.parentElement;
    }
    return false;
}"""

# Fills a field with a value inside its own document, never through the
# page's keyboard, and says whether it then holds the value. Like
# Playwright's fill (1.63's injected script): text goes into the text kinds
# of <input>, <textarea> and contenteditable elements, replacing what they
# hold (execCommand's insertText, which fires `input`, though not
# `beforeinput` or key events); the date-like kinds of <input> take the
# value as set. A field whose page changes the value as it goes in (a mask)
# reads back otherwise, and counts as not filled.
FILL = """(element, value) => {
    const set = ["color", "date", "time", "datetime-local", "month", "range", "week"];
    const typed = ["", "email", "number", "password", "search", "tel", "text", "url"];
    if (element instanceof HTMLInputElement && set.includes(element.type)) {
        element.focus();
        element.value = value;
        element.dispatchEvent(new Event("input", {bubbles: true, composed: true}));
        element.dispatchEvent(new Event("change", {bubbles: true}));
        return element.value === value;
    }
    if (element instanceof HTMLInputElement && typed.includes(element.type)) {
        element.focus();
        element.select();
    } else if (element instanceof HTMLTextAreaElement) {
        element.focus();
        element.select();
    } else if (element.isContentEditable) {
        element.focus();
        const all = document.createRange();
        all.selectNodeContents(element);
        getSelection().removeAllRanges();
        getSelection().addRange(all);
    } else {
        return false;
    }
    if (value === "") document.execCommand("delete");
    else document.execCommand("insertText", false, value);
    return (element.isContentEditable ? element.textContent : element.value) === value;
}"""

# An element's rendered text, or nothing when the page doesn't render it,
# worked out in one evaluation so the page can't change between the two.
# Rendered as Playwright 1.63 judges an element visible, as far as one script
# can: a box of some size and a computed visibility of `visible` (`hidden`,
# `display: none` on it or an ancestor, and a detached element leave no box).
# Unlike Playwright, an element with `display: contents` counts as having no
# box. Only HTML elements have rendered text: any other, such as SVG's, raises,
# as Playwright's innerText does, since its text content holds what the page
# hides (a `<tspan visibility="hidden">`, a `<title>`).
RENDERED_TEXT = """(element) => {
    if (!(element instanceof HTMLElement)) {
        throw new Error(`text_of reads HTML elements only: <${element.localName}> isn't one`);
    }
    const box = element.getBoundingClientRect();
    const shown = box.width > 0 && box.height > 0 &&
        getComputedStyle(element).visibility === "visible";
    return shown ? element.innerText : "";
}"""

# Whether an element with the focus stands for nothing focused: the body, or
# the document's root.
NOTHING_FOCUSED = """(element) =>
    element === element.ownerDocument.body ||
    element === element.ownerDocument.documentElement"""

# The keys `press` may hold down before the key it presses
# (https://playwright.dev/python/docs/api/class-keyboard#keyboard-press).
MODIFIERS = frozenset({"Shift", "Control", "Alt", "Meta", "ControlOrMeta"})

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

    Every observation and action first checks that the page is on one of
    the run's allowed origins, from `policy`, and an action that the frame
    it acts in is too; otherwise it records a policy event in
    `policy_events` and raises `PolicyEventError`. `navigate` checks the URL
    it goes to instead, so it is the way back. A snapshot leaves out the
    content of every frame that isn't on one of them. Every page another page
    opens is recorded in `popups` and closed, and is a policy event too when
    it isn't on one of them.

    Each action returns the settle window of the requests it starts, which
    `settle` waits on (`aqa_runner.settling`).

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
        # Before the page's first navigation: `open_browser_session` hands
        # over a blank page.
        self._traffic = Traffic(page)
        # https://playwright.dev/python/docs/api/class-page#page-event-crash
        self._crashed = False
        page.on("crash", self._note_crash)

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

    async def navigate(self, url: str) -> Window:
        """Load `url`, an absolute URL on one of the run's allowed origins. Any
        other is a policy event (kind `navigation`), refused before anything
        is requested. The current page needn't be on an allowed origin:
        navigating is the way back after a policy event. The page it lands
        on, after any redirects, is checked as every page is. A network
        failure raises Playwright's `Error` as it is; the egress gate's
        records tell an egress block from an infrastructure failure.

        Returns the settle window of the requests it starts, as every action
        does: a refused action starts none."""
        async with self._turn:
            origin = navigable_origin(url)
            if origin not in self._policy.allowed_origins:
                raise self._refuse(PolicyEvent("navigation", url, origin))
            window = self._traffic.next_window()
            # https://playwright.dev/python/docs/api/class-page#page-goto
            await self.page.goto(url)
            await self._require_allowed_page()
            return window

    async def reload(self) -> Window:
        """Reload the page, which must be on one of the run's allowed origins,
        and check the page it lands on."""
        async with self._turn:
            await self._require_allowed_page()
            window = self._traffic.next_window()
            # https://playwright.dev/python/docs/api/class-page#page-reload
            await self.page.reload()
            await self._require_allowed_page()
            return window

    async def click(self, element: ElementHandle) -> Window:
        """Click `element`, from `locate` or `resolve`, once the page and the
        element's frame are checked, and Playwright will check what its click
        hits."""
        async with self._turn:
            await self._require_actionable(element)
            await self._require_hit_check(element)
            window = self._traffic.next_window()
            # https://playwright.dev/python/docs/api/class-elementhandle#element-handle-click
            await element.click()
            return window

    async def fill(self, element: ElementHandle, value: str) -> Window:
        """Fill `element` with `value`, once the page and the element's frame
        are checked. The value goes in inside the element's own document, in
        one script, and is read back; Playwright's `Error` (whose message
        never holds the value) says the element didn't take it.

        Not Playwright's own fill: it focuses the field, then inserts the
        text with the page's keyboard in a second call, into whichever frame
        has the focus by then, and another origin's frame can take it."""
        async with self._turn:
            await self._require_actionable(element)
            window = self._traffic.next_window()
            if not await element.evaluate(FILL, value):
                raise Error(
                    "fill: the element didn't take the value: it takes no text, "
                    "or its page changed the value"
                )
            return window

    async def fill_secret(self, element: ElementHandle, secret: BoundSecret) -> Window:
        """Fill `element` with `secret`'s value, as `fill` fills, once the page
        and the element's frame are checked as for every action, and only
        where its binding allows (ADR-0026, Test secrets; SECURITY §5): the
        page is on one of its destinations, the element's frame and every
        frame around it are on the page's own origin, each reached by its
        parent, and the element is the field the binding names. Otherwise
        `SecretRefusedError`, before anything is filled.

        A fill that fails raises Playwright's `Error` with a message of ours,
        and keeps nothing of Playwright's: the page's own scripts can throw
        back the value they were handed."""
        async with self._turn:
            frame = await self._require_actionable(element)
            page = await frame_origin(self.page.main_frame)
            origins = secret.destination.origins
            if page not in origins:
                raise SecretRefusedError(
                    f"fill_secret refused {secret.name}: the page is on {page}, "
                    f"which isn't one of its destinations: {', '.join(origins)}"
                )
            if (outside := await self._outside_the_page(frame, page)) is not None:
                raise SecretRefusedError(
                    f"fill_secret refused {secret.name}: the field is in {outside}, "
                    f"not on the page's origin, {page}"
                )
            if not await field_matches(frame, element, secret.destination.field):
                raise SecretRefusedError(
                    f"fill_secret refused {secret.name}: the field isn't "
                    f"{described(secret.destination.field)}"
                )
            window = self._traffic.next_window()
            try:
                filled = await element.evaluate(FILL, secret.value.get_secret_value())
            except Error:
                # Playwright's general error type: the page broke the fill,
                # or went. Its message can hold what the page's scripts threw,
                # which can be the value, so it is dropped here, unbound, and
                # the error below has no context to show it.
                filled = False
            if not filled:
                raise Error(
                    f"fill_secret: the field didn't take {secret.name}: it takes no "
                    "text, its page changed the value, or its page broke the fill"
                )
            return window

    async def select(self, element: ElementHandle, option: str) -> Window:
        """Select the option of `element` whose value or label is `option`,
        once the page and the element's frame are checked."""
        async with self._turn:
            await self._require_actionable(element)
            window = self._traffic.next_window()
            # A string matches an option's value or its label:
            # https://playwright.dev/python/docs/api/class-elementhandle#element-handle-select-option
            await element.select_option(option)
            return window

    async def press(self, key: str) -> Window:
        """Press `key` on the keyboard, once the page, the frame whose document
        has the focus, where the key goes, and the element with the focus are
        checked: the element mustn't contain a frame off the allowed origins,
        into which Tab would move the focus. With nothing focused (the body
        or the root has the focus), the key goes to the page.

        `key` is one key, with only modifiers held down before it (`Shift+A`,
        `ControlOrMeta+a`); anything else raises `ValueError`. A key held
        down, such as Tab in `Tab+a`, could move the focus, and the next key
        would follow it unchecked; and Playwright leaves the held keys down
        when the last is empty (`Shift+`)."""
        if not one_key(key):
            raise ValueError(
                f"press takes one key, with only modifiers held before it "
                f"({', '.join(sorted(MODIFIERS))}): {key[:40]!r} isn't that"
            )
        async with self._turn:
            await self._require_allowed_page()
            focus = await self._focus()
            if focus is None:
                # Where the key goes can't be told: refused if it could be a
                # frame off the allowed origins.
                foreign = await self._foreign_frame(self.page.main_frame)
                if foreign is not None:
                    raise self._refuse(PolicyEvent("frame", *foreign))
            else:
                frame, focused = focus
                try:
                    await self._require_allowed(frame, "frame")
                    if focused is not None and not await focused.evaluate(
                        NOTHING_FOCUSED
                    ):
                        await self._require_no_foreign_frame(focused, frame)
                finally:
                    if focused is not None:
                        await focused.dispose()
            window = self._traffic.next_window()
            # https://playwright.dev/python/docs/api/class-keyboard#keyboard-press
            await self.page.keyboard.press(key)
            return window

    def limit_waits(self, *, action_seconds: float, navigation_seconds: float) -> None:
        """Bound how long an action waits (for its element to be actionable,
        or an option to appear) and how long `navigate` and `reload` wait,
        in seconds, instead of Playwright's 30 s for each. A wait holds the
        session's lock.
        https://playwright.dev/python/docs/api/class-page#page-set-default-timeout
        https://playwright.dev/python/docs/api/class-page#page-set-default-navigation-timeout"""
        self.page.set_default_timeout(action_seconds * 1000)
        self.page.set_default_navigation_timeout(navigation_seconds * 1000)

    async def settle(self, window: Window) -> Settled:
        """Wait until the action whose settle window is `window` has settled
        (`aqa_runner.settling.settle`): `"idle"` once its requests have
        finished and the page has been quiet, `"timeout"` once
        `settling.SETTLE_SECONDS` have passed. Each look at the page is an
        observation, so a page off the allowed origins raises
        `PolicyEventError`; nothing else the session raises is caught, and a
        navigation meanwhile counts as the page changing."""
        return await settling.settle(window, self._dom_changed)

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
        and after. If any frame navigated or was removed meanwhile, what the
        lookup saw may be another document's, even when the page is back on
        an allowed origin: it is discarded, a resolved element let go, and
        `DocumentChangedError` raised, as `snapshot` does. The caller owns a
        resolved element's handle."""
        async with self._turn:
            changes = self._frame_changes
            await self._require_allowed_page()
            found = await resolve_target(self.page, target, use)
            await self._require_allowed_page()
            if self._frame_changes != changes:
                if isinstance(found, Resolved):
                    await found.element.dispose()
                raise DocumentChangedError
            return found

    async def text_of(self, element: ElementHandle) -> str:
        """`element`'s rendered text, as `text_in_target` reads it, once the
        page and the element's frame are checked: its innerText, or nothing
        for an element the page doesn't render, whose innerText would be its
        text content (`RENDERED_TEXT` says which elements count as rendered).
        One evaluation in the page's world decides both, so it is the page's
        word, as everything a page renders is. The element is held, so it
        can't read a document that replaced its own."""
        async with self._turn:
            await self._require_allowed_element(element)
            text = await element.evaluate(RENDERED_TEXT)
            return text if isinstance(text, str) else ""

    async def visible_text(self) -> str:
        """The page's rendered text, as `text_visible` reads it: its body's,
        as `text_of` reads an element's, empty when the page has no body.
        Only the page's own document: rendered text never enters a frame,
        an allowed origin's included.

        An observation, so the page is checked before and after. If any
        frame navigated or was removed meanwhile, the text may be another
        document's, even when the page is back on an allowed origin, so it
        is discarded and `DocumentChangedError` raised, as `resolve` does."""
        async with self._turn:
            changes = self._frame_changes
            await self._require_allowed_page()
            # https://playwright.dev/python/docs/api/class-page#page-query-selector
            body = await self.page.query_selector("body")
            if body is None:
                text = ""
            else:
                try:
                    text = await rendered_text(body)
                finally:
                    await body.dispose()
            await self._require_allowed_page()
            if self._frame_changes != changes:
                raise DocumentChangedError
            return text

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

    async def _dom_changed(self) -> bool:
        """Whether the page's document changed since the last look, which
        runs a script in the page, once the page is checked. The script runs
        in the page's world, so the answer is the page's word."""
        async with self._turn:
            await self._require_allowed_page()
            try:
                return bool(await self.page.evaluate(settling.DOM_CHANGED))
            except Error:
                # Playwright's general error type: here, a navigation replaced
                # the document mid-look, or the page's own scripts broke the
                # look. Either is the page's doing, and whether it changed
                # can't be told, so it counts as changing. A closed or
                # crashed page is not.
                if self.page.is_closed() or self._crashed:
                    raise
                return True

    def _note_crash(self, _: Page) -> None:
        self._crashed = True

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

    async def _require_actionable(self, element: ElementHandle) -> Frame:
        """Record and raise a policy event unless the page, and the frame of
        `element`, are on the run's allowed origins, and `element` contains
        no frame that isn't, at any depth; that frame otherwise. An element
        in no frame is on none.

        An action lands wherever the browser draws at the point it uses:
        Playwright's hit check accepts the element or any descendant, and
        every point over a frame hits the frame's element, so a click on an
        element around another origin's frame lands in that frame."""
        frame = await self._require_allowed_element(element)
        await self._require_no_foreign_frame(element, frame)
        return frame

    async def _outside_the_page(self, frame: Frame, page: str | None) -> str | None:
        """Which of `frame` and the frames around it, up to the page, isn't
        on `page`, the page's origin, as a refusal says it; None when all
        are. Each must also be one its parent reaches: a frame at a URL on
        the page's origin is on an opaque origin when it is sandboxed, by
        its frame's attribute or its response's CSP, which is where an app
        puts content it doesn't trust. The origins are checked first, so
        only documents on the page's origin are asked whether they reach."""
        frames = []
        while frame.parent_frame is not None:
            frames.append(frame)
            frame = frame.parent_frame
        for framed in frames:
            if (here := await frame_origin(framed)) != page:
                return f"a frame on {here or 'no origin a run could allow'}"
        for framed in frames:
            if not await reaches(framed):
                return "a frame the page can't reach"
        return None

    async def _require_allowed_element(self, element: ElementHandle) -> Frame:
        """Record and raise a policy event unless the page, and the frame of
        `element`, are on the run's allowed origins; that frame otherwise.
        An element in no frame is on none."""
        await self._require_allowed_page()
        frame = await element.owner_frame()
        if frame is None:
            raise self._refuse(PolicyEvent("frame", "", None))
        await self._require_allowed(frame, "frame")
        return frame

    async def _require_hit_check(self, element: ElementHandle) -> None:
        """Record and raise a policy event if Playwright's click on `element`
        would check nothing it hits while the page has a frame off the
        allowed origins, which could be drawn at the click's point.

        Playwright 1.63 makes no hit check for an element in a frame whose
        element, or one around it, is transformed (`_checkFrameIsHitTarget`
        and `describeIFrameStyle`): the click lands in whatever is drawn at
        its point."""
        frame = await element.owner_frame()
        while frame is not None and frame.parent_frame is not None:
            owner = await frame.frame_element()
            try:
                transformed = await owner.evaluate(TRANSFORMED)
            finally:
                await owner.dispose()
            if transformed:
                foreign = await self._foreign_frame(self.page.main_frame)
                if foreign is not None:
                    raise self._refuse(PolicyEvent("frame", *foreign))
                return
            frame = frame.parent_frame

    async def _require_no_foreign_frame(
        self, element: ElementHandle, frame: Frame
    ) -> None:
        """Record and raise a policy event if `element`, in `frame`, contains a
        frame, at any depth, that isn't on one of the run's allowed origins."""
        for child in frame.child_frames:
            foreign = await self._foreign_frame(child)
            if foreign is None:
                continue
            owner = await child.frame_element()
            try:
                inside = await element.evaluate(CONTAINS, owner)
            finally:
                await owner.dispose()
            if inside:
                raise self._refuse(PolicyEvent("frame", *foreign))

    async def _foreign_frame(self, frame: Frame) -> tuple[str, str | None] | None:
        """The URL and origin of the first frame, `frame` or one inside it,
        that isn't on one of the run's allowed origins; None when all are."""
        origin = await frame_origin(frame)
        if origin not in self._policy.allowed_origins:
            return frame.url, origin
        for child in frame.child_frames:
            if (foreign := await self._foreign_frame(child)) is not None:
                return foreign
        return None

    async def _focus(self) -> tuple[Frame, ElementHandle | None] | None:
        """Where a key would go, asking only documents on the run's allowed
        origins, from the page down: the frame, and its focused element when
        that is no frame's element; None when that can't be told.

        It descends into the allowed child frame whose document has the
        focus (`document.hasFocus()`). Where none has, the focus is in the
        frame itself, unless its focused element is a frame's: then in that
        frame, which isn't allowed, and so isn't asked. Chromium can leave a
        document's `activeElement` on a frame that lost the focus to a
        sibling, so `activeElement` alone can't lead the way down, and when
        it names an allowed frame without the focus, the focus is somewhere
        no allowed document says."""
        frame = self.page.main_frame
        while (child := await self._focused_child(frame)) is not None:
            frame = child
        focused = await frame.evaluate_handle("document.activeElement")
        element = focused.as_element()
        if element is None:
            await focused.dispose()
            return frame, None
        child = await element.content_frame()
        if child is None:
            return frame, element
        await element.dispose()
        if await frame_origin(child) in self._policy.allowed_origins:
            return None
        return child, None

    async def _focused_child(self, frame: Frame) -> Frame | None:
        """The child frame of `frame` on an allowed origin whose document has
        the focus, if one has."""
        for child in frame.child_frames:
            if await frame_origin(child) not in self._policy.allowed_origins:
                continue
            if await child.evaluate("document.hasFocus()") is True:
                return child
        return None

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


def one_key(key: str) -> bool:
    """Whether `key` is what `BrowserSession.press` takes: one key, with only
    modifiers held down before it."""
    *held, pressed = press_keys(key)
    return bool(pressed) and set(held) <= MODIFIERS


def press_keys(key: str) -> list[str]:
    """`key` split into the keys Playwright's press holds down and then the
    key it presses, as Playwright 1.63's Keyboard.press splits it: a `+`
    ends a key only after one, so `Shift++` is Shift and `+`."""
    keys: list[str] = []
    building = ""
    for char in key:
        if char == "+" and building:
            keys.append(building)
            building = ""
        else:
            building += char
    return [*keys, building]


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
