"""`aqa replay` of a `pixel_diff` check on the pixel_diff fixture pages (#324;
DATA_MODEL §7, Pixel diff; ADR-0025's #324 amendment), following the
documented way a person makes a baseline: a replay with no baseline file is
inconclusive and leaves the badge's crop in its run record; that crop,
copied below `.compiled/baselines/` and pinned by its sha256, makes the next
replay of the same page pass, and a page whose badge differs a visual
violation. Strict replay compares it with no model client. Real Chromium
against the executor's fixture app, serving one page at `/page/order`."""

import hashlib
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import anthropic
import pytest
from aqa_core.compiled import CompiledScript
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.model_router import ModelRouter
from langchain_anthropic import ChatAnthropic

from packages.cli.tests.test_explore_runs import record_in
from packages.cli.tests.test_replay import SECONDS, committed, on, replay, viewer
from packages.cli.tests.test_replay_drift_pages import judged
from packages.runner.tests.drift_fixtures import order_spec, serving
from packages.runner.tests.executor_fixtures import compiled

PIXEL_DIFF = Path(__file__).resolve().parents[3] / "tests/fixtures/pages/pixel_diff"
SHOWN = "The order's badge shows as it was reviewed"
BASELINE = "order/badge.png"
CROP = "pixel_diff/assertion-0/crop.png"
MISSING = "pixel_diff's baseline file is missing"
INVARIANTS = [
    ("console_errors", "pass", None, None),
    ("js_exceptions", "pass", None, None),
    ("http_5xx", "pass", None, None),
    ("broken_images", "pass", None, None),
]


def badge(pinned: str = "sha256:" + "0" * 64) -> CompiledScript:
    """The badge's pixels against `BASELINE`, pinned to `pinned`, as a person
    writes the check by hand: a placeholder sha256 until the baseline is
    made."""
    return compiled(
        [],
        targets={
            "badge": {"semantic": "the order's badge", "locators": [{"css": "#badge"}]}
        },
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "pixel_diff",
                "target": "badge",
                "baseline": BASELINE,
                "baseline_sha256": pinned,
                "max_diff_ratio": 0,
                "pixel_threshold": 0,
            }
        ],
    )


def page(name: str) -> str:
    """The pixel_diff page `name`."""
    return (PIXEL_DIFF / f"{name}.html").read_text()


def only_record(root: Path) -> Path:
    """The one run record under the spec root `root`, moved out of the way
    once read, so the next replay's is the only one there."""
    [record] = (root / ".aqa" / "runs").iterdir()
    kept = root.parent / "kept" / record.name
    kept.parent.mkdir(exist_ok=True)
    return Path(shutil.move(record, kept))


def pinned_from(crop: Path, spec: Path) -> Path:
    """`crop` copied to `BASELINE` below the spec root's
    `.compiled/baselines/`, and the badge's check, committed again, pinned to
    its sha256 (`shasum -a 256`); the baseline's path."""
    baseline = spec.parent / ".compiled" / "baselines" / BASELINE
    baseline.parent.mkdir(parents=True)
    shutil.copyfile(crop, baseline)
    committed(
        spec, badge("sha256:" + hashlib.sha256(baseline.read_bytes()).hexdigest())
    )
    return baseline


def test_a_baseline_made_from_a_replays_crop_passes_and_a_differing_badge_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(order_spec(root, SHOWN), badge())

    # 1. No baseline file yet: inconclusive, and the crop is in the record.
    with serving(page("matches")) as app:
        missing = replay(monkeypatch, on(app, spec))

    assert missing.exit_code == 3, missing.output
    assert missing.stdout == ""
    assert f"  a1: inconclusive, {MISSING}\n" in missing.stderr
    assert judged(root) == [
        (0, "pass", None, None),
        ("a1", "inconclusive", MISSING, None),
        *INVARIANTS,
    ]
    assert f", run record: {record_in(root)}\n" in missing.stderr
    crop = only_record(root) / CROP

    # 2. The crop copied below .compiled/baselines/ and pinned: it passes.
    baseline = pinned_from(crop, spec)

    with serving(page("matches")) as app:
        matched = replay(monkeypatch, on(app, spec))

    assert matched.exit_code == 0, matched.output
    assert re.fullmatch(
        rf"passed order: {SECONDS}, \$0\n{re.escape(viewer(root))}", matched.stdout
    )
    assert judged(root) == [
        (0, "pass", None, None),
        ("a1", "pass", None, None),
        *INVARIANTS,
    ]
    assert not (only_record(root) / CROP).exists()

    # 3. A badge of another colour: a visual violation, and its crop saved.
    with serving(page("differs")) as app:
        differs = replay(monkeypatch, on(app, spec))

    assert differs.exit_code == 1, differs.output
    assert differs.stdout == ""
    assert "  a1: expectation_violated\n" in differs.stderr
    assert judged(root) == [
        (0, "pass", None, None),
        (
            "a1",
            "expectation_violated",
            None,
            {"category": "visual", "title": SHOWN, "repro_steps": [0]},
        ),
        *INVARIANTS,
    ]
    assert (only_record(root) / CROP).read_bytes() != baseline.read_bytes()


def test_strict_replay_of_a_pixel_diff_check_builds_no_model_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    built: list[str] = []

    class ModelClientBuiltError(Exception):
        pass

    def refuse(name: str) -> Callable[..., None]:
        def constructor(*_: object, **__: object) -> None:
            built.append(name)
            raise ModelClientBuiltError(name)

        return constructor

    clients: list[type] = [
        ModelRouter,
        AnthropicClient,
        ChatAnthropic,
        anthropic.Anthropic,
        anthropic.AsyncAnthropic,
    ]
    for client in clients:
        monkeypatch.setattr(client, "__init__", refuse(client.__name__))
    # The control: building any of them now is caught.
    for client in clients:
        with pytest.raises(ModelClientBuiltError):
            client()
    built.clear()
    root = tmp_path / "qa"
    spec = committed(order_spec(root, SHOWN), badge())

    with serving(page("matches")) as app:
        missing = replay(monkeypatch, on(app, spec))
        pinned_from(only_record(root) / CROP, spec)
        matched = replay(monkeypatch, on(app, spec))

    assert (missing.exit_code, matched.exit_code) == (3, 0), matched.output
    assert built == []
