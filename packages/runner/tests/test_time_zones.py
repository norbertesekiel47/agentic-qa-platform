"""Every time zone the check accepts opens in Chromium (ADR-0025's 2026-10-02
amendment, #90). These tests launch real Chromium on the OS that runs them."""

import asyncio
from typing import Any

from aqa_core.browser import BrowserSettings, time_zones
from aqa_runner.sandbox import launch
from playwright.async_api import Browser, CDPSession, Error, Page, async_playwright

# What a page reports about its time zone. Chromium reports some zones under
# another name (LAB_NOTES, 2026-10-01), so tests compare reports across two ways
# of setting a zone, never a report with the name set.
REPORT = """() => ({
    zone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    offset_minutes_1970: new Date(0).getTimezoneOffset(),
    offset_minutes_2026_07_01: new Date(Date.UTC(2026, 6, 1)).getTimezoneOffset(),
})"""

# Names the check refuses and Chromium must too.
REFUSED = {"Mars/Phobos", "utc", "localtime"}


async def override_accepts(session: CDPSession, name: str) -> bool:
    """Whether Chromium applies `name` as the page's time zone through CDP's
    Emulation.setTimezoneOverride, what Playwright's `timezone_id` does for each
    page of a context. One page and one command per zone take about a second for
    the whole list, where a context per zone takes about twenty.

    Chromium checks the id only while no override is in effect: with one set, an
    invalid id is accepted and ignored (LAB_NOTES, 2026-10-02). An empty id
    clears the override, so each name is checked as if on a fresh page
    (https://chromedevtools.github.io/devtools-protocol/tot/Emulation/#method-setTimezoneOverride)."""
    try:
        await session.send("Emulation.setTimezoneOverride", {"timezoneId": ""})
        await session.send("Emulation.setTimezoneOverride", {"timezoneId": name})
    except Error:
        return False
    return True


async def report_with_override(page: Page, session: CDPSession, name: str) -> Any:
    """The page's report after the override sets `name`, or None when Chromium
    refuses it."""
    return (
        await page.evaluate(REPORT) if await override_accepts(session, name) else None
    )


async def report_with_timezone_id(browser: Browser, name: str) -> Any:
    """The report of a page in a context opened with `timezone_id=name`, as
    `open_browser_session` opens one
    (https://playwright.dev/python/docs/api/class-browser#browser-new-context),
    or None when Chromium refuses the name. It refuses at the page, not the context."""
    context = await browser.new_context(timezone_id=name)
    try:
        return await (await context.new_page()).evaluate(REPORT)
    except Error:
        return None
    finally:
        await context.close()


def test_every_time_zone_the_check_accepts_opens_in_chromium() -> None:
    zones = sorted(time_zones())

    async def scenario() -> list[str]:
        async with async_playwright() as playwright:
            browser = await launch(playwright.chromium)
            try:
                context = await browser.new_context()
                session = await context.new_cdp_session(await context.new_page())
                return [
                    zone
                    for zone in zones
                    if not await override_accepts(
                        session, BrowserSettings(timezone=zone).timezone
                    )
                ]
            finally:
                await browser.close()

    # The issue measured 598 zones; the floor only keeps the loop from passing empty.
    assert len(zones) > 500
    assert asyncio.run(scenario()) == []


def test_the_override_refuses_and_reports_what_timezone_id_does_on_a_sample() -> None:
    # Every 30th zone, then the refused names: set after a valid override is in
    # effect, which is where Chromium stops checking an id.
    names = [*sorted(time_zones())[::30], *sorted(REFUSED)]

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with async_playwright() as playwright:
            browser = await launch(playwright.chromium)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                session = await context.new_cdp_session(page)
                via_override = {
                    name: await report_with_override(page, session, name)
                    for name in names
                }
                via_timezone_id = {
                    name: await report_with_timezone_id(browser, name) for name in names
                }
                return via_override, via_timezone_id
            finally:
                await browser.close()

    via_override, via_timezone_id = asyncio.run(scenario())

    assert via_override == via_timezone_id
    assert {name for name, report in via_timezone_id.items() if report is None} == (
        REFUSED
    )
