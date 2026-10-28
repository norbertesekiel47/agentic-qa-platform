"""Screenshots in the run record (#171, ADR-0034): once a test secret has
been filled in the browser session, a screenshot is a render of a scrubbed
snapshot, so no hostile page can make a bound field show its value in a
saved PNG; before that, and in a run that binds nothing, it is the live
page's. Each page draws the bound value in pure red, which nothing else on
it is. The browser tests launch real Chromium, sandboxed, on the OS that
runs them: Linux in CI, macOS locally."""

import asyncio
import json
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.browser import BrowserSettings
from aqa_runner import screenshots
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
from playwright.async_api import CDPSession, ElementHandle, Error, Page

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.screenshot_pages import (
    CASES,
    FIELD,
    RED_VALUE,
    SCALED,
    STYLE,
    TRIGGER,
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


def test_a_value_sharing_four_characters_with_a_bound_value_is_dropped(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[int, int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            scratch = await scratch_page(session)
            await session.navigate(f"{sites.app}/shot/plain")
            await session.fill_secret(
                await element(session, "#f"), secret(sites, "RED")
            )
            await session.page.evaluate(
                "document.body.insertAdjacentHTML('beforeend', "
                "'<input id=g style=\"position:absolute;top:100px\">')"
            )
            other = await element(session, "#g")
            await session.fill(other, "Zq7 Plain")
            three = await red_pixels(scratch, (await take_screenshot(session)).png)
            await session.fill(other, "Zq7f Plain")
            four = await red_pixels(scratch, (await take_screenshot(session)).png)
            return three, four

    three, four = asyncio.run(scenario())
    assert three > 0
    assert four == 0


# The events, observers and long tasks the prototype watched a live page for
# during its snapshots and renders (scratchpad proto-171, m5_side_effects).
EVENTS = [
    "input",
    "change",
    "select",
    "selectionchange",
    "focus",
    "blur",
    "focusin",
    "focusout",
    "beforeinput",
    "visibilitychange",
    "pagehide",
    "pageshow",
    "freeze",
    "resume",
    "beforeprint",
    "afterprint",
    "scroll",
    "resize",
    "load",
    "DOMContentLoaded",
    "beforeunload",
    "unload",
    "animationstart",
    "animationiteration",
    "transitionstart",
    "invalid",
    "submit",
    "reset",
    "copy",
    "securitypolicyviolation",
    "error",
]
WATCH = """(events) => {
    window.seen = {};
    const saw = (what) => { seen[what] = (seen[what] ?? 0) + 1; };
    for (const type of events) {
        addEventListener(type, () => saw(type), true);
        document.addEventListener(type, () => saw(type), true);
    }
    new MutationObserver((records) => records.forEach((r) => saw("mutation:" + r.type)))
        .observe(document, {attributes: true, childList: true, subtree: true, characterData: true});
    new ResizeObserver(() => saw("resize-observer")).observe(document.body);
    new IntersectionObserver(() => saw("intersection-observer")).observe(document.body);
    new PerformanceObserver((list) => list.getEntries().forEach(() => saw("longtask")))
        .observe({type: "longtask"});
}"""
STATE = """() => [f.value, f.selectionStart, f.selectionEnd,
    document.activeElement.id, document.hasFocus()]"""


def test_a_capture_changes_nothing_the_page_can_see(sites: Sites) -> None:
    async def scenario() -> list[object]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            await session.navigate(f"{sites.app}/shot/plain")
            await session.page.evaluate(WATCH, EVENTS)
            seen: list[object] = []
            for fill in (False, True):
                if fill:
                    field = await element(session, "#f")
                    await session.fill_secret(field, secret(sites, "RED"))
                    await session.page.evaluate("f.setSelectionRange(2, 5)")
                await asyncio.sleep(0.3)
                before = await session.page.evaluate(STATE)
                await session.page.evaluate("seen = {}")
                for _ in range(3):
                    await take_screenshot(session)
                await asyncio.sleep(0.3)
                seen += [
                    await session.page.evaluate("seen"),
                    await session.page.evaluate(STATE) == before,
                ]
            return seen

    assert asyncio.run(scenario()) == [{}, True, {}, True]


async def withheld(
    session: BrowserSession, record: RunRecord
) -> tuple[str, list[Path], int]:
    """Why the session's screenshot is withheld, what the record then holds,
    and how many browser contexts are left open beyond the page's own."""
    browser = session.page.context.browser
    assert browser is not None
    with pytest.raises(ScreenshotWithheldError) as raised:
        await take_screenshot(session)
    return str(raised.value), list(record.path.iterdir()), len(browser.contexts) - 1


async def red_filled(
    session: BrowserSession, sites: Sites, page: str = "plain"
) -> None:
    """The case page `page` with the red value filled into its field."""
    await session.navigate(f"{sites.app}/shot/{page}")
    await session.fill_secret(await element(session, "#f"), secret(sites, "RED"))


def withholding(
    sites: Sites, tmp_path: Path, script: str
) -> tuple[str, list[Path], int]:
    """What `withheld` reports once the red value is filled and the page
    has run `script`."""

    async def scenario() -> tuple[str, list[Path], int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await red_filled(session, sites)
            await session.page.evaluate(script)
            return await withheld(session, record)

    return asyncio.run(scenario())


def test_a_part_in_an_encoding_the_scrub_cant_read_withholds_it(
    sites: Sites, tmp_path: Path
) -> None:
    async def scenario() -> tuple[str, list[Path], int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            # WHATWG's label, which Python's codecs don't know.
            await session.page.route(
                "**/odd",
                lambda route: route.fulfill(
                    body=f"<!doctype html>{TRIGGER}{STYLE}{FIELD}",
                    content_type="text/html; charset=x-mac-cyrillic",
                ),
            )
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await red_filled(session, sites, "../odd")
            return await withheld(session, record)

    assert asyncio.run(scenario()) == (
        "a part is in an encoding the scrub can't read",
        [],
        0,
    )


def test_a_spelling_the_scrubs_cut_leaves_withholds_it(
    sites: Sites, tmp_path: Path
) -> None:
    # Each of the scrub's two passes cuts the value out of the middle and
    # joins what is left around it into the value again, so the value nested
    # twice outlasts both.
    middle = len(RED_VALUE) // 2
    head, tail = RED_VALUE[:middle], RED_VALUE[middle:]
    joined = head + head + RED_VALUE + tail + tail
    script = f"document.body.append({json.dumps(joined)})"

    assert withholding(sites, tmp_path, script) == (
        "a bound value's spelling is left in a part after the scrub",
        [],
        0,
    )


def test_a_page_that_holds_its_snapshot_back_past_the_bound_withholds_it(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screenshots, "CAPTURE_SECONDS", 0.5)

    async def scenario() -> tuple[str, list[Path], int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await red_filled(session, sites)
            await session.page.evaluate(
                "setTimeout(() => { const end = Date.now() + 2000; "
                "while (Date.now() < end) {} }, 50)"
            )
            await asyncio.sleep(0.2)
            found = await withheld(session, record)
            await asyncio.sleep(2)
            return found

    assert asyncio.run(scenario()) == (
        "the page's snapshot or its render took over 0.5 s",
        [],
        0,
    )


def test_a_devtools_call_that_fails_withholds_it(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    send = CDPSession.send

    async def refusing(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureSnapshot":
            raise Error("Protocol error (Page.captureSnapshot): refused")
        return await send(devtools, method, params)

    monkeypatch.setattr(CDPSession, "send", refusing)

    assert withholding(sites, tmp_path, "0") == (
        "a DevTools call or the render failed",
        [],
        0,
    )


@pytest.mark.parametrize(
    "script",
    [
        "document.designMode = 'on'",
        (
            "const sheet = new CSSStyleSheet(); "
            "sheet.replaceSync('p { -webkit-user-modify: read-write }'); "
            "document.adoptedStyleSheets = [sheet]; "
            "document.body.append(Object.assign(document.createElement('p'), "
            "{textContent: 'notes'}))"
        ),
    ],
    ids=["design-mode", "adopted-style-sheet"],
)
def test_editing_the_render_cant_reproduce_withholds_it(
    sites: Sites, tmp_path: Path, script: str
) -> None:
    assert withholding(sites, tmp_path, script) == (
        (
            "an element is editable in a way the snapshot doesn't hold "
            "(designMode, or a style it leaves out)"
        ),
        [],
        0,
    )


def test_fields_the_render_doesnt_have_withhold_it(
    sites: Sites, tmp_path: Path
) -> None:
    # A template's children, put there by script rather than in its content,
    # are in the page's tree; its archive's parser puts them in the content.
    script = (
        "document.body.append(document.createElement('template')); "
        "document.querySelector('template').append(document.createElement('input'))"
    )

    assert withholding(sites, tmp_path, script) == (
        "the snapshot's fields and frames don't match its render's",
        [],
        0,
    )


def test_an_image_whose_bytes_spell_a_bound_value_withholds_it(
    sites: Sites, tmp_path: Path
) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checked = kind + data
        return (
            struct.pack(">I", len(data))
            + checked
            + struct.pack(">I", zlib.crc32(checked))
        )

    # A 1x1 PNG whose text chunk holds the value, which a render would serve.
    image = b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)),
            chunk(b"tEXt", b"Comment\x00" + RED_VALUE.encode()),
            chunk(b"IDAT", zlib.compress(b"\x00\x00")),
            chunk(b"IEND", b""),
        )
    )

    async def scenario() -> tuple[str, list[Path], int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            await session.page.route(
                "**/picture.png",
                lambda route: route.fulfill(body=image, content_type="image/png"),
            )
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await red_filled(session, sites)
            await session.page.evaluate(
                "new Promise((loaded) => document.body.append(Object.assign("
                "new Image(), {src: '/picture.png', onload: loaded, onerror: loaded})))"
            )
            return await withheld(session, record)

    assert asyncio.run(scenario()) == ("a binary part spells a bound value", [], 0)


@pytest.mark.parametrize(
    ("archive", "reason"),
    [
        ("From: nobody\r\n\r\nno parts", "the archive names no page"),
        ("Content-Type: text/html\r\n\r\né", "the archive isn't 7-bit MIME"),
    ],
    ids=["no-page", "not-7-bit"],
)
def test_an_archive_the_scrub_cant_read_withholds_it(
    sites: Sites,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive: str,
    reason: str,
) -> None:
    send = CDPSession.send

    async def handing_over(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureSnapshot":
            return {"data": archive}
        return await send(devtools, method, params)

    monkeypatch.setattr(CDPSession, "send", handing_over)

    assert withholding(sites, tmp_path, "0") == (reason, [], 0)
