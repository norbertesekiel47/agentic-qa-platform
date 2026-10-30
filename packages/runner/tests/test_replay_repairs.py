"""`replays.replay_run(..., repair=...)` (#242; ADR-0024's #242 amendment): a
step whose target drifted goes to the caller's repair, which offers the whole
script to go on with; replay checks it against the committed script, records
it and runs the step again, or records the refusal and ends the run as drift.
Real Chromium against #240's drift pages, with every locator, as heal runs."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import Click, CompiledScript, Press, Target
from aqa_core.project import load_config, load_spec
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import Drift, RunResult
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.locators import Miss
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.run_record import RunRecord
from playwright.async_api import async_playwright

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.drift_fixtures import (
    DETAILS,
    SHIPPING,
    TOGGLE,
    order_spec,
    page,
    serving,
)
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.secret_fixtures import (
    BOUND_AT_START,
    FAKE_VALUE,
    OTHER_FAKE_VALUE,
    copies_found,
)
from packages.runner.tests.test_every_locator import around

# How the toggle drifts on the moved page: its role locator still finds it,
# its toolbar-scoped css locator finds nothing.
MISSES: tuple[Miss, ...] = ("not unanimous", "no match")
# The toggle bound where the moved page has it, by locators that agree.
MOVED_TOGGLE = {
    **TOGGLE,
    "locators": [
        fx.by_role("button", "Show details"),
        {"css": "button", "scope": {"css": "#order"}},
    ],
}


@dataclass(frozen=True)
class Repairing:
    """A replay with a repair: its result, its record's directory, each drift
    the repair was handed, and the committed script with its dump from
    before the run."""

    result: RunResult
    record: Path
    handed: list[Drift]
    committed: CompiledScript
    before: dict[str, Any]


def shipping_spec(root: Path) -> Path:
    return order_spec(root, SHIPPING)


def repairing(
    tmp_path: Path,
    offer: Callable[[Drift], CompiledScript],
    *,
    script: CompiledScript = DETAILS,
    spec_at: Callable[[Path], Path] = shipping_spec,
) -> Repairing:
    """`script` replayed with every locator on the moved page, for the spec
    `spec_at` writes under a spec root, with a repair that notes each drift
    and offers what `offer` gives for it."""
    path = spec_at(tmp_path / "qa")
    config = load_config(path.parent / "config.yaml")
    spec = load_spec(path, config)
    # Deep, so a repair that edits in place can't reach the shared fixture.
    committed = fx.for_spec(script, spec).model_copy(deep=True)
    before = committed.model_dump(mode="json")
    record = RunRecord.create(path.parent)
    handed: list[Drift] = []

    async def repair(drift: Drift) -> CompiledScript:
        handed.append(drift)
        return offer(drift)

    with serving(page("moved")) as app:
        run_gate = gate(allowed=(app.origin,))

        @asynccontextmanager
        async def opening() -> AsyncIterator[Opened]:
            async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
                await register_identity_engine(playwright)
                yield Opened(playwright.chromium, proxy, record)

        async def scenario() -> RunResult:
            replaying = Replaying(spec, config, app.origin, run_gate)
            # No replay test may hang the suite.
            async with asyncio.timeout(60):
                return await replay_run(
                    committed, replaying, opening, every_locator=True, repair=repair
                )

        result = asyncio.run(scenario())
    return Repairing(result, record.path, handed, committed, before)


def rebound(script: CompiledScript, **toggle: Any) -> CompiledScript:
    """`script` with its toggle bound as the moved page has it, with
    `toggle`'s fields over that binding's."""
    moved = Target.model_validate({**MOVED_TOGGLE, **toggle})
    return script.model_copy(update={"targets": {**script.targets, "toggle": moved}})


def outcomes(run: Repairing) -> list[tuple[object, ...]]:
    return [(step.seq, step.outcome, step.misses) for step in run.result.steps]


def states(run: Repairing) -> list[tuple[object, ...]]:
    return [(line["seq"], line["state"]) for line in fx.read_steps(run.record)]


def test_a_repaired_step_runs_again_and_every_assertion_is_evaluated(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, lambda drift: rebound(drift.script))

    assert [(drift.step.seq, drift.misses) for drift in run.handed] == [(1, MISSES)]
    assert outcomes(run) == [(0, "completed", ()), (1, "completed", ())]
    assert run.result.assertions == (
        AssertionResult("a1", "pass", locator_index=0),
        AssertionResult("a2", "pass", locator_index=0),
    )
    assert run.result.outcome == "passed"
    assert states(run) == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
        (1, "completed"),
    ]


def document(run: Repairing, seq: int) -> object:
    return json.loads((run.record / f"repairs/{seq}.json").read_text())


def repairs(run: Repairing) -> list[tuple[object, ...]]:
    return [
        (repaired.seq, repaired.misses, repaired.problems, repaired.script)
        for repaired in run.result.repairs
    ]


def test_the_patched_script_is_recorded_and_the_committed_one_is_unchanged(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, lambda drift: rebound(drift.script))

    patched = rebound(run.committed)
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [],
        "script": patched.model_dump(mode="json", exclude_none=True),
    }
    assert repairs(run) == [(1, MISSES, (), patched)]
    assert run.committed.model_dump(mode="json") == run.before


def unlocated(script: CompiledScript) -> CompiledScript:
    """`script` with its toggle found by no locator, which only the loader
    refuses: `model_copy` checks nothing."""
    toggle = script.targets["toggle"].model_copy(update={"locators": ()})
    return script.model_copy(update={"targets": {**script.targets, "toggle": toggle}})


def pressing_two_keys(script: CompiledScript) -> CompiledScript:
    """`script`, rebound, with a replay-safe `press` of two keys inserted
    first, which only replay's admission refuses."""
    press = Press(seq=2, action="press", key="a+b", side_effect=False)
    moved = rebound(script)
    return moved.model_copy(update={"steps": (press, *moved.steps)})


@pytest.mark.parametrize(
    ("offer", "problem"),
    [
        pytest.param(
            lambda drift: rebound(drift.script, semantic="the order's details"),
            "/targets/toggle/semantic: a heal never changes a target's meaning",
            id="validator",
        ),
        pytest.param(
            lambda drift: unlocated(drift.script),
            "repairs/1.json: targets.toggle.locators: must list at least 1 item",
            id="loader",
        ),
        pytest.param(
            lambda drift: pressing_two_keys(drift.script),
            "steps[0] (seq 2): press takes one key, with only modifiers held "
            "before it: 'a+b'",
            id="admission",
        ),
    ],
)
def test_a_refused_repair_ends_the_run_as_drift_with_its_problems_recorded(
    tmp_path: Path, offer: Callable[[Drift], CompiledScript], problem: str
) -> None:
    run = repairing(tmp_path, offer)

    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert run.result.assertions == (
        AssertionResult("a1", "not_evaluated", stopped_at=1),
        AssertionResult("a2", "not_evaluated", stopped_at=1),
    )
    assert run.result.outcome == "failed"
    assert states(run) == [(0, "intent"), (0, "completed")]
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [problem],
        "script": None,
    }
    assert repairs(run) == [(1, MISSES, (problem,), None)]


def adding_a_target(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, with a target added in place: a
    frozen model's `targets` is still a plain dict."""
    drift.script.targets["extra"] = drift.script.targets["toggle"]
    return drift.script


def test_a_repair_that_edits_the_script_it_was_handed_is_checked_against_the_committed_one(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, adding_a_target)

    problem = "/targets/extra: a heal adds no target"
    assert repairs(run) == [(1, MISSES, (problem,), None)]
    assert run.committed.model_dump(mode="json") == run.before


def rewriting_the_step_in_place(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, holding the step it handed
    over with its fields written in place: a frozen model's storage is
    still writable."""
    vars(drift.step).update(
        target="summary", side_effect=True, side_effect_basis="network: POST /order"
    )
    return rebound(drift.script).model_copy(update={"steps": (drift.step,)})


def test_a_repair_that_rewrites_the_step_it_was_handed_in_place_is_refused(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, rewriting_the_step_in_place)

    problems = (
        "/steps/0/side_effect: a heal rewrites no existing step",
        "/steps/0/side_effect_basis: a heal rewrites no existing step",
        "/steps/0/target: a heal rewrites no existing step",
    )
    assert repairs(run) == [(1, MISSES, problems, None)]
    assert run.committed.model_dump(mode="json") == run.before


def retiming_the_session(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with the browser settings
    the live session holds, their time zone written in place."""
    vars(drift.session.settings)["timezone"] = "Asia/Tokyo"
    return rebound(drift.script).model_copy(update={"browser": drift.session.settings})


def test_a_repair_is_checked_against_the_script_as_replay_was_given_it(
    tmp_path: Path,
) -> None:
    # The session holds the settings the run opened with, the committed
    # script's own: what the repair writes there reaches no check.
    run = repairing(tmp_path, retiming_the_session)

    problem = "/browser: a heal changes nothing but target locators and inserted steps"
    assert repairs(run) == [(1, MISSES, (problem,), None)]


def test_a_step_that_drifts_again_after_its_repair_stops_the_run(
    tmp_path: Path,
) -> None:
    # The script handed over, offered unchanged, so the step drifts again.
    run = repairing(tmp_path, lambda drift: drift.script)

    assert [drift.step.seq for drift in run.handed] == [1]
    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert run.result.assertions == (
        AssertionResult("a1", "not_evaluated", stopped_at=1),
        AssertionResult("a2", "not_evaluated", stopped_at=1),
    )


# DETAILS, then a click on the footer's contact button, found by a class the
# button doesn't have, so it drifts too.
CONTACT = {"semantic": "the contact button", "locators": [{"css": "button.contact"}]}
TWO_CLICKS = DETAILS.model_copy(
    update={
        "targets": {**DETAILS.targets, "contact": Target.model_validate(CONTACT)},
        "steps": (
            *DETAILS.steps,
            Click(seq=2, action="click", target="contact", side_effect=False),
        ),
    }
)


def rebinding_each(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, with the drifted step's target bound
    as the moved page has it."""
    if drift.step.seq == 1:
        return rebound(drift.script)
    footer = {**CONTACT, "locators": [{"css": "button", "scope": {"css": "footer"}}]}
    targets = {**drift.script.targets, "contact": Target.model_validate(footer)}
    return drift.script.model_copy(update={"targets": targets})


def test_a_later_drift_gets_its_own_repair_handed_the_script_in_use(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, rebinding_each, script=TWO_CLICKS)

    assert [(drift.step.seq, drift.misses) for drift in run.handed] == [
        (1, MISSES),
        (2, ("no match",)),
    ]
    assert run.handed[1].script.targets["toggle"] == Target.model_validate(MOVED_TOGGLE)
    assert outcomes(run) == [
        (0, "completed", ()),
        (1, "completed", ()),
        (2, "completed", ()),
    ]
    assert [(r.seq, r.problems) for r in run.result.repairs] == [(1, ()), (2, ())]
    assert run.result.outcome == "passed"


# A fetch to a host the run doesn't allow, awaited until the proxy refuses it.
FETCH_UNDECLARED = "() => fetch('http://undeclared.example.test:8080/x').catch(() => 0)"


def test_no_repair_is_asked_for_once_an_egress_block_interrupted_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The block lands during the toggle's first lookup, before it drifts.
    fired = around(monkeypatch, "role=button", (1, FETCH_UNDECLARED, "() => 0"))

    run = repairing(tmp_path, lambda drift: rebound(drift.script))

    assert fired == [1]
    assert run.handed == []
    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert (run.result.outcome, run.result.error_code) == ("errored", "egress_blocked")


# A Tab pressed (seq 1), then the toggle clicked (seq 2).
TAB_FIRST = DETAILS.model_copy(
    update={
        "steps": (
            Press(seq=1, action="press", key="Tab", side_effect=False),
            Click(seq=2, action="click", target="toggle", side_effect=False),
        )
    }
)


def inserting_before_the_tab(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with a replay-safe `press`
    inserted before seq 1, which already ran."""
    press = Press(seq=3, action="press", key="Tab", side_effect=False)
    moved = rebound(drift.script)
    return moved.model_copy(update={"steps": (press, *moved.steps)})


def test_a_repair_that_inserts_a_step_before_one_that_ran_is_refused(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, inserting_before_the_tab, script=TAB_FIRST)

    problem = "/steps: a repair inserts no step before a step that already ran"
    assert repairs(run) == [(2, MISSES, (problem,), None)]
    assert outcomes(run) == [
        (0, "completed", ()),
        (1, "completed", ()),
        (2, "drifted", MISSES),
    ]


def secret_spec_at(root: Path) -> Path:
    """The order spec, whose account's password is the test secret
    TEST_PASSWORD, which its config binds at the start origin."""
    path = order_spec(root, SHIPPING)
    (root / "config.yaml").write_text(
        f"budgets: {{ resolve_seconds: 1 }}\nsecrets: {{ {BOUND_AT_START} }}\n"
    )
    account = "{ email: reader@example.test, password: { secret: TEST_PASSWORD } }"
    path.write_text(
        path.read_text().replace(
            "preconditions:\n", f"preconditions:\n  account: {account}\n"
        )
    )
    return path


def held(tmp_path: Path, value: str, run: Repairing) -> list[str]:
    """Where a copy of `value` is: any file under `tmp_path` (the spec root
    with the run's record), or the run's result."""
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    return copies_found(value, files=files, texts=[repr(run.result)])


def test_a_repaired_script_that_spells_a_bound_value_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    named = [
        fx.by_role("button", FAKE_VALUE),
        {"css": "button", "scope": {"css": "#order"}},
    ]

    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script, locators=named),
        spec_at=secret_spec_at,
    )

    problem = "repairs/1.json: a bound value's spelling is in it, so it isn't written"
    assert repairs(run) == [(1, MISSES, (problem,), None)]
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [problem],
        "script": None,
    }
    assert held(tmp_path, FAKE_VALUE, run) == []


def misfilled(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with the toggle's meaning a
    list holding the bound value: `model_copy` checks no field's type."""
    moved = rebound(drift.script)
    toggle = moved.targets["toggle"].model_copy(update={"semantic": [FAKE_VALUE]})
    return moved.model_copy(update={"targets": {**moved.targets, "toggle": toggle}})


def test_a_repaired_script_holding_a_value_its_field_doesnt_allow_is_refused_unquoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every warning is an error in this suite, so a serializer's warning,
    # which quotes the value, would reach this test as an exception.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)

    run = repairing(tmp_path, misfilled, spec_at=secret_spec_at)

    problem = (
        "repairs/1.json: a field holds a value its type doesn't allow, so it isn't read"
    )
    assert repairs(run) == [(1, MISSES, (problem,), None)]
    assert held(tmp_path, FAKE_VALUE, run) == []


def test_a_refusals_problems_are_scanned_before_they_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A value with no `~` or `/`, which a JSON pointer would escape.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", OTHER_FAKE_VALUE)

    def adding_a_named_target(drift: Drift) -> CompiledScript:
        targets = {
            **drift.script.targets,
            OTHER_FAKE_VALUE: drift.script.targets["toggle"],
        }
        return drift.script.model_copy(update={"targets": targets})

    run = repairing(tmp_path, adding_a_named_target, spec_at=secret_spec_at)

    problem = "/targets/[SECRET:TEST_PASSWORD]: a heal adds no target"
    assert repairs(run) == [(1, MISSES, (problem,), None)]
    assert held(tmp_path, OTHER_FAKE_VALUE, run) == []
