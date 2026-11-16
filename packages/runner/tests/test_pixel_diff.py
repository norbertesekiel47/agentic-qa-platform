"""`pixel_diff` through the evaluator (DATA_MODEL §7, Pixel diff; ADR-0025's
#324 amendment): the target's crop of the masked screenshot compared with
the baseline committed below the spec root's `.compiled/baselines/`, a pass
or a failure, or the fixed reason of the first case P1 to P11 met, which
leaves every later check not evaluated. With `RunSetup.screenshots`, which
only `strict_replay` sets, a failed check and a refused baseline save the
crop as `pixel_diff/assertion-<index>/crop.png`, the masked render's in a
run that fills a secret; nothing else saves one. Real Chromium against the
executor's fixture app."""

import asyncio
import hashlib
import io
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import Target
from aqa_core.config import ProjectConfig
from aqa_core.project import load_config
from aqa_core.spec import Spec
from aqa_runner import evaluation
from aqa_runner.browser_session import BrowserSession, launched_browser
from aqa_runner.evidence import Held, held_screenshot
from aqa_runner.executor import RunSetup
from aqa_runner.locators import Resolved, Use
from aqa_runner.pixel_diff import Box
from PIL import Image
from playwright.async_api import ElementHandle, async_playwright

from packages.runner.tests.executor_fixtures import (
    PAGES,
    App,
    Run,
    a_spec,
    compiled,
    run,
    serving_app,
)
from packages.runner.tests.screenshot_pages import FIELD, RED, RED_VALUE, red_pixels
from packages.runner.tests.secret_fixtures import secret_spec

# The order's badge, a solid blue block 40 by 24 CSS pixels at (10, 20);
# recoloured, hidden, with no width, half below the fold or gone, by its
# state.
PAGE = """<div id="badge" style="position:absolute;left:10px;top:20px;width:40px;
    height:24px;background:#3366cc"></div>
    <script>
        const state = new URLSearchParams(location.search).get("state");
        const badge = document.querySelector("#badge");
        if (state === "recoloured") badge.style.background = "#cc3333";
        if (state === "hidden") badge.style.display = "none";
        if (state === "no-width") badge.style.width = "0";
        if (state === "below") badge.style.top = "calc(100vh - 12px)";
        if (state === "missing") badge.remove();
    </script>"""
BLUE = (51, 102, 204, 255)
RED_BADGE = (204, 51, 51, 255)
SIZE = (40, 24)
BASELINE = "order/badge.png"
TARGETS = {"badge": {"semantic": "the order's badge", "locators": [{"css": "#badge"}]}}
AFTER: dict[str, Any] = {
    "id": "after",
    "expect_index": 0,
    "check": "url_matches",
    "pattern": "/",
}
CROP = "pixel_diff/assertion-0/crop.png"
LATER = "not evaluated after badge's look at the page raised"
# A drift is found sooner than the default 10 s.
QUICK = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})


def png(colour: tuple[int, ...], size: tuple[int, int] = SIZE) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, colour).save(out, format="PNG")
    return out.getvalue()


def pinned(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def check(data: bytes = png(BLUE), **changed: object) -> dict[str, Any]:
    """The badge's pixel_diff check against `BASELINE`, pinned to `data`,
    with not a pixel allowed to differ."""
    return {
        "id": "badge",
        "expect_index": 0,
        "check": "pixel_diff",
        "target": "badge",
        "baseline": BASELINE,
        "baseline_sha256": pinned(data),
        "max_diff_ratio": 0,
        "pixel_threshold": 0,
    } | changed


def committed(root: Path, data: bytes, name: str = BASELINE) -> Path:
    """`data` committed as the baseline `name` under the spec root `root`."""
    path = root / ".compiled" / "baselines" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setitem(PAGES, "pixel", PAGE)
    with serving_app() as served:
        yield served


def replayed(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    badge: dict[str, Any],
    *,
    screenshots: bool = True,
) -> Run:
    """A replay of `badge` then `AFTER` on the badge page in `state`, as
    strict replay runs one: its baselines below the spec root `tmp_path`,
    and the crops it saves asked for, unless `screenshots` is off."""
    monkeypatch.setattr(
        "packages.runner.tests.executor_fixtures.RunSetup",
        partial(RunSetup, screenshots=screenshots, spec_root=tmp_path),
    )
    return run(
        app,
        tmp_path,
        compiled([], targets=TARGETS, assertions=[badge, AFTER]),
        config=QUICK,
        spec=a_spec(tmp_path, QUICK, start_url=f"/page/pixel?state={state}"),
    )


def saved(done: Run) -> bytes | None:
    """The crop the run saved for the badge's check, if it saved one."""
    path = done.record.path / CROP
    return path.read_bytes() if path.exists() else None


def colours(data: bytes) -> object:
    image = Image.open(io.BytesIO(data))
    return image.format, image.size, image.getcolors()


def test_a_target_that_matches_its_baseline_passes_and_saves_no_crop(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    assert [a.outcome for a in done.result.assertions] == ["pass", "pass"]
    assert done.result.assertions[0].locator_index == 0
    assert done.result.outcome == "passed"
    assert saved(done) is None


def test_a_recoloured_target_fails_and_saves_its_crop(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "recoloured", check())

    assert [a.outcome for a in done.result.assertions] == ["failed", "pass"]
    assert done.result.assertions[0].locator_index == 0
    assert done.result.outcome == "failed"
    crop = saved(done)
    assert crop is not None
    assert colours(crop) == ("PNG", SIZE, [(40 * 24, RED_BADGE)])


@pytest.mark.parametrize(("ratio", "outcome"), [(0.05, "pass"), (0.04, "failed")])
def test_a_crop_that_differs_in_one_row_passes_only_within_its_ratio(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ratio: float,
    outcome: str,
) -> None:
    # One row of the baseline is red: 40 of the 960 pixels differ.
    image = Image.new("RGBA", SIZE, BLUE)
    image.paste(RED_BADGE, (0, 0, 40, 1))
    out = io.BytesIO()
    image.save(out, format="PNG")
    data = out.getvalue()
    committed(tmp_path, data)

    done = replayed(
        app, tmp_path, monkeypatch, "clear", check(data, max_diff_ratio=ratio)
    )

    assert [a.outcome for a in done.result.assertions] == [outcome, "pass"]


@pytest.mark.parametrize(("threshold", "outcome"), [(10, "pass"), (9, "failed")])
def test_a_baseline_a_shade_off_passes_only_within_its_threshold(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    threshold: int,
    outcome: str,
) -> None:
    # Every pixel's red is 10 above the badge's.
    data = png((61, 102, 204, 255))
    committed(tmp_path, data)

    done = replayed(
        app, tmp_path, monkeypatch, "clear", check(data, pixel_threshold=threshold)
    )

    assert [a.outcome for a in done.result.assertions] == [outcome, "pass"]


def test_a_failed_check_saves_no_crop_in_a_replay_that_asks_for_none(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed(tmp_path, png(BLUE))

    done = replayed(
        app, tmp_path, monkeypatch, "recoloured", check(), screenshots=False
    )

    assert [a.outcome for a in done.result.assertions] == ["failed", "pass"]
    assert saved(done) is None


def test_a_missing_target_is_drift(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "missing", check())

    first = done.result.assertions[0]
    assert (first.outcome, first.locator_index) == ("binding_unresolved", None)
    assert first.misses
    assert saved(done) is None


def refused_with(done: Run, reason: str) -> None:
    """The badge's check was refused for `reason`, and the check after it
    wasn't evaluated."""
    first, after = done.result.assertions
    assert (first.outcome, first.error) == ("not_evaluated", reason)
    assert first.locator_index is None
    assert (after.outcome, after.error) == ("not_evaluated", LATER)
    assert done.result.outcome == "errored"


def test_a_target_in_a_frame_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def in_frame(session: BrowserSession, _target: Target, use: Use) -> Resolved:
        assert use == "assertion"
        frame = next(
            frame for frame in session.page.frames if frame.parent_frame is not None
        )
        element = await frame.query_selector("button")
        assert element is not None
        return Resolved(0, element)

    monkeypatch.setattr(BrowserSession, "resolve", in_frame)
    monkeypatch.setitem(PAGES, "pixel", '<iframe src="/page/form"></iframe>')
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(done, "pixel_diff does not support elements inside frames")
    assert saved(done) is None


# An assertion's target resolves once it is attached, shown or not.
@pytest.mark.parametrize("state", ["hidden", "no-width"])
def test_a_target_with_no_box_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, state, check())

    refused_with(done, "pixel_diff's target has no box to compare")
    assert saved(done) is None


def test_a_target_half_below_the_fold_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "below", check())

    refused_with(done, "pixel_diff compares only a target wholly inside the viewport")
    assert saved(done) is None


def test_a_withheld_screenshot_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def withheld(_session: BrowserSession) -> Held:
        return "the image's text spells a bound value"

    monkeypatch.setattr(evaluation, "held_screenshot", withheld)
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(done, "pixel_diff's screenshot was withheld")
    assert saved(done) is None


def test_a_target_that_moves_while_the_page_is_captured_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def moving(session: BrowserSession) -> Held:
        held = await held_screenshot(session)
        await session.page.evaluate(
            "document.querySelector('#badge').style.left = '11px'"
        )
        return held

    monkeypatch.setattr(evaluation, "held_screenshot", moving)
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(done, "pixel_diff's target moved while the page was captured")
    assert saved(done) is None


def test_a_screenshot_not_at_the_viewports_size_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = BrowserSession.resolve

    async def widening(
        session: BrowserSession, target: Target, use: Use, *, every: bool = False
    ) -> object:
        # The page stays 1280 px wide, and its capture too; the session now
        # says its viewport is 1281.
        session.settings = session.settings.model_copy(update={"viewport": (1281, 800)})
        return await resolve(session, target, use, every=every)

    monkeypatch.setattr(BrowserSession, "resolve", widening)
    committed(tmp_path, png(BLUE))

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(
        done, "pixel_diff could not read the page's screenshot at the viewport's size"
    )
    assert saved(done) is None


def test_a_missing_baseline_is_refused_and_saves_the_crop(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(done, "pixel_diff's baseline file is missing")
    crop = saved(done)
    assert crop is not None
    assert colours(crop) == ("PNG", SIZE, [(40 * 24, BLUE)])


def test_a_replay_with_no_baselines_place_refuses_at_once(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked: list[str] = []

    async def box(_session: BrowserSession, _element: ElementHandle) -> Box | None:
        looked.append("box")
        return None

    async def captured(_session: BrowserSession) -> Held:
        looked.append("capture")
        return "never"

    monkeypatch.setattr(BrowserSession, "box", box)
    monkeypatch.setattr(evaluation, "held_screenshot", captured)
    committed(tmp_path, png(BLUE))
    monkeypatch.setattr(
        "packages.runner.tests.executor_fixtures.RunSetup",
        partial(RunSetup, screenshots=True),
    )

    done = run(
        app,
        tmp_path,
        compiled([], targets=TARGETS, assertions=[check(), AFTER]),
        config=QUICK,
        spec=a_spec(tmp_path, QUICK, start_url="/page/pixel?state=clear"),
    )

    refused_with(done, "pixel_diff's baseline file is missing")
    assert looked == []
    assert saved(done) is None


@pytest.mark.parametrize("link", ["file", "directory"])
def test_a_baseline_reached_through_a_link_is_refused_and_saves_the_crop(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link: str
) -> None:
    real = committed(tmp_path / "elsewhere", png(BLUE))
    place = tmp_path / ".compiled" / "baselines"
    if link == "file":
        (place / "order").mkdir(parents=True)
        (place / BASELINE).symlink_to(real)
    else:
        place.mkdir(parents=True)
        (place / "order").symlink_to(real.parent)

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(done, "pixel_diff could not read its baseline file")
    assert saved(done) is not None


def test_a_baseline_that_isnt_a_regular_file_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed(tmp_path, b"").unlink()
    (tmp_path / ".compiled" / "baselines" / BASELINE).mkdir()

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(done, "pixel_diff could not read its baseline file")
    assert saved(done) is not None


def test_a_baseline_its_sha256_doesnt_pin_is_refused_and_saves_the_crop(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed(tmp_path, png(RED_BADGE))

    done = replayed(app, tmp_path, monkeypatch, "clear", check(png(BLUE)))

    refused_with(done, "pixel_diff's baseline file isn't the one baseline_sha256 pins")
    assert saved(done) is not None


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"not a png", "pixel_diff could not decode its baseline as a PNG"),
        (png(BLUE)[:-30], "pixel_diff could not decode its baseline as a PNG"),
        (
            png(BLUE, (40, 25)),
            "pixel_diff's baseline isn't the size of its target's crop",
        ),
    ],
    ids=["not a PNG", "truncated", "another size"],
)
def test_a_baseline_that_doesnt_decode_at_the_crops_size_is_refused(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    reason: str,
) -> None:
    committed(tmp_path, data)

    done = replayed(app, tmp_path, monkeypatch, "clear", check(data))

    refused_with(done, reason)
    assert saved(done) is not None


def test_the_look_at_the_box_is_bounded_and_lets_its_element_go(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[ElementHandle] = []
    disposed: list[ElementHandle] = []
    dispose = ElementHandle.dispose

    async def disposing(element: ElementHandle) -> None:
        disposed.append(element)
        await dispose(element)

    async def unanswered(_session: BrowserSession, element: ElementHandle) -> Box:
        observed.append(element)
        await asyncio.Event().wait()
        raise AssertionError("answered")

    monkeypatch.setattr(ElementHandle, "dispose", disposing)
    monkeypatch.setattr(BrowserSession, "box", unanswered)
    committed(tmp_path, png(BLUE))
    started = time.monotonic()

    done = replayed(app, tmp_path, monkeypatch, "clear", check())

    refused_with(done, "the page didn't answer within 2 s")
    assert len(observed) == 1
    assert disposed.count(observed[0]) == 1
    assert time.monotonic() - started < 10


# A field drawn in pure red, which the red test secret goes in, inside the
# block the check compares.
SECRET_PAGE = (
    f"<style>body{{margin:0;background:#fff}}input{{{RED}}}</style>"
    '<div id="badge" style="position:absolute;left:0;top:0;width:520px;'
    f'height:90px;background:#fff">{FIELD}</div>'
)
# The red test secret, as `screenshot_pages.bound` binds it: RED, for the
# field named Secret, on the run's start origin.
RED_BINDING = "RED: { origins: [start], field: { role: textbox, name: Secret } }"


def into_the_field(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"seq": 1, "target": "field", "side_effect": False} | step]


@pytest.mark.parametrize(
    ("step", "red"),
    [
        ({"action": "fill_secret", "secret": "RED"}, False),
        ({"action": "fill", "value": "Plain Unbound"}, True),
    ],
    ids=["the red test secret", "an unbound value"],
)
def test_a_crop_in_a_run_that_fills_a_secret_is_the_masked_render(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: dict[str, Any],
    red: bool,
) -> None:
    monkeypatch.setenv("AQA_SECRET_RED", RED_VALUE)
    monkeypatch.setitem(PAGES, "secret-field", SECRET_PAGE)
    spec: Spec = secret_spec(
        tmp_path,
        RED_BINDING,
        account="{ email: reader@example.test, password: { secret: RED } }",
        start_url="/page/secret-field",
    )
    config = load_config(tmp_path / "qa" / "config.yaml")
    monkeypatch.setattr(
        "packages.runner.tests.executor_fixtures.RunSetup",
        partial(RunSetup, screenshots=True, spec_root=tmp_path),
    )
    targets = TARGETS | {
        "field": {"semantic": "the secret field", "locators": [{"css": "#f"}]}
    }

    done = run(
        app,
        tmp_path,
        compiled(into_the_field(step), targets=targets, assertions=[check(), AFTER]),
        config=config,
        spec=spec,
    )

    assert [s.outcome for s in done.result.steps] == ["completed", "completed"]
    refused_with(done, "pixel_diff's baseline file is missing")
    crop = saved(done)
    assert crop is not None
    assert Image.open(io.BytesIO(crop)).size == (520, 90)
    assert (reds(crop) > 0) is red


def reds(data: bytes) -> int:
    """How many of the PNG `data`'s pixels are pure red (`red_pixels`)."""

    async def counted() -> int:
        async with (
            async_playwright() as playwright,
            launched_browser(playwright.chromium) as browser,
        ):
            return await red_pixels(await browser.new_page(), data)

    return asyncio.run(counted())
