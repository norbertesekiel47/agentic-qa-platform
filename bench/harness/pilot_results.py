"""Pure pilot observations; manifest answers enter only the later comparison."""

from dataclasses import dataclass
from typing import NamedTuple

from aqa_core.compiled import CompiledScript
from aqa_core.spec import InvariantName, Invariants
from aqa_runner.executor import (
    AssertionOutcome,
    ErrorCode,
    RunOutcome,
    RunResult,
    StepOutcome,
)
from aqa_runner.invariants import INVARIANTS, InvariantOutcome
from aqa_runner.locators import Miss
from aqa_runner.settling import Settled
from manifest import DRIFT, VIOLATED, Expected


class StepObservation(NamedTuple):
    seq: int
    outcome: StepOutcome
    settled: Settled | None
    locator_index: int | None
    misses: tuple[Miss, ...]
    error: bool


class AssertionObservation(NamedTuple):
    id: str
    outcome: AssertionOutcome
    misses: tuple[Miss, ...]
    stopped_at: int | None
    error: bool


class InvariantObservation(NamedTuple):
    name: InvariantName
    outcome: InvariantOutcome
    total: int


@dataclass(frozen=True)
class Observation:
    """Semantic replay value with no mutable windows or raw runtime text."""

    spec_id: str
    outcome: RunOutcome
    error_code: ErrorCode | None
    steps: tuple[StepObservation, ...]
    assertions: tuple[AssertionObservation, ...]
    invariants: tuple[InvariantObservation, ...]
    failed_expectations: frozenset[int]
    violated_invariants: frozenset[InvariantName]
    eligible: bool


def _identities(actual: tuple[str, ...], expected: tuple[str, ...], kind: str) -> None:
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise ValueError(f"{kind} identities must appear exactly once")


def observe(
    script: CompiledScript, result: RunResult, *, invariants: Invariants
) -> Observation:
    """Project a replay of a strict-loaded script using its spec's settings.

    Malformed identities/counts raise; incomplete or inconclusive attempts
    remain observable but cannot score. Ground truth is never an input.
    """
    _identities(
        tuple(a.id for a in result.assertions),
        tuple(a.id for a in script.assertions),
        "assertion",
    )
    _identities(tuple(i.name for i in result.invariants), INVARIANTS, "invariant")
    expected_steps = (0, *(step.seq for step in script.steps))
    if tuple(step.seq for step in result.steps) != expected_steps[: len(result.steps)]:
        raise ValueError("steps must follow the compiled prefix including navigation")
    if any(i.total < 0 for i in result.invariants):
        raise ValueError("invariant counts must be nonnegative")
    assertions = {a.id: a for a in result.assertions}
    outcomes = {i.name: i for i in result.invariants}
    steps = tuple(
        StepObservation(
            s.seq, s.outcome, s.settled, s.locator_index, s.misses, s.error is not None
        )
        for s in result.steps
    )
    checks = tuple(
        AssertionObservation(
            a.id, a.outcome, a.misses, a.stopped_at, a.error is not None
        )
        for compiled in script.assertions
        for a in (assertions[compiled.id],)
    )
    observed_invariants = tuple(
        InvariantObservation(name, outcomes[name].outcome, outcomes[name].total)
        for name in INVARIANTS
    )
    return Observation(
        script.spec_id,
        result.outcome,
        result.error_code,
        steps,
        checks,
        observed_invariants,
        frozenset(
            a.expect_index
            for a in script.assertions
            if assertions[a.id].outcome == "failed"
        ),
        frozenset(i.name for i in observed_invariants if i.outcome == "violated"),
        len(steps) == len(expected_steps) and _eligible(result, invariants),
    )


def _eligible(result: RunResult, settings: Invariants) -> bool:
    return (
        result.outcome != "errored"
        and result.error_code is None
        and not result.infrastructure_events
        and not result.egress_blocks.blocked
        and all(
            s.outcome == "completed" and s.settled == "idle" and s.error is None
            for s in result.steps
        )
        and all(
            a.outcome in ("pass", "failed") and a.error is None and a.stopped_at is None
            for a in result.assertions
        )
        and all(
            i.outcome
            == (
                "disabled"
                if not settings.inherit or i.name in settings.disable
                else "violated"
                if i.total
                else "held"
            )
            for i in result.invariants
        )
    )


def compare(observation: Observation, expected: Expected) -> bool:
    """Match one explicit, validated manifest row after replay has finished."""
    if (
        not observation.eligible
        or observation.spec_id != expected.spec
        or observation.failed_expectations != frozenset(expected.expect)
        or observation.violated_invariants != frozenset(expected.invariants)
    ):
        return False
    has_failure = bool(
        observation.failed_expectations or observation.violated_invariants
    )
    if expected.verdict == VIOLATED:
        return has_failure and observation.outcome == "failed"
    return (
        expected.verdict == DRIFT
        and not has_failure
        and observation.outcome == "passed"
    )
