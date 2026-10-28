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
from aqa_core.compiled import CompiledScript, Press, Target
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


def repairing(
    tmp_path: Path,
    offer: Callable[[Drift], CompiledScript],
    *,
    html: str | None = None,
    script: CompiledScript = DETAILS,
) -> Repairing:
    """`script` replayed with every locator on the moved page, or on `html`,
    for the order spec, with a repair that notes each drift and offers what
    `offer` gives for it."""
    path = order_spec(tmp_path / "qa", SHIPPING)
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

    with serving(html or page("moved")) as app:
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
