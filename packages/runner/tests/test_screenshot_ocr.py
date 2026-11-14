"""The OCR check on screenshots (#250, ADR-0035): in a run that binds a test
secret, a capture whose image's text spells a bound value, live or
rendered, is withheld, and the record saves nothing."""

import asyncio
import html
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from aqa_core.project import SecretDestination
from aqa_runner import ocr, screenshots
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import ScreenshotWithheldError, take_screenshot
from pydantic import SecretStr

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.screenshot_pages import (
    RED_VALUE,
    UNBOUND,
    bound,
    red_filled,
    serve_cases,
)

# Draws `text` into an image on the page, as a page can once the value is
# in: a render carries a data: image, and no scrub reads its pixels.
DRAW = """(text) => {
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 80;
    const context = canvas.getContext("2d");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, 640, 80);
    context.fillStyle = "#000";
    context.font = "40px sans-serif";
    context.fillText(text, 10, 55);
    const image = Object.assign(new Image(), {src: canvas.toDataURL()});
    image.style.cssText = "position:absolute;left:0;top:200px";
    document.body.append(image);
    return image.decode();
}"""

# Why a capture whose image's text spells a bound value is withheld.
SPELLED = "the image's text spells a bound value"


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


async def saved(session: BrowserSession, record: RunRecord) -> tuple[str, list[str]]:
    """The session's screenshot written to `record`, or why it is withheld,
    and the files the record then holds."""
    try:
        record.write_screenshot("shots/0.png", await take_screenshot(session))
    except ScreenshotWithheldError as withheld:
        outcome = str(withheld)
    else:
        outcome = "saved"
    files = sorted(
        path.relative_to(record.path).as_posix()
        for path in record.path.rglob("*")
        if path.is_file()
    )
    return outcome, files


def rendered(sites: Sites, tmp_path: Path, text: str) -> tuple[str, list[str]]:
    """What `saved` reports once the red value is filled, so a capture is a
    render, and the page has drawn `text` into an image."""

    async def scenario() -> tuple[str, list[str]]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await red_filled(session, sites)
            await session.page.evaluate(DRAW, text)
            return await saved(session, record)

    return asyncio.run(scenario())


def reflected(
    sites: Sites,
    tmp_path: Path,
    text: str,
    redactor: Redactor | None = None,
    *,
    font: str = "40px sans-serif",
) -> tuple[str, list[str]]:
    """What `shown` reports of a page that shows `text` in `font`, as an
    app's server can show a bound value before any fill."""
    body = f"<p style='font:{font};margin:20px'>{html.escape(text)}</p>"
    return shown(sites, tmp_path, body, redactor)


def shown(
    sites: Sites, tmp_path: Path, body: str, redactor: Redactor | None = None
) -> tuple[str, list[str]]:
    """What `saved` reports of a page of `body`, so the capture is the live
    page's, in a session that scans with `redactor` (by default, of the
    test's bound secrets)."""
    page = f"<!doctype html><meta charset=utf-8>{body}"

    async def scenario() -> tuple[str, list[str]]:
        scan = Redactor(bound(sites.app)) if redactor is None else redactor
        async with browsing(sites, redactor=scan) as session:
            await session.page.route(
                "**/reflected",
                lambda route: route.fulfill(
                    body=page, content_type="text/html; charset=utf-8"
                ),
            )
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            await session.navigate(f"{sites.app}/reflected")
            return await saved(session, record)

    return asyncio.run(scenario())


def test_a_render_whose_image_shows_a_bound_value_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    assert rendered(sites, tmp_path, RED_VALUE) == (SPELLED, [])


def test_a_render_whose_image_shows_an_unbound_value_is_kept(
    sites: Sites, tmp_path: Path
) -> None:
    assert rendered(sites, tmp_path, UNBOUND) == ("saved", ["shots/0.png"])


def test_a_live_page_that_shows_a_bound_value_before_any_fill_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, RED_VALUE) == (SPELLED, [])


def test_a_live_page_that_shows_an_unbound_value_is_kept(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, UNBOUND) == ("saved", ["shots/0.png"])


# The red value, Zq7fakeSECx, in base64.
def test_a_base64_spelling_the_image_shows_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, "WnE3ZmFrZVNFQ3g=") == (SPELLED, [])


# The red value in capitals, as a page's text-transform can show it.
def test_the_value_the_image_shows_in_another_case_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, "ZQ7FAKESECX") == (SPELLED, [])


# The red value with each byte percent-encoded, in capital hex.
def test_a_percent_encoded_spelling_the_image_shows_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    shown = "%5A%71%37%66%61%6B%65%53%45%43%78"

    assert reflected(sites, tmp_path, shown) == (SPELLED, [])


# Turned over, which OCR's classifier sees and turns back before reading.
def test_a_value_the_image_shows_upside_down_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    body = (
        "<p style='display:inline-block;margin:20px;font:40px sans-serif;"
        f"transform:rotate(180deg)'>{RED_VALUE}</p>"
    )

    assert shown(sites, tmp_path, body) == (SPELLED, [])


# Split as a page may show a key, in groups: OCR reads the spaces too, and
# can add some of its own.
def test_a_value_the_image_shows_split_by_spaces_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, "Zq7f akeS ECx") == (SPELLED, [])


def phrased(sites: Sites) -> Redactor:
    """A scan of one bound value with spaces in it."""
    return Redactor(
        [
            BoundSecret(
                "PHRASE",
                SecretStr("fake pass phrase"),
                SecretDestination((sites.app,), "password"),
            )
        ]
    )


# Read as OCR read it, too: with its spaces taken out, a value that has
# some isn't the value.
def test_a_value_with_spaces_in_it_the_image_shows_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, "fake pass phrase", phrased(sites)) == (
        SPELLED,
        [],
    )


# Its word spaces and the page's own, which match neither the value as
# written nor the value with OCR's spaces taken out of the text alone.
def test_a_value_with_spaces_in_it_the_image_shows_in_groups_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, "f a k e pass phrase", phrased(sites)) == (
        SPELLED,
        [],
    )


def test_a_run_that_binds_nothing_runs_no_ocr(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[bytes] = []

    def reading(png: bytes, _redactor: Redactor) -> bool:
        read.append(png)
        return True

    monkeypatch.setattr(ocr, "spells_a_value", reading)

    assert reflected(sites, tmp_path, RED_VALUE, NO_SECRETS) == (
        "saved",
        ["shots/0.png"],
    )
    assert read == []


def test_ocr_past_its_bound_withholds_it(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screenshots, "OCR_SECONDS", 0.05)

    assert reflected(sites, tmp_path, UNBOUND) == (
        "the image's OCR took over 0.05 s",
        [],
    )


# A check its bound cut off goes on in its thread, on the process's one
# engine. The next check waits for it inside its own bound, so the two
# never overlap, and is withheld while it waits.
def test_a_check_waits_for_one_its_bound_cut_off_and_is_withheld_meanwhile(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reading, release = threading.Event(), threading.Event()

    def stuck_once(_png: bytes, _redactor: Redactor) -> bool:
        if not reading.is_set():
            reading.set()
            release.wait(10)
        return False

    monkeypatch.setattr(ocr, "spells_a_value", stuck_once)
    monkeypatch.setattr(screenshots, "OCR_SECONDS", 0.05)

    async def scenario() -> tuple[tuple[str, list[str]], tuple[str, list[str]]]:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await session.navigate(f"{sites.app}/kept")
            record = RunRecord.create(tmp_path).redacting(session.redactor)
            first = await saved(session, record)
            await asyncio.to_thread(reading.wait, 10)
            second = await saved(session, record)
            release.set()
            return first, second

    assert asyncio.run(scenario()) == (
        ("the image's OCR took over 0.05 s", []),
        ("the image's OCR took over 0.05 s", []),
    )


# A page of small text, more than OCR reads in its bound on any host.
DENSE = " ".join(f"word{place} lorem ipsum" for place in range(4000))


def test_text_too_long_to_read_in_the_bound_withholds_it(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, DENSE, font="10px sans-serif") == (
        "the image has more text than OCR reads in its bound",
        [],
    )


# 144 numbers far apart. Each is read at the recogniser's shortest, 320/48,
# so the image's text is 960 long in any host's fonts.
SPREAD = (
    "<div style='display:grid;grid-template-columns:repeat(12,1fr);"
    "grid-auto-rows:64px;font:16px sans-serif'>"
    + "".join(f"<div>{place:03}</div>" for place in range(144))
    + "</div>"
)


def test_an_image_just_under_the_most_text_ocr_reads_is_read_in_its_bound(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_the_cap = shown(sites, tmp_path, SPREAD)
    monkeypatch.setattr(ocr, "TEXT_CAP", ocr.TEXT_CAP * 0.95)

    assert (at_the_cap, shown(sites, tmp_path, SPREAD)) == (
        ("saved", ["shots/0.png"]),
        ("the image has more text than OCR reads in its bound", []),
    )


# A line that runs off the screen, so as wide as it in any host's fonts,
# about 55 long, above eleven numbers each read at the recogniser's
# shortest. Read in batches of six, sorted, each as wide as its widest, the
# image's text is about 370 long: the line's batch holds five of the
# numbers. Read one a time, or each batch at its narrowest, it would be
# under 250, and twelve a batch, over 500.
LINE = "lorem ipsum dolor sit amet " * 20
MIXED = (
    "<div style='font:16px sans-serif'>"
    f"<p style='margin:20px;white-space:nowrap'>{LINE}</p>"
    "<div style='display:grid;grid-template-columns:repeat(4,1fr);"
    "grid-auto-rows:64px'>"
    + "".join(f"<div>{place:03}</div>" for place in range(11))
    + "</div></div>"
)


def test_an_images_text_is_counted_as_the_recogniser_reads_it_in_batches(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "TEXT_CAP", 500)
    under = shown(sites, tmp_path, MIXED)
    monkeypatch.setattr(ocr, "TEXT_CAP", 250)

    assert (under, shown(sites, tmp_path, MIXED)) == (
        ("saved", ["shots/0.png"]),
        ("the image has more text than OCR reads in its bound", []),
    )


# Each of the three models missing, beside links to the other two: the
# engine loads each by the path it is given, which no download replaces.
@pytest.mark.parametrize(
    "missing",
    [ocr.DETECT, ocr.CLASSIFY, ocr.RECOGNISE],
    ids=["detection", "classification", "recognition"],
)
def test_a_missing_model_file_saves_nothing_and_downloads_nothing(
    sites: Sites,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    models = tmp_path_factory.mktemp("models")
    for name in {ocr.DETECT, ocr.CLASSIFY, ocr.RECOGNISE} - {missing}:
        (models / name).symlink_to(ocr.MODELS / name)
    monkeypatch.setattr(ocr, "MODELS", models)

    assert reflected(sites, tmp_path, UNBOUND) == (ENGINE_FAILED, [])

    # The run record's own .gitignore: no screenshot, and no model file.
    assert [path.name for path in tmp_path.rglob("*") if path.is_file()] == [
        ".gitignore"
    ]


# Why a capture whose OCR engine raised is withheld (ADR-0035's #251
# amendment).
ENGINE_FAILED = "the image's OCR engine failed"


def test_an_ocr_engine_error_withholds_it_and_keeps_nothing_of_the_error(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_png: bytes, _redactor: Redactor) -> bool:
        raise RuntimeError(f"fake engine failure quoting {RED_VALUE}")

    monkeypatch.setattr(ocr, "spells_a_value", failing)

    async def scenario() -> ScreenshotWithheldError:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await session.navigate(f"{sites.app}/kept")
            with pytest.raises(ScreenshotWithheldError) as withheld:
                await take_screenshot(session)
            return withheld.value

    withheld = asyncio.run(scenario())
    assert str(withheld) == ENGINE_FAILED
    assert (withheld.__cause__, withheld.__context__) == (None, None)


def test_a_defect_outside_the_ocr_engine_still_raises(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("fake defect in the scrub")

    monkeypatch.setattr(screenshots, "_prepared", broken)

    async def scenario() -> None:
        async with browsing(sites, redactor=Redactor(bound(sites.app))) as session:
            await serve_cases(session.page, sites.other)
            await red_filled(session, sites)
            await take_screenshot(session)

    with pytest.raises(RuntimeError, match="fake defect in the scrub"):
        asyncio.run(scenario())
