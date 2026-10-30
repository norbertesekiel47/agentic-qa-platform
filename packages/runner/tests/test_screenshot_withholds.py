"""Screenshots a capture withholds (#171, ADR-0034): once a test secret has
been filled, a snapshot whose scrub, render or DevTools replies can't be
trusted saves nothing, and says why in words that never quote the page."""

import asyncio
import json
import struct
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import RoleField
from aqa_core.project import SecretDestination
from aqa_runner import screenshots
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import ScreenshotWithheldError, take_screenshot
from playwright.async_api import CDPSession, Error
from pydantic import SecretStr

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.screenshot_pages import (
    EDITABLE,
    EDITABLE_STYLE,
    FIELD,
    RED,
    RED_VALUE,
    SPECIALS_VALUE,
    STYLE,
    TRIGGER,
    bound,
    element,
    red_filled,
    red_pixels,
    scratch_page,
    serve_cases,
)


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


# Why a render whose scrub cut markup, or that the page changed, is withheld.
MISMATCHED = (
    "the snapshot's fields, frames, options and editable elements don't match "
    "its render's"
)


async def withheld(
    session: BrowserSession, record: RunRecord
) -> tuple[str, list[Path], int]:
    """Why the session's screenshot is withheld, what the record then holds,
    and how many browser contexts are left open beyond the page's own. The
    error keeps nothing of an error behind it, which can quote the page."""
    browser = session.page.context.browser
    assert browser is not None
    with pytest.raises(ScreenshotWithheldError) as raised:
        await take_screenshot(session)
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)
    return str(raised.value), list(record.path.iterdir()), len(browser.contexts) - 1


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


def test_an_archive_just_under_the_largest_a_capture_scrubs_is_saved(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    lengths: list[int] = []
    send = CDPSession.send

    async def measuring(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        found = await send(devtools, method, params)
        if method == "Page.captureSnapshot":
            lengths.append(len(found["data"]))
        return found

    monkeypatch.setattr(CDPSession, "send", measuring)
    # A page of text, as the cap was measured on.
    paragraph = "<p>" + "the order ships once payment clears " * 12 + "</p>\n"
    filler = paragraph * (int(screenshots.ARCHIVE_CHARS * 0.92) // len(paragraph))

    async def scenario() -> int:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            scratch = await scratch_page(session)
            await red_filled(session, sites)
            await session.page.evaluate(
                "(html) => document.body.insertAdjacentHTML('beforeend', html)", filler
            )
            return await red_pixels(scratch, (await take_screenshot(session)).png)

    assert asyncio.run(scenario()) == 0
    assert 0.95 < lengths[0] / screenshots.ARCHIVE_CHARS <= 1


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


@pytest.mark.parametrize("child", ["input", "iframe", "option"])
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


@pytest.mark.parametrize(
    "script",
    [
        # The archive's parser drops a select in a select, so the inner one's
        # option has no select in the render.
        (
            "const outer = document.createElement('select'); "
            "const inner = document.createElement('select'); "
            "inner.append(new Option('N')); outer.append(inner); "
            "document.body.append(outer)"
        ),
        # The render's choice copies the chosen option's content into the
        # <selectedcontent>, which on the page holds a copy taken before the
        # page added an editable element to that option.
        (
            "document.body.insertAdjacentHTML('beforeend', \"<select id=s "
            "style='appearance:base-select'><button><selectedcontent>"
            "</selectedcontent></button><option>A</option><option value=b>B</option>"
            "</select>\"); s.value = 'b'; "
            "s.options[1].append(Object.assign(document.createElement('div'), "
            "{contentEditable: 'true'}))"
        ),
        # Emptying the editable element takes the chosen option out, and the
        # select chooses another.
        (
            "document.body.insertAdjacentHTML('beforeend', '<select id=s>"
            "<option>A</option><div contenteditable><option value=x>X</option>"
            "</div></select>'); s.value = 'x'"
        ),
    ],
    ids=["option-in-a-nested-select", "chosen-option-changed", "chosen-option-emptied"],
)
def test_options_the_render_holds_or_chooses_otherwise_withhold_it(
    sites: Sites, tmp_path: Path, script: str
) -> None:
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
        # Cutting the value makes a customizable select of two chosen options
        # single, where a choice copies an option's content into its
        # <selectedcontent>: here the value, split by an empty <b>, which the
        # scrub doesn't find.
        (
            "multiple",
            STYLE
            + FIELD
            + "<select id=s multiple style='appearance:base-select'><button>"
            + "<selectedcontent><div contenteditable></div></selectedcontent></button>"
            + f"<option value=a><div contenteditable style='{RED}'></div></option>"
            + f"<option value=b><div id=d contenteditable style='{RED}'></div></option>"
            + "</select>",
            "#f",
            (
                "d.replaceChildren(f.value.slice(0, 4), document.createElement('b'), "
                "f.value.slice(4)); for (const option of s.options) option.selected = true"
            ),
            MISMATCHED,
        ),
        # Cutting the value makes a list a drop-down.
        (
            "multiple",
            STYLE
            + FIELD
            + "<select multiple><option>A</option><option>B</option></select>",
            "#f",
            "0",
            MISMATCHED,
        ),
    ],
    ids=[
        "value-contenteditable",
        "value-hidden-split-by-a-newline",
        "split-by-a-nul",
        "value-multiple-copied",
        "value-multiple",
    ],
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


def snapshot(
    strings: list[str],
    nodes: dict[str, Any],
    layout: tuple[list[int], list[list[int]]] = ([], []),
) -> dict[str, Any]:
    """A DOM snapshot reply of one document, of one node named by the first
    string unless `nodes` says otherwise, and `layout`'s nodes and styles."""
    return {
        "strings": strings,
        "documents": [
            {
                "nodes": {
                    "nodeName": [0],
                    "parentIndex": [-1],
                    "attributes": [[]],
                    **nodes,
                },
                "layout": {"nodeIndex": layout[0], "styles": layout[1]},
            }
        ],
    }


@pytest.mark.parametrize(
    ("method", "reply"),
    [
        ("Page.getLayoutMetrics", {"layoutViewport": {}}),
        ("DOMSnapshot.captureSnapshot", {"strings": [], "documents": [{}]}),
        # A rare value's places and values out of step.
        (
            "DOMSnapshot.captureSnapshot",
            snapshot(["INPUT"], {"inputValue": {"index": [0], "value": []}}),
        ),
        # An index past what it indexes: a string, a node's parent, an
        # attribute's value, an editable node, a frame's document.
        ("DOMSnapshot.captureSnapshot", snapshot(["INPUT"], {"nodeName": [1]})),
        (
            "DOMSnapshot.captureSnapshot",
            snapshot(["P", "read-write"], {"parentIndex": [1]}, ([0], [[1]])),
        ),
        ("DOMSnapshot.captureSnapshot", snapshot(["INPUT"], {"attributes": [[0, 1]]})),
        (
            "DOMSnapshot.captureSnapshot",
            snapshot(["P", "read-write"], {}, ([1], [[1]])),
        ),
        (
            "DOMSnapshot.captureSnapshot",
            snapshot(
                ["IFRAME"], {"contentDocumentIndex": {"index": [0], "value": [1]}}
            ),
        ),
    ],
    ids=[
        "metrics",
        "dom-snapshot",
        "rare-data",
        "name-index",
        "parent-index",
        "attribute-index",
        "layout-index",
        "document-index",
    ],
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


def test_a_live_capture_devtools_hands_back_undecodable_withholds_it(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    send = CDPSession.send

    async def replying(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureScreenshot":
            return {"data": "not base64"}
        return await send(devtools, method, params)

    monkeypatch.setattr(CDPSession, "send", replying)

    async def scenario() -> tuple[str, list[Path], int]:
        async with browsing(sites) as session:
            await serve_cases(session.page, sites.other)
            await session.navigate(f"{sites.app}/shot/plain")
            return await withheld(session, RunRecord.create(tmp_path))

    assert asyncio.run(scenario()) == (
        "DevTools replied in a shape the capture doesn't read",
        [],
        0,
    )
