"""The capture's attempts (#337, ADR-0034's #337 amendment): when DevTools
answers that it couldn't capture the page's surface, which Chromium does
under CPU load, `take_screenshot` makes a whole new capture, up to
`CAPTURE_ATTEMPTS` in all. No other ending is tried again."""

import asyncio
import html
import struct
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aqa_runner import ocr, screenshots
from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import take_screenshot
from playwright.async_api import BrowserContext, CDPSession, Error, Frame, Page

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.screenshot_pages import (
    RED_VALUE,
    bound,
    red_filled,
    serve_cases,
)
from packages.runner.tests.test_screenshot_withholds import withheld

# What Playwright 1.63 raises, word for word, when Chromium 153's
# `Page.captureScreenshot` finds the page's surface empty: seen under load
# (LAB_NOTES, 2026-11-20).
UNCAPTURED = (
    "CDPSession.send: Protocol error (Page.captureScreenshot): "
    "Unable to capture screenshot"
)

# Why a capture whose DevTools call failed is withheld.
FAILED = "a DevTools call or the render failed"


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


@dataclass
class Sessions:
    """The DevTools sessions opened, and those detached, in the order they
    were."""

    opened: list[CDPSession] = field(default_factory=list)
    detached: list[CDPSession] = field(default_factory=list)

    def restart(self) -> None:
        """Count from here, so only the capture's own: a navigation and a fill
        open sessions of their own (`document_origins.isolated_origin`)."""
        self.opened.clear()
        self.detached.clear()


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> Sessions:
    found = Sessions()
    new_cdp_session = BrowserContext.new_cdp_session
    detach = CDPSession.detach

    async def opening(context: BrowserContext, page: Page | Frame) -> CDPSession:
        devtools = await new_cdp_session(context, page)
        found.opened.append(devtools)
        return devtools

    async def detaching(devtools: CDPSession) -> None:
        await detach(devtools)
        found.detached.append(devtools)

    monkeypatch.setattr(BrowserContext, "new_cdp_session", opening)
    monkeypatch.setattr(CDPSession, "detach", detaching)
    return found


def uncapturing(monkeypatch: pytest.MonkeyPatch, times: int) -> None:
    """DevTools' `Page.captureScreenshot` raises the fault its first `times`
    calls."""
    send = CDPSession.send
    calls = []

    async def refusing(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureScreenshot":
            calls.append(method)
            if len(calls) <= times:
                raise Error(UNCAPTURED)
        return await send(devtools, method, params)

    monkeypatch.setattr(CDPSession, "send", refusing)


def test_a_capture_devtools_couldnt_take_once_is_taken_again_and_returned(
    sites: Sites,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: Sessions,
) -> None:
    uncapturing(monkeypatch, 1)
    read: list[bytes] = []
    spells_a_value = ocr.spells_a_value

    def reading(png: bytes, redactor: Redactor) -> bool:
        read.append(png)
        return spells_a_value(png, redactor)

    monkeypatch.setattr(ocr, "spells_a_value", reading)

    async def scenario() -> tuple[bytes, tuple[int, int], list[Path], int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            await session.navigate(f"{sites.app}/shot/plain")
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            browser = session.page.context.browser
            assert browser is not None
            sessions.restart()
            shot = await take_screenshot(session)
            record.write_screenshot("shots/0.png", shot)
            return (
                shot.png,
                session.settings.viewport,
                list((record.path / "shots").iterdir()),
                len(browser.contexts) - 1,
            )

    png, viewport, files, contexts = asyncio.run(scenario())
    # A PNG's width and height are the first fields of its header.
    assert struct.unpack(">II", png[16:24]) == viewport
    assert [path.name for path in files] == ["0.png"]
    assert contexts == 0
    assert len(sessions.opened) == 2
    assert sessions.detached == sessions.opened
    # The OCR check read the image the second attempt returned, and no other.
    assert read == [png]


def test_a_capture_devtools_never_takes_is_withheld_after_every_attempt(
    sites: Sites,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: Sessions,
) -> None:
    uncapturing(monkeypatch, screenshots.CAPTURE_ATTEMPTS)

    async def scenario() -> tuple[str, list[Path], int]:
        async with browsing(sites) as session:
            await serve_cases(session.page, sites.other)
            await session.navigate(f"{sites.app}/shot/plain")
            sessions.restart()
            return await withheld(session, RunRecord.create(tmp_path))

    assert asyncio.run(scenario()) == (FAILED, [], 0)
    assert len(sessions.opened) == screenshots.CAPTURE_ATTEMPTS
    assert sessions.detached == sessions.opened


# The page as `test_screenshot_ocr.py`'s `reflected` shows a value.
REFLECTED = (
    "<!doctype html><meta charset=utf-8>"
    f"<p style='font:40px sans-serif;margin:20px'>{html.escape(RED_VALUE)}</p>"
)


@dataclass(frozen=True)
class Case:
    """What an ending sets up its capture with."""

    monkeypatch: pytest.MonkeyPatch
    session: BrowserSession
    sites: Sites


async def refused(case: Case) -> None:
    send = CDPSession.send

    async def refusing(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureScreenshot":
            raise Error("Protocol error (Page.captureScreenshot): refused")
        return await send(devtools, method, params)

    case.monkeypatch.setattr(CDPSession, "send", refusing)
    await case.session.navigate(f"{case.sites.app}/shot/plain")


async def unreadable(case: Case) -> None:
    send = CDPSession.send

    async def replying(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureScreenshot":
            return {"data": "not base64"}
        return await send(devtools, method, params)

    case.monkeypatch.setattr(CDPSession, "send", replying)
    await case.session.navigate(f"{case.sites.app}/shot/plain")


async def too_large(case: Case) -> None:
    case.monkeypatch.setattr(screenshots, "ARCHIVE_CHARS", 1000)
    await red_filled(case.session, case.sites)


async def held_back(case: Case) -> None:
    case.monkeypatch.setattr(screenshots, "CAPTURE_SECONDS", 0.5)
    await red_filled(case.session, case.sites)
    await case.session.page.evaluate(
        "setTimeout(() => { const end = Date.now() + 2000; "
        "while (Date.now() < end) {} }, 50)"
    )
    await asyncio.sleep(0.2)


async def reflected(case: Case) -> None:
    await case.session.page.route(
        "**/reflected",
        lambda route: route.fulfill(
            body=REFLECTED, content_type="text/html; charset=utf-8"
        ),
    )
    await case.session.navigate(f"{case.sites.app}/reflected")


@pytest.mark.parametrize(
    ("ending", "reason"),
    [
        (refused, FAILED),
        (unreadable, "DevTools replied in a shape the capture doesn't read"),
        (too_large, "the archive is over 1000 characters"),
        (held_back, "the page's snapshot, its scrub or its render took over 0.5 s"),
        (reflected, "the image's text spells a bound value"),
    ],
    ids=["refused", "unreadable", "archive", "bound", "ocr"],
)
def test_a_capture_that_ends_any_other_way_makes_one_attempt(
    sites: Sites,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: Sessions,
    *,
    ending: Callable[[Case], Awaitable[None]],
    reason: str,
) -> None:
    async def scenario() -> tuple[str, list[Path], int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await ending(Case(monkeypatch, session, sites))
            sessions.restart()
            found = await withheld(session, record)
            # One DevTools session, so one attempt.
            assert len(sessions.opened) == 1
            # A held-back page answers again, so its browser closes.
            await asyncio.sleep(2 if ending is held_back else 0)
            return found

    assert asyncio.run(scenario()) == (reason, [], 0)
