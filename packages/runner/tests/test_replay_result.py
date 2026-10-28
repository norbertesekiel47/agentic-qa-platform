"""`aqa_runner.replay_result`: the outcome a strict replay exits with when
several causes hold, per run and across specs (ADR-0024's #238 amendment),
and the verdict each step, assertion and invariant gets in its document,
`replay.json` (DATA_MODEL §7). Records and redactors are real, results are made by hand, and every
expected value is a literal. A result's own `outcome` stays `passed`: what
the run left decides, never the executor's word for it."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.spec import InvariantName, Spec
from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks, RefusedHost
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import Evidence, RunResult, StepResult
from aqa_runner.invariants import InvariantResult
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.replay_result import (
    EXIT_CODES,
    Outcome,
    ReplayEnd,
    Stop,
    final_outcome,
    present,
    rolled_up,
)
from aqa_runner.run_record import RunRecord

from packages.runner.tests.executor_fixtures import a_spec, compiled
from packages.runner.tests.result_redaction_fixtures import fake_secret

APP = "http://app.example.test:8080"
BOUND = Redactor([fake_secret("TEST_PASSWORD", "fake-bound-value", APP)])
CLICK = {"action": "click", "target": "save"}
NAVIGATE = {"action": "navigate", "url": "/"}
CLEAN = EgressBlocks((), False)
CDN = EgressBlocks((RefusedHost("cdn.evil.test", 443),), False)
EVENT = InfrastructureEvent("api.evil.test", 8443, "the name didn't resolve")
SANDBOX = "Chromium's sandbox didn't start"
RESET = "the reset hook answered 500"
UNWRITTEN = "the run record couldn't be written"
HELD = tuple(
    InvariantResult(name, "held", (), 0)
    for name in ("console_errors", "js_exceptions", "http_5xx", "broken_images")
)


@dataclass(frozen=True)
class Made:
    """A strict run's spec, script and record, scanning with a redactor."""

    spec: Spec
    script: CompiledScript
    record: RunRecord


def made(
    tmp_path: Path,
    redactor: Redactor = NO_SECRETS,
    *,
    second: str = "The save button shows",
) -> Made:
    """A run of a script that clicks save, then checks three things of the
    spec's first expectation and one of its second, `second`."""
    script = compiled(
        [{"seq": 1, "action": "click", "target": "save", "side_effect": False}],
        targets={"save": {"semantic": "the save button", "locators": [{"css": "b"}]}},
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"},
            {"id": "a2", "expect_index": 0, "check": "text_visible", "text": "saved"},
            {"id": "a3", "expect_index": 0, "check": "text_visible", "text": "done"},
            {
                "id": "a4",
                "expect_index": 1,
                "check": "visible_unoccluded",
                "target": "save",
                "min_size_px": [10, 10],
                "in_viewport": True,
            },
        ],
    )
    # `extra` follows the spec's first expectation, so it lists a second.
    listed = f"  - {second}\n"
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/form", extra=listed)
    return Made(spec, script, RunRecord.create(tmp_path).redacting(redactor))


def ending(made: Made, **changes: Any) -> ReplayEnd:
    """How `made`'s run ended: replay returned, steps 0 and 1 completed, a1
    passed and every invariant held, with no block, event or stop, plus the
    steps, assertions and stops `changes` adds, the blocks and events it
    gives, and the result fields it sets."""
    stops = changes.pop("stops", ())
    blocks = changes.pop("blocks", CLEAN)
    events = changes.pop("infrastructure_events", ())
    result = RunResult(
        made.record.run_id,
        "passed",
        (StepResult(0, "completed"), StepResult(1, "completed", locator_index=0)),
        (AssertionResult("a1", "pass"),),
        (),
        (),
        HELD,
        CLEAN,
        None,
        0,
        Evidence(1, None),
    )
    result = replace(
        result,
        steps=result.steps + changes.pop("steps", ()),
        assertions=result.assertions + changes.pop("assertions", ()),
        **changes,
    )
    return ReplayEnd(made.record, made.spec, made.script, result, blocks, events, stops)


def side_effect_left_unresolved(made: Made) -> dict[str, Any]:
    made.record.step_intent(2, CLICK, side_effect=True, target_used="save")
    return {"steps": (StepResult(2, "failed", error="the page crashed"),)}


# What makes each outcome hold, and nothing more decisive.
CAUSES: dict[str, Callable[[Made], dict[str, Any]]] = {
    "egress_blocked": lambda _: {"blocks": CDN},
    "non_resumable": side_effect_left_unresolved,
    "no_sandbox": lambda _: {
        "stops": (Stop("no_sandbox", NO_SECRETS.redact(SANDBOX)),)
    },
    "reset_unsafe": lambda _: {
        "stops": (Stop("reset_unsafe", NO_SECRETS.redact(RESET)),)
    },
    "unreachable": lambda _: {"infrastructure_events": (EVENT,)},
    "record_error": lambda _: {
        "stops": (Stop("record_error", NO_SECRETS.redact(UNWRITTEN)),)
    },
    "expectation_violated": lambda _: {
        "assertions": (AssertionResult("a2", "failed", locator_index=0),)
    },
    "inconclusive": lambda _: {
        "assertions": (AssertionResult("a3", "check_timed_out"),)
    },
    "passed": lambda _: {},
}

# Each outcome with its exit code, most decisive first.
EXITS = [
    ("egress_blocked", 6),
    ("non_resumable", 4),
    ("no_sandbox", 10),
    ("reset_unsafe", 14),
    ("unreachable", 13),
    ("record_error", 15),
    ("expectation_violated", 1),
    ("inconclusive", 3),
    ("passed", 0),
]


def merged(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Two causes' changes at once: what both add, both added."""
    both = first | second
    for added in ("steps", "assertions", "stops"):
        both[added] = first.get(added, ()) + second.get(added, ())
    return both


@pytest.mark.parametrize(("cause", "code"), EXITS)
def test_each_outcome_alone_exits_with_its_code(
    tmp_path: Path, cause: str, code: int
) -> None:
    run = made(tmp_path)

    outcome: Outcome = final_outcome(ending(run, **CAUSES[cause](run)))

    assert (outcome, EXIT_CODES[outcome]) == (cause, code)


@pytest.mark.parametrize(
    ("first", "second"), list(combinations([cause for cause, _ in EXITS], 2))
)
def test_each_pair_of_outcomes_resolves_by_precedence(
    tmp_path: Path, first: str, second: str
) -> None:
    run = made(tmp_path)

    end = ending(run, **merged(CAUSES[first](run), CAUSES[second](run)))

    assert final_outcome(end) == first


def test_a_replay_that_returned_no_result_never_passes(tmp_path: Path) -> None:
    run = made(tmp_path)

    end = ReplayEnd(run.record, run.spec, run.script, None, CLEAN, ())

    assert final_outcome(end) == "inconclusive"


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        (["passed"], "passed"),
        (["passed", "inconclusive", "expectation_violated"], "expectation_violated"),
        (["inconclusive", "passed"], "inconclusive"),
        (["expectation_violated", "unreachable", "record_error"], "unreachable"),
        (["passed", "no_sandbox", "non_resumable", "egress_blocked"], "egress_blocked"),
    ],
)
def test_runs_roll_up_by_the_same_precedence(
    outcomes: list[Outcome], expected: Outcome
) -> None:
    assert rolled_up(outcomes) == expected


def document(end: ReplayEnd) -> Any:
    """`end`'s document as a reader of its JSON gets it."""
    return json.loads(json.dumps(present(end)))


def judged(end: ReplayEnd, kind: str, name: object) -> dict[str, Any]:
    """The verdict `end`'s document gives the step, assertion or invariant
    `kind` called `name`."""
    verdicts: list[dict[str, Any]] = document(end)["verdicts"]
    return next(verdict for verdict in verdicts if verdict.get(kind) == name)


VIOLATED = (InvariantResult("js_exceptions", "violated", ("boom",), 1),)


@pytest.mark.parametrize(
    ("added", "kind", "name", "expected"),
    [
        ({}, "step", 1, ("pass", None)),
        (
            {
                "steps": (
                    StepResult(2, "drifted", misses=("no match", "not unanimous")),
                )
            },
            "step",
            2,
            (
                "inconclusive",
                "drift: strict replay doesn't heal (no match, not unanimous)",
            ),
        ),
        (
            {"steps": (StepResult(2, "failed", error="the page crashed"),)},
            "step",
            2,
            ("inconclusive", "the page crashed"),
        ),
        ({}, "assertion", "a1", ("pass", None)),
        (
            {"assertions": (AssertionResult("a2", "failed", locator_index=0),)},
            "assertion",
            "a2",
            ("expectation_violated", None),
        ),
        (
            {
                "assertions": (
                    AssertionResult("a2", "binding_unresolved", ("ambiguous",)),
                )
            },
            "assertion",
            "a2",
            ("inconclusive", "drift: strict replay doesn't heal (ambiguous)"),
        ),
        (
            {"assertions": (AssertionResult("a2", "binding_unresolved"),)},
            "assertion",
            "a2",
            ("inconclusive", "drift: strict replay doesn't heal"),
        ),
        (
            {"assertions": (AssertionResult("a2", "check_timed_out"),)},
            "assertion",
            "a2",
            ("inconclusive", "check_timed_out"),
        ),
        (
            {
                "assertions": (
                    AssertionResult("a2", "not_evaluated", error="the page stopped"),
                )
            },
            "assertion",
            "a2",
            ("inconclusive", "the page stopped"),
        ),
        ({}, "invariant", "console_errors", ("pass", None)),
        (
            {"invariants": VIOLATED},
            "invariant",
            "js_exceptions",
            ("expectation_violated", None),
        ),
    ],
)
def test_each_executor_result_gets_its_verdict(
    tmp_path: Path, added: dict[str, Any], kind: str, name: object, expected: object
) -> None:
    verdict = judged(ending(made(tmp_path), **added), kind, name)

    assert (verdict["verdict"], verdict["reason"]) == expected


def unevaluated(seq: int) -> tuple[AssertionResult, ...]:
    return tuple(
        AssertionResult(check, "not_evaluated", stopped_at=seq)
        for check in ("a1", "a2", "a3")
    )


@pytest.mark.parametrize(
    ("steps", "assertions", "expected"),
    [
        # A step drifted, in a script with no check.
        (
            (StepResult(2, "drifted", misses=("no match",)),),
            (),
            [("pass", "step"), ("pass", "step"), ("inconclusive", "step")],
        ),
        # An interruption after the last step, so no check was evaluated.
        ((), unevaluated(1), [("pass", "step"), ("pass", "step")]),
    ],
    ids=["stopped-step", "interrupted"],
)
def test_what_never_ran_or_was_turned_off_gets_no_verdict(
    tmp_path: Path,
    steps: tuple[StepResult, ...],
    assertions: tuple[AssertionResult, ...],
    expected: list[tuple[str, str]],
) -> None:
    end = ending(made(tmp_path), steps=steps)
    assert end.result is not None
    # What a stopped run held is no more established than an unevaluated
    # check; what it violated is.
    observed = (
        HELD[0],
        *VIOLATED,
        HELD[2],
        InvariantResult("broken_images", "disabled", (), 0),
    )
    end = replace(
        end, result=replace(end.result, assertions=assertions, invariants=observed)
    )

    verdicts = document(end)["verdicts"]

    assert [(verdict["verdict"], *list(verdict)[:1]) for verdict in verdicts] == [
        *expected,
        ("expectation_violated", "invariant"),
    ]


def test_an_egress_blocked_run_has_no_verdict_and_no_finding(tmp_path: Path) -> None:
    blocked = ending(
        made(tmp_path),
        blocks=CDN,
        assertions=(AssertionResult("a2", "failed", locator_index=0),),
        invariants=VIOLATED,
    )

    presented = document(blocked)

    keys = ("outcome", "exit_code", "error_code", "reasons", "verdicts")
    assert tuple(presented[key] for key in keys) == (
        "egress_blocked",
        6,
        "egress_blocked",
        [],
        [],
    )


def violated(name: InvariantName) -> dict[str, Any]:
    return {"invariants": (InvariantResult(name, "violated", ("seen",), 1),)}


@pytest.mark.parametrize(
    ("added", "kind", "name", "about"),
    [
        (
            {"assertions": (AssertionResult("a2", "failed", locator_index=0),)},
            "assertion",
            "a2",
            ("functional", "The form is saved"),
        ),
        (
            {"assertions": (AssertionResult("a4", "failed", locator_index=0),)},
            "assertion",
            "a4",
            ("visual", "The save button shows"),
        ),
        (
            violated("console_errors"),
            "invariant",
            "console_errors",
            ("js_error", "invariant console_errors violated"),
        ),
        (
            violated("js_exceptions"),
            "invariant",
            "js_exceptions",
            ("js_error", "invariant js_exceptions violated"),
        ),
        (
            violated("http_5xx"),
            "invariant",
            "http_5xx",
            ("backend_5xx", "invariant http_5xx violated"),
        ),
        (
            violated("broken_images"),
            "invariant",
            "broken_images",
            ("visual", "invariant broken_images violated"),
        ),
    ],
)
def test_each_violation_carries_a_finding_of_its_category(
    tmp_path: Path,
    added: dict[str, Any],
    kind: str,
    name: str,
    about: tuple[str, str],
) -> None:
    verdict = judged(ending(made(tmp_path), **added), kind, name)

    category, title = about
    assert verdict["finding"] == {
        "category": category,
        "title": title,
        "repro_steps": [0, 1],
    }


def evidence(seq: int) -> dict[str, object]:
    return {"path": f"evidence/{seq}", "screenshot": None}


def test_the_document_holds_each_verdict_with_its_evidence(tmp_path: Path) -> None:
    run = made(tmp_path)
    end = ending(
        run,
        assertions=(
            AssertionResult("a2", "failed", locator_index=1),
            AssertionResult("a3", "check_timed_out"),
            AssertionResult("a4", "binding_unresolved", ("no match",)),
        ),
        invariants=(
            HELD[0],
            InvariantResult("js_exceptions", "violated", ("boom",), 1),
            HELD[2],
            InvariantResult("broken_images", "disabled", (), 0),
        ),
        # The page where the invariant was first violated.
        evidence=Evidence(0, None),
    )

    presented = document(end)

    assert presented.pop("run_id") == run.record.run_id
    assert presented == {
        "schema_version": 1,
        "spec_id": "replay",
        "mode": "strict",
        "outcome": "expectation_violated",
        "exit_code": 1,
        "withheld": False,
        "error_code": None,
        "reasons": [
            "a2: expectation_violated",
            "a3: inconclusive, check_timed_out",
            "a4: inconclusive, drift: strict replay doesn't heal (no match)",
            "invariant js_exceptions: expectation_violated",
        ],
        "non_resumable": [],
        "infrastructure_events": {"count": 0, "events": []},
        "verdicts": [
            {
                "step": 0,
                "verdict": "pass",
                "reason": None,
                "locator_index": None,
                "misses": [],
                "evidence": evidence(0),
                "finding": None,
            },
            {
                "step": 1,
                "verdict": "pass",
                "reason": None,
                "locator_index": 0,
                "misses": [],
                "evidence": evidence(1),
                "finding": None,
            },
            {
                "assertion": "a1",
                "verdict": "pass",
                "reason": None,
                "locator_index": None,
                "misses": [],
                "evidence": evidence(1),
                "finding": None,
            },
            {
                "assertion": "a2",
                "verdict": "expectation_violated",
                "reason": None,
                "locator_index": 1,
                "misses": [],
                "evidence": evidence(1),
                "finding": {
                    "category": "functional",
                    "title": "The form is saved",
                    "repro_steps": [0, 1],
                },
            },
            {
                "assertion": "a3",
                "verdict": "inconclusive",
                "reason": "check_timed_out",
                "locator_index": None,
                "misses": [],
                "evidence": evidence(1),
                "finding": None,
            },
            {
                "assertion": "a4",
                "verdict": "inconclusive",
                "reason": "drift: strict replay doesn't heal (no match)",
                "locator_index": None,
                "misses": ["no match"],
                "evidence": evidence(1),
                "finding": None,
            },
            {
                "invariant": "console_errors",
                "verdict": "pass",
                "reason": None,
                "evidence": evidence(1),
                "finding": None,
            },
            {
                "invariant": "js_exceptions",
                "verdict": "expectation_violated",
                "reason": None,
                "evidence": evidence(0),
                "finding": {
                    "category": "js_error",
                    "title": "invariant js_exceptions violated",
                    "repro_steps": [0, 1],
                },
            },
            {
                "invariant": "http_5xx",
                "verdict": "pass",
                "reason": None,
                "evidence": evidence(1),
                "finding": None,
            },
        ],
    }


@pytest.mark.parametrize(
    ("redactor", "events"),
    [
        (
            NO_SECRETS,
            [
                {
                    "host": "api.evil.test",
                    "port": 8443,
                    "cause": "the name didn't resolve",
                }
            ],
        ),
        (BOUND, None),
    ],
)
def test_infrastructure_events_are_named_only_in_a_run_that_binds_no_secret(
    tmp_path: Path, redactor: Redactor, events: object
) -> None:
    end = ending(made(tmp_path, redactor), infrastructure_events=(EVENT,))

    presented = document(end)

    assert (presented["outcome"], presented["exit_code"]) == ("unreachable", 13)
    assert presented["withheld"] == (redactor is BOUND)
    assert presented["infrastructure_events"] == {"count": 1, "events": events}


def test_every_reason_and_title_is_scanned_again_with_the_runs_scan(
    tmp_path: Path,
) -> None:
    # Scanned by another scan than the run's, so each still spells the value.
    leak = NO_SECRETS.redact("fake-bound-value was in it")
    end = ending(
        made(tmp_path, BOUND, second=leak),
        steps=(StepResult(2, "failed", error=leak),),
        assertions=(AssertionResult("a4", "failed", locator_index=0),),
        stops=(Stop("record_error", leak),),
    )

    presented = document(end)

    assert presented["reasons"] == [
        "[SECRET:TEST_PASSWORD] was in it",
        "step 2: inconclusive, [SECRET:TEST_PASSWORD] was in it",
        "a4: expectation_violated",
    ]
    assert judged(end, "step", 2)["reason"] == "[SECRET:TEST_PASSWORD] was in it"
    assert judged(end, "assertion", "a4")["finding"]["title"] == (
        "[SECRET:TEST_PASSWORD] was in it"
    )


def test_an_egress_block_decides_though_replay_raised_after_it(tmp_path: Path) -> None:
    run = made(tmp_path)
    side_effect_left_unresolved(run)
    stops = (Stop("record_error", NO_SECRETS.redact(UNWRITTEN)),)

    raised = ReplayEnd(run.record, run.spec, run.script, None, CDN, (), stops)

    keys = ("outcome", "exit_code", "error_code", "reasons", "non_resumable")
    assert tuple(document(raised)[key] for key in keys) == (
        "egress_blocked",
        6,
        "egress_blocked",
        [UNWRITTEN],
        [2],
    )


def test_a_replay_that_raised_still_records_its_outcome_reasons_and_non_resumable_steps(
    tmp_path: Path,
) -> None:
    run = made(tmp_path)
    run.record.step_intent(0, NAVIGATE, side_effect=False, target_used=None)
    run.record.step_completed(0, locator_used=None, settled="idle")
    run.record.step_intent(1, CLICK, side_effect=True, target_used="save")
    stops = (Stop("record_error", NO_SECRETS.redact(UNWRITTEN)),)

    raised = ReplayEnd(run.record, run.spec, run.script, None, CLEAN, (), stops)

    presented = document(raised)

    assert presented.pop("run_id") == run.record.run_id
    assert presented == {
        "schema_version": 1,
        "spec_id": "replay",
        "mode": "strict",
        "outcome": "non_resumable",
        "exit_code": 4,
        "withheld": False,
        "error_code": None,
        "reasons": ["the run record couldn't be written"],
        "non_resumable": [1],
        "infrastructure_events": {"count": 0, "events": []},
        "verdicts": [],
    }
