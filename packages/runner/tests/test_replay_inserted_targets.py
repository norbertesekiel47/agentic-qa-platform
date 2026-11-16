"""A repair may add a target for a replay-safe click it inserts (#325; ADR-0024's
#246 amendment, slice 2): on #240's modal page, where nothing committed names the
dialog's "No thanks" button, a scripted repair adds it and inserts a click on it
before the covered toggle's step. Replay takes it, the click shows it changed
nothing, the run ends `drift_consistent` and its proposal adds the target and the
step. The same repair with one planted fault is refused and proposes nothing.
`replays.replay_run(..., repair=...)` in real Chromium, with no model and no
cassette."""

from pathlib import Path
from typing import Any

from aqa_core.compiled import Click
from aqa_runner.heal_proposal import diff, final_script, proposal_lines

from packages.runner.tests.drift_fixtures import (
    DETAILS,
    DISMISS,
    DISMISSED_PATCH,
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
