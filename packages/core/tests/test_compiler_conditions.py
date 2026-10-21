"""Keeping the coverage plan's required conditions in the path `finish`
selects (#55; ADR-0024's "Path selection and attempts"; DATA_MODEL §7,
`requires`): the step `finish` says meets a condition carries `satisfies`,
only the reload that ends the path meets a condition of kind reload, and a
path that keeps no step meeting a required condition fails by name."""

from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import RequiredCondition
from aqa_core.compiler import CompileError, ExploredPath, compile_script

from packages.core.tests.test_compiler import (
    BOUND,
    BY,
    EXPECTATIONS,
    KEPT,
    STEPS,
    demo,
    plan,
)

RELOADED = {
    "id": "c1",
    "condition": "checked after reloading the demo page",
    "kind": "reload",
}
SIGNED_IN = {"id": "c2", "condition": "checked while signed in"}
# The demo path's step 3 is a reload, which compiles as step 2. Step 1 is a
# navigate and steps 2 and 4 are clicks. ENDS keeps step 2's click, then the
# reload, so the path ends on its reload.
ENDS = (2, 3)
MISSING = (
    'requires c1 ("checked after reloading the demo page"): finish keeps no step '
    "that meets it"
)


def compiled(
    tmp_path: Path,
    satisfies: tuple[tuple[str, int], ...],
    *,
    kept: tuple[int, ...] = KEPT,
    requires: tuple[dict[str, str], ...] = (RELOADED,),
) -> Any:
    spec, config = demo(tmp_path)
    path = ExploredPath(STEPS, kept, BOUND, satisfies)
    return compile_script(
        spec, config, plan(*EXPECTATIONS, requires=requires), path, BY
    )


def problems(tmp_path: Path, satisfies: tuple[tuple[str, int], ...], **kw: Any) -> Any:
    with pytest.raises(CompileError) as failed:
        compiled(tmp_path, satisfies, **kw)
    return failed.value.problems


def test_the_reload_finish_says_meets_a_reload_condition_carries_it(
    tmp_path: Path,
) -> None:
    script = compiled(tmp_path, (("c1", 3),), kept=ENDS)

    assert [(step.action, step.satisfies) for step in script.steps] == [
        ("click", ()),
        ("reload", ("c1",)),
    ]
    assert script.coverage.requires == (RequiredCondition.model_validate(RELOADED),)


def test_a_reload_condition_said_of_a_reload_a_step_follows_fails_naming_both(
    tmp_path: Path,
) -> None:
    # The checks run after the last step, so the click runs between the
    # reload and them.
    not_last = (
        "requires c1: finish says step 3 meets it, but c1 needs the reload that "
        "ends the path, and step 3 doesn't end it"
    )

    assert problems(tmp_path, (("c1", 3),), kept=(3, 4)) == (not_last, MISSING)


def test_a_step_meeting_two_conditions_carries_them_in_plan_order(
    tmp_path: Path,
) -> None:
    script = compiled(
        tmp_path, (("c2", 3), ("c1", 3)), kept=ENDS, requires=(RELOADED, SIGNED_IN)
    )

    assert script.steps[1].satisfies == ("c1", "c2")


@pytest.mark.parametrize("number", [1, 4], ids=["navigate", "click"])
def test_a_reload_condition_said_of_a_step_that_is_no_reload_fails_naming_both(
    tmp_path: Path, number: int
) -> None:
    not_a_reload = (
        f"requires c1: finish says step {number} meets it, but c1 needs a reload "
        f"and step {number} isn't one"
    )

    assert problems(tmp_path, (("c1", number),)) == (not_a_reload, MISSING)


def test_a_condition_without_a_kind_is_met_by_the_step_finish_names(
    tmp_path: Path,
) -> None:
    # The documented residual (SECURITY §4 item 5): which step meets a
    # condition's free text is the model's claim.
    script = compiled(tmp_path, (("c2", 4),), requires=(SIGNED_IN,))

    assert [step.satisfies for step in script.steps][:3] == [(), (), ("c2",)]


def test_a_path_that_keeps_no_step_meeting_a_required_condition_fails_by_name(
    tmp_path: Path,
) -> None:
    assert problems(tmp_path, ()) == (MISSING,)


def test_a_condition_said_of_a_step_finish_leaves_out_fails_by_name(
    tmp_path: Path,
) -> None:
    dropped = tuple(number for number in KEPT if number != 3)

    assert problems(tmp_path, (("c1", 3),), kept=dropped) == (
        "requires c1: finish says step 3 meets it, but doesn't keep step 3",
        MISSING,
    )


def test_a_condition_the_plan_does_not_require_fails_without_its_name(
    tmp_path: Path,
) -> None:
    assert problems(tmp_path, (("c1", 3), ("fake-c9", 3)), kept=ENDS) == (
        "finish names a condition the plan doesn't require",
    )


def test_a_condition_said_of_a_step_the_attempt_never_took_fails_once_by_name(
    tmp_path: Path,
) -> None:
    assert problems(tmp_path, (("c1", 12),), kept=(*KEPT, 12)) == (
        "finish names step 12, which the attempt never took",
        MISSING,
    )
