"""`replays.replay_run(..., repair=...)` for an assertion (#244; ADR-0024's
#244 amendment): once every step has completed, a positive check whose target
drifted goes to the repair; taken, the checks on the targets it changed are
evaluated again and the rest keep their results. Real Chromium against #240's
drift pages, with every locator, as heal runs."""

from collections.abc import Callable
from pathlib import Path

import pytest
from aqa_core.compiled import CompiledScript, Press, Target
from aqa_runner.evaluation import AssertionResult
from aqa_runner.repairs import Drift

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.drift_fixtures import (
    BUTTON,
    CONTRASTED,
    IN_VIEW,
    TOGGLE,
    order_spec,
    page,
)
from packages.runner.tests.test_every_locator import around
from packages.runner.tests.test_replay_repairs import (
    FETCH_UNDECLARED,
    MISSES,
    document,
    rebound,
    repairing,
    repairs,
    seen_on,
)


def in_view_spec(root: Path) -> Path:
    return order_spec(root, IN_VIEW)


def test_a_drifted_assertion_target_goes_to_the_repair_and_is_evaluated_again(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script),
        script=BUTTON,
        spec_at=in_view_spec,
    )

    assert [(drift.at.key, drift.misses) for drift in run.handed] == [
        ("assertion-0", MISSES)
    ]
    assert run.result.assertions == (AssertionResult("a1", "pass", locator_index=0),)
    assert run.result.outcome == "passed"
    assert document(run, "assertion-0") == {
        "assertion": "a1",
        "misses": ["not unanimous", "no match"],
        "problems": [],
        "script": rebound(run.committed).model_dump(mode="json", exclude_none=True),
    }


def test_a_drifted_contrast_min_target_goes_to_the_repair_and_is_evaluated_again(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script),
        script=CONTRASTED,
        spec_at=in_view_spec,
    )

    assert [(drift.at.key, drift.misses) for drift in run.handed] == [
        ("assertion-0", MISSES)
    ]
    assert run.result.assertions == (AssertionResult("a1", "pass", locator_index=0),)
    assert run.result.outcome == "passed"


HEADING = {"semantic": "the order's heading", "locators": [{"css": "h1"}]}
# The toggle seen, the heading seen, and the toggle seen again.
HEADING_BETWEEN = fx.compiled(
    [],
    targets={"toggle": TOGGLE, "heading": HEADING},
    assertions=[
        seen_on("toggle", "a1"),
        seen_on("heading", "a2"),
        seen_on("toggle", "a3"),
    ],
)


async def hiding_the_heading(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with the heading hidden first:
    time passing, under a check that already passed."""
    await drift.session.page.evaluate(
        "document.querySelector('h1').style.display = 'none'"
    )
    return rebound(drift.script)


def test_only_assertions_on_a_rebound_target_are_evaluated_again(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path, hiding_the_heading, script=HEADING_BETWEEN, spec_at=in_view_spec
    )

    assert [drift.at.key for drift in run.handed] == ["assertion-0"]
    assert run.result.assertions == (
        AssertionResult("a1", "pass", locator_index=0),
        AssertionResult("a2", "pass", locator_index=0),
        AssertionResult("a3", "pass", locator_index=0),
    )


def inserting_a_step(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with a replay-safe `press`
    after every step, which the validator allows."""
    press = Press(seq=1, action="press", key="Tab", side_effect=False)
    moved = rebound(drift.script)
    return moved.model_copy(update={"steps": (*moved.steps, press)})


def test_an_assertion_repair_that_inserts_a_step_is_refused(tmp_path: Path) -> None:
    run = repairing(tmp_path, inserting_a_step, script=BUTTON, spec_at=in_view_spec)

    problem = "/steps: an assertion's repair inserts no step: every step has run"
    assert repairs(run) == [("assertion-0", MISSES, (problem,), None)]
    assert run.result.assertions == (
        AssertionResult("a1", "binding_unresolved", MISSES),
    )


CONTACT = {"semantic": "the contact button", "locators": [{"css": "button.contact"}]}
# Two positive checks that drift on the moved page, on different targets.
TWO_DRIFTS = fx.compiled(
    [],
    targets={"toggle": TOGGLE, "contact": CONTACT},
    assertions=[seen_on("toggle", "a1"), seen_on("contact", "a2")],
)
RENAMED_MEANING = "/targets/toggle/semantic: a heal never changes a target's meaning"


@pytest.mark.parametrize(
    ("offer", "problems"),
    [
        pytest.param(
            lambda drift: rebound(drift.script, semantic="the order's details"),
            [RENAMED_MEANING],
            id="refused",
        ),
        pytest.param(lambda drift: drift.script, [], id="gave_up"),
    ],
)
def test_a_refused_or_given_up_assertion_repair_ends_the_handoff(
    tmp_path: Path, offer: Callable[[Drift], CompiledScript], problems: list[str]
) -> None:
    run = repairing(tmp_path, offer, script=TWO_DRIFTS, spec_at=in_view_spec)

    assert [drift.at.key for drift in run.handed] == ["assertion-0"]
    assert run.result.assertions == (
        AssertionResult("a1", "binding_unresolved", MISSES),
        AssertionResult("a2", "binding_unresolved", ("no match",)),
    )
    assert document(run, "assertion-0") == {
        "assertion": "a1",
        "misses": ["not unanimous", "no match"],
        "problems": problems,
        "script": None,
    }


# Two positive checks that drift, then one on the heading, which doesn't.
TWO_DRIFTS_THEN_THE_HEADING = fx.compiled(
    [],
    targets={"toggle": TOGGLE, "contact": CONTACT, "heading": HEADING},
    assertions=[
        seen_on("toggle", "a1"),
        seen_on("contact", "a2"),
        seen_on("heading", "a3"),
    ],
)


def contact_rebound(script: CompiledScript) -> CompiledScript:
    """`script` with its contact button bound as the moved page has it."""
    footer = {**CONTACT, "locators": [{"css": "button", "scope": {"css": "footer"}}]}
    targets = {**script.targets, "contact": Target.model_validate(footer)}
    return script.model_copy(update={"targets": targets})


async def rebinding_each_as_time_passes(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, with the drifted check's target rebound,
    and what a check already passed on hidden first: the heading for the
    toggle, and the toggle for the contact button."""
    if drift.at.key == "assertion-0":
        await drift.session.page.evaluate(
            "document.querySelector('h1').style.display = 'none'"
        )
        return rebound(drift.script)
    await drift.session.page.evaluate(
        "document.querySelector('#order button').style.display = 'none'"
    )
    return contact_rebound(drift.script)


def test_each_drifted_assertion_target_goes_to_its_own_repair_in_turn(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path,
        rebinding_each_as_time_passes,
        script=TWO_DRIFTS_THEN_THE_HEADING,
        spec_at=in_view_spec,
    )

    assert [(drift.at.key, drift.misses) for drift in run.handed] == [
        ("assertion-0", MISSES),
        ("assertion-1", ("no match",)),
    ]
    assert run.result.assertions == (
        AssertionResult("a1", "pass", locator_index=0),
        AssertionResult("a2", "pass", locator_index=0),
        AssertionResult("a3", "pass", locator_index=0),
    )
    assert document(run, "assertion-0") == {
        "assertion": "a1",
        "misses": ["not unanimous", "no match"],
        "problems": [],
        "script": rebound(run.committed).model_dump(mode="json", exclude_none=True),
    }
    assert document(run, "assertion-1") == {
        "assertion": "a2",
        "misses": ["no match"],
        "problems": [],
        "script": contact_rebound(rebound(run.committed)).model_dump(
            mode="json", exclude_none=True
        ),
    }


# The contact button seen, which drifts while nothing finds it, and not visible,
# which that absence passes.
CONTACT_SEEN_AND_NOT_VISIBLE = fx.compiled(
    [],
    targets={"contact": CONTACT},
    assertions=[
        seen_on("contact", "a1"),
        {"id": "a2", "expect_index": 0, "check": "not_visible", "target": "contact"},
    ],
)


def test_a_not_visible_check_on_a_rebound_target_is_evaluated_again(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path,
        lambda drift: contact_rebound(drift.script),
        script=CONTACT_SEEN_AND_NOT_VISIBLE,
        spec_at=in_view_spec,
    )

    assert [drift.at.key for drift in run.handed] == ["assertion-0"]
    assert run.result.assertions == (
        AssertionResult("a1", "pass", locator_index=0),
        AssertionResult("a2", "failed", locator_index=0),
    )
    assert run.result.outcome == "failed"


def test_a_not_visible_checks_drift_goes_to_no_repair(tmp_path: Path) -> None:
    banner = {
        "semantic": "the order's banner",
        "locators": [{"css": "p", "scope": {"css": "#banner"}}],
    }
    script = fx.compiled(
        [],
        targets={"banner": banner},
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "not_visible", "target": "banner"}
        ],
    )

    run = repairing(tmp_path, lambda drift: drift.script, script=script)

    assert run.handed == []
    assert run.result.assertions == (
        AssertionResult("a1", "binding_unresolved", ("no scope",)),
    )
    assert not (run.record / "repairs").exists()


# A text check on the heading, then the toggle, which drifts, seen.
HEADING_READ_FIRST = fx.compiled(
    [],
    targets={"heading": HEADING, "toggle": TOGGLE},
    assertions=[
        {
            "id": "a1",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "heading",
            "text": "Order 43",
        },
        seen_on("toggle", "a2"),
    ],
)


def test_no_assertion_drift_is_handed_once_another_assertion_failed(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script),
        script=HEADING_READ_FIRST,
        spec_at=in_view_spec,
    )

    assert run.handed == []
    assert run.result.assertions == (
        AssertionResult("a1", "failed", locator_index=0),
        AssertionResult("a2", "binding_unresolved", MISSES),
    )


ICON = {"semantic": "the order's icon", "locators": [{"css": "#icon"}]}
# The toggle, which drifts, seen, then the text of an element that isn't
# HTML read, a look that raises.
ICON_READ_LAST = fx.compiled(
    [],
    targets={"toggle": TOGGLE, "icon": ICON},
    assertions=[
        seen_on("toggle", "a1"),
        {
            "id": "a2",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "icon",
            "text": "Order 42",
        },
    ],
)


def test_no_assertion_drift_is_handed_once_a_look_at_the_page_raised(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script),
        script=ICON_READ_LAST,
        spec_at=in_view_spec,
        html=page("moved").replace("<footer>", '<svg id="icon"></svg><footer>'),
    )

    assert run.handed == []
    [drifted, raised] = run.result.assertions
    assert drifted == AssertionResult("a1", "binding_unresolved", MISSES)
    assert (raised.outcome, raised.error is not None) == ("not_evaluated", True)


def test_no_assertion_drift_is_handed_once_an_invariant_is_violated(
    tmp_path: Path,
) -> None:
    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script),
        script=BUTTON,
        spec_at=in_view_spec,
        html=page("moved") + "<script>console.error('the app broke');</script>",
    )

    assert run.handed == []
    assert run.result.assertions == (
        AssertionResult("a1", "binding_unresolved", MISSES),
    )


def test_no_assertion_drift_is_handed_once_an_egress_block_interrupted_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The block lands during the toggle's first lookup, before it drifts.
    fired = around(monkeypatch, "role=button", (1, FETCH_UNDECLARED, "() => 0"))

    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script),
        script=BUTTON,
        spec_at=in_view_spec,
    )

    assert fired == [1]
    assert run.handed == []
    assert (run.result.outcome, run.result.error_code) == ("errored", "egress_blocked")
