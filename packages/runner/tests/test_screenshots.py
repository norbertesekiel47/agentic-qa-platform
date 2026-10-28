"""Screenshots in the run record (#171, ADR-0034): once a test secret has
been filled in the browser session, a screenshot is a render of a scrubbed
snapshot, so no hostile page can make a bound field show its value in a
saved PNG; before that, and in a run that binds nothing, it is the live
page's. Each page draws the bound value in pure red, which nothing else on
it is. The browser tests launch real Chromium, sandboxed, on the OS that
runs them: Linux in CI, macOS locally."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from aqa_core.browser import BrowserSettings
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import (
    Screenshot,
    ScreenshotWithheldError,
    take_screenshot,
)
from aqa_runner.secret_fields import SecretNotFilledError
from playwright.async_api import ElementHandle, Error, Page

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.screenshot_pages import (
    CASES,
    RED_VALUE,
    SCALED,
    UNBOUND,
    Case,
    armed,
    bound,
    red_pixels,
    serve_cases,
)

# Captures of each hostile page with the bound value in, as the prototype
# took (reviews/171-prototype.md): a page that alternates gets as many
# chances.
REPEATS = 8


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def secret(sites: Sites, name: str) -> BoundSecret:
    """The secret `screenshot_pages.bound` binds as `name` on the app's origin."""
    return next(held for held in bound(sites.app) if held.name == name)


async def element(session: BrowserSession, selector: str) -> ElementHandle:
    """The element `selector` finds in the page's top document."""
    return await session.page.locator(selector).element_handle()


async def field(session: BrowserSession, case: Case) -> ElementHandle:
    """The element `case`'s value goes into."""
    match case.into:
        case "frame" | "cross-site":
            return (
                await session.page.frame_locator("#fr").locator("#f").element_handle()
            )
        case "second":
            return await element(session, "#f2")
        case "editable":
            return await element(session, "#ce")
        case _:
            return await element(session, "#f")


async def scratch_page(session: BrowserSession) -> Page:
    """A page of its own in the session's browser, to decode PNGs in."""
    browser = session.page.context.browser
    assert browser is not None
    return await browser.new_page()


def test_a_screenshot_cant_be_made_from_bytes() -> None:
    with pytest.raises(TypeError):
        Screenshot(b"\x89PNG\r\n\x1a\n", Redactor([]))


def test_a_run_that_binds_nothing_screenshots_the_live_page(sites: Sites) -> None:
    async def scenario() -> int:
        async with browsing(sites) as session:
            await serve_cases(session.page, sites.other)
            await session.navigate(f"{sites.app}/shot/canvas")
            await session.fill(await element(session, "#f2"), RED_VALUE)
            await session.page.evaluate("arm()")
            shot = await take_screenshot(session)
            return await red_pixels(await scratch_page(session), shot.png)

    # The canvas is drawn only on the live page: a render has none.
    assert asyncio.run(scenario()) > 0


def test_until_a_secret_is_filled_the_screenshot_is_the_live_page(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[int, int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            scratch = await scratch_page(session)
            await session.navigate(f"{sites.app}/shot/canvas")
            await session.page.evaluate(
                "const x = c.getContext('2d'); x.fillStyle = '#f00'; x.fillRect(0, 0, 50, 50)"
            )
            before = await red_pixels(scratch, (await take_screenshot(session)).png)
            trigger = await element(session, "#trigger")
            await session.fill_secret(trigger, secret(sites, "TRIGGER"))
            after = await red_pixels(scratch, (await take_screenshot(session)).png)
            return before, after

    assert asyncio.run(scenario()) == (2500, 0)


def test_a_screenshot_goes_only_to_a_record_with_its_sessions_scan(
    sites: Sites, tmp_path: Path
) -> None:
    async def scenario() -> Screenshot:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            await session.navigate(f"{sites.app}/shot/plain")
            return await take_screenshot(session)

    shot = asyncio.run(scenario())
    record = RunRecord.create(tmp_path).redacting(Redactor(bound(sites.app)))
    with pytest.raises(ValueError, match="its session's redactor"):
        record.write_screenshot("shots/0.png", shot)
    assert list(record.path.iterdir()) == []


async def control(
    session: BrowserSession, sites: Sites, name: str, served: dict[str, str]
) -> bytes:
    """The case's page with `UNBOUND` where the bound value goes. Another
    secret is filled first, so the render runs, except on an editable
    case, which a render empties: there nothing is filled, so the
    screenshot is the live page's."""
    case = CASES[name]
    served["value"] = UNBOUND
    await session.navigate(f"{sites.app}/shot/{name}")
    if case.into != "editable":
        trigger = await element(session, "#trigger")
        await session.fill_secret(trigger, secret(sites, "TRIGGER"))
    if case.into != "page":
        if case.partial:
            with pytest.raises(Error):
                await session.fill(await field(session, case), UNBOUND)
        else:
            await session.fill(await field(session, case), UNBOUND)
    script = case.arm if case.control is True else case.control
    if script is not None:
        await session.page.evaluate(armed(script, UNBOUND))
    await asyncio.sleep(0.3)
    return (await take_screenshot(session)).png


async def attack(
    session: BrowserSession,
    sites: Sites,
    name: str,
    served: dict[str, str],
    record: RunRecord,
) -> list[str]:
    """The case's page with its bound value in and its attack started,
    captured `REPEATS` times into `record`: each capture's file, or
    "withheld"."""
    case = CASES[name]
    filled = secret(sites, case.binding)
    value = filled.value.get_secret_value()
    served["value"] = value
    await session.navigate(f"{sites.app}/shot/{name}")
    if case.into in ("page", "cross-site"):
        trigger = await element(session, "#trigger")
        await session.fill_secret(trigger, secret(sites, "TRIGGER"))
    if case.into == "cross-site":
        # fill_secret never fills another origin's frame, so the value gets
        # there as a page would put it.
        await session.fill(await field(session, case), value)
    elif case.into != "page":
        if case.partial:
            with pytest.raises(SecretNotFilledError):
                await session.fill_secret(await field(session, case), filled)
        else:
            await session.fill_secret(await field(session, case), filled)
    if case.arm is not None:
        await session.page.evaluate(armed(case.arm, value))
    await asyncio.sleep(0.3)
    outcomes = []
    for repeat in range(REPEATS):
        try:
            shot = await take_screenshot(session)
        except ScreenshotWithheldError:
            outcomes.append("withheld")
            continue
        record.write_screenshot(f"shots/{repeat}.png", shot)
        outcomes.append(f"shots/{repeat}.png")
    return outcomes


@pytest.mark.parametrize(
    ("name", "scale"),
    [*((name, 1) for name in CASES), *((name, 2) for name in SCALED)],
)
def test_a_saved_screenshot_shows_no_bound_value_whatever_the_page_does(
    sites: Sites, tmp_path: Path, name: str, scale: int
) -> None:
    async def scenario() -> tuple[int, list[str], list[str], list[int]]:
        async with browsing(
            sites,
            redactor=Redactor(bound(sites.app)),
            settings=BrowserSettings(device_scale_factor=scale),
        ) as session:
            served = await serve_cases(session.page, sites.other)
            scratch = await scratch_page(session)
            shown = await red_pixels(
                scratch, await control(session, sites, name, served)
            )
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            outcomes = await attack(session, sites, name, served, record)
            saved = sorted(
                path.relative_to(record.path).as_posix()
                for path in record.path.rglob("*")
                if path.is_file()
            )
            reds = [
                await red_pixels(scratch, (record.path / file).read_bytes())
                for file in saved
            ]
            return shown, outcomes, saved, reds

    shown, outcomes, saved, reds = asyncio.run(scenario())
    assert shown > 0
    assert saved == sorted(outcome for outcome in outcomes if outcome != "withheld")
    assert reds == [0] * len(saved)
    if not CASES[name].withholds:
        assert outcomes == [f"shots/{repeat}.png" for repeat in range(REPEATS)]
