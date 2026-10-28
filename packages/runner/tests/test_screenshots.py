"""Screenshots in the run record (#171, ADR-0034): once a test secret has
been filled in the browser session, a screenshot is a render of a scrubbed
snapshot, so no hostile page can make a bound field show its value in a
saved PNG; before that, and in a run that binds nothing, it is the live
page's. Each page draws the bound value in pure red, which nothing else on
it is. The browser tests launch real Chromium, sandboxed, on the OS that
runs them: Linux in CI, macOS locally."""

import asyncio
import struct
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.browser import BrowserSettings
from aqa_runner import browser_session
from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import (
    Screenshot,
    ScreenshotWithheldError,
    take_screenshot,
)
from aqa_runner.secret_fields import SecretNotFilledError, field_matches
from playwright.async_api import CDPSession, ElementHandle, Error, Route

from packages.runner.tests.document_fixtures import (
    APP,
    Sites,
    browsing,
    serving_sites,
)
from packages.runner.tests.screenshot_pages import (
    CASES,
    MARKED,
    RED,
    RED_VALUE,
    SCALED,
    TRIGGER,
    UNBOUND,
    Case,
    armed,
    bound,
    element,
    green_pixels,
    red_filled,
    red_pixels,
    scratch_page,
    secret,
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


def test_a_record_never_writes_a_screenshot_twice(sites: Sites, tmp_path: Path) -> None:
    redactor = Redactor(bound(sites.app))

    async def scenario() -> tuple[Screenshot, Screenshot]:
        async with browsing(sites, redactor=redactor) as session:
            await serve_cases(session.page, sites.other)
            await session.navigate(f"{sites.app}/shot/plain")
            first = await take_screenshot(session)
            await session.page.evaluate("document.body.style.background = '#f00'")
            return first, await take_screenshot(session)

    first, second = asyncio.run(scenario())
    record = RunRecord.create(tmp_path).redacting(redactor)
    record.write_screenshot("shots/0.png", first)

    with pytest.raises(FileExistsError):
        record.write_screenshot("shots/0.png", second)

    assert first.png != second.png
    assert (record.path / "shots/0.png").read_bytes() == first.png


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

    async def scenario() -> tuple[int, list[str], list[str], list[tuple[int, int]]]:
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
            pixels = [
                (await red_pixels(scratch, png), await green_pixels(scratch, png))
                for png in ((record.path / file).read_bytes() for file in saved)
            ]
            return shown, outcomes, saved, pixels

    shown, outcomes, saved, pixels = asyncio.run(scenario())
    assert shown > 0
    assert saved == sorted(outcome for outcome in outcomes if outcome != "withheld")
    # No red, and the marker: what is saved is the page, not a blank.
    assert pixels == [(0, MARKED)] * len(saved)
    if CASES[name].withholds:
        assert saved != []
    else:
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


def test_a_render_has_the_live_pages_size_scale_and_colour_scheme(
    sites: Sites,
) -> None:
    # Only a dark colour scheme at scale 2 paints the page red.
    page = (
        f"<!doctype html><meta charset=utf-8>{TRIGGER}<style>"
        "@media (prefers-color-scheme: dark) and (min-resolution: 2dppx)"
        "{body{background:#f00}}</style>"
    )
    settings = BrowserSettings(
        viewport=(640, 480), device_scale_factor=2, color_scheme="dark"
    )

    async def scenario() -> list[tuple[int, int, int]]:
        async with browsing(
            sites, redactor=Redactor(bound(sites.app)), settings=settings
        ) as session:
            scratch = await scratch_page(session)
            await session.page.route(
                "**/dark",
                lambda route: route.fulfill(body=page, content_type="text/html"),
            )
            await session.navigate(f"{sites.app}/dark")
            shots = [(await take_screenshot(session)).png]
            trigger = await element(session, "#trigger")
            await session.fill_secret(trigger, secret(sites, "TRIGGER"))
            shots.append((await take_screenshot(session)).png)
            # A PNG's width and height are the first fields of its header.
            return [
                (*struct.unpack(">II", png[16:24]), await red_pixels(scratch, png))
                for png in shots
            ]

    # A live capture is in CSS pixels, whatever the scale it was drawn at.
    live, rendered = asyncio.run(scenario())
    assert live[:2] == (640, 480)
    assert live[2] > 300_000
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
