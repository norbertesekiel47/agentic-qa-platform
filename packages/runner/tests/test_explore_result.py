"""`aqa_runner.explore_result`: the outcome an explore run exits with when
several causes hold (ADR-0024's #53 P12 amendment), and the document it
presents (ADR-0026's): an allow-list, withheld by the run record's scan,
whose parts must be made from that record. Records and redactors are real;
every expected value is a literal."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, make_dataclass, replace
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner.document_origins import PolicyEvent, Records
from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks, PhaseTraffic, RefusedHost
from aqa_runner.executor import AssertionResult, RunResult, StepResult
from aqa_runner.explore_result import (
    EXIT_CODES,
    Confirmation,
    ExploreEnd,
    Outcome,
    Phase,
    Stop,
    final_outcome,
    present,
)
from aqa_runner.invariants import InvariantResult
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.run_record import (
    PendingIntent,
    RefusedWriteError,
    RunRecord,
    require_clean,
)
from aqa_runner.settling import Exchange, PageRequest, Window

from packages.runner.tests.executor_fixtures import (
    App,
    Run,
    a_spec,
    compiled,
    run,
    serving_app,
)
from packages.runner.tests.result_redaction_fixtures import (
    fake_secret,
    probing_account,
)
from packages.runner.tests.secret_fixtures import secret_spec

APP = "http://app.example.test:8080"
BOUND = Redactor([fake_secret("TEST_PASSWORD", "fake-bound-value", APP)])
CLICK = {"action": "click", "target": "save"}
NAVIGATE = {"action": "navigate", "url": "/"}
CLEAN = EgressBlocks((), False)
QUIET = PhaseTraffic(0, 0)
CDN = EgressBlocks((RefusedHost("cdn.evil.test", 443),), False)
EVENT = InfrastructureEvent("api.evil.test", 8443, "the name didn't resolve")
SANDBOX = "Chromium's sandbox didn't start"
BROKEN = "steps.jsonl: an earlier write to it failed, so nothing more is appended"
MINUTES = "the minutes budget ran out"


@dataclass(frozen=True)
class Parts:
    """A run's record, scanning with a redactor, and its two parts."""

    record: RunRecord
    attempt: RunRecord
    confirmation: RunRecord


def parts(tmp_path: Path, redactor: Redactor = NO_SECRETS) -> Parts:
    record = RunRecord.create(tmp_path).redacting(redactor)
    return Parts(record, record.part("attempt-1"), record.part("confirmation"))


def ending(made: Parts, **changes: Any) -> ExploreEnd:
    """How `made`'s run ended: nothing held, nothing published, no
    confirmation, except what `changes` says."""
    nothing = ExploreEnd(
        record=made.record,
        attempt=Phase(made.attempt, QUIET, 0),
        confirmation=None,
        blocks=CLEAN,
        infrastructure_events=(),
        checks={},
        stops=(),
        published=None,
    )
    return replace(nothing, **changes)


def confirmed(
    made: Parts, result: RunResult | None = None, **changes: Any
) -> ExploreEnd:
    """`ending`, with a confirmation phase whose replay returned `result`."""
    confirmation = Confirmation(made.confirmation, QUIET, 0, result)
    return ending(made, confirmation=confirmation, **changes)


def replayed(made: Parts, **changes: Any) -> RunResult:
    """What the confirmation's replay returned: it passed, with no step,
    assertion, event, invariant or block, except what `changes` says."""
    passed = RunResult(made.record.run_id, "passed", (), (), (), (), (), CLEAN, None)
    return replace(passed, **changes)


def stop(outcome: Any, text: str) -> Stop:
    return Stop(outcome, NO_SECRETS.redact(text))


def outcome_of(end: ExploreEnd) -> tuple[object, object, object]:
    document = present(end)
    return document["outcome"], document["exit_code"], document["non_resumable"]


def side_effect_in_the_attempt(made: Parts) -> dict[str, Any]:
    made.attempt.step_intent(3, CLICK, side_effect=True, target_used="save")
    return {}


# What makes each outcome hold, and nothing else.
CAUSES: dict[str, Callable[[Parts], dict[str, Any]]] = {
    "egress_blocked": lambda _: {"blocks": CDN},
    "non_resumable": side_effect_in_the_attempt,
    "no_sandbox": lambda _: {"stops": (stop("no_sandbox", SANDBOX),)},
    "reset_unsafe": lambda made: {
        "attempt": Phase(made.attempt, PhaseTraffic(0, 1), 0)
    },
    "unreachable": lambda _: {"infrastructure_events": (EVENT,)},
    "record_error": lambda _: {"stops": (stop("record_error", BROKEN),)},
    "no_response": lambda _: {"stops": (stop("no_response", "no model response"),)},
    "gave_up": lambda _: {"stops": (stop("gave_up", MINUTES),)},
    "compiled": lambda _: {"published": "confirmed"},
}

# Each outcome with its exit code, most decisive first.
EXITS = [
    ("egress_blocked", 6),
    ("non_resumable", 4),
    ("no_sandbox", 10),
    ("reset_unsafe", 14),
    ("unreachable", 13),
    ("record_error", 15),
    ("no_response", 11),
    ("gave_up", 3),
    ("compiled", 0),
]


@pytest.mark.parametrize(("cause", "code"), EXITS)
def test_each_outcome_alone_exits_with_its_code(
    tmp_path: Path, cause: str, code: int
) -> None:
    made = parts(tmp_path)

    outcome: Outcome = final_outcome(ending(made, **CAUSES[cause](made)))

    assert (outcome, EXIT_CODES[outcome]) == (cause, code)


@pytest.mark.parametrize(
    ("first", "second"), list(combinations([cause for cause, _ in EXITS], 2))
)
def test_each_pair_of_outcomes_resolves_by_precedence(
    tmp_path: Path, first: str, second: str
) -> None:
    made = parts(tmp_path)
    facts = CAUSES[first](made)
    more = CAUSES[second](made)
    facts["stops"] = facts.get("stops", ()) + more.pop("stops", ())

    assert final_outcome(ending(made, **facts, **more)) == first


def test_stops_decide_by_precedence_and_keep_their_order(tmp_path: Path) -> None:
    stops = (
        stop("gave_up", MINUTES),
        stop("record_error", BROKEN),
        stop("no_sandbox", SANDBOX),
    )

    document = present(ending(parts(tmp_path), stops=stops))

    assert (document["outcome"], document["exit_code"]) == ("no_sandbox", 10)
    assert document["reasons"] == [
        "the minutes budget ran out",
        "steps.jsonl: an earlier write to it failed, so nothing more is appended",
        "Chromium's sandbox didn't start",
    ]


@pytest.mark.parametrize(
    ("published", "expected"),
    [(None, ("gave_up", 3, [], [])), ("unconfirmed", ("compiled", 0, [], []))],
)
def test_a_run_that_published_nothing_gives_up_and_an_unconfirmed_one_compiles(
    tmp_path: Path, published: str | None, expected: tuple[object, ...]
) -> None:
    document = present(ending(parts(tmp_path), published=published))

    keys = ("outcome", "exit_code", "reasons", "non_resumable")
    assert tuple(document[key] for key in keys) == expected
    assert document["published"] == published


@pytest.mark.parametrize("part", ["attempt-1", "confirmation"])
@pytest.mark.parametrize("published", [None, "unconfirmed", "confirmed"])
def test_an_unresolved_read_only_intent_is_terminal_but_never_exit_4(
    tmp_path: Path, part: str, published: str | None
) -> None:
    made = parts(tmp_path)
    phase = made.attempt if part == "attempt-1" else made.confirmation
    phase.step_intent(0, NAVIGATE, side_effect=False, target_used=None)

    assert outcome_of(confirmed(made, published=published)) == ("gave_up", 3, [])


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"infrastructure_events": (EVENT,)}, ("unreachable", 13, [])),
        ({"blocks": CDN}, ("egress_blocked", 6, [])),
    ],
)
def test_an_unresolved_read_only_intent_leaves_a_more_decisive_cause_to_decide(
    tmp_path: Path, facts: dict[str, Any], expected: tuple[object, ...]
) -> None:
    made = parts(tmp_path)
    made.attempt.step_intent(0, NAVIGATE, side_effect=False, target_used=None)

    assert outcome_of(ending(made, published="confirmed", **facts)) == expected


@pytest.mark.parametrize("published", [None, "confirmed"])
def test_an_unresolved_side_effect_intent_in_the_confirmation_alone_is_non_resumable(
    tmp_path: Path, published: str | None
) -> None:
    made = parts(tmp_path)
    made.confirmation.step_intent(2, CLICK, side_effect=True, target_used="save")

    end = confirmed(made, published=published)

    assert outcome_of(end) == ("non_resumable", 4, [{"part": "confirmation", "seq": 2}])


def test_non_resumable_is_presented_whatever_the_exit(tmp_path: Path) -> None:
    made = parts(tmp_path)
    made.attempt.step_intent(3, CLICK, side_effect=True, target_used="save")
    made.confirmation.step_intent(2, CLICK, side_effect=True, target_used="save")

    assert outcome_of(confirmed(made, blocks=CDN)) == (
        "egress_blocked",
        6,
        [{"part": "attempt-1", "seq": 3}, {"part": "confirmation", "seq": 2}],
    )


# The attempt's phase as the document presents it, nothing uncertain.
QUIET_ATTEMPT = {
    "part": "attempt-1",
    "plain_uncertain": 0,
    "tunnel_uncertain": 0,
    "policy_events": 0,
}


def test_an_uncertain_confirmation_exits_14_and_presents_its_counts(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path)
    confirmation = Confirmation(made.confirmation, PhaseTraffic(2, 1), 0, None)

    document = present(ending(made, confirmation=confirmation))

    assert (document["outcome"], document["exit_code"]) == ("reset_unsafe", 14)
    assert document["phases"] == [
        QUIET_ATTEMPT,
        {
            "part": "confirmation",
            "plain_uncertain": 2,
            "tunnel_uncertain": 1,
            "policy_events": 0,
        },
    ]


def test_a_phase_interrupted_mid_dispatch_is_non_resumable_not_gave_up(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path)
    made.attempt.step_intent(4, NAVIGATE, side_effect=False, target_used=None)
    made.attempt.step_intent(5, CLICK, side_effect=True, target_used="save")
    attempt = Phase(made.attempt, PhaseTraffic(1, 0), 0)

    end = ending(made, attempt=attempt, stops=(stop("gave_up", MINUTES),))

    assert outcome_of(end) == ("non_resumable", 4, [{"part": "attempt-1", "seq": 5}])
    assert present(end)["reasons"] == ["the minutes budget ran out"]


RETIRED = (
    "the egress proxy stopped accepting the browser's connections after an "
    "accept error (EMFILE)"
)


def test_a_phase_interrupted_by_an_infrastructure_event_exits_13(
    tmp_path: Path,
) -> None:
    retired = InfrastructureEvent("127.0.0.1", 41234, RETIRED)
    stops = (stop("gave_up", MINUTES),)

    document = present(
        ending(parts(tmp_path), infrastructure_events=(retired,), stops=stops)
    )

    assert (document["outcome"], document["exit_code"]) == ("unreachable", 13)
    assert document["infrastructure_events"] == {
        "count": 1,
        "events": [{"host": "127.0.0.1", "port": 41234, "cause": RETIRED}],
    }


# A value a page could choose, in every channel the presented document keeps
# out or withholds: a refused host, an infrastructure event's host and cause,
# a settle window's URLs, a policy event's URL and origin, what an invariant
# saw, and the confirmation's own events and blocks.
LEAK = "http://fake-bound-value.evil.test:31337/x"
LEAKED_EVENT = InfrastructureEvent(
    "fake-bound-value.api.evil.test", 31337, "fake-bound-value didn't resolve"
)


def leaking(made: Parts) -> ExploreEnd:
    window = Window(
        requests=Records([PageRequest("GET", LEAK)], 1),
        responses=Records([Exchange("GET", LEAK, 200)], 1),
    )
    result = replayed(
        made,
        steps=(
            StepResult(0, "completed", window=window, settled="idle"),
            StepResult(1, "completed", window=window, locator_index=0, settled="idle"),
        ),
        assertions=(AssertionResult("a1", "pass"),),
        infrastructure_events=(LEAKED_EVENT,),
        policy_events=(PolicyEvent("navigation", LEAK, LEAK.removesuffix("/x")),),
        invariants=(InvariantResult("console_errors", "disabled", (LEAK,), 1),),
        egress_blocks=EgressBlocks(
            (RefusedHost("fake-bound-value.ads.evil.test", 31337),), True
        ),
    )
    return ending(
        made,
        confirmation=Confirmation(made.confirmation, QUIET, 1, result),
        blocks=EgressBlocks((RefusedHost("fake-bound-value.evil.test", 31337),), False),
        infrastructure_events=(LEAKED_EVENT,),
        checks={"a1": AssertionResult("a1", "pass")},
        published="confirmed",
    )


# How the document presents a passing replay's step 0 and assertion a1.
STEP_0: dict[str, object] = {
    "seq": 0,
    "outcome": "completed",
    "locator_index": None,
    "settled": "idle",
    "misses": [],
    "reason": None,
}
PASSED_A1: dict[str, object] = {
    "id": "a1",
    "outcome": "pass",
    "misses": [],
    "stopped_at": None,
    "reason": None,
}


def presented(made: Parts, *, withheld: bool) -> dict[str, object]:
    """The document `leaking(made)` presents, but for what withholding
    changes: its egress and infrastructure events."""
    named = {
        "error_code": "egress_blocked",
        "refused": [{"host": "fake-bound-value.evil.test", "port": 31337}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }
    counted = {
        "error_code": "egress_blocked",
        "refused_count": 1,
        "overflowed": False,
        "hosts_and_ports_withheld": True,
    }
    event = {
        "host": "fake-bound-value.api.evil.test",
        "port": 31337,
        "cause": "fake-bound-value didn't resolve",
    }
    return {
        "run_id": made.record.run_id,
        "outcome": "egress_blocked",
        "exit_code": 6,
        "withheld": withheld,
        "published": "confirmed",
        "reasons": [],
        "non_resumable": [],
        "phases": [
            QUIET_ATTEMPT,
            {
                "part": "confirmation",
                "plain_uncertain": 0,
                "tunnel_uncertain": 0,
                "policy_events": 1,
            },
        ],
        "egress": counted if withheld else named,
        "infrastructure_events": {"count": 1, "events": None if withheld else [event]},
        "confirmation": {
            "outcome": "passed",
            "error_code": None,
            "steps": [
                STEP_0,
                {
                    "seq": 1,
                    "outcome": "completed",
                    "locator_index": 0,
                    "settled": "idle",
                    "misses": [],
                    "reason": None,
                },
            ],
            "assertions": [PASSED_A1],
            "invariants": [
                {"name": "console_errors", "outcome": "disabled", "total": 1}
            ],
        },
    }


@pytest.mark.parametrize(("redactor", "withheld"), [(NO_SECRETS, False), (BOUND, True)])
def test_the_presenter_copies_only_its_allow_list(
    tmp_path: Path, redactor: Redactor, withheld: bool
) -> None:
    made = parts(tmp_path, redactor)

    document = present(leaking(made))

    assert document == presented(made, withheld=withheld)
    text = json.dumps(document)
    # Present when nothing is bound: only withholding keeps them out.
    assert ("fake-bound-value" in text) is not withheld
    assert ("evil.test" in text) is not withheld


THREE = EgressBlocks(
    (
        RefusedHost("p1.fake.evil.test", 1111),
        RefusedHost("p2.fake.evil.test", 2222),
        RefusedHost("p3.fake.evil.test", 3333),
    ),
    False,
)


@pytest.mark.parametrize(
    ("blocks", "named", "counted"),
    [
        pytest.param(
            THREE,
            {
                "error_code": "egress_blocked",
                "refused": [
                    {"host": "p1.fake.evil.test", "port": 1111},
                    {"host": "p2.fake.evil.test", "port": 2222},
                    {"host": "p3.fake.evil.test", "port": 3333},
                ],
                "overflowed": False,
                "hosts_and_ports_withheld": False,
            },
            {
                "error_code": "egress_blocked",
                "refused_count": 3,
                "overflowed": False,
                "hosts_and_ports_withheld": True,
            },
            id="three refused hosts",
        ),
        pytest.param(
            EgressBlocks((), True),
            {
                "error_code": "egress_blocked",
                "refused": [],
                "overflowed": True,
                "hosts_and_ports_withheld": False,
            },
            {
                "error_code": "egress_blocked",
                "refused_count": 0,
                "overflowed": True,
                "hosts_and_ports_withheld": True,
            },
            id="an overflow only",
        ),
    ],
)
def test_a_value_encoded_across_refused_ports_and_hosts_is_not_presented(
    tmp_path: Path, blocks: EgressBlocks, named: object, counted: object
) -> None:
    unbound = present(ending(parts(tmp_path), blocks=blocks))
    bound = present(ending(parts(tmp_path, BOUND), blocks=blocks))

    assert (bound["outcome"], bound["exit_code"]) == ("egress_blocked", 6)
    assert (unbound["egress"], bound["egress"]) == (named, counted)


ADS = EgressBlocks((RefusedHost("ads.evil.test", 80),), False)
SHOWN_EVENT = {
    "host": "api.evil.test",
    "port": 8443,
    "cause": "the name didn't resolve",
}


def test_a_final_block_and_event_decide_though_the_confirmation_saw_none(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path)
    final = {"blocks": CDN, "infrastructure_events": (EVENT,)}

    document = present(confirmed(made, replayed(made), published="confirmed", **final))

    assert (document["outcome"], document["exit_code"]) == ("egress_blocked", 6)
    assert document["egress"] == {
        "error_code": "egress_blocked",
        "refused": [{"host": "cdn.evil.test", "port": 443}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }
    assert document["infrastructure_events"] == {"count": 1, "events": [SHOWN_EVENT]}


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        ((), ("gave_up", 3, None, {"count": 0, "events": []})),
        ((EVENT,), ("unreachable", 13, None, {"count": 1, "events": [SHOWN_EVENT]})),
    ],
)
def test_the_confirmations_own_block_and_event_never_decide(
    tmp_path: Path,
    final: tuple[InfrastructureEvent, ...],
    expected: tuple[object, ...],
) -> None:
    made = parts(tmp_path)
    result = replayed(
        made,
        outcome="errored",
        egress_blocks=ADS,
        infrastructure_events=(EVENT,),
        error_code="egress_blocked",
    )

    document = present(confirmed(made, result, infrastructure_events=final))

    keys = ("outcome", "exit_code", "egress", "infrastructure_events")
    assert tuple(document[key] for key in keys) == expected


def later[T](base: type[T]) -> type[T]:
    """`base` as it may be one day: with a field the presenter doesn't list."""
    leaked = ("leaked", str, field(default="fake-bound-value"))
    return make_dataclass(f"Later{base.__name__}", [leaked], bases=(base,), frozen=True)


def test_a_field_the_presenter_does_not_list_never_reaches_its_output(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path)
    result = later(RunResult)(
        run_id=made.record.run_id,
        outcome="passed",
        steps=(later(StepResult)(0, "completed", settled="idle"),),
        assertions=(later(AssertionResult)("a1", "pass"),),
        infrastructure_events=(),
        policy_events=(),
        invariants=(later(InvariantResult)("console_errors", "held", (), 0),),
        egress_blocks=CLEAN,
        error_code=None,
    )
    checks = {"a1": later(AssertionResult)("a1", "failed")}

    document = present(confirmed(made, result, checks=checks))

    assert document["confirmation"] == {
        "outcome": "passed",
        "error_code": None,
        "steps": [STEP_0],
        "assertions": [PASSED_A1],
        "invariants": [{"name": "console_errors", "outcome": "held", "total": 0}],
    }
    assert document["reasons"] == ["a1: failed"]
    assert "fake-bound-value" not in json.dumps(document)


UNRESOLVED = AssertionResult(
    "a2", "binding_unresolved", misses=("no match", "outside region")
)
UNANSWERED = AssertionResult(
    "a2", "not_evaluated", error="the page didn't answer within 11 s"
)


@pytest.mark.parametrize(
    ("result", "reasons"),
    [
        (None, ["a2: never bound"]),
        (AssertionResult("a2", "failed"), ["a2: failed"]),
        (UNRESOLVED, ["a2: binding_unresolved (no match, outside region)"]),
        (AssertionResult("a2", "check_timed_out"), ["a2: check_timed_out"]),
        (UNANSWERED, ["a2: not_evaluated: the page didn't answer within 11 s"]),
        (
            AssertionResult("a2", "not_evaluated", stopped_at=4),
            ["a2: not_evaluated: step 4 stopped the run"],
        ),
        (AssertionResult("a2", "pass"), []),
    ],
)
def test_each_planned_check_that_never_passed_is_a_reason_in_outcome_words(
    tmp_path: Path, result: AssertionResult | None, reasons: list[str]
) -> None:
    document = present(ending(parts(tmp_path), checks={"a2": result}))

    assert document["reasons"] == reasons


@pytest.mark.parametrize("redactor", [NO_SECRETS, BOUND])
def test_a_failed_confirmation_gives_its_reasons_in_outcome_words(
    tmp_path: Path, redactor: Redactor
) -> None:
    made = parts(tmp_path, redactor)
    result = replayed(
        made,
        outcome="failed",
        steps=(
            StepResult(0, "completed", settled="idle"),
            StepResult(3, "drifted", misses=("not the part",)),
            StepResult(4, "failed"),
        ),
        assertions=(AssertionResult("a1", "failed"), AssertionResult("a2", "pass")),
        invariants=(
            InvariantResult("console_errors", "held", (), 0),
            InvariantResult(
                "js_exceptions", "violated", ("Error: one", "Error: two"), 3
            ),
            InvariantResult("http_5xx", "disabled", ("GET /api 503",), 2),
            InvariantResult("broken_images", "held", (), 0),
        ),
    )

    document = present(confirmed(made, result))

    confirmation: Any = document["confirmation"]
    assert document["reasons"] == [
        "confirmation step 3: drifted (not the part)",
        "confirmation step 4: failed",
        "confirmation a1: failed",
        "confirmation invariant js_exceptions: violated",
    ]
    # Names, outcomes and totals stay visible in a bound run; what each saw doesn't.
    assert confirmation["invariants"] == [
        {"name": "console_errors", "outcome": "held", "total": 0},
        {"name": "js_exceptions", "outcome": "violated", "total": 3},
        {"name": "http_5xx", "outcome": "disabled", "total": 2},
        {"name": "broken_images", "outcome": "held", "total": 0},
    ]


@pytest.mark.parametrize(
    ("redactor", "reasons"),
    [
        (BOUND, ["gave up after [SECRET:TEST_PASSWORD]"]),
        (NO_SECRETS, ["gave up after fake-bound-value"]),
    ],
)
def test_a_stop_reason_is_scanned_again_with_the_runs_redactor(
    tmp_path: Path, redactor: Redactor, reasons: list[str]
) -> None:
    # Scanned by a redactor that binds nothing: the mark doesn't say whose.
    stops = (stop("gave_up", "gave up after fake-bound-value"),)

    document = present(ending(parts(tmp_path, redactor), stops=stops))

    assert document["reasons"] == reasons


@pytest.mark.parametrize(
    ("redactor", "step", "assertion"),
    [
        (BOUND, "Error: [SECRET:TEST_PASSWORD]", "saw [SECRET:TEST_PASSWORD]"),
        (NO_SECRETS, "Error: fake-bound-value", "saw fake-bound-value"),
    ],
)
def test_a_copied_confirmation_error_is_scanned_again(
    tmp_path: Path, redactor: Redactor, step: str, assertion: str
) -> None:
    made = parts(tmp_path, redactor)
    result = replayed(
        made,
        outcome="errored",
        steps=(StepResult(3, "failed", error="Error: fake-bound-value"),),
        assertions=(
            AssertionResult("a1", "not_evaluated", error="saw fake-bound-value"),
        ),
    )

    confirmation: Any = present(confirmed(made, result))["confirmation"]

    assert confirmation["steps"][0]["reason"] == step
    assert confirmation["assertions"][0]["reason"] == assertion


def spanning(value: str) -> Redactor:
    """A redactor binding `value`, which a reason's words and a copied error
    can each hold only a piece of."""
    return Redactor([fake_secret("TEST_PASSWORD", value, APP)])


@pytest.mark.parametrize(
    ("redactor", "reason"),
    [
        (
            spanning("failed: fake-value-x"),
            "confirmation step 3: [SECRET:TEST_PASSWORD]",
        ),
        (NO_SECRETS, "confirmation step 3: failed: fake-value-x"),
    ],
)
def test_a_bound_value_spanning_a_step_reasons_join_is_scanned(
    tmp_path: Path, redactor: Redactor, reason: str
) -> None:
    made = parts(tmp_path, redactor)
    failed = StepResult(3, "failed", error="fake-value-x")

    document = present(
        confirmed(made, replayed(made, outcome="errored", steps=(failed,)))
    )

    confirmation: Any = document["confirmation"]
    assert document["reasons"] == [reason]
    assert confirmation["steps"][0]["reason"] == "fake-value-x"


UNEVALUATED = AssertionResult("a2", "not_evaluated", error="fake-x")


@pytest.mark.parametrize(
    ("redactor", "result", "reason"),
    [
        (spanning("not_evaluated: fake-x"), UNEVALUATED, "a2: [SECRET:TEST_PASSWORD]"),
        (NO_SECRETS, UNEVALUATED, "a2: not_evaluated: fake-x"),
        (spanning("a2: never"), None, "[SECRET:TEST_PASSWORD] bound"),
    ],
)
def test_a_bound_value_spanning_a_checks_reason_join_is_scanned(
    tmp_path: Path, redactor: Redactor, result: AssertionResult | None, reason: str
) -> None:
    document = present(ending(parts(tmp_path, redactor), checks={"a2": result}))

    assert document["reasons"] == [reason]


REFUSED = "^{} isn't a part made from this run's record, scanning with its redactor$"


def before_scanning(tmp_path: Path) -> Parts:
    unscanned = RunRecord.create(tmp_path)
    early = unscanned.part("attempt-1")
    return Parts(unscanned.redacting(BOUND), early, unscanned.part("confirmation"))


def another_records(tmp_path: Path) -> Parts:
    return replace(parts(tmp_path, BOUND), attempt=parts(tmp_path, BOUND).attempt)


def the_confirmations(tmp_path: Path) -> Parts:
    made = parts(tmp_path, BOUND)
    return replace(made, attempt=made.confirmation)


def another_run_id(tmp_path: Path) -> Parts:
    made = parts(tmp_path, BOUND)
    return replace(made, attempt=replace(made.attempt, run_id="another-run"))


@pytest.mark.parametrize(
    "mismatched", [before_scanning, another_records, the_confirmations, another_run_id]
)
def test_a_phase_of_another_record_or_scan_is_refused(
    tmp_path: Path, mismatched: Callable[[Path], Parts]
) -> None:
    with pytest.raises(ValueError, match=REFUSED.format("attempt-1")):
        ending(mismatched(tmp_path))


def test_a_confirmation_result_of_another_run_is_refused(tmp_path: Path) -> None:
    made = parts(tmp_path, BOUND)
    result = replayed(parts(tmp_path, BOUND))

    with pytest.raises(
        ValueError, match=r"^the confirmation's result is another run's$"
    ):
        confirmed(made, result)


@pytest.mark.parametrize("part", ["attempt-1", "confirmation"])
@pytest.mark.parametrize("hidden", [True, False])
def test_a_record_made_directly_in_a_parts_place_is_refused(
    tmp_path: Path, part: str, hidden: bool
) -> None:
    made = parts(tmp_path, BOUND)
    real = made.attempt if part == "attempt-1" else made.confirmation
    if hidden:
        real.step_intent(3, CLICK, side_effect=True, target_used="save")
    direct = RunRecord(real.run_id, real.path, real.redactor)
    place = "attempt" if part == "attempt-1" else "confirmation"

    with pytest.raises(ValueError, match=REFUSED.format(part)):
        confirmed(replace(made, **{place: direct}), published="confirmed")
    if hidden:
        assert real.unresolved() == (PendingIntent(seq=3, side_effect=True),)
        assert direct.unresolved() == ()


def test_parts_beside_a_run_record_made_directly_are_refused(tmp_path: Path) -> None:
    made = parts(tmp_path, BOUND)
    made.attempt.step_intent(3, CLICK, side_effect=True, target_used="save")
    direct = RunRecord(made.record.run_id, made.record.path, made.record.redactor)

    with pytest.raises(ValueError, match=REFUSED.format("attempt-1")):
        confirmed(replace(made, record=direct), published="confirmed")


def test_replays_copy_of_the_confirmation_part_shares_its_intents(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path, BOUND)
    # Replay scans with a redactor of its own, made from the same part.
    replays = made.confirmation.redacting(Redactor([]))
    replays.step_intent(2, CLICK, side_effect=True, target_used="save")

    end = confirmed(made, published="confirmed")

    assert outcome_of(end) == ("non_resumable", 4, [{"part": "confirmation", "seq": 2}])


def test_a_part_copied_from_the_run_record_by_hand_shares_its_intents(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path, BOUND)
    by_hand = replace(made.record, path=made.record.path / "attempt-1")
    by_hand.step_intent(4, CLICK, side_effect=True, target_used="save")

    end = ending(replace(made, attempt=by_hand), published="confirmed")

    assert outcome_of(end) == ("non_resumable", 4, [{"part": "attempt-1", "seq": 4}])


def test_a_scanning_copy_of_a_part_shares_the_intents_written_through_it(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path, BOUND)
    made.attempt.step_intent(6, CLICK, side_effect=True, target_used="save")
    copy = made.attempt.redacting(made.record.redactor)

    end = ending(replace(made, attempt=copy), published="confirmed")

    assert outcome_of(end) == ("non_resumable", 4, [{"part": "attempt-1", "seq": 6}])


@pytest.mark.parametrize(
    ("outcome", "text", "code"),
    [
        ("no_sandbox", "Chromium's sandbox didn't start", 10),
        ("reset_unsafe", "the reset hook failed before the attempt", 14),
    ],
)
def test_a_stop_before_any_session_presents_with_the_records_scan(
    tmp_path: Path, outcome: str, text: str, code: int
) -> None:
    made = parts(tmp_path, BOUND)

    document = present(ending(made, stops=(stop(outcome, text),), checks={"a1": None}))

    assert document == {
        "run_id": made.record.run_id,
        "outcome": outcome,
        "exit_code": code,
        "withheld": True,
        "published": None,
        "reasons": [text, "a1: never bound"],
        "non_resumable": [],
        "phases": [QUIET_ATTEMPT],
        "egress": None,
        "infrastructure_events": {"count": 0, "events": None},
        "confirmation": None,
    }


def test_the_presented_documents_pass_the_records_exact_text_check(
    tmp_path: Path,
) -> None:
    made = parts(tmp_path, BOUND)
    document = present(leaking(made))
    record = made.record

    for name, written in (
        ("explore.json", document),
        ("egress.json", document["egress"]),
    ):
        path = record.write(name, written, check=require_clean(BOUND, name))
        assert json.loads(path.read_text()) == written
    # The same run's document, had it been presented unbound, is refused.
    with pytest.raises(RefusedWriteError):
        record.write(
            "unbound.json",
            present(leaking(parts(tmp_path))),
            check=require_clean(BOUND, "unbound.json"),
        )


# The fixture app's malformed probe's status line, as a bound value (#48,
# #160): what h11 raises would quote it.
MALFORMED = "fake-sensitive"
PROBE: dict[str, Any] = {
    "id": "a1",
    "expect_index": 0,
    "check": "probe_equals",
    "probe": "count",
    "json_path": "$.count",
    "value": 2,
}


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def after(ran: Run, redactor: Redactor) -> ExploreEnd:
    """An explore run that ended with `ran`'s infrastructure events."""
    record = ran.record.redacting(redactor)
    made = Parts(record, record.part("attempt-1"), record.part("confirmation"))
    return ending(made, infrastructure_events=ran.result.infrastructure_events)


def test_an_h11_malformed_status_cause_is_presented_as_its_class_only(
    app: App, tmp_path: Path
) -> None:
    probes = {"count": "GET /probe/malformed"}
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/form", probes=probes)
    ran = run(app, tmp_path, compiled([], assertions=[PROBE]), spec=spec)

    document = present(after(ran, NO_SECRETS))

    assert document["infrastructure_events"] == {
        "count": 1,
        "events": [
            {
                "host": "127.0.0.1",
                "port": int(app.origin.rsplit(":", 1)[1]),
                "cause": "the exchange broke off: h11.RemoteProtocolError",
            }
        ],
    }


def test_a_bound_malformed_status_cause_is_withheld(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", MALFORMED)
    account = probing_account("/probe/malformed")
    spec = secret_spec(tmp_path, start_url="/page/form", account=account)
    ran = run(app, tmp_path, compiled([], assertions=[PROBE]), spec=spec)
    redactor = Redactor([fake_secret("TEST_PASSWORD", MALFORMED, app.origin)])

    document = present(after(ran, redactor))

    assert document["infrastructure_events"] == {"count": 1, "events": None}
    assert MALFORMED not in json.dumps(document)
