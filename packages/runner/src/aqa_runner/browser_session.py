"""The browser session every run uses, for explore and replay alike: a fresh
browser launched through the sandbox check, with an empty environment and the
run's browser settings, and an accessibility snapshot whose element refs the
agent's tools act on (ADR-0025, ADR-0026 and its 2026-10-01 amendment)."""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from aqa_core.browser import BrowserSettings
from playwright.async_api import ElementHandle, Page

from aqa_runner.sandbox import Chromium, launch

# The settings every run uses unless the caller passes its own (ADR-0025).
PINNED_SETTINGS = BrowserSettings()

# One line of Playwright's AI snapshot: `- ` and a key, then `:` and a value or
# children, or nothing. Playwright single-quotes a key YAML would misread, so an
# unquoted key ends at the first colon that a space or the line's end follows.
LINE = re.compile(
    r"^(?P<head> *- )?(?P<key>'(?:[^'\n]|'')*'|(?:[^:\n]|:(?=\S))*)(?P<rest>.*)$",
    re.MULTILINE,
)

# An element's own ref ends its key, followed by nothing but these attributes.
# Page text can't end a key so: a name is JSON-quoted or written /like this/.
ELEMENT_REF = re.compile(
    r"\[ref=((?:f[0-9]+)?e[0-9]+)\]((?: \[cursor=pointer\])?(?: \[box=[^\]]*\])?)\Z"
)

# Anything else shaped like a ref, which only page text writes.
TEXT_REF = re.compile(r"\[ref=([^\]\n]*)\]")

# A ref this session gives.
SESSION_REF = re.compile(r"e([1-9][0-9]{0,17})")


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
    every accessibility snapshot of the page goes through `snapshot`."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self._refs_given = 0
        self._first_current_ref = 1
        self._current: dict[str, str] = {}
        self._snapshotting = asyncio.Lock()

    async def snapshot(self) -> str:
        """The page's accessibility snapshot in Playwright's AI mode
        (https://playwright.dev/python/docs/api/class-page#page-aria-snapshot),
        with this session's refs, unredacted. It retires every earlier
        snapshot's refs, and page text shaped like a ref reads `(ref=…)`."""
        async with self._snapshotting:
            # Retired before the call: Playwright may store this snapshot, and
            # resolve refs against it, even if the call never returns.
            self._current = {}
            self._first_current_ref = self._refs_given + 1
            current: dict[str, str] = {}

            def give(playwright_ref: str) -> str:
                self._refs_given += 1
                ref = f"e{self._refs_given}"
                current[ref] = playwright_ref
                return ref

            text = renumber(await self.page.aria_snapshot(mode="ai"), give)
            self._current = current
            return text

    async def locate(self, ref: str) -> ElementHandle:
        """The element `ref` names in the current snapshot, held: it never
        becomes another element, and acting on it fails once it has left the
        page. Any other ref raises `RefError`, so no string but a current ref
        reaches a selector."""
        playwright_ref = self._current.get(ref)
        if playwright_ref is None:
            given = SESSION_REF.fullmatch(ref)
            if given is not None and int(given[1]) < self._first_current_ref:
                raise RefError(
                    f"{ref!r} is from an older snapshot: a ref acts only on the "
                    "snapshot that gave it, so take a new snapshot"
                )
            raise RefError(f"{ref[:40]!r} isn't a ref in the current snapshot")
        # Playwright's built-in aria-ref selector engine. It isn't in
        # Playwright's public docs; the session's tests pin its behavior on 1.63.
        found = await self.page.locator(f"aria-ref={playwright_ref}").element_handles()
        if not found:
            raise RefError(f"{ref!r} names an element that has left the page")
        return found[0]


def renumber(snapshot: str, give: Callable[[str], str]) -> str:
    """`snapshot` with each element's own ref replaced by `give(ref)`, and
    every other `[ref=…]`, which page text wrote, rewritten as `(ref=…)`."""

    def line(found: re.Match[str]) -> str:
        head, key, rest = found["head"] or "", found["key"], found["rest"]
        quote = "'" if key.startswith("'") else ""
        inner = key[len(quote) : len(key) - len(quote)]
        own = ELEMENT_REF.search(inner) if head else None
        if own is None:
            return head + as_text(key) + as_text(rest)
        return (
            f"{head}{quote}{as_text(inner[: own.start()])}[ref={give(own[1])}]"
            f"{own[2]}{quote}{as_text(rest)}"
        )

    return LINE.sub(line, snapshot)


def as_text(text: str) -> str:
    """`text` with every ref-shaped token made unlike a ref."""
    return TEXT_REF.sub(r"(ref=\1)", text)


@asynccontextmanager
async def open_browser_session(
    chromium: Chromium, *, settings: BrowserSettings = PINNED_SETTINGS
) -> AsyncIterator[BrowserSession]:
    """Launch a fresh browser through the sandbox check, open one page with
    `settings` and downloads refused, and close the browser, its temporary
    profile with it, when the session ends."""
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
        )
        yield BrowserSession(await context.new_page())
    finally:
        await browser.close()
