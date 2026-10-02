"""The pilot's pages, for the locator generator's tests: saved renderings of
the clean Conduit app, whose pilot cases are all on the dev split (ADR-0025's
2026-10-02 amendment, "generating locators").

`pilot_renderings/capture.py` captured them on 2026-10-02 from the local stack
(`bench/apps/conduit` at 5fcaada, clean, freshly reset), with Playwright 1.63.0
through a browser session. Each file is the page's DOM without its scripts,
links and `base`, so loading one fetches and runs nothing. `show` puts back
Conduit's own two stylesheets, read from the vendored app, whose icon font's
glyphs reach accessible names (LAB_NOTES, 2026-09-29).

The pages, by the specs that visit them:
- `login`: /login signed out, the form filled in, so Sign in is enabled;
- `home`: / signed in as reader;
- `article`: /article/flaky-tests-are-bugs signed in as reader, and
  `article-favorited`, the same after favoriting it and reloading;
- `article-signed-out`: /article/testing-without-flakes signed out;
- `editor`: /editor signed in as jake, filled in with two tags."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from aqa_runner.browser_session import BrowserSession, open_browser_session
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy

RENDERINGS = Path(__file__).parent / "pilot_renderings"
PAGES = (
    "login",
    "home",
    "article",
    "article-favorited",
    "article-signed-out",
    "editor",
)
_FRONTEND = Path(__file__).parents[3] / "bench/apps/conduit/frontend"
_STYLESHEETS = (
    _FRONTEND / "realworld/assets/theme/styles.css",
    _FRONTEND / "src/vendor/ionicons/css/ionicons.min.css",
)


async def show(session: BrowserSession, page: str) -> None:
    """Load the pilot page `page` into the session, styled as the app is."""
    await session.page.set_content((RENDERINGS / f"{page}.html").read_text())
    for stylesheet in _STYLESHEETS:
        await session.page.add_style_tag(path=stylesheet)


def in_session[T](scenario: Callable[[BrowserSession], Awaitable[T]]) -> T:
    """`scenario`'s result in a fresh browser session."""

    async def run() -> T:
        async with (
            async_playwright() as playwright,
            egress_proxy() as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            return await scenario(session)

    return asyncio.run(run())
