import json
import unittest
from dataclasses import astuple, replace
from inspect import Parameter, signature
from pathlib import Path
from typing import cast

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import parse_compiled
from aqa_core.spec import InvariantName, Invariants
from aqa_runner.document_origins import PolicyEvent
from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks, RefusedHost
from aqa_runner.executor import AssertionResult, RunResult, StepResult
from aqa_runner.invariants import InvariantResult
from aqa_runner.settling import PageRequest, Window
from manifest import Expected
from pilot_results import Observation, compare, observe

NAMES = ("console_errors", "js_exceptions", "http_5xx", "broken_images")


def script() -> CompiledScript:
    text = (Path(__file__).resolve().parents[2] / "DATA_MODEL.md").read_text()
    data = json.loads(text.split("\n## 7. ")[1].split("```json\n")[1].split("\n```")[0])
    data.update(
        spec_id="pilot",
        probe_baselines={},
        steps=[{"seq": n, "action": "reload", "side_effect": False} for n in (1, 3)],
    )
    data["assertions"] = [
        {"id": f"a{n}", "expect_index": index, "check": "text_visible", "text": "Ready"}
        for n, index in enumerate((0, 1, 1, 2))
    ]
    data["coverage"]["requires"] = []
    data["coverage"]["expectations"] = [
        {"expect_index": n, "subject": "page", "claim": "Ready", "assertions": ids}
        for n, ids in enumerate((["a0"], ["a1", "a2"], ["a3"]))
    ]
    return parse_compiled(json.dumps(data), ProjectConfig(), source=Path("pilot.json"))


def clean() -> RunResult:
    return RunResult(
        "attempt",
        "passed",
        tuple(
            StepResult(
                n, "completed", settled="idle", locator_index=0 if n == 1 else None
            )
            for n in (0, 1, 3)
        ),
        tuple(AssertionResult(f"a{n}", "pass") for n in range(4)),
        (),
        (),
        (
            InvariantResult("console_errors", "held", (), 0),
            InvariantResult("js_exceptions", "held", (), 0),
            InvariantResult("http_5xx", "held", (), 0),
            InvariantResult("broken_images", "held", (), 0),
        ),
        EgressBlocks((), False),
        None,
    )


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = script()
        self.clean = clean()

    def observe(
        self, result: RunResult, settings: Invariants | None = None
    ) -> Observation:
        return observe(
            self.script,
            result,
            invariants=settings if settings is not None else Invariants(),
        )

    def test_clean_is_immutable_detached_and_contains_only_primitives(self) -> None:
        observations = []
        for attempt in range(3):
            window = Window(changed_at=float(attempt))
            window.requests.add(PageRequest("GET", "https://fake-raw-request.test"))
            result = replace(
                self.clean,
                run_id=f"fake-run-{attempt}",
                steps=(
                    replace(self.clean.steps[0], window=window),
                    *self.clean.steps[1:],
                ),
                policy_events=(PolicyEvent("popup", "fake-policy-url", "fake-origin"),),
            )
            observation = self.observe(result)
            window.changed_at = 999
            window.requests.add(PageRequest("POST", "fake-later-request"))
            self.assertEqual(observation, self.observe(self.clean))
            self.assertTrue(observation.eligible)
            self.assertEqual(tuple(i.name for i in observation.invariants), NAMES)
            self.assertEqual(
                tuple(a.id for a in observation.assertions), ("a0", "a1", "a2", "a3")
            )
            self.assertEqual(tuple(s.seq for s in observation.steps), (0, 1, 3))
            self.assertEqual(observation.steps[1].locator_index, 0)
            self.assertEqual(
                (observation.failed_expectations, observation.violated_invariants),
                (frozenset(), frozenset()),
            )
            pending: list[object] = [astuple(observation)]
            while pending:
                value = pending.pop()
                if isinstance(value, (tuple, frozenset)):
                    pending.extend(value)
                else:
                    self.assertIn(type(value), (str, int, bool, type(None)))
                    self.assertNotIn("fake-", str(value))
            observations.append(observation)
        self.assertEqual(observations, [observations[0]] * 3)
        self.assertEqual(len({*observations}), 1)

    def test_projection_retains_each_semantic_field(self) -> None:
        step, assertion, invariant = (
            self.clean.steps[1],
            self.clean.assertions[1],
            self.clean.invariants[1],
        )
        variants = [
            *(
                replace(
                    self.clean, steps=(self.clean.steps[0], value, self.clean.steps[2])
                )
                for value in (
                    replace(step, outcome="drifted"),
                    replace(step, settled="timeout"),
                    replace(step, locator_index=1),
                    replace(step, misses=("no match",)),
                    replace(step, error="fake-step"),
                )
            ),
            *(
                replace(
                    self.clean,
                    assertions=(
                        self.clean.assertions[0],
                        value,
                        *self.clean.assertions[2:],
                    ),
                )
                for value in (
                    replace(assertion, outcome="failed"),
                    replace(assertion, misses=("no scope",)),
                    replace(assertion, stopped_at=1),
                    replace(assertion, error="fake-assertion"),
                )
            ),
            *(
                replace(
                    self.clean,
                    invariants=(
                        self.clean.invariants[0],
                        value,
                        *self.clean.invariants[2:],
                    ),
                )
                for value in (
                    replace(invariant, outcome="violated"),
                    replace(invariant, total=2),
                )
            ),
            replace(self.clean, outcome="failed"),
            replace(self.clean, error_code="egress_blocked"),
        ]
        for result in variants:
            with self.subTest(result=result):
                self.assertNotEqual(self.observe(result), self.observe(self.clean))
        raw = replace(
            self.clean,
            assertions=(
                replace(assertion, id="a0", error="fake-one"),
                *self.clean.assertions[1:],
            ),
        )
        changed = replace(
            raw,
            assertions=(
                replace(raw.assertions[0], error="fake-two"),
                *raw.assertions[1:],
            ),
            invariants=(
                replace(invariant, name="console_errors", seen=("fake-seen",)),
                *raw.invariants[1:],
            ),
        )
        self.assertEqual(self.observe(raw), self.observe(changed))
        self.assertNotIn("fake-", repr(self.observe(changed)))

    def test_keyed_rows_canonicalize_without_hiding_bad_multiplicity(self) -> None:
        self.assertEqual(
            self.observe(self.clean),
            self.observe(
                replace(
                    self.clean,
                    assertions=self.clean.assertions[::-1],
                    invariants=self.clean.invariants[::-1],
                )
            ),
        )
        malformed_results = (
            *(
                replace(self.clean, assertions=rows)
                for rows in malformed_rows(
                    self.clean.assertions,
                    replace(self.clean.assertions[0], id="unknown"),
                )
            ),
            *(
                replace(self.clean, invariants=rows)
                for rows in malformed_rows(
                    self.clean.invariants,
                    replace(
                        self.clean.invariants[0], name=cast(InvariantName, "unknown")
                    ),
                )
            ),
        )
        for result in malformed_results:
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.observe(result)
        with self.assertRaises(ValueError):
            self.observe(
                replace(
                    self.clean,
                    invariants=(
                        replace(self.clean.invariants[0], total=-1),
                        *self.clean.invariants[1:],
                    ),
                )
            )

    def test_navigation_and_sparse_compiled_step_sequence(self) -> None:
        for seqs, eligible in (((), False), ((0, 1), False), ((0, 1, 3), True)):
            self.assertEqual(
                self.observe(
                    replace(
                        self.clean,
                        steps=tuple(
                            StepResult(n, "completed", settled="idle") for n in seqs
                        ),
                    )
                ).eligible,
                eligible,
            )
        for seqs in ((1, 3), (0, 1, 1), (0, 1, 9), (0, 3), (0, 3, 1)):
            with self.subTest(seqs=seqs), self.assertRaises(ValueError):
                self.observe(
                    replace(
                        self.clean,
                        steps=tuple(
                            StepResult(n, "completed", settled="idle") for n in seqs
                        ),
                    )
                )

    def test_errors_and_inconclusive_results_cannot_score(self) -> None:
        for result in ineligible_results():
            with self.subTest(result=result):
                observed = self.observe(result)
                self.assertFalse(observed.eligible)
                self.assertEqual(observed.failed_expectations, frozenset({1}))
        timeout = replace(
            self.clean,
            steps=(
                replace(self.clean.steps[0], settled="timeout"),
                *self.clean.steps[1:],
            ),
        )
        observations = [self.observe(replace(timeout, run_id=str(n))) for n in range(3)]
        self.assertEqual(observations, [observations[0]] * 3)
        self.assertEqual(observations[0].steps[0].settled, "timeout")
        self.assertFalse(observations[0].eligible)

    def test_disabled_invariants_follow_the_actual_spec(self) -> None:
        disabled = replace(
            self.clean,
            invariants=(
                *self.clean.invariants[:2],
                InvariantResult("http_5xx", "disabled", ("fake-disabled",), 3),
                self.clean.invariants[3],
            ),
        )
        settings = Invariants(disable=("http_5xx",))
        observation = self.observe(disabled, settings)
        self.assertTrue(observation.eligible)
        self.assertEqual(observation.violated_invariants, frozenset())
        self.assertFalse(self.observe(disabled).eligible)
        self.assertFalse(self.observe(self.clean, settings).eligible)
        for invariant in (
            InvariantResult("console_errors", "held", (), 3),
            InvariantResult("console_errors", "violated", (), 0),
        ):
            with self.subTest(outcome=invariant.outcome):
                inconsistent = replace(
                    self.clean,
                    invariants=(
                        invariant,
                        *self.clean.invariants[1:],
                    ),
                )
                self.assertFalse(self.observe(inconsistent).eligible)
        all_disabled = replace(
            self.clean,
            invariants=tuple(
                replace(i, outcome="disabled", total=3) for i in self.clean.invariants
            ),
        )
        self.assertTrue(self.observe(all_disabled, Invariants(inherit=False)).eligible)
        self.assertFalse(self.observe(disabled, Invariants(inherit=False)).eligible)


def failed() -> RunResult:
    result = clean()
    return replace(
        result,
        outcome="failed",
        assertions=(
            result.assertions[0],
            AssertionResult("a1", "failed"),
            AssertionResult("a2", "failed"),
            result.assertions[3],
        ),
    )


def ineligible_results() -> tuple[RunResult, ...]:
    result = failed()
    return (
        replace(result, outcome="errored"),
        replace(result, error_code="egress_blocked"),
        replace(
            result,
            infrastructure_events=(
                InfrastructureEvent("fake-host", 443, "fake-cause"),
            ),
        ),
        replace(
            result, egress_blocks=EgressBlocks((RefusedHost("fake-host", 443),), False)
        ),
        replace(result, egress_blocks=EgressBlocks((), True)),
        *(
            replace(result, steps=(step, *result.steps[1:]))
            for step in (
                replace(result.steps[0], outcome="failed"),
                replace(result.steps[0], outcome="drifted"),
                replace(result.steps[0], settled=None),
                replace(result.steps[0], settled="timeout"),
                replace(result.steps[0], error="fake-step-error"),
            )
        ),
        *(
            replace(result, assertions=(assertion, *result.assertions[1:]))
            for assertion in (
                AssertionResult("a0", "binding_unresolved"),
                AssertionResult("a0", "check_timed_out"),
                AssertionResult("a0", "not_evaluated"),
                AssertionResult("a0", "pass", stopped_at=0),
                AssertionResult("a0", "pass", error="fake-assertion-error"),
            )
        ),
        replace(result, steps=()),
        replace(result, steps=result.steps[:2]),
    )


def malformed_rows[T](rows: tuple[T, ...], unknown: T) -> tuple[tuple[T, ...], ...]:
    return rows[1:], (*rows, rows[0]), (rows[0], *rows[:-1]), (unknown, *rows[1:])


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = script()
        self.expected = Expected("pilot", "expectation_violated", (1,), ())
        self.drift = Expected("pilot", "drift_consistent", (), ())

    def compare(
        self, result: RunResult, expected: Expected, settings: Invariants | None = None
    ) -> bool:
        return compare(
            observe(
                self.script,
                result,
                invariants=settings if settings is not None else Invariants(),
            ),
            expected,
        )

    def test_exact_expectation_set_counts_shared_assertions_once(self) -> None:
        result = failed()
        self.assertEqual(
            observe(self.script, result, invariants=Invariants()).failed_expectations,
            frozenset({1}),
        )
        self.assertTrue(self.compare(result, self.expected))
        for indexes in ((0,), (1, 2), ()):
            with self.subTest(indexes=indexes):
                self.assertFalse(
                    self.compare(result, replace(self.expected, expect=indexes))
                )
        extra = replace(
            result, assertions=(AssertionResult("a0", "failed"), *result.assertions[1:])
        )
        self.assertFalse(self.compare(extra, self.expected))
        self.assertFalse(self.compare(clean(), self.expected))
        self.assertFalse(
            self.compare(replace(clean(), outcome="failed"), self.expected)
        )
        self.assertFalse(self.compare(replace(result, outcome="passed"), self.expected))

    def test_invariant_set_is_exact_and_independent(self) -> None:
        result = clean()
        violated = replace(
            result,
            outcome="failed",
            invariants=(
                result.invariants[0],
                InvariantResult("js_exceptions", "violated", ("fake-js",), 2),
                *result.invariants[2:],
            ),
        )
        expected = Expected("pilot", "expectation_violated", (), ("js_exceptions",))
        self.assertTrue(self.compare(violated, expected))
        for names in ((), ("http_5xx",), ("js_exceptions", "http_5xx")):
            with self.subTest(names=names):
                self.assertFalse(
                    self.compare(violated, replace(expected, invariants=names))
                )
        extra = replace(
            violated,
            invariants=(
                *violated.invariants[:2],
                InvariantResult("http_5xx", "violated", (), 1),
                violated.invariants[3],
            ),
        )
        self.assertFalse(self.compare(extra, expected))
        both = replace(failed(), invariants=violated.invariants)
        combined = Expected("pilot", "expectation_violated", (1,), ("js_exceptions",))
        self.assertTrue(self.compare(both, combined))
        self.assertFalse(self.compare(both, expected))
        self.assertFalse(self.compare(both, self.expected))
        self.assertFalse(self.compare(violated, combined))

    def test_exact_sets_cannot_turn_errors_or_timeouts_into_matches(self) -> None:
        self.assertTrue(self.compare(failed(), self.expected))
        for result in ineligible_results():
            with self.subTest(result=result):
                self.assertFalse(self.compare(result, self.expected))
        timeout = replace(
            clean(),
            steps=tuple(
                StepResult(n, "completed", settled="timeout") for n in (0, 1, 3)
            ),
        )
        self.assertFalse(self.compare(timeout, self.drift))
        assertion_timeout = replace(
            clean(),
            assertions=(
                AssertionResult("a0", "check_timed_out"),
                *clean().assertions[1:],
            ),
        )
        observation = observe(self.script, assertion_timeout, invariants=Invariants())
        self.assertEqual(observation.assertions[0].outcome, "check_timed_out")
        self.assertEqual(observation.failed_expectations, frozenset())
        self.assertFalse(compare(observation, self.drift))

    def test_disabled_observations_cannot_match_expected_violations(self) -> None:
        result = clean()
        disabled = replace(
            result,
            invariants=(
                *result.invariants[:2],
                InvariantResult("http_5xx", "disabled", (), 3),
                result.invariants[3],
            ),
        )
        settings = Invariants(disable=("http_5xx",))
        self.assertTrue(self.compare(disabled, self.drift, settings))
        expected = Expected("pilot", "expectation_violated", (), ("http_5xx",))
        self.assertFalse(self.compare(disabled, expected, settings))
        self.assertFalse(self.compare(disabled, self.drift))
        self.assertFalse(
            self.compare(replace(disabled, outcome="failed"), expected, settings)
        )

    def test_explicit_row_must_match_spec_verdict_and_run_outcome(self) -> None:
        self.assertTrue(self.compare(clean(), self.drift))
        self.assertFalse(
            self.compare(failed(), replace(self.expected, spec="another-spec"))
        )
        self.assertFalse(
            self.compare(clean(), replace(self.drift, verdict="expectation_violated"))
        )
        self.assertFalse(
            self.compare(failed(), replace(self.expected, verdict="drift_consistent"))
        )
        self.assertFalse(self.compare(replace(clean(), outcome="failed"), self.drift))
        self.assertFalse(self.compare(clean(), replace(self.drift, verdict="unknown")))
        self.assertIs(
            signature(compare).parameters["expected"].default, Parameter.empty
        )
