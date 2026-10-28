"""A strict replay's outcome and its run document, `replay.json`
(ADR-0024's #238 amendment, DATA_MODEL §7).

`present` projects a run into `replay.json`: a verdict for each step that
ran, each assertion evaluated or whose look raised, and each invariant the
spec keeps enabled, with the evidence each cites and, for each
`expectation_violated` verdict, its finding. An egress block has no verdict
and makes no finding (ADR-0026). Like `explore_result.present`, it copies an
allow-list, field by field, never a result object or a reflection of one.

The run's record decides what is withheld, by the predicate ADR-0026's #53
P12 amendment names: when its scan binds any test secret, the document
names no infrastructure host, port or cause. Every reason and title copied
is scanned again with that scan, and so is every reason assembled here.

`final_outcome` picks the outcome when several causes hold, and
`rolled_up` picks the run that decides when several specs ran, by the same
order. Spec and secret errors (5, 12) are found before any run, so they
never reach here; no strict replay calls a model or heals, so neither 11
nor 7 is here either.

Neither touches a browser, file or clock: a record is read for its ID, scan
and the intents it holds unresolved in memory."""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from aqa_core.compiled import Assertion, CompiledScript, VisibleUnoccluded
from aqa_core.spec import InvariantName, Spec

from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import RunResult, StepResult
from aqa_runner.invariants import InvariantResult
from aqa_runner.locators import Miss
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.run_record import RunRecord

# How a strict replay ends, with its exit code (API.md §7). The order is the
# precedence, `final_outcome` and `rolled_up` taking the first that holds
# (ADR-0024's #238 amendment).
type Outcome = Literal[
    "egress_blocked",
    "non_resumable",
    "no_sandbox",
    "reset_unsafe",
    "unreachable",
    "record_error",
    "expectation_violated",
    "inconclusive",
    "passed",
]
EXIT_CODES: Final[Mapping[Outcome, int]] = {
    "egress_blocked": 6,
    "non_resumable": 4,
    "no_sandbox": 10,
    "reset_unsafe": 14,
    "unreachable": 13,
    "record_error": 15,
    "expectation_violated": 1,
    "inconclusive": 3,
    "passed": 0,
}

# The causes a run stops on that its result can't show: replay raised, or
# its document couldn't be written.
type Stoppage = Literal["no_sandbox", "reset_unsafe", "unreachable", "record_error"]

# A step's, assertion's or invariant's verdict in strict replay, which never
# heals, so never `drift_consistent` (CONTEXT.md, "Verdict").
type Verdict = Literal["pass", "expectation_violated", "inconclusive"]

# The `findings.category` of each violation's finding (DATA_MODEL §2).
type Category = Literal["functional", "visual", "backend_5xx", "js_error"]
_CATEGORIES: Final[Mapping[InvariantName, Category]] = {
    "console_errors": "js_error",
    "js_exceptions": "js_error",
    "http_5xx": "backend_5xx",
    "broken_images": "visual",
}
# Why a run with an infrastructure event exits 13 (API.md §7), in fixed
# words: an event's host and cause are withheld in a run that binds a secret.
_UNREACHED: Final = "an allowed host or the egress proxy couldn't be reached"


@dataclass(frozen=True)
class Stop:
    """A cause the run stopped on, and why, scanned with the run's
    redactor."""

    outcome: Stoppage
    reason: Redacted


@dataclass(frozen=True)
class ReplayEnd:
    """What a strict run knows once replay returned or raised: its record,
    whose scan decides what is withheld and which holds the intents the run
    left unresolved; the spec and the compiled script it replayed; replay's
    result, None when replay raised; the egress blocks and the gate's
    infrastructure events as the run left them, which hold whether or not
    replay returned or its record took them, as explore's do; and each
    cause it stopped on."""

    record: RunRecord
    spec: Spec
    script: CompiledScript
    result: RunResult | None
    blocks: EgressBlocks
    infrastructure_events: tuple[InfrastructureEvent, ...]
    stops: tuple[Stop, ...] = ()


def final_outcome(end: ReplayEnd) -> Outcome:
    """The outcome `end` exits with: of every cause that holds, the first in
    `EXIT_CODES`. An unresolved intent with `side_effect: true` makes the run
    non-resumable whatever else holds. A run with no result never passes."""
    holding: set[Outcome] = {stop.outcome for stop in end.stops}
    if _non_resumable(end):
        holding.add("non_resumable")
    if end.blocks.blocked:
        holding.add("egress_blocked")
    if end.infrastructure_events:
        holding.add("unreachable")
    if end.result is None:
        holding.add("inconclusive")
    verdicts = {judged.verdict for judged in _judged(end)}
    if "expectation_violated" in verdicts:
        holding.add("expectation_violated")
    if "inconclusive" in verdicts:
        holding.add("inconclusive")
    holding.add("passed")
    return next(outcome for outcome in EXIT_CODES if outcome in holding)


def rolled_up(outcomes: Iterable[Outcome]) -> Outcome:
    """The outcome several runs exit with together: the first in
    `EXIT_CODES` of any run's."""
    held = set(outcomes)
    return next(outcome for outcome in EXIT_CODES if outcome in held)


def present(end: ReplayEnd) -> dict[str, object]:
    """`end` as `replay.json` holds it, JSON-ready: schema 1. Every
    non-resumable step is listed whatever the outcome."""
    outcome = final_outcome(end)
    withheld = not end.record.redactor.empty
    events = end.infrastructure_events
    judged = _judged(end)
    return {
        "schema_version": 1,
        "run_id": end.record.run_id,
        "spec_id": end.spec.frontmatter.id,
        "mode": "strict",
        "outcome": outcome,
        "exit_code": EXIT_CODES[outcome],
        "withheld": withheld,
        "error_code": "egress_blocked" if end.blocks.blocked else None,
        "reasons": list(_reasons(end, judged)),
        "non_resumable": _non_resumable(end),
        "infrastructure_events": {
            "count": len(events),
            "events": None
            if withheld
            else [
                {"host": event.host, "port": event.port, "cause": event.cause}
                for event in events
            ],
        },
        "verdicts": [verdict.entry for verdict in judged],
    }


def _non_resumable(end: ReplayEnd) -> list[int]:
    return [intent.seq for intent in end.record.unresolved() if intent.side_effect]


@dataclass(frozen=True)
class _Judged:
    """One verdict: what the run's reasons call its subject (`step 1`,
    `a2`, `invariant js_exceptions`), the verdict, and its document entry."""

    label: str
    verdict: Verdict
    entry: dict[str, object]


def _judged(end: ReplayEnd) -> list[_Judged]:
    """Each step's verdict, then each assertion's and each enabled
    invariant's. An assertion a stopped step left unevaluated has none: the
    step's verdict says why. A run that stopped before its checks were
    evaluated gives a held invariant none either, since what it held so far
    establishes nothing; a violation is established. A run with no result,
    or with an egress block, has none at all."""
    result = end.result
    if result is None or end.blocks.blocked:
        return []
    stopped = result.steps[-1].outcome != "completed" or any(
        assertion.stopped_at is not None for assertion in result.assertions
    )
    last = result.steps[-1].seq
    judging = _Judging(
        end.record.redactor,
        [step.seq for step in result.steps],
        last,
        last if result.evidence is None else result.evidence.seq,
    )
    checks = {check.id: check for check in end.script.assertions}
    expect = end.spec.frontmatter.expect
    judged = [judging.step(step) for step in result.steps]
    for assertion in result.assertions:
        if assertion.stopped_at is None:
            check = checks[assertion.id]
            text = expect[check.expect_index].text
            judged.append(judging.assertion(assertion, check, text))
    judged.extend(
        judging.invariant(invariant)
        for invariant in result.invariants
        if invariant.outcome == "violated"
        or (invariant.outcome == "held" and not stopped)
    )
    return judged


@dataclass(frozen=True)
class _Judging:
    """What one run's verdicts are judged with: its scan; the seqs of the
    steps that ran, each finding's repro steps; the last step, whose
    evidence is the page the assertions were evaluated on; and the step
    whose evidence a violated invariant cites, the page that first showed a
    violation (`RunResult.evidence`)."""

    redactor: Redactor
    ran: list[int]
    last: int
    violated_at: int

    def step(self, step: StepResult) -> _Judged:
        located = {"step": step.seq, **_located(step.locator_index, step.misses)}
        return self._judged(f"step {step.seq}", located, _step_verdict(step), step.seq)

    def assertion(
        self, result: AssertionResult, check: Assertion, expectation: str
    ) -> _Judged:
        located = {
            "assertion": result.id,
            **_located(result.locator_index, result.misses),
        }
        category: Category = (
            "visual" if isinstance(check, VisibleUnoccluded) else "functional"
        )
        return self._judged(
            result.id,
            located,
            _assertion_verdict(result),
            self.last,
            about=(category, expectation),
        )

    def invariant(self, result: InvariantResult) -> _Judged:
        name = result.name
        held = result.outcome == "held"
        return self._judged(
            f"invariant {name}",
            {"invariant": name},
            ("pass" if held else "expectation_violated", None),
            self.last if held else self.violated_at,
            about=(_CATEGORIES[name], f"invariant {name} violated"),
        )

    def _judged(
        self,
        label: str,
        named: dict[str, object],
        judgement: tuple[Verdict, str | None],
        evidence: int,
        *,
        about: tuple[Category, str] | None = None,
    ) -> _Judged:
        """The verdict on `named` and its reason, citing step `evidence`'s
        evidence, which no screenshot is part of until #251 (DATA_MODEL §7).
        A violation's finding is `about` its category and title."""
        verdict, reason = judgement
        scan = self.redactor.redact
        finding = None
        if verdict == "expectation_violated" and about is not None:
            category, title = about
            finding = {
                "category": category,
                "title": scan(title),
                "repro_steps": self.ran,
            }
        return _Judged(
            label,
            verdict,
            {
                **named,
                "verdict": verdict,
                "reason": None if reason is None else scan(reason),
                "evidence": {"path": f"evidence/{evidence}", "screenshot": None},
                "finding": finding,
            },
        )


def _located(index: int | None, misses: tuple[Miss, ...]) -> dict[str, object]:
    return {"locator_index": index, "misses": list(misses)}


def _step_verdict(step: StepResult) -> tuple[Verdict, str | None]:
    if step.outcome == "completed":
        return "pass", None
    if step.outcome == "drifted":
        return "inconclusive", _drift(step.misses)
    return "inconclusive", step.error


def _assertion_verdict(result: AssertionResult) -> tuple[Verdict, str | None]:
    """A false check is a violation. Drift, a timed-out check and a look at
    the page that raised establish neither verdict (ADR-0024's 2026-10-02
    and #238 amendments)."""
    if result.outcome == "pass":
        return "pass", None
    if result.outcome == "failed":
        return "expectation_violated", None
    if result.outcome == "binding_unresolved":
        return "inconclusive", _drift(result.misses)
    if result.outcome == "check_timed_out":
        return "inconclusive", "check_timed_out"
    return "inconclusive", result.error


def _drift(misses: tuple[Miss, ...]) -> str:
    said = f" ({', '.join(misses)})" if misses else ""
    return f"drift: strict replay doesn't heal{said}"


def _reasons(end: ReplayEnd, judged: list[_Judged]) -> Iterator[Redacted]:
    """Why the run ended as it did: each stop's reason, `_UNREACHED` after
    an infrastructure event, then each verdict but a pass, with its reason,
    each scanned whole, so a value spanning a join is found."""
    scan = end.record.redactor.redact
    yield from (scan(stop.reason) for stop in end.stops)
    if end.infrastructure_events:
        yield scan(_UNREACHED)
    for verdict in judged:
        if verdict.verdict != "pass":
            reason = verdict.entry["reason"]
            said = "" if reason is None else f", {reason}"
            yield scan(f"{verdict.label}: {verdict.verdict}{said}")
