"""`aqa_runner.replay_result` for a run that heals (#244; ADR-0024's #244
amendment, DATA_MODEL §7): what each subject's verdict becomes, what it cites, and
the proposal. Records and redactors are real, results are made by hand, and every
expected value is a literal. `test_heal_verdicts.py` holds what a real heal
leaves."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import Click, NotVisible, Select, Target, VisibleUnoccluded
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import RunResult, StepResult
from aqa_runner.redaction import NO_SECRETS
from aqa_runner.repairs import AssertionAt, Basis, Repaired, StepAt
from aqa_runner.replay_result import ReplayEnd, Stop, unrecorded
from aqa_runner.settling import Window

from packages.runner.tests.test_replay_result import (
    BLOB,
    BOUND_AS,
    CLEAN,
    CLICK,
    EVENT,
    MISSED,
    NEW_SAVE,
    REPORTED,
    SILENT,
    Made,
    document,
    ending,
    healing_the_step,
    judged,
    made,
    rebinding,
    the_click,
    violated,
    with_contrast,
)


def passing_with(run: Made, **changes: Any) -> ReplayEnd:
    """A run whose a1 and a4 pass, a4 reading `save`, with `changes` over it."""
    a4 = AssertionResult("a4", "pass", locator_index=0)
    return ending(run, assertions=(a4,), **changes)


def cast_result(end: ReplayEnd) -> RunResult:
    assert end.result is not None
    return end.result


HEAL_CITED = {
    "repair": "repairs/1.json",
    "record": "heals/1",
    "bound": 'button "Save"',
    "model_reported": {"confidence": 0.9, "rationale": "it moved"},
}
# The patch healing `save` leaves, and the proposal it makes.
PATCH = [
    {
        "op": "replace",
        "path": "/targets/save/locators",
        "value": [{"role": "button", "name": "Save"}],
    }
]


def test_a_healed_step_is_drift_consistent_citing_its_heal(tmp_path: Path) -> None:
    run = made(tmp_path)

    end = passing_with(run, **healing_the_step(run))
    written = document(end)

    assert (written["mode"], written["outcome"], written["exit_code"]) == (
        "run",
        "drift_consistent",
        7,
    )
    assert judged(end, "step", 1) == {
        "step": 1,
        "locator_index": 0,
        "misses": [],
        "verdict": "drift_consistent",
        "reason": None,
        "evidence": {"path": "evidence/1", "screenshot": None},
        "finding": None,
        "heal": HEAL_CITED,
    }
    assert judged(end, "assertion", "a4")["verdict"] == "drift_consistent"
    assert judged(end, "assertion", "a4")["heal"] == HEAL_CITED
    assert written["reasons"] == []


def test_a_subject_off_the_rebound_targets_passes_with_no_heal(tmp_path: Path) -> None:
    run = made(tmp_path)

    end = passing_with(run, **healing_the_step(run))

    assert judged(end, "step", 0)["verdict"] == "pass"
    assert judged(end, "step", 0)["heal"] is None
    assert judged(end, "assertion", "a1")["verdict"] == "pass"
    assert judged(end, "assertion", "a1")["heal"] is None
    assert judged(end, "invariant", "http_5xx")["verdict"] == "pass"
    assert judged(end, "invariant", "http_5xx")["heal"] is None


def test_a_healed_run_proposes_its_patch_on_the_files_blob(tmp_path: Path) -> None:
    run = made(tmp_path)

    written = document(passing_with(run, **healing_the_step(run)))

    proposal = written["heal_proposal"]
    assert proposal.pop("id").startswith("ah_")
    assert proposal == {
        "compiled": f".compiled/{run.spec.frontmatter.id}.json",
        "base": {"blob_sha": BLOB, "pr_head_sha": None},
        "patch": PATCH,
    }


def test_a_strict_document_has_no_heal_field(tmp_path: Path) -> None:
    written = document(ending(made(tmp_path)))

    assert written["mode"] == "strict"
    assert "heal_proposal" not in written
    assert all("heal" not in verdict for verdict in written["verdicts"])


def test_a_rebound_step_in_a_run_that_didnt_pass_is_inconclusive_and_proposes_nothing(
    tmp_path: Path,
) -> None:
    run = made(tmp_path)
    a4 = AssertionResult("a4", "pass", locator_index=0)
    failed = AssertionResult("a2", "failed", locator_index=0)

    end = ending(run, assertions=(a4, failed), **healing_the_step(run))
    written = document(end)

    assert written["outcome"] == "expectation_violated"
    assert written["heal_proposal"] is None
    assert judged(end, "step", 1)["verdict"] == "inconclusive"
    assert judged(end, "step", 1)["reason"] == (
        "heal rebound its target, but the run didn't pass"
    )
    assert judged(end, "step", 1)["heal"] == HEAL_CITED
    # A rebound assertion keeps its own deterministic verdict.
    assert judged(end, "assertion", "a4")["verdict"] == "pass"
    assert judged(end, "assertion", "a4")["heal"] == HEAL_CITED
    assert judged(end, "assertion", "a2")["verdict"] == "expectation_violated"
    assert judged(end, "assertion", "a2")["heal"] is None


def test_an_assertion_on_a_rebound_target_that_fails_is_a_violation_citing_the_heal(
    tmp_path: Path,
) -> None:
    run = made(tmp_path)
    failed = AssertionResult("a4", "failed", locator_index=0)

    end = ending(run, assertions=(failed,), **healing_the_step(run))

    entry = judged(end, "assertion", "a4")
    assert entry["verdict"] == "expectation_violated"
    assert entry["finding"]["category"] == "visual"
    assert entry["heal"] == HEAL_CITED
    assert document(end)["exit_code"] == 1


def test_a_contrast_min_on_a_rebound_target_cites_the_heal(tmp_path: Path) -> None:
    run = with_contrast(made(tmp_path))
    failed = AssertionResult("a5", "failed", locator_index=0)

    end = ending(run, assertions=(failed,), **healing_the_step(run))

    entry = judged(end, "assertion", "a5")
    assert entry["verdict"] == "expectation_violated"
    assert entry["finding"]["category"] == "visual"
    assert entry["heal"] == HEAL_CITED


def test_a_finding_lists_each_step_on_the_rebound_set_and_no_other(
    tmp_path: Path,
) -> None:
    """Steps 1 and 3 ran through `save`, which heal rebound; step 2 through `other`,
    which it didn't. The failed check reads neither."""
    run = made(tmp_path)
    other = Target.model_validate(
        {"semantic": "another button", "locators": [{"css": "i"}]}
    )
    steps = tuple(
        Click.model_validate(
            {"seq": seq, "action": "click", "target": target, "side_effect": False}
        )
        for seq, target in ((1, "save"), (2, "other"), (3, "save"))
    )
    update = {"steps": steps, "targets": {**run.script.targets, "other": other}}
    run = replace(run, script=run.script.model_copy(update=update))

    end = ending(
        run,
        steps=(StepResult(2, "completed"), StepResult(3, "completed")),
        assertions=(AssertionResult("a2", "failed", locator_index=0),),
        **healing_the_step(run),
    )

    assert judged(end, "assertion", "a2")["finding"] == {
        "category": "functional",
        "title": "The form is saved",
        "repro_steps": [0, 1, 2, 3],
        "rebound_steps": [1, 3],
    }


def test_a_finding_on_a_repaired_assertion_alone_lists_no_step(tmp_path: Path) -> None:
    """Its own `heal` cites the repair, and no step went through the new binding."""
    run = made(tmp_path)
    check = VisibleUnoccluded.model_validate(
        {
            "id": "a5",
            "expect_index": 0,
            "check": "visible_unoccluded",
            "target": "status",
            "min_size_px": [1, 1],
            "in_viewport": True,
        }
    )
    status = Target.model_validate({"semantic": "a status", "locators": [{"css": "i"}]})
    update = {
        "targets": {**run.script.targets, "status": status},
        "assertions": [*run.script.assertions, check],
    }
    run = replace(run, script=run.script.model_copy(update=update))
    taken = Repaired(
        AssertionAt(4, check),
        MISSED,
        (),
        rebinding(run.script, status=NEW_SAVE),
        Basis(BOUND_AS, REPORTED, None, "heals/assertion-4"),
    )

    end = ending(
        run,
        assertions=(AssertionResult("a5", "failed", locator_index=0),),
        repairs=(taken,),
        base=BLOB,
    )

    entry = judged(end, "assertion", "a5")
    assert entry["finding"]["rebound_steps"] == []
    assert entry["heal"]["repair"] == "repairs/assertion-4.json"


def test_a_step_that_drifted_again_is_not_listed_in_a_finding(tmp_path: Path) -> None:
    """It acted through nothing, so its binding can't be why the invariant failed."""
    run = made(tmp_path)
    taken = repaired_step(
        run,
        script=rebinding(run.script, save=NEW_SAVE),
        basis=Basis(BOUND_AS, REPORTED, None, "heals/1"),
    )

    end = replace(
        ending(run, base=BLOB),
        result=replace(
            cast_result(ending(run)),
            steps=(StepResult(0, "completed"), StepResult(1, "drifted", misses=MISSED)),
            assertions=(),
            repairs=(taken,),
            **violated("console_errors"),
        ),
    )

    entry = judged(end, "invariant", "console_errors")
    assert entry["finding"]["rebound_steps"] == []


def a_rebound_step_that_failed_beside_a_broken_invariant(run: Made) -> ReplayEnd:
    """Step 1, through `save`, which heal rebound, failed; `console_errors` broke."""
    healed = ending(run, **healing_the_step(run), **violated("console_errors"))
    return replace(
        healed,
        result=replace(
            cast_result(healed),
            steps=(
                StepResult(0, "completed"),
                StepResult(1, "failed", error="the page crashed"),
            ),
            assertions=(),
        ),
    )


def test_a_step_refused_before_its_dispatch_is_not_listed_in_a_finding(
    tmp_path: Path,
) -> None:
    """The gate refused it, so it wrote no intent and did nothing: its binding can't
    be why the invariant failed."""
    end = a_rebound_step_that_failed_beside_a_broken_invariant(made(tmp_path))

    entry = judged(end, "invariant", "console_errors")
    assert entry["finding"]["rebound_steps"] == []


def test_a_step_that_failed_after_its_dispatch_is_listed_in_a_finding(
    tmp_path: Path,
) -> None:
    """Its intent has no completion, so its action may have taken effect."""
    run = made(tmp_path)
    run.record.step_intent(1, CLICK, side_effect=False, target_used="save")

    end = a_rebound_step_that_failed_beside_a_broken_invariant(run)

    entry = judged(end, "invariant", "console_errors")
    assert entry["finding"]["rebound_steps"] == [1]


def test_an_inserted_step_that_settled_and_then_failed_is_listed_in_a_finding(
    tmp_path: Path,
) -> None:
    """Step 2, which heal inserted through `save`, which it rebound, settled, then
    didn't show it changed nothing (#246): its action took effect, so it reached
    dispatch."""
    run = made(tmp_path)
    inserted = Click.model_validate(
        {"seq": 2, "action": "click", "target": "save", "side_effect": False}
    )
    patched = rebinding(run.script, save=NEW_SAVE)
    taken = Repaired(
        the_click(run.script),
        MISSED,
        (),
        patched.model_copy(update={"steps": (inserted, *patched.steps)}),
        Basis(BOUND_AS, REPORTED, None, "heals/1"),
    )
    failed = StepResult(
        2, "failed", Window(heard=True, wrote=True), 0, "idle", error="it wrote"
    )

    healed = ending(run, repairs=(taken,), base=BLOB, **violated("console_errors"))
    end = replace(
        healed,
        result=replace(
            cast_result(healed),
            steps=(StepResult(0, "completed"), failed),
            assertions=(),
        ),
    )

    entry = judged(end, "invariant", "console_errors")
    assert entry["finding"]["rebound_steps"] == [2]


def with_the_check_on_save_called(run: Made, name: str) -> Made:
    """`run`, whose check a4, the one reading `save`, has the id `name`."""
    checks = [
        check.model_copy(update={"id": name}) if check.id == "a4" else check
        for check in run.script.assertions
    ]
    return replace(run, script=run.script.model_copy(update={"assertions": checks}))


def test_a_check_with_a_steps_label_on_a_rebound_target_keeps_its_own_verdict(
    tmp_path: Path,
) -> None:
    run = with_the_check_on_save_called(made(tmp_path), "step 9")
    seen = AssertionResult("step 9", "pass", locator_index=0)
    failed = AssertionResult("a2", "failed", locator_index=0)

    end = ending(run, assertions=(seen, failed), **healing_the_step(run))

    entry = judged(end, "assertion", "step 9")
    assert (entry["verdict"], entry["reason"]) == ("pass", None)
    assert entry["heal"] == HEAL_CITED


def test_a_check_with_an_invariants_label_leaves_the_invariant_its_verdict(
    tmp_path: Path,
) -> None:
    run = with_the_check_on_save_called(made(tmp_path), "invariant http_5xx")
    seen = AssertionResult("invariant http_5xx", "pass", locator_index=0)

    end = ending(run, assertions=(seen,), **healing_the_step(run))

    assert judged(end, "invariant", "http_5xx")["verdict"] == "pass"
    assert judged(end, "invariant", "http_5xx")["heal"] is None
    assert judged(end, "assertion", "invariant http_5xx")["heal"] == HEAL_CITED


def test_a_drifted_check_with_a_healed_steps_label_cites_no_repair_of_the_step(
    tmp_path: Path,
) -> None:
    """The check's drift went to no repair; the one with its label is the step's."""
    run = made(tmp_path)
    check = VisibleUnoccluded.model_validate(
        {
            "id": "step 1",
            "expect_index": 0,
            "check": "visible_unoccluded",
            "target": "status",
            "min_size_px": [1, 1],
            "in_viewport": True,
        }
    )
    status = Target.model_validate({"semantic": "a status", "locators": [{"css": "i"}]})
    update = {
        "targets": {**run.script.targets, "status": status},
        "assertions": [*run.script.assertions, check],
    }
    run = replace(run, script=run.script.model_copy(update=update))
    drifted = AssertionResult("step 1", "binding_unresolved", MISSED)

    end = ending(run, assertions=(drifted,), **healing_the_step(run))

    entry = judged(end, "assertion", "step 1")
    assert entry["reason"] == "drift: it went to no heal (no match)"
    assert entry["heal"] is None


def test_a_failed_step_on_a_rebound_target_keeps_its_own_reason(tmp_path: Path) -> None:
    run = made(tmp_path)
    failed = StepResult(1, "failed", error="the page crashed")

    healed = ending(run, **healing_the_step(run))
    end = replace(
        healed,
        result=replace(
            cast_result(healed),
            steps=(StepResult(0, "completed"), failed),
            assertions=(),
        ),
    )

    entry = judged(end, "step", 1)
    assert (entry["verdict"], entry["reason"]) == ("inconclusive", "the page crashed")
    assert entry["heal"] == HEAL_CITED


def a_side_effect_left_open(run: Made) -> dict[str, Any]:
    """An intent nothing resolved, in a run whose every verdict passes."""
    run.record.step_intent(2, CLICK, side_effect=True, target_used="save")
    return {}


@pytest.mark.parametrize(
    ("trouble", "outcome"),
    [
        pytest.param(
            lambda _: {"infrastructure_events": (EVENT,)},
            "unreachable",
            id="an_infrastructure_event",
        ),
        pytest.param(
            a_side_effect_left_open, "non_resumable", id="a_non_resumable_step"
        ),
        pytest.param(
            lambda _: {"stops": (Stop("no_response", NO_SECRETS.redact(SILENT)),)},
            "no_response",
            id="a_stop",
        ),
    ],
)
def test_a_healed_run_that_was_also_troubled_has_no_drift_consistent_subject(
    tmp_path: Path, trouble: Callable[[Made], dict[str, Any]], outcome: str
) -> None:
    run = made(tmp_path)

    end = passing_with(run, **healing_the_step(run), **trouble(run))

    written = document(end)
    assert (written["outcome"], written["heal_proposal"]) == (outcome, None)
    assert judged(end, "step", 1)["verdict"] == "inconclusive"
    assert judged(end, "step", 1)["reason"] == (
        "heal rebound its target, but the run didn't pass"
    )
    assert judged(end, "step", 1)["heal"] == HEAL_CITED
    assert judged(end, "assertion", "a4")["verdict"] == "pass"
    assert judged(end, "assertion", "a4")["heal"] == HEAL_CITED


def test_a_select_step_on_a_rebound_target_is_marked_and_cites_the_heal(
    tmp_path: Path,
) -> None:
    run = made(tmp_path)
    select = Select.model_validate(
        {
            "seq": 1,
            "action": "select",
            "target": "save",
            "option": "x",
            "side_effect": False,
        }
    )
    run = replace(run, script=run.script.model_copy(update={"steps": (select,)}))
    taken = Repaired(
        StepAt(select),
        MISSED,
        (),
        rebinding(run.script, save=NEW_SAVE),
        Basis(BOUND_AS, REPORTED, None, "heals/1"),
    )

    end = passing_with(run, repairs=(taken,), base=BLOB)

    assert judged(end, "step", 1)["verdict"] == "drift_consistent"
    assert judged(end, "step", 1)["heal"] == HEAL_CITED


def test_a_not_visible_check_on_a_rebound_target_is_marked_and_cites_the_heal(
    tmp_path: Path,
) -> None:
    run = made(tmp_path)
    gone = NotVisible.model_validate(
        {"id": "a5", "expect_index": 0, "check": "not_visible", "target": "save"}
    )
    update = {"assertions": [*run.script.assertions, gone]}
    run = replace(run, script=run.script.model_copy(update=update))

    end = ending(
        run,
        assertions=(AssertionResult("a5", "pass"),),
        **healing_the_step(run),
    )

    assert judged(end, "assertion", "a5")["verdict"] == "drift_consistent"
    assert judged(end, "assertion", "a5")["heal"] == HEAL_CITED


def repaired_step(run: Made, **changes: Any) -> Repaired:
    fields: dict[str, Any] = {
        "at": the_click(run.script),
        "misses": MISSED,
        "problems": (),
        "script": None,
        "basis": None,
    }
    return Repaired(**(fields | changes))


GAVE_UP = Basis(NO_SECRETS.redact("the healer found no element"), None, None, "heals/1")


@pytest.mark.parametrize(
    ("changes", "reason", "record"),
    [
        pytest.param(
            {"basis": GAVE_UP},
            "drift: heal gave up: the healer found no element (no match)",
            "heals/1",
            id="gave_up",
        ),
        pytest.param(
            {"problems": (NO_SECRETS.redact("/targets/save: refused"),)},
            "drift: replay refused the heal's repair: /targets/save: refused (no match)",
            None,
            id="refused",
        ),
    ],
)
def test_a_drift_whose_heal_came_to_nothing_is_inconclusive_and_cites_it(
    tmp_path: Path, changes: dict[str, Any], reason: str, record: str | None
) -> None:
    run = made(tmp_path)
    drifted = StepResult(1, "drifted", misses=MISSED)

    end = replace(
        ending(run, base=BLOB, repairs=(repaired_step(run, **changes),)),
        result=replace(
            cast_result(ending(run)),
            steps=(StepResult(0, "completed"), drifted),
            assertions=(),
            repairs=(repaired_step(run, **changes),),
        ),
    )

    entry = judged(end, "step", 1)
    assert (entry["verdict"], entry["reason"]) == ("inconclusive", reason)
    assert entry["heal"] == {
        "repair": "repairs/1.json",
        "record": record,
        "bound": None,
        "model_reported": None,
    }
    assert document(end)["heal_proposal"] is None


def test_a_step_that_drifted_again_after_its_repair_says_so(tmp_path: Path) -> None:
    run = made(tmp_path)
    taken = repaired_step(
        run,
        script=rebinding(run.script, save=NEW_SAVE),
        basis=Basis(BOUND_AS, REPORTED, None, "heals/1"),
    )

    end = replace(
        ending(run, base=BLOB, repairs=(taken,)),
        result=replace(
            cast_result(ending(run)),
            steps=(StepResult(0, "completed"), StepResult(1, "drifted", misses=MISSED)),
            assertions=(),
            repairs=(taken,),
        ),
    )

    entry = judged(end, "step", 1)
    assert entry["reason"] == "drift: drifted again after its repair (no match)"
    assert entry["heal"] == HEAL_CITED | {"bound": None, "model_reported": None}


def test_a_drift_no_repair_was_handed_says_so(tmp_path: Path) -> None:
    run = made(tmp_path)
    drifted = AssertionResult("a4", "binding_unresolved", MISSED)

    end = ending(run, assertions=(drifted,), base=BLOB)

    entry = judged(end, "assertion", "a4")
    assert entry["verdict"] == "inconclusive"
    assert entry["reason"] == "drift: it went to no heal (no match)"
    assert entry["heal"] is None


def test_an_assertion_handed_to_the_heal_cites_its_own_repair(tmp_path: Path) -> None:
    run = made(tmp_path)
    check = run.script.assertions[3]
    assert isinstance(check, VisibleUnoccluded)
    repaired = Repaired(AssertionAt(3, check), MISSED, (), None, GAVE_UP)
    drifted = AssertionResult("a4", "binding_unresolved", MISSED)

    end = ending(run, assertions=(drifted,), base=BLOB, repairs=(repaired,))

    entry = judged(end, "assertion", "a4")
    assert entry["reason"] == (
        "drift: heal gave up: the healer found no element (no match)"
    )
    assert entry["heal"]["repair"] == "repairs/assertion-3.json"


def test_each_rebound_subject_cites_the_repair_that_first_changed_its_target(
    tmp_path: Path,
) -> None:
    run = made(tmp_path)
    first = rebinding(run.script, save=NEW_SAVE)
    later = rebinding(first, save={"css": "button.save"})
    check = run.script.assertions[3]
    assert isinstance(check, VisibleUnoccluded)
    repairs = (
        Repaired(
            the_click(run.script),
            MISSED,
            (),
            first,
            Basis(BOUND_AS, REPORTED, None, "heals/1"),
        ),
        Repaired(
            AssertionAt(3, check),
            MISSED,
            (),
            later,
            Basis(NO_SECRETS.redact("later"), REPORTED, None, "heals/assertion-3"),
        ),
    )

    end = passing_with(run, base=BLOB, repairs=repairs)
    written = document(end)

    assert judged(end, "assertion", "a4")["heal"]["repair"] == "repairs/1.json"
    assert written["heal_proposal"]["patch"][0]["value"] == [{"css": "button.save"}]


def flipping_back(run: Made) -> tuple[Repaired, Repaired]:
    """Two repairs of `save` that end on its committed locators."""
    away = rebinding(run.script, save=NEW_SAVE)
    check = run.script.assertions[3]
    assert isinstance(check, VisibleUnoccluded)
    return (
        Repaired(AssertionAt(3, check), MISSED, (), away, None),
        Repaired(AssertionAt(3, check), MISSED, (), run.script, None),
    )


def test_a_final_script_with_the_committed_locators_passes_and_proposes_nothing(
    tmp_path: Path,
) -> None:
    """A target that flipped back: both evaluations read the committed locators."""
    run = made(tmp_path)

    end = passing_with(run, base=BLOB, repairs=flipping_back(run))
    written = document(end)

    assert (written["outcome"], written["exit_code"]) == ("passed", 0)
    assert written["heal_proposal"] is None
    assert judged(end, "assertion", "a4")["verdict"] == "pass"


def test_a_finding_lists_no_step_on_a_target_that_flipped_back(tmp_path: Path) -> None:
    """Step 1 ran through `save`, and `save` ended on its committed locators."""
    run = made(tmp_path)

    end = passing_with(
        run, base=BLOB, repairs=flipping_back(run), **violated("console_errors")
    )

    entry = judged(end, "invariant", "console_errors")
    assert entry["finding"]["rebound_steps"] == []


def test_a_run_that_cant_write_its_record_says_what_mode_it_was(tmp_path: Path) -> None:
    spec = made(tmp_path).spec

    assert unrecorded(spec, NO_SECRETS, "no record")["mode"] == "strict"
    assert "heal_proposal" not in unrecorded(spec, NO_SECRETS, "no record")
    healing = unrecorded(spec, NO_SECRETS, "no record", healing=True)
    assert (healing["mode"], healing["heal_proposal"]) == ("run", None)


def test_a_no_response_stop_keeps_the_verdicts(tmp_path: Path) -> None:
    run = made(tmp_path)
    stop = Stop("no_response", NO_SECRETS.redact(SILENT))

    written = document(ending(run, stops=(stop,), base=BLOB))

    assert (written["outcome"], written["exit_code"]) == ("no_response", 11)
    assert written["reasons"] == [SILENT]
    assert [
        next(entry[key] for key in ("step", "assertion", "invariant") if key in entry)
        for entry in written["verdicts"]
    ] == [0, 1, "a1", "console_errors", "js_exceptions", "http_5xx", "broken_images"]
    assert {entry["verdict"] for entry in written["verdicts"]} == {"pass"}


def test_a_run_that_heals_and_raised_proposes_nothing(tmp_path: Path) -> None:
    run = made(tmp_path)

    raised = ReplayEnd(run.record, run.spec, run.script, None, CLEAN, (), (), BLOB)

    written = document(raised)
    assert (written["mode"], written["heal_proposal"]) == ("run", None)
    assert written["outcome"] == "inconclusive"
