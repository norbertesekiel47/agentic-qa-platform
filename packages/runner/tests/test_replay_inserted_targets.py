"""A repair may add a target for a replay-safe click it inserts (#325; ADR-0024's
#246 amendment, slice 2): on #240's modal page, where nothing committed names the
dialog's "No thanks" button, a scripted repair adds it and inserts a click on it
before the covered toggle's step. Replay takes it, the click shows it changed
nothing, the run ends `drift_consistent` and its proposal adds the target and the
step. The same repair with one planted fault is refused and proposes nothing.
Behind the validator, replay fails any other step through an added target before
its intent, shown with a fault planted in the validator (the maintainer's
backstop). An added target whose meaning a subject row governs is refused at the
seam, as `aqa heal apply` refuses it (#332). `replays.replay_run(...,
repair=...)` in real Chromium, with no model and no cassette."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import Click, CompiledScript, Fill, FillSecret, Select
from aqa_runner import repairs
from aqa_runner.admission import Targeted
from aqa_runner.heal_proposal import diff, final_script, proposal_lines
from aqa_runner.redaction import NO_SECRETS
from aqa_runner.repairs import Drift, StepAt, added_target_refusal

from packages.runner.tests.drift_fixtures import (
    DETAILS,
    DISMISS,
    DISMISSED_PATCH,
    GOVERNED,
    SUBJECT_CONFIG,
    SUMMARY,
    UNCONTRACTED,
    dismissed,
    dismissing,
    page,
)
from packages.runner.tests.test_replay_inserted_steps import Inserting, replayed

MODAL = page("modal")
# Both locators of the covered toggle find it, and neither can click it.
COVERED = "(not actionable, not actionable)"
REFUSED = "drift: replay refused the heal's repair: "
# What the document says of a run whose repair was refused.
INCONCLUSIVE: tuple[object, ...] = ("inconclusive", 3, None, [])


def dismissed_on_the_modal(tmp_path: Path, click: Click = DISMISS) -> Inserting:
    """`DETAILS` replayed on the modal page, with the repair that adds the
    dismiss button and inserts `click` before the toggle's step."""
    return replayed(tmp_path, dismissing(click), html=MODAL, script=DETAILS)


def test_a_repair_that_adds_the_dismiss_button_and_clicks_it_heals_the_modal(
    tmp_path: Path,
) -> None:
    run = dismissed_on_the_modal(tmp_path)

    assert [(step.seq, step.outcome) for step in run.result.steps] == [
        (0, "completed"),
        (2, "completed"),
        (1, "completed"),
    ]
    assert run.states() == [
        (0, "intent"),
        (0, "completed"),
        (2, "intent"),
        (2, "completed"),
        (1, "intent"),
        (1, "completed"),
    ]
    assert [(each.id, each.outcome) for each in run.result.assertions] == [
        ("a1", "pass"),
        ("a2", "pass"),
    ]
    assert run.ended()[:2] == ("drift_consistent", 7)
    assert run.document["heal_proposal"]["patch"] == DISMISSED_PATCH
    verdicts: list[dict[str, Any]] = run.document["verdicts"]
    inserted = next(each for each in verdicts if each.get("step") == 2)
    assert inserted["verdict"] == "drift_consistent"
    assert (inserted["heal"]["repair"], inserted["heal"]["bound"]) == (
        "repairs/1.json",
        None,
    )


def test_aqa_runs_lines_show_the_added_target_and_the_inserted_step(
    tmp_path: Path,
) -> None:
    run = dismissed_on_the_modal(tmp_path)
    committed, repairs = run.end.script, run.result.repairs
    proposal = run.document["heal_proposal"]["id"]

    lines = proposal_lines("order", proposal, committed, repairs)

    final = final_script(committed, repairs)
    shown = diff("order", committed, final)
    # No binding line: the toggle's own binding is unchanged.
    assert lines == (f"heal proposal {proposal}:", *shown)
    added = [line for line in shown if line.startswith("+")]
    assert any('"no_thanks"' in line for line in added)
    assert any('"seq": 2' in line for line in added)


def test_an_inserted_side_effect_click_on_the_added_target_is_refused(
    tmp_path: Path,
) -> None:
    flagged = DISMISS.model_copy(
        update={"side_effect": True, "side_effect_basis": "network: a write request"}
    )

    run = dismissed_on_the_modal(tmp_path, flagged)

    assert run.ended() == INCONCLUSIVE
    reason = f"{REFUSED}/steps/0/side_effect: a heal inserts only replay-safe steps"
    assert run.verdict(1) == ("inconclusive", f"{reason} {COVERED}")
    assert 2 not in {seq for seq, _ in run.states()}


def test_an_added_target_no_inserted_click_goes_through_is_refused(
    tmp_path: Path,
) -> None:
    run = dismissed_on_the_modal(
        tmp_path, DISMISS.model_copy(update={"target": "toggle"})
    )

    assert run.ended() == INCONCLUSIVE
    reason = (
        f"{REFUSED}/targets/no_thanks: a heal adds a target only for a click it inserts"
    )
    assert run.verdict(1) == ("inconclusive", f"{reason} {COVERED}")
    assert 2 not in {seq for seq, _ in run.states()}


# Why a step through a target a repair added didn't run: only a replay-safe click
# a repair inserted may act through one (the maintainer's backstop on #325).
UNVOUCHED = (
    "a repair added this step's target, which only a replay-safe click a repair "
    "inserted acts through, so the step did not run"
)
SIDE_EFFECT_CLICK = DISMISS.model_copy(
    update={"side_effect": True, "side_effect_basis": "network: a write request"}
)
FILL = Fill(seq=2, action="fill", target="no_thanks", value="x", side_effect=False)


@pytest.mark.parametrize(
    ("step", "inserted", "refused"),
    [
        pytest.param(DISMISS, True, None, id="an-inserted-replay-safe-click"),
        pytest.param(SIDE_EFFECT_CLICK, True, UNVOUCHED, id="a-side-effect-click"),
        pytest.param(DISMISS, False, UNVOUCHED, id="a-committed-click"),
        pytest.param(FILL, True, UNVOUCHED, id="a-fill"),
        pytest.param(
            Select(
                seq=2,
                action="select",
                target="no_thanks",
                option="x",
                side_effect=False,
            ),
            True,
            UNVOUCHED,
            id="a-select",
        ),
        pytest.param(
            FillSecret.model_validate(
                {
                    "seq": 2,
                    "action": "fill_secret",
                    "target": "no_thanks",
                    "secret": "TEST_PASSWORD",
                    "side_effect": False,
                }
            ),
            True,
            UNVOUCHED,
            id="a-fill-secret",
        ),
    ],
)
def test_only_an_inserted_replay_safe_click_acts_through_an_added_target(
    step: Targeted, inserted: bool, refused: str | None
) -> None:
    assert added_target_refusal(step, inserted=inserted, redactor=NO_SECRETS) == (
        refused
    )


def taking_any_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fault planted in the heal-patch validator: it takes any script, so
    replay's own backstop is all that stands between an added target and the
    page."""
    monkeypatch.setattr(repairs, "heal_patch_problems", lambda *_: [])


def retargeting(drift: Drift) -> CompiledScript:
    """A repair that adds the dismiss button and points the drifted step, a
    committed one, at it."""
    assert isinstance(drift.at, StepAt)
    pointed = drift.at.step.model_copy(update={"target": "no_thanks"})
    return dismissed(drift.script).model_copy(update={"steps": (pointed,)})


@pytest.mark.parametrize(
    ("offer", "seq"),
    [
        pytest.param(
            dismissing(SIDE_EFFECT_CLICK), 2, id="an-inserted-side-effect-click"
        ),
        pytest.param(dismissing(FILL), 2, id="an-inserted-fill"),
        pytest.param(retargeting, 1, id="a-committed-step-retargeted"),
    ],
)
def test_a_step_through_an_added_target_the_validator_let_through_fails_with_no_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offer: Callable[[Drift], CompiledScript],
    seq: int,
) -> None:
    taking_any_script(monkeypatch)

    run = replayed(tmp_path, offer, html=MODAL, script=DETAILS)

    assert run.outcomes() == [(0, "completed", None), (seq, "failed", UNVOUCHED)]
    assert run.states() == [(0, "intent"), (0, "completed")]
    assert run.ended() == INCONCLUSIVE
    assert run.verdict(seq) == ("inconclusive", UNVOUCHED)


def test_the_backstop_lets_an_inserted_replay_safe_click_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same planted fault, with the repair the validator would take."""
    taking_any_script(monkeypatch)

    run = dismissed_on_the_modal(tmp_path)

    assert [(step.seq, step.outcome) for step in run.result.steps] == [
        (0, "completed"),
        (2, "completed"),
        (1, "completed"),
    ]
    assert run.ended()[:2] == ("drift_consistent", 7)


def meaning_the_summary(drift: Drift) -> CompiledScript:
    """`dismissing()`'s script, with the dismiss button given the summary's
    meaning, which the config's subject row governs."""
    script = dismissing()(drift)
    no_thanks = script.targets["no_thanks"].model_copy(
        update={"semantic": SUMMARY["semantic"]}
    )
    return script.model_copy(
        update={"targets": {**script.targets, "no_thanks": no_thanks}}
    )


def test_an_added_target_with_a_governed_meaning_is_refused_at_the_seam(
    tmp_path: Path,
) -> None:
    """Replay's own admission at the seam, as `aqa heal apply` runs it: the
    inserted click never runs (#332)."""
    run = replayed(
        tmp_path,
        meaning_the_summary,
        html=MODAL,
        script=GOVERNED,
        config_yaml=SUBJECT_CONFIG,
    )

    repaired = json.loads((run.record / "repairs/1.json").read_text())
    assert repaired["problems"] == [UNCONTRACTED]
    assert 2 not in {seq for seq, _ in run.states()}
    assert run.ended() == INCONCLUSIVE
    _, reason = run.verdict(1)
    assert isinstance(reason, str)
    assert UNCONTRACTED in reason


def test_an_added_target_with_its_own_meaning_heals_under_the_subject_row(
    tmp_path: Path,
) -> None:
    """The same config and script: only the governed meaning is refused."""
    run = replayed(
        tmp_path, dismissing(), html=MODAL, script=GOVERNED, config_yaml=SUBJECT_CONFIG
    )

    assert run.ended()[:2] == ("drift_consistent", 7)
