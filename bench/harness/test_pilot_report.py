import json
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from aqa_core.compiled import CompiledScript
from aqa_core.spec import Invariants
from aqa_runner.egress_proxy import EgressBlocks
from aqa_runner.executor import AssertionResult, RunResult, StepResult
from aqa_runner.invariants import INVARIANTS, InvariantResult
from manifest import DRIFT, VIOLATED, Expected
from pilot_replay import AttemptHealth, FailedAttempt, ObservedAttempt
from pilot_report import (
    Pair,
    Report,
    admits_patch,
    classify,
    document,
    judge_case,
    judge_clean,
    receipt,
    write_once,
)
from pilot_results import (
    AssertionObservation,
    InvariantObservation,
    Observation,
    StepObservation,
    observe,
)
from test_pilot_replay import COMPILED, SEND

type Attempt = ObservedAttempt | FailedAttempt

SETTINGS = Invariants()
HEALTH = AttemptHealth(True, False, False)
PRIVATE = Path("/private/record")
NONE, ONE, JS_NAME = frozenset[Any](), frozenset({1}), frozenset({"js_exceptions"})
HELD = tuple(InvariantObservation(name, "held", 0) for name in INVARIANTS)
JS = (HELD[0], InvariantObservation("js_exceptions", "violated", 1), *HELD[2:])
JS_VIOLATED: dict[str, Any] = {"invariants": JS, "violated_invariants": JS_NAME}
NAV = StepObservation(0, "completed", "idle", None, (), False)
CLICK = StepObservation(1, "completed", "idle", 0, (), False)
A0 = AssertionObservation("a0", "pass", (), None, False)
A1 = A0._replace(id="a1")
UNRESOLVED = A1._replace(outcome="binding_unresolved", misses=("no match",))
NOT_RUN = tuple(a._replace(outcome="not_evaluated", stopped_at=1) for a in (A0, A1))
STEPS, CHECKS = (NAV, CLICK), (A0, A1)
PASSED = Observation("pilot", "passed", None, STEPS, CHECKS, HELD, NONE, NONE, True)
LOST = StepObservation(1, "drifted", None, None, ("no match",), False)
DRIFTED = replace(
    PASSED, outcome="failed", steps=(NAV, LOST), assertions=NOT_RUN, eligible=False
)
UNBOUND = replace(PASSED, outcome="failed", assertions=(A0, UNRESOLVED), eligible=False)
FAILED = (A0, A1._replace(outcome="failed"))
FAILING = replace(PASSED, outcome="failed", assertions=FAILED, failed_expectations=ONE)
RESET_500 = FailedAttempt("r9", PRIVATE, "reset_rejected", "completed", "closed")
BUG = Expected("pilot", VIOLATED, (1,), ("js_exceptions",))
BENIGN = Expected("pilot", DRIFT, (), ())
BASE = json.loads(COMPILED)
TEXT = BASE["assertions"][0]
IN_SEND = TEXT | {"id": "a1", "check": "text_in_target", "target": "send"}
SCRIPT = CompiledScript.model_validate_json(
    json.dumps(BASE | {"steps": [SEND], "assertions": [TEXT, IN_SEND]})
)


def seen(
    base: Observation = PASSED, health: AttemptHealth = HEALTH, **changes: Any
) -> ObservedAttempt:
    return ObservedAttempt("r1", PRIVATE, replace(base, **changes), health)


def report(*pairs: Pair, **changes: Any) -> Report:
    hashes = {"manifest": "sha256:m", "script:pilot": "sha256:s"}
    base = Report(
        "conduit", "c" * 40, "t" * 40, True, 3, ("pilot",), (), hashes, (), None, True
    )
    return replace(base, pairs=pairs, **changes)


def encoded(value: Report) -> Any:
    return json.loads(json.dumps(document(value, {"pilot": SCRIPT})))


def judged(original: Attempt, row: Expected | None = None) -> Pair:
    return judge_case("c", "pilot", row, original, settings=SETTINGS)


class PilotReportTests(unittest.TestCase):
    def test_binding_only_admits_exactly_the_two_shapes(self) -> None:
        timed_out = (A0, A1._replace(outcome="check_timed_out"))
        failed_step = (NAV, CLICK._replace(outcome="failed", error=True))
        unsettled = (NAV._replace(settled="timeout"), DRIFTED.steps[1])
        miscounted = (HELD[0]._replace(total=2), *HELD[1:])
        beside = (A0._replace(outcome="failed"), UNRESOLVED)
        early = tuple(a._replace(stopped_at=0) for a in DRIFTED.assertions)
        cases: list[tuple[Attempt, str]] = [
            (seen(), "eligible"),
            (seen(UNBOUND), "binding_only"),
            (seen(DRIFTED), "binding_only"),
            (seen(UNBOUND, assertions=timed_out), "fatal"),
            (seen(DRIFTED, outcome="errored", steps=failed_step), "fatal"),
            (seen(DRIFTED, steps=unsettled), "fatal"),
            (seen(DRIFTED, AttemptHealth(True, True, False)), "fatal"),
            (seen(DRIFTED, AttemptHealth(True, False, True)), "fatal"),
            (seen(DRIFTED, invariants=miscounted), "fatal"),
            (RESET_500, "fatal"),
            (seen(DRIFTED, **JS_VIOLATED), "ineligible"),
            (seen(UNBOUND, assertions=beside), "ineligible"),
            (seen(DRIFTED, assertions=early), "ineligible"),
        ]
        kinds = [classify(attempt, SETTINGS) for attempt, _ in cases]
        self.assertEqual(kinds, [kind for _, kind in cases])
        off = seen(
            DRIFTED, invariants=(HELD[0]._replace(outcome="disabled"), *HELD[1:])
        )
        disabled = Invariants(disable=("console_errors",))
        both = [classify(off, disabled), classify(off, SETTINGS)]
        self.assertEqual(both, ["binding_only", "fatal"])

    def test_a_step_or_check_error_is_fatal_in_any_shape(self) -> None:
        errors = [
            seen(UNBOUND, steps=(NAV, CLICK._replace(error=True))),
            seen(DRIFTED, steps=(NAV, LOST._replace(error=True))),
            seen(UNBOUND, assertions=(A0._replace(error=True), UNRESOLVED)),
            seen(UNBOUND, assertions=(A0, UNRESOLVED._replace(error=True))),
            seen(DRIFTED, assertions=(NOT_RUN[0]._replace(error=True), NOT_RUN[1])),
            seen(steps=(NAV._replace(error=True), CLICK)),
            seen(assertions=(A0, A1._replace(error=True))),
        ]
        attempts = (*errors, seen(UNBOUND), seen(DRIFTED), seen())
        kinds = [classify(a, SETTINGS) for a in attempts]
        admitted = [admits_patch(a, BENIGN, SETTINGS) for a in attempts]
        controls = ["binding_only", "binding_only", "eligible"]
        self.assertEqual(kinds, ["fatal"] * 7 + controls)
        self.assertEqual(admitted, [False] * 7 + [True] * 3)

    def test_pair_policy_follows_the_acceptance_table(self) -> None:
        drifted, fatal = seen(DRIFTED), seen(DRIFTED, AttemptHealth(True, True, False))
        rows: list[tuple[Expected | None, Attempt, Attempt | None, str]] = [
            (BUG, seen(FAILING, **JS_VIOLATED), None, "matched"),
            (BUG, seen(), None, "mismatch"),
            (BUG, RESET_500, None, "fatal"),
            (BENIGN, seen(), seen(), "accepted_pending_C"),
            (BENIGN, drifted, seen(), "accepted_pending_C"),
            (BENIGN, drifted, None, "patch_missing"),
            (BENIGN, drifted, seen(FAILING), "mismatch"),
            (BENIGN, drifted, RESET_500, "fatal"),
            (BENIGN, fatal, None, "fatal"),
            (BENIGN, seen(FAILING), None, "mismatch"),
            (None, seen(FAILING), None, "unscored"),
            (None, drifted, None, "unscored_binding_blocked"),
            (None, RESET_500, None, "fatal"),
        ]
        statuses = [
            judge_case("c", "pilot", row, original, patched, settings=SETTINGS).status
            for row, original, patched, _ in rows
        ]
        self.assertEqual(statuses, [status for *_, status in rows])
        candidates = (seen(), drifted, fatal, seen(FAILING), RESET_500)
        admitted = [admits_patch(a, BENIGN, SETTINGS) for a in candidates]
        self.assertEqual(admitted, [True, True, False, False, False])
        pair = judge_case("c", "pilot", BENIGN, drifted, seen(), settings=SETTINGS)
        self.assertEqual([e.role for e in pair.attempts], ["diagnostic", "acceptance"])

    def test_three_clean_attempts_must_pass_and_match(self) -> None:
        fallback = seen(steps=(NAV, CLICK._replace(locator_index=1)))
        runs: list[tuple[list[Attempt], str]] = [
            ([seen()] * 3, "passed"),
            ([seen(), fallback, seen()], "mismatch"),
            ([seen(), seen(FAILING), seen()], "mismatch"),
            ([seen(), RESET_500], "fatal"),
        ]
        statuses = [judge_clean("pilot", a, SETTINGS).status for a, _ in runs]
        self.assertEqual(statuses, [status for _, status in runs])

    def test_scored_pairs_report_exact_missing_and_extra_failures(self) -> None:
        http = InvariantObservation("http_5xx", "violated", 1)
        extra = {"js_exceptions", "http_5xx"}
        attempts = (
            seen(),
            seen(FAILING, failed_expectations={0, 1}, **JS_VIOLATED),
            seen(FAILING, invariants=(*JS[:2], http, JS[3]), violated_invariants=extra),
        )
        found = []
        for attempt in attempts:
            pair = encoded(report(judged(attempt, BUG)))["pairs"][0]
            found.append((pair["status"], pair["missing"], pair["unexpected"]))
        none: dict[str, list[object]] = {"expect": [], "invariants": []}
        self.assertEqual(
            found,
            [
                ("mismatch", {"expect": [1], "invariants": ["js_exceptions"]}, none),
                ("mismatch", none, {"expect": [0], "invariants": []}),
                ("mismatch", none, {"expect": [], "invariants": ["http_5xx"]}),
            ],
        )

    def test_report_pins_source_and_marks_omitted_pairs_unscored(self) -> None:
        omitted = replace(judged(seen(DRIFTED)), case="conduit-bug-003")
        doc = encoded(report(judge_clean("pilot", [seen()], SETTINGS), omitted))
        pair = doc.pop("pairs")[1]
        attempt = pair.pop("attempts")[0]
        self.assertEqual(
            doc,
            {
                "app": "conduit",
                "source": {"commit": "c" * 40, "tree": "t" * 40},
                "source_unchanged": True,
                "repeat": 3,
                "diagnostic": False,
                "selected": ["pilot"],
                "omitted": [],
                "hashes": {"manifest": "sha256:m", "script:pilot": "sha256:s"},
                "assertion_provenance": "pending",
                "halt": None,
                "reservation_release": "allowed",
                "switched_back_clean": True,
                "exit": 0,
            },
        )
        unscored = {"case": "conduit-bug-003", "spec": "pilot", "expected": None}
        status = {
            "status": "unscored_binding_blocked",
            "missing": None,
            "unexpected": None,
        }
        self.assertEqual(pair, unscored | status)
        steps = [
            (s["seq"], s["target"], s["outcome"], s["misses"]) for s in attempt["steps"]
        ]
        self.assertEqual(
            steps, [(0, None, "completed", []), (1, "send", "drifted", ["no match"])]
        )
        checks = [
            (a["id"], a["target"], a["stopped_at"]) for a in attempt["assertions"]
        ]
        self.assertEqual(checks, [("a0", None, 1), ("a1", "send", 1)])
        self.assertEqual((attempt["kind"], attempt["run_id"]), ("binding_only", "r1"))
        self.assertNotIn("/private", json.dumps(attempt))
        self.assertTrue(encoded(report(repeat=1))["diagnostic"])

    def test_report_lists_every_unselected_spec_as_omitted(self) -> None:
        full = encoded(report(selected=("pilot", "excluded")))
        partial = encoded(report(judged(seen(FAILING)), omitted=("zeta", "excluded")))
        sets = [(doc["selected"], doc["omitted"]) for doc in (full, partial)]
        self.assertEqual(
            sets, [(["pilot", "excluded"], []), (["pilot"], ["excluded", "zeta"])]
        )
        pairs = [(p["spec"], p["expected"], p["status"]) for p in partial["pairs"]]
        self.assertEqual(pairs, [("pilot", None, "unscored")])
        kept = ("source", "hashes", "assertion_provenance", "exit")
        self.assertEqual([partial[k] for k in kept], [full[k] for k in kept])
        values = {f.name: getattr(report(), f.name) for f in fields(Report)}
        del values["omitted"]
        with self.assertRaisesRegex(TypeError, "omitted"):
            Report(**values)

    def test_an_observed_error_makes_a_pair_without_a_row_fatal(self) -> None:
        nav = StepResult(0, "completed", settled="idle")
        steps = (nav, replace(nav, seq=1, locator_index=0))
        checks = (AssertionResult("a0", "pass"), AssertionResult("a1", "failed"))
        held = tuple(InvariantResult(name, "held", (), 0) for name in INVARIANTS)
        egress = EgressBlocks((), False)
        failed = RunResult("r1", "failed", steps, checks, (), (), held, egress, None)
        raised = AssertionResult("a1", "not_evaluated", error="fake page error")
        found = []
        for result in (failed, replace(failed, assertions=(checks[0], raised))):
            observed = observe(SCRIPT, result, invariants=SETTINGS)
            doc = encoded(report(judged(seen(observed))))
            found.append((observed.eligible, doc["pairs"][0]["status"], doc["exit"]))
        self.assertEqual(found, [(True, "unscored", 0), (False, "fatal", 3)])
        self.assertNotIn("fake page error", json.dumps(doc))

    def test_an_error_in_any_leg_makes_its_pair_fatal(self) -> None:
        broken = (NAV, CLICK._replace(error=True))
        original = seen(DRIFTED, steps=(NAV, LOST._replace(error=True)))
        patched = seen(steps=broken)
        pairs = (
            judge_clean("pilot", [patched] * 3, SETTINGS),
            judged(seen(FAILING, steps=broken, **JS_VIOLATED), BUG),
            judge_case("c", "pilot", BENIGN, original, settings=SETTINGS),
            judge_case("c", "pilot", BENIGN, seen(DRIFTED), patched, settings=SETTINGS),
        )
        self.assertEqual([p.status for p in pairs], ["fatal"] * 4)
        self.assertFalse(admits_patch(original, BENIGN, SETTINGS))
        doc = encoded(report(*pairs))
        scored = doc["pairs"][1]
        none: dict[str, list[object]] = {"expect": [], "invariants": []}
        self.assertEqual((scored["missing"], scored["unexpected"]), (none, none))
        steps = [(s["seq"], s["error"]) for s in scored["attempts"][0]["steps"]]
        self.assertEqual((steps, doc["exit"]), ([(0, False), (1, True)], 3))

    def test_exit_codes_and_release_follow_the_table(self) -> None:
        secret = replace(RESET_500, failure="secret_unusable")
        held = replace(RESET_500, failure="operation_timeout", resources="unknown")
        cases: list[tuple[Report, int, str]] = [
            (report(judged(seen())), 0, "allowed"),
            (report(judged(seen(), BUG)), 1, "allowed"),
            (report(judged(seen(DRIFTED), BENIGN)), 1, "allowed"),
            (report(judged(RESET_500)), 3, "allowed"),
            (report(judged(secret)), 12, "allowed"),
            (report(judged(held), switched_back=False), 3, "forbidden"),
            (report(judged(seen()), unchanged=False), 3, "allowed"),
            (report(halt="unexpected", switched_back=False), 3, "forbidden"),
            (report(judged(seen(), BUG), halt="interrupted"), 130, "forbidden"),
        ]
        docs = [encoded(value) for value, *_ in cases]
        found = [(doc["exit"], doc["reservation_release"]) for doc in docs]
        self.assertEqual(found, [(code, release) for _, code, release in cases])

    def test_receipts_and_reports_are_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            pair = judged(RESET_500)
            write_once(path, receipt(pair, pair.attempts[0], SCRIPT))
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_once(path, {"other": True})
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            json.loads(before),
            {
                "case": "c",
                "spec": "pilot",
                "role": "unscored",
                "run_id": "r9",
                "kind": "fatal",
                "failure": "reset_rejected",
                "cleanup": "completed",
                "resources": "closed",
            },
        )
