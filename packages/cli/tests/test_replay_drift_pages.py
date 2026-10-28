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


def judged(root: Path) -> list[tuple[object, ...]]:
    """`replay.json`'s verdicts under the spec root `root`, each as what it
    judges, the verdict, its reason and its finding."""
    return [
        (
            next(v[key] for key in ("step", "assertion", "invariant") if key in v),
            v["verdict"],
            v["reason"],
            v["finding"],
        )
        for v in document(root)["verdicts"]
    ]


@dataclass(frozen=True)
class Pinned:
    """What `aqa replay` of `script`, under a spec expecting `expectation`,
    gives on the drift page `page`: its outcome and exit code, the one
    reason under its line, and its verdicts as `judged` gives them."""

    page: str
    script: CompiledScript
    expectation: str
    outcome: str
    code: int
    reason: str
    verdicts: list[tuple[object, ...]]


@pytest.mark.parametrize(
    "pinned",
    [
        # One locator still finds the toggle; every locator must agree (#25).
        pytest.param(
            Pinned(
                page="moved",
                script=DETAILS,
                expectation=SHIPPING,
                outcome="inconclusive",
                code=3,
                reason="step 1: inconclusive, drift: strict replay doesn't heal (not unanimous, no match)",
                verdicts=[
                    (0, "pass", None, None),
                    (
                        1,
                        "inconclusive",
                        "drift: strict replay doesn't heal (not unanimous, no match)",
                        None,
                    ),
                ],
            ),
            id="moved",
        ),
        pytest.param(
            Pinned(
                page="renamed",
                script=DETAILS,
                expectation=SHIPPING,
                outcome="inconclusive",
                code=3,
                reason="step 1: inconclusive, drift: strict replay doesn't heal (no match, not unanimous)",
                verdicts=[
                    (0, "pass", None, None),
                    (
                        1,
                        "inconclusive",
                        "drift: strict replay doesn't heal (no match, not unanimous)",
                        None,
                    ),
                ],
            ),
            id="renamed",
        ),
        pytest.param(
            Pinned(
                page="removed",
                script=DETAILS,
                expectation=SHIPPING,
                outcome="inconclusive",
                code=3,
                reason="step 1: inconclusive, drift: strict replay doesn't heal (no match, no match)",
                verdicts=[
                    (0, "pass", None, None),
                    (
                        1,
                        "inconclusive",
                        "drift: strict replay doesn't heal (no match, no match)",
                        None,
                    ),
                ],
            ),
            id="removed",
        ),
        # An action's target must take the click: under the modal it can't.
        pytest.param(
            Pinned(
                page="modal",
                script=DETAILS,
                expectation=SHIPPING,
                outcome="inconclusive",
                code=3,
                reason="step 1: inconclusive, drift: strict replay doesn't heal (not actionable, not actionable)",
                verdicts=[
                    (0, "pass", None, None),
                    (
                        1,
                        "inconclusive",
                        "drift: strict replay doesn't heal (not actionable, not actionable)",
                        None,
                    ),
                ],
            ),
            id="covered",
        ),
        # An assertion's target drifts too: its check isn't evaluated.
        pytest.param(
            Pinned(
                page="moved",
                script=BUTTON,
                expectation=IN_VIEW,
                outcome="inconclusive",
                code=3,
                reason="a1: inconclusive, drift: strict replay doesn't heal (not unanimous, no match)",
                verdicts=[
                    (0, "pass", None, None),
                    (
                        "a1",
                        "inconclusive",
                        "drift: strict replay doesn't heal (not unanimous, no match)",
                        None,
                    ),
                    ("console_errors", "pass", None, None),
                    ("js_exceptions", "pass", None, None),
                    ("http_5xx", "pass", None, None),
                    ("broken_images", "pass", None, None),
                ],
            ),
            id="moved-assertion",
        ),
        pytest.param(
            Pinned(
                page="removed",
                script=BUTTON,
                expectation=IN_VIEW,
                outcome="inconclusive",
                code=3,
                reason="a1: inconclusive, drift: strict replay doesn't heal (no match, no match)",
                verdicts=[
                    (0, "pass", None, None),
                    (
                        "a1",
                        "inconclusive",
                        "drift: strict replay doesn't heal (no match, no match)",
                        None,
                    ),
                    ("console_errors", "pass", None, None),
                    ("js_exceptions", "pass", None, None),
                    ("http_5xx", "pass", None, None),
                    ("broken_images", "pass", None, None),
                ],
            ),
            id="removed-assertion",
        ),
    ],
)
def test_a_drifted_target_is_inconclusive_never_expectation_violated(
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
    assert judged(root) == pinned.verdicts


def test_a_modal_over_a_checked_target_is_expectation_violated_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An assertion's covered target still resolves, so the check is false.
    root = tmp_path / "qa"
    spec = committed(order_spec(root, IN_VIEW), BUTTON)

    with serving(page("modal")) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    record = re.escape(str(record_in(root)))
    assert re.fullmatch(
        rf"expectation_violated order: {SECONDS}, \$0, run record: {record}\n"
        r"  a1: expectation_violated\n",
        result.stderr,
    )
    assert judged(root) == [
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
    ]
