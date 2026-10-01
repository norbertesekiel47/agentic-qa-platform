"""The browser session every run uses, for explore and replay alike: a fresh
browser launched through the sandbox check, with an empty environment and the
run's browser settings, and an accessibility snapshot whose element refs the
agent's tools act on (ADR-0025, ADR-0026 and its 2026-10-01 amendment)."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aqa_core.browser import BrowserSettings
from playwright.async_api import Locator, Page

from aqa_runner.sandbox import Chromium, launch

# The settings every run uses unless the caller passes its own (ADR-0025).
PINNED = BrowserSettings()

# A ref in Playwright's AI snapshot: `e12`, or `f2e12` in a frame or in a later
# document of the page. The same pattern in page text is rewritten too, so no
# token in a snapshot keeps Playwright's numbering.
PLAYWRIGHT_REF = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")

# A ref this session gives.
SESSION_REF = re.compile(r"e([1-9]\d*)")


class RefError(LookupError):
    """A ref the current snapshot didn't give: one from an older snapshot, or
    one never given."""


class BrowserSession:
    """One run's browser: a fresh Chromium process and the page the run uses.

    Playwright's refs aren't unique within a page: it numbers them afresh in
    each new document, and keeps a frame's prefix across some of them (a
    page's first navigation away from about:blank, any iframe's), so an old
    ref can name a different element (LAB_NOTES, 2026-10-01). The session
    gives each element ref of each snapshot a number it never gives again, and
    resolves only the current snapshot's."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self._given = 0
        self._first = 1
        self._current: dict[str, str] = {}

    async def snapshot(self) -> str:
        """The page's accessibility snapshot in Playwright's AI mode
        (https://playwright.dev/python/docs/api/class-page#page-aria-snapshot),
        with this session's refs. It retires every earlier snapshot's refs."""
        text = await self.page.aria_snapshot(mode="ai")
        first = self._given + 1
        ours: dict[str, str] = {}

        def renumber(found: re.Match[str]) -> str:
            if found[1] not in ours:
                self._given += 1
                ours[found[1]] = f"e{self._given}"
            return f"[ref={ours[found[1]]}]"

        text = PLAYWRIGHT_REF.sub(renumber, text)
        self._first = first
        self._current = {ref: theirs for theirs, ref in ours.items()}
        return text

    def locate(self, ref: str) -> Locator:
        """The element `ref` names in the current snapshot. A ref that snapshot
        didn't give raises `RefError`, so no other string reaches a selector."""
        theirs = self._current.get(ref)
        if theirs is not None:
            # Playwright's built-in aria-ref selector engine, which resolves a
            # ref against the frame's latest snapshot. It isn't in Playwright's
            # public docs; the session's tests pin its behavior on 1.63.
            return self.page.locator(f"aria-ref={theirs}")
        given = SESSION_REF.fullmatch(ref)
        if given is not None and int(given[1]) < self._first:
            raise RefError(
                f"{ref} is from an older snapshot: a ref acts only on the "
                "snapshot that gave it, so take a new snapshot"
            )
        raise RefError(f"{ref} isn't a ref in the current snapshot")


@asynccontextmanager
async def open_session(
    chromium: Chromium, *, settings: BrowserSettings = PINNED
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
