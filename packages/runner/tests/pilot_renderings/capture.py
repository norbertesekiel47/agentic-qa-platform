"""Captures the saved renderings of the pilot's pages from the local Conduit
stack, run by hand, never by the tests (ADR-0025's 2026-10-02 amendment,
"generating locators"):

    cd bench/apps/conduit && docker compose up --build --wait
    uv run python -m packages.runner.tests.pilot_renderings.capture

It resets the clean app, walks the pages the five pilot specs visit through a
browser session, and writes each page's DOM, without its scripts and links,
beside this file. The tests put Conduit's own stylesheets back
(`packages/runner/tests/pilot_pages.py`)."""

import asyncio
import http.client
import re
from pathlib import Path

from aqa_runner.browser_session import BrowserSession, open_browser_session
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy

APP = "http://127.0.0.1:4100"
HERE = Path(__file__).parent
CONDUIT = HERE.parents[3] / "bench/apps/conduit"

# The page's DOM as it is now, without what would load or run anything.
_SERIALIZE = """() => {
    const root = document.documentElement.cloneNode(true);
    for (const node of root.querySelectorAll("script, link, base, noscript")) node.remove();
    return "<!doctype html>\\n" + root.outerHTML + "\\n";
}"""


def seeded_password() -> str:
    """The seeded accounts' public fixture password, from the compose file
    that builds them (bench/apps/conduit/README.md)."""
    compose = (CONDUIT / "compose.yaml").read_text()
    found = re.search(r"CONDUIT_SEED_PASSWORD: (\S+)", compose)
    assert found is not None, "compose.yaml no longer sets CONDUIT_SEED_PASSWORD"
    return found[1]


def reset() -> None:
    """The app's seeded state (`POST /test-api/reset?fixture=seed`)."""
    connection = http.client.HTTPConnection("127.0.0.1", 4100)
    try:
        connection.request("POST", "/test-api/reset?fixture=seed")
        assert connection.getresponse().status == 204
    finally:
        connection.close()


async def save(session: BrowserSession, name: str) -> None:
    rendering: str = await session.page.evaluate(_SERIALIZE)
    (HERE / f"{name}.html").write_text(rendering)


async def sign_in(session: BrowserSession, email: str) -> None:
    page = session.page
    await page.goto(f"{APP}/login")
    await page.fill("input[name=email]", email)
    await page.fill("input[name=password]", seeded_password())
    await page.locator("button[type=submit]:not([disabled])").wait_for()


async def capture() -> None:
    reset()
    async with (
        async_playwright() as playwright,
        egress_proxy(APP) as egress,
        open_browser_session(playwright.chromium, egress=egress) as session,
    ):
        page = session.page
        await page.goto(f"{APP}/article/testing-without-flakes")
        await page.locator("app-article-comment").first.wait_for()
        await save(session, "article-signed-out")

        await sign_in(session, "reader@conduit.test")
        await save(session, "login")
        await page.click("button[type=submit]")
        await page.get_by_role("link", name="Your Feed").wait_for()
        await page.locator("app-article-preview a.preview-link").first.wait_for()
        await save(session, "home")

        await page.goto(f"{APP}/article/flaky-tests-are-bugs")
        favorite = page.locator(".banner app-favorite-button button")
        await favorite.wait_for()
        await save(session, "article")
        await favorite.click()
        await page.locator(".banner", has_text="Unfavorite Article").wait_for()
        await page.reload()
        await page.locator(".banner", has_text="Unfavorite Article").wait_for()
        await save(session, "article-favorited")

        await page.evaluate("() => localStorage.clear()")
        await sign_in(session, "jake@conduit.test")
        await page.click("button[type=submit]")
        await page.get_by_role("link", name="Your Feed").wait_for()
        await page.goto(f"{APP}/editor")
        await page.fill("input[name=title]", "Benchmarks we trust")
        await page.fill("input[name=description]", "How we measure agents.")
        await page.fill(
            "textarea[name=body]", "Every number cites the commit that produced it."
        )
        for tag in ("testing", "benchmarks"):
            await page.fill("input[placeholder='Enter tags']", tag)
            await page.press("input[placeholder='Enter tags']", "Enter")
        await page.locator(".tag-list .tag-pill").nth(1).wait_for()
        await save(session, "editor")
    reset()


if __name__ == "__main__":
    asyncio.run(capture())
