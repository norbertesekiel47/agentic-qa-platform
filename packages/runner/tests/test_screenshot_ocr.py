"""The OCR check on screenshots (#250, ADR-0035): in a run that binds a test
secret, a capture whose image's text spells a bound value, live or
rendered, is withheld, and the record saves nothing."""

import asyncio
import html
import math
import os
import time
from base64 import b64encode
from collections.abc import Iterator
from importlib.metadata import version
from itertools import batched
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from aqa_core.project import SecretDestination
from aqa_runner import ocr, screenshots
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import ScreenshotWithheldError, take_screenshot
from pydantic import SecretStr
from rapidocr.main import RapidOCR, RapidOCRError

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
    """What `saved` reports of a page that shows `text` in `font`, as an
    app's server can show a bound value before any fill, so the capture is
    the live page's, in a session that scans with `redactor` (by default,
    of the test's bound secrets)."""
    page = (
        "<!doctype html><meta charset=utf-8>"
        f"<p style='font:{font};margin:20px'>{html.escape(text)}</p>"
    )

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


def test_a_base64_spelling_the_image_shows_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    spelled = b64encode(RED_VALUE.encode()).decode()

    assert reflected(sites, tmp_path, spelled) == (SPELLED, [])


# Split as a page may show a key, in groups: OCR reads the spaces too, and
# can add some of its own.
def test_a_value_the_image_shows_split_by_spaces_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    grouped = " ".join(RED_VALUE[start : start + 4] for start in range(0, 11, 4))

    assert reflected(sites, tmp_path, grouped) == (SPELLED, [])


# Read as OCR read it, too: with its spaces taken out, a value that has
# some isn't the value.
def test_a_value_with_spaces_in_it_the_image_shows_is_dropped(
    sites: Sites, tmp_path: Path
) -> None:
    phrase = BoundSecret(
        "PHRASE",
        SecretStr("fake pass phrase"),
        SecretDestination((sites.app,), "password"),
    )

    assert reflected(sites, tmp_path, "fake pass phrase", Redactor([phrase])) == (
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


# A page of small text, more than OCR reads in its bound on any host.
DENSE = " ".join(f"word{place} lorem ipsum" for place in range(4000))


def test_text_too_long_to_read_in_the_bound_withholds_it(
    sites: Sites, tmp_path: Path
) -> None:
    assert reflected(sites, tmp_path, DENSE, font="10px sans-serif") == (
        "the image has more text than OCR reads in its bound",
        [],
    )


def test_a_missing_model_file_saves_nothing_and_downloads_nothing(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(ocr, "MODELS", models)

    with pytest.raises(FileNotFoundError):
        reflected(sites, tmp_path, UNBOUND)

    # The run record's own .gitignore: no screenshot, and no model file.
    assert [path.name for path in tmp_path.rglob("*") if path.is_file()] == [
        ".gitignore"
    ]


# Throwaway (m2/250-calibration, never merged): times the OCR check's stages
# on CI's runner, to set `ocr.TEXT_CAP` there as `ARCHIVE_CHARS` was set
# (LAB_NOTES, 2026-10-28), then fails with every timing so the log shows them.
SCRATCH_IMAGES = Path(__file__).parent / "ocr_calibration"
# Each target batched length, and the font size and line height of the
# 1280x800 page of lines whose length is the first at or over it.
SCRATCH_TARGETS = {
    300: (12, 1.3),
    500: (12, 1.3),
    1_000: (12, 1.3),
    2_000: (12, 1.3),
    9_000: (7, 1.15),
}
SCRATCH_RUNS = 3
SCRATCH_LINES = """([px, height, count]) => {
    document.body.style.cssText = `margin:8px;font:${px}px sans-serif;line-height:${height}`;
    document.body.replaceChildren(...Array.from({length: count}, (_, place) => {
        const line = document.createElement("div");
        line.style.cssText = "white-space:nowrap;overflow:hidden";
        line.textContent = Array.from(
            {length: 30}, (_, word) => `order ${place * 30 + word} ships once payment clears`,
        ).join(" ");
        return line;
    }));
}"""


def scratch_engine() -> RapidOCR:
    """An engine configured as `aqa_runner.ocr` configures its own."""
    return RapidOCR(
        params={
            "Det.model_path": str(ocr.MODELS / ocr.DETECT),
            "Cls.model_path": str(ocr.MODELS / ocr.CLASSIFY),
            "Rec.model_path": str(ocr.MODELS / ocr.RECOGNISE),
            "Global.log_level": "critical",
        }
    )


def scratch_detected(
    engine: RapidOCR, png: bytes
) -> tuple[list[np.ndarray[Any, Any]], float]:
    """`png`'s detected lines, as `ocr.spells_a_value` detects them, and the
    seconds that took, decoding included."""
    start = time.perf_counter()
    image, record = engine.preprocess_img(engine.load_img(png))
    try:
        crops, _ = engine.detect_and_crop(image, record)
    except RapidOCRError:
        crops = []
    return crops, time.perf_counter() - start


def scratch_length(crops: list[np.ndarray[Any, Any]]) -> float:
    """The batched length, as `ocr.spells_a_value` counts it: each line at
    least 320/48, in sorted batches of six, each as wide as its widest."""
    widths = sorted(max(320 / 48, crop.shape[1] / crop.shape[0]) for crop in crops)
    batches = batched(widths, 6, strict=False)
    length: float = sum(len(batch) * batch[-1] for batch in batches)
    return length


def scratch_read(
    engine: RapidOCR, crops: list[np.ndarray[Any, Any]]
) -> tuple[float, float]:
    """The seconds classification and recognition take on `crops`."""
    if not crops:
        return 0.0, 0.0
    start = time.perf_counter()
    rotated, _ = engine.cls_and_rotate(crops)
    classified = time.perf_counter()
    engine.recognize_txt(rotated)
    return classified - start, time.perf_counter() - classified


def scratch_pages(sites: Sites, engine: RapidOCR) -> dict[str, bytes]:
    """For each target, the live capture of the page of lines whose batched
    length is the first at or over it (the most lines that fit, if none)."""

    async def scenario() -> dict[str, bytes]:
        pages: dict[str, bytes] = {}
        async with browsing(sites) as session:
            await session.page.route(
                "**/lines",
                lambda route: route.fulfill(
                    body="<!doctype html><body></body>", content_type="text/html"
                ),
            )
            await session.navigate(f"{sites.app}/lines")

            async def lines(px: int, height: float, count: int) -> bytes:
                await session.page.evaluate(SCRATCH_LINES, [px, height, count])
                return (await take_screenshot(session)).png

            for target, (px, height) in SCRATCH_TARGETS.items():
                most = int((800 - 16) // (px * height))
                found: tuple[int, bytes] | None = None
                low, high = 1, most
                while low <= high:
                    count = (low + high) // 2
                    png = await lines(px, height, count)
                    if scratch_length(scratch_detected(engine, png)[0]) >= target:
                        found, high = (count, png), count - 1
                    else:
                        low = count + 1
                if found is None:
                    found = (most, await lines(px, height, most))
                pages[f"text-{target} {px}px {found[0]} lines"] = found[1]
        return pages

    return asyncio.run(scenario())


def test_scratch_ci_ocr_timings(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redactor = Redactor(bound(sites.app))
    login = (SCRATCH_IMAGES / "control-login-1440.png").read_bytes()
    # The first check with a models directory builds its engine and loads
    # the models, inside the bound, as a run's first check does; the second
    # is warm. Links to the same files make a directory no check has used.
    models = tmp_path / "models"
    models.mkdir()
    for name in (ocr.DETECT, ocr.CLASSIFY, ocr.RECOGNISE):
        (models / name).symlink_to(ocr.MODELS / name)
    monkeypatch.setattr(ocr, "MODELS", models)
    start = time.perf_counter()
    ocr.spells_a_value(login, redactor)
    first = time.perf_counter() - start
    start = time.perf_counter()
    ocr.spells_a_value(login, redactor)
    warm = time.perf_counter() - start

    engine = scratch_engine()
    images = {
        path.name: path.read_bytes() for path in sorted(SCRATCH_IMAGES.glob("*.png"))
    } | scratch_pages(sites, engine)
    rows = [
        (
            f"{'image':38} {'length':>7} {'lines':>5} {'detect s':>11}"
            f" {'classify s':>11} {'recognise s':>13} {'ms/unit':>11}"
        )
    ]
    detects: dict[str, float] = {}
    units: dict[str, float] = {}
    for name, png in images.items():
        runs: list[tuple[float, float, float, float]] = []
        for _ in range(SCRATCH_RUNS):
            crops, detect = scratch_detected(engine, png)
            length = scratch_length(crops)
            classify, recognise = scratch_read(engine, crops)
            per_unit = (classify + recognise) * 1000 / length if length else 0.0
            runs.append((detect, classify, recognise, per_unit))
        detect_s, classify_s, recognise_s, per_unit_s = zip(*runs, strict=True)
        detects[name], units[name] = max(detect_s), max(per_unit_s)
        rows.append(
            f"{name:38} {length:7.0f} {len(crops):5}"
            f" {min(detect_s):5.3f}-{max(detect_s):5.3f}"
            f" {min(classify_s):5.3f}-{max(classify_s):5.3f}"
            f" {min(recognise_s):6.3f}-{max(recognise_s):6.3f}"
            f" {min(per_unit_s):5.2f}-{max(per_unit_s):5.2f}"
        )
    slowest_detect = max(detects, key=detects.__getitem__)
    slowest_unit = max(units, key=units.__getitem__)
    cap = math.floor(
        (screenshots.OCR_SECONDS / 2 - detects[slowest_detect])
        / units[slowest_unit]
        * 1000
    )
    cpu = Path("/proc/cpuinfo")
    model = next(
        (
            line
            for line in (cpu.read_text().splitlines() if cpu.exists() else [])
            if line.startswith("model name")
        ),
        "model name: unknown",
    )
    report = [
        "SCRATCH-CI-OCR-TIMINGS",
        f"cpus={os.cpu_count()} {model}",
        (
            f"rapidocr={version('rapidocr')} onnxruntime={version('onnxruntime')}"
            f" TEXT_CAP={ocr.TEXT_CAP} OCR_SECONDS={screenshots.OCR_SECONDS}"
            f" runs={SCRATCH_RUNS} (each range is min-max over the runs)"
        ),
        (
            "first check of control-login-1440.png (engine built, models loaded):"
            f" {first:.3f} s; warm: {warm:.3f} s"
        ),
        *(f"SCRATCH {row}" for row in rows),
        f"slowest detect: {detects[slowest_detect]:.3f} s ({slowest_detect})",
        f"slowest ms/unit: {units[slowest_unit]:.2f} ({slowest_unit})",
        (
            "TEXT_CAP = floor((OCR_SECONDS/2 - slowest detect) / slowest ms/unit)"
            f" = {cap}"
        ),
    ]
    pytest.fail("\n".join(report))
