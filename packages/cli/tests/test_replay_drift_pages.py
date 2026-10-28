"""`aqa replay` on the drift fixture pages (#240; ADR-0024's #238 amendment,
ADR-0025's #25 amendment): each script recorded against the clean order
page passes there, and each drift of it replays to its pinned outcome. A
binding the locators don't all agree on is drift, `inconclusive`, never
`expectation_violated`. Real Chromium against the executor's fixture app,
serving one page at `/page/order`."""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from aqa_core.compiled import CompiledScript

from packages.cli.tests.test_explore_runs import record_in
from packages.cli.tests.test_replay import SECONDS, committed, document, on, replay
from packages.runner.tests.drift_fixtures import (
    BUTTON,
    DETAILS,
    IN_VIEW,
    SHIPPING,
    order_spec,
    page,
    serving,
)


@pytest.mark.parametrize(
    ("script", "expectation"),
    [
        pytest.param(DETAILS, SHIPPING, id="details"),
        pytest.param(BUTTON, IN_VIEW, id="button"),
    ],
)
def test_each_recorded_script_passes_on_the_clean_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: CompiledScript,
    expectation: str,
) -> None:
    spec = committed(order_spec(tmp_path / "qa", expectation), script)

    with serving(page("order")) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 0, result.output
    assert re.fullmatch(rf"passed order: {SECONDS}, \$0\n", result.stdout)
    assert result.stderr == ""


@dataclass(frozen=True)
class Pinned:
    """What `aqa replay` of `script`, under a spec expecting `expectation`,
    gives on the drift page `page`: its outcome and exit code, the one
    reason under its line, and `replay.json`'s verdicts, each as what it
    judges, the verdict, its reason and its finding."""

    page: str
    script: CompiledScript
    expectation: str
    outcome: str
    code: int
    reason: str
    verdicts: list[tuple[object, ...]]


def drifted(name: str, misses: str) -> Pinned:
    """DETAILS on the drift page `name`, whose click drifts with `misses`:
    step 0 passes, step 1 is inconclusive, and no check is evaluated, so
    nothing is `expectation_violated` and nothing is a finding."""
    reason = f"drift: strict replay doesn't heal ({misses})"
    return Pinned(
        name,
        DETAILS,
        SHIPPING,
        "inconclusive",
        3,
        f"step 1: inconclusive, {reason}",
        [(0, "pass", None, None), (1, "inconclusive", reason, None)],
    )


# BUTTON under the modal: an assertion's covered target still resolves, so
# the check is false.
OCCLUDED = Pinned(
    "modal",
    BUTTON,
    IN_VIEW,
    "expectation_violated",
    1,
    "a1: expectation_violated",
    [
        (0, "pass", None, None),
        (
            "a1",
            "expectation_violated",
            None,
            {"category": "visual", "title": IN_VIEW, "repro_steps": [0]},
        ),
        ("console_errors", "pass", None, None),
        ("js_exceptions", "pass", None, None),
        ("http_5xx", "pass", None, None),
        ("broken_images", "pass", None, None),
    ],
)


@pytest.mark.parametrize(
    "pinned",
    [
        # One locator still finds the toggle; every locator must agree (#25).
        pytest.param(drifted("moved", "not unanimous, no match"), id="moved"),
        pytest.param(drifted("renamed", "no match, not unanimous"), id="renamed"),
        pytest.param(drifted("removed", "no match, no match"), id="removed"),
        pytest.param(OCCLUDED, id="modal"),
        # An action's target must take the click: under the modal it can't.
        pytest.param(drifted("modal", "not actionable, not actionable"), id="covered"),
    ],
)
def test_a_drifted_page_replays_to_its_pinned_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: Pinned
) -> None:
    root = tmp_path / "qa"
    spec = committed(order_spec(root, pinned.expectation), pinned.script)

    with serving(page(pinned.page)) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == pinned.code, result.output
    assert result.stdout == ""
    record = re.escape(str(record_in(root)))
    assert re.fullmatch(
        rf"{pinned.outcome} order: {SECONDS}, \$0, run record: {record}\n"
        rf"  {re.escape(pinned.reason)}\n",
        result.stderr,
    )
    assert [
        (
            next(v[key] for key in ("step", "assertion", "invariant") if key in v),
            v["verdict"],
            v["reason"],
            v["finding"],
        )
        for v in document(root)["verdicts"]
    ] == pinned.verdicts
