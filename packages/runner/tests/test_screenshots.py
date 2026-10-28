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
import time
import zlib
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.browser import BrowserSettings
from aqa_core.config import RoleField
from aqa_core.project import SecretDestination
from aqa_runner import browser_session, screenshots
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import (
    Screenshot,
    ScreenshotWithheldError,
    take_screenshot,
)
from aqa_runner.secret_fields import SecretNotFilledError, field_matches
from playwright.async_api import CDPSession, ElementHandle, Error, Page, Route
from pydantic import SecretStr

from packages.runner.tests.document_fixtures import (
    APP,
    Sites,
    browsing,
    serving_sites,
)
from packages.runner.tests.screenshot_pages import (
    CASES,
    EDITABLE,
    EDITABLE_STYLE,
    FIELD,
    RED,
    RED_VALUE,
    SCALED,
    SPECIALS_VALUE,
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


def test_a_screenshot_waiting_on_the_first_fill_is_a_render(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> int:
        paused, released = asyncio.Event(), asyncio.Event()

        async def held(*args: Any) -> bool:
            paused.set()
            await released.wait()
            return await field_matches(*args)

        monkeypatch.setattr(browser_session, "field_matches", held)
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            scratch = await scratch_page(session)
            await session.navigate(f"{sites.app}/shot/plain")
            filling = asyncio.create_task(
                session.fill_secret(await element(session, "#f"), secret(sites, "RED"))
            )
            await paused.wait()
            # Queued on the session's lock, which the fill holds through its
            # binding checks.
            shooting = asyncio.create_task(take_screenshot(session))
            await asyncio.sleep(0.2)
            released.set()
            await filling
            return await red_pixels(scratch, (await shooting).png)

    assert asyncio.run(scenario()) == 0


def test_a_fill_that_fails_partway_counts_as_filled(sites: Sites) -> None:
    # The page's own execCommand takes the value in, then throws, so the
    # fill's script fails with the value in the field.
    breaking = (
        "() => { const insert = Document.prototype.execCommand; "
        "document.execCommand = function (...args) { "
        "insert.apply(this, args); throw new Error('no'); }; }"
    )

    async def scenario() -> int:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            scratch = await scratch_page(session)
            await session.navigate(f"{sites.app}/shot/plain")
            await session.page.evaluate(breaking)
            with pytest.raises(SecretNotFilledError):
                await session.fill_secret(
                    await element(session, "#f"), secret(sites, "RED")
                )
            assert await session.page.evaluate("f.value") == RED_VALUE
            return await red_pixels(scratch, (await take_screenshot(session)).png)

    assert asyncio.run(scenario()) == 0


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
    case, which a render empties, and a `shown_live` one: there nothing is
    filled, so the screenshot is the live page's."""
    case = CASES[name]
    served["value"] = UNBOUND
    await session.navigate(f"{sites.app}/shot/{name}")
    if case.into != "editable" and not case.shown_live:
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
        if case.partial:
            with pytest.raises(Error):
                await session.fill(await field(session, case), value)
        else:
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
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, scale: int
) -> None:
    if CASES[name].unseen:
        send = CDPSession.send

        async def unseen(
            devtools: CDPSession, method: str, params: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            found = await send(devtools, method, params)
            if method == "DOMSnapshot.captureSnapshot":
                for document in found["documents"]:
                    document["nodes"].pop("contentDocumentIndex", None)
            return found

        monkeypatch.setattr(CDPSession, "send", unseen)

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


def test_a_render_shows_text_and_images_that_spell_an_archive_reference(
    sites: Sites,
) -> None:
    square = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='50' height='50'>"
        "<rect width='50' height='50' fill='#f00'/></svg>"
    )
    page = (
        f"<!doctype html><meta charset=utf-8>{TRIGGER}"
        f"<p style='{RED}'>Folic acid: 5mg</p><img src='/acid:square.svg'>"
    )

    async def scenario() -> tuple[int, int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            scratch = await scratch_page(session)
            await session.page.route(
                "**/acid:square.svg",
                lambda route: route.fulfill(body=square, content_type="image/svg+xml"),
            )
            await session.page.route(
                "**/folic",
                lambda route: route.fulfill(body=page, content_type="text/html"),
            )
            await session.navigate(f"{sites.app}/folic")
            live = await red_pixels(scratch, (await take_screenshot(session)).png)
            trigger = await element(session, "#trigger")
            await session.fill_secret(trigger, secret(sites, "TRIGGER"))
            rendered = await red_pixels(scratch, (await take_screenshot(session)).png)
            return live, rendered

    live, rendered = asyncio.run(scenario())
    assert live > 2500
    assert rendered == live


def test_a_render_keeps_a_buttons_label_and_what_is_checked(sites: Sites) -> None:
    # The page checks by script, which the archive doesn't hold, and each red
    # square shows only while the control before it is checked.
    page = (
        f"<!doctype html><meta charset=utf-8>{TRIGGER}"
        "<style>.on{display:inline-block;width:50px;height:50px}"
        ":checked+.on{background:#f00}</style>"
        f"<input type=submit style='{RED}'><input type=checkbox id=c>"
        "<span class=on></span><input type=radio name=r id=r><span class=on></span>"
        "<input type=radio name=r checked><span class=on></span>"
        "<script>c.checked = true; r.checked = true</script>"
    )

    async def scenario() -> tuple[int, int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            scratch = await scratch_page(session)
            await session.page.route(
                "**/controls",
                lambda route: route.fulfill(body=page, content_type="text/html"),
            )
            await session.navigate(f"{sites.app}/controls")
            live = await red_pixels(scratch, (await take_screenshot(session)).png)
            trigger = await element(session, "#trigger")
            await session.fill_secret(trigger, secret(sites, "TRIGGER"))
            rendered = await red_pixels(scratch, (await take_screenshot(session)).png)
            return live, rendered

    live, rendered = asyncio.run(scenario())
    assert live > 5000
    assert rendered == live


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


# Why a render whose scrub cut markup, or that the page changed, is withheld.
MISMATCHED = (
    "the snapshot's fields, frames and editable elements don't match its render's"
)


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


@pytest.mark.parametrize(
    "value", [RED_VALUE, SPECIALS_VALUE], ids=["raw", "entity-encoded"]
)
def test_a_spelling_the_scrubs_cut_leaves_withholds_it(
    sites: Sites, tmp_path: Path, value: str
) -> None:
    # Each of the scrub's passes that finds the value cuts it out of the
    # middle and joins what is left around it into the value again, so the
    # value nested twice outlasts them. The archive writes the second's `&`
    # and `<` as references, which only the decoded pass reads.
    middle = len(value) // 2
    head, tail = value[:middle], value[middle:]
    joined = head + head + value + tail + tail
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
        "the page's snapshot, its scrub or its render took over 0.5 s",
        [],
        0,
    )


def test_a_scrub_past_the_bound_withholds_it_and_holds_up_nothing_else(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screenshots, "CAPTURE_SECONDS", 0.5)
    ticks: list[int] = []
    during: list[int] = []
    spans = Redactor.spans

    # The first scan of the scrub takes a second, as a large archive's does,
    # and notes how often the event loop ran meanwhile.
    def slow(redactor: Redactor, text: str) -> list[tuple[int, int]]:
        if not during:
            before = len(ticks)
            time.sleep(1)
            during.append(len(ticks) - before)
        return spans(redactor, text)

    monkeypatch.setattr(Redactor, "spans", slow)

    async def scenario() -> tuple[str, list[Path], int]:
        async def tick() -> None:
            while True:
                ticks.append(0)
                await asyncio.sleep(0.01)

        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await red_filled(session, sites)
            ticking = asyncio.create_task(tick())
            found = await withheld(session, record)
            ticking.cancel()
            return found

    assert asyncio.run(scenario()) == (
        "the page's snapshot, its scrub or its render took over 0.5 s",
        [],
        0,
    )
    assert during[0] > 0


def test_an_archive_too_large_to_scrub_in_the_bound_withholds_it(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screenshots, "ARCHIVE_CHARS", 1000)

    assert withholding(sites, tmp_path, "0") == (
        "the archive is over 1000 characters",
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


@pytest.mark.parametrize("child", ["input", "iframe"])
def test_fields_or_frames_the_render_doesnt_have_withhold_it(
    sites: Sites, tmp_path: Path, child: str
) -> None:
    # A template's children, put there by script rather than in its content,
    # are in the page's tree; its archive's parser puts them in the content.
    script = (
        "document.body.append(document.createElement('template')); "
        f"document.querySelector('template').append(document.createElement('{child}'))"
    )

    assert withholding(sites, tmp_path, script) == (MISMATCHED, [], 0)


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


@pytest.mark.parametrize(
    ("value", "body", "into", "script", "reason"),
    [
        # Cutting the value takes the attribute that made the element
        # editable, so the render wouldn't empty it.
        (
            "contenteditable",
            EDITABLE_STYLE + EDITABLE,
            "#ce",
            "const t = ce.firstChild; t.splitText(7); t.after(document.createElement('b'))",
            MISMATCHED,
        ),
        # Cutting the value would make a hidden field a text field, which
        # takes the newline out of what it shows, but Blink's archive leaves
        # every hidden field out: the render shows nothing of it.
        (
            "hidden",
            STYLE + FIELD + "<input id=h type=hidden>",
            "#f",
            "h.value = 'hid\\nden'",
            0,
        ),
        # The render's parser drops a NUL in text.
        (
            RED_VALUE,
            STYLE + FIELD + f"<p id=p style='{RED}'></p>",
            "#f",
            f"p.textContent = {json.dumps(RED_VALUE[:4] + chr(0) + RED_VALUE[4:])}",
            "the render spells a bound value",
        ),
    ],
    ids=["value-contenteditable", "value-hidden-split-by-a-newline", "split-by-a-nul"],
)
def test_a_value_the_scrub_changes_the_markup_or_parse_of_is_never_shown(
    sites: Sites, *, value: str, body: str, into: str, script: str, reason: str | int
) -> None:
    filled = BoundSecret(
        "MARKUP",
        SecretStr(value),
        SecretDestination((sites.app,), RoleField(role="textbox", name="Secret")),
    )

    async def scenario() -> str | int:
        redactor = Redactor([filled, *bound(sites.app)])
        async with browsing(sites, redactor=redactor) as session:
            scratch = await scratch_page(session)
            await session.page.route(
                "**/markup",
                lambda route: route.fulfill(
                    body=f"<!doctype html><meta charset=utf-8>{TRIGGER}{body}",
                    content_type="text/html; charset=utf-8",
                ),
            )
            await session.navigate(f"{sites.app}/markup")
            await session.fill_secret(await element(session, into), filled)
            await session.page.evaluate(script)
            try:
                shot = await take_screenshot(session)
            except ScreenshotWithheldError as withheld:
                return str(withheld)
            return await red_pixels(scratch, shot.png)

    assert asyncio.run(scenario()) == reason


@pytest.mark.parametrize(
    ("method", "reply"),
    [
        ("Page.getLayoutMetrics", {"layoutViewport": {}}),
        ("DOMSnapshot.captureSnapshot", {"strings": [], "documents": [{}]}),
        # A rare value's places and values out of step.
        (
            "DOMSnapshot.captureSnapshot",
            {
                "strings": ["INPUT"],
                "documents": [
                    {
                        "nodes": {
                            "nodeName": [0],
                            "parentIndex": [-1],
                            "attributes": [[]],
                            "inputValue": {"index": [0], "value": []},
                        },
                        "layout": {"nodeIndex": [], "styles": []},
                    }
                ],
            },
        ),
    ],
    ids=["metrics", "dom-snapshot", "rare-data"],
)
def test_a_devtools_reply_in_a_shape_the_capture_doesnt_read_withholds_it(
    sites: Sites,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    reply: dict[str, Any],
) -> None:
    send = CDPSession.send

    async def replying(
        devtools: CDPSession, called: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return reply if called == method else await send(devtools, called, params)

    monkeypatch.setattr(CDPSession, "send", replying)

    assert withholding(sites, tmp_path, "0") == (
        "DevTools replied in a shape the capture doesn't read",
        [],
        0,
    )


def test_a_render_asks_nothing_of_the_network(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A source only a page without script picks: the render asks for it,
    # the live page never does until it loads it itself, at the end.
    picture = (
        "<picture><source media='(scripting: none)' srcset='/without-script.png'>"
        "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='></picture>"
    )
    answers: list[tuple[str, str]] = []

    def noted(method: str) -> Callable[..., Awaitable[None]]:
        answer = getattr(Route, method)

        async def noting(route: Route, *args: Any, **kwargs: Any) -> None:
            answers.append((method, route.request.url))
            await answer(route, *args, **kwargs)

        return noting

    for method in ("fulfill", "abort", "continue_", "fallback"):
        monkeypatch.setattr(Route, method, noted(method))

    async def scenario() -> tuple[list[tuple[str, str]], bool, bool, bool]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            await red_filled(session, sites)
            await session.page.evaluate(
                "(picture) => document.body.insertAdjacentHTML('beforeend', picture)",
                picture,
            )
            answers.clear()
            await take_screenshot(session)
            rendered = list(answers)
            asked = await asyncio.to_thread(
                sites.saw, APP, "/without-script.png", within=1
            )
            await session.page.evaluate(
                "new Promise((done) => Object.assign(new Image(), "
                "{src: '/without-script.png', onload: done, onerror: done}))"
            )
            loaded = await asyncio.to_thread(
                sites.saw, APP, "/without-script.png", within=5
            )
            served = ("fulfill", f"{sites.app}/shot/plain") in rendered
            refused = [answer for answer in rendered if answer[0] != "fulfill"]
            return refused, served, asked, loaded

    assert asyncio.run(scenario()) == (
        [("abort", f"{sites.app}/without-script.png")],
        True,
        False,
        True,
    )


def test_a_render_runs_no_script_its_archive_carries(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Blink's archive keeps no handler, so one is handed over in its place,
    # with the plain page's two fields in the plain page's order.
    page = (
        '<!doctype html><input><input><img onload="document.body.style.background'
        "='#f00'\" src='data:image/gif;base64,"
        "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'>"
    )
    send = CDPSession.send

    async def handing_over(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureSnapshot":
            return {
                "data": (
                    'Content-Type: multipart/related; boundary="part"\r\n\r\n'
                    "--part\r\nContent-Type: text/html\r\n"
                    f"Content-Location: {sites.app}/shot/plain\r\n\r\n"
                    f"{page}\r\n--part--\r\n"
                )
            }
        return await send(devtools, method, params)

    async def scenario() -> tuple[int, int]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            scratch = await scratch_page(session)
            await scratch.set_content(page)
            ran = await red_pixels(scratch, await scratch.screenshot())
            await red_filled(session, sites)
            monkeypatch.setattr(CDPSession, "send", handing_over)
            shot = await take_screenshot(session)
            return ran, await red_pixels(scratch, shot.png)

    ran, rendered = asyncio.run(scenario())
    assert ran > 0
    assert rendered == 0
