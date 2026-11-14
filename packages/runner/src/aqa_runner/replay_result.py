"""A replay's outcome and its run document, `replay.json` (ADR-0024's #238 and
#244 amendments, DATA_MODEL §7), for a strict replay and for a run that heals.

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
never reach here. No strict replay calls a model or heals, so only a run that
heals can end 11 or 7, and only its document has `heal_proposal` and each
verdict's `heal`.

Neither touches a browser, file or clock: a record is read for its ID, scan
and the intents it holds unresolved in memory."""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

from aqa_core.compiled import (
    Assertion,
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    NotVisible,
    Select,
    TextInTarget,
    VisibleUnoccluded,
)
from aqa_core.spec import InvariantName, Spec

from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks
from aqa_runner.evaluation import AssertionResult
from aqa_runner.evidence import Shot
from aqa_runner.executor import RunResult, StepResult
from aqa_runner.heal_proposal import final_script, proposal_document, rebinds
from aqa_runner.invariants import InvariantResult
from aqa_runner.locators import Miss
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.repairs import Repaired, StepAt
from aqa_runner.run_record import RunRecord

# How a replay ends, with its exit code (API.md §7). The order is the
# precedence, `final_outcome` and `rolled_up` taking the first that holds
# (ADR-0024's #238 amendment).
type Outcome = Literal[
    "egress_blocked",
    "non_resumable",
    "no_sandbox",
    "reset_unsafe",
    "unreachable",
    "record_error",
    "no_response",
    "expectation_violated",
    "inconclusive",
    "drift_consistent",
    "passed",
]
EXIT_CODES: Final[Mapping[Outcome, int]] = {
    "egress_blocked": 6,
    "non_resumable": 4,
    "no_sandbox": 10,
    "reset_unsafe": 14,
    "unreachable": 13,
    "record_error": 15,
    "no_response": 11,
    "expectation_violated": 1,
    "inconclusive": 3,
    "drift_consistent": 7,
    "passed": 0,
}

# The causes a run stops on that its result can't show: replay raised, or
# its document couldn't be written.
type Stoppage = Literal[
    "no_sandbox", "reset_unsafe", "unreachable", "record_error", "no_response"
]

# A step's, assertion's or invariant's verdict. Strict replay never heals, so
# never gives `drift_consistent` (CONTEXT.md, "Verdict").
type Verdict = Literal[
    "pass", "expectation_violated", "inconclusive", "drift_consistent"
]

# The `findings.category` of each violation's finding (DATA_MODEL §2).
type Category = Literal["functional", "visual", "backend_5xx", "js_error"]
_CATEGORIES: Final[Mapping[InvariantName, Category]] = {
    "console_errors": "js_error",
    "js_exceptions": "js_error",
    "http_5xx": "backend_5xx",
    "broken_images": "visual",
}
# The reason an infrastructure event gives (exit 13, API.md §7), in fixed
# words: an event's host and cause are withheld in a run that binds a secret.
_UNREACHED: Final = "an allowed host or the egress proxy couldn't be reached"
# Why a completed step on a rebound target gets no verdict of its own (Q12).
_NOT_PASSED: Final = "heal rebound its target, but the run didn't pass"


@dataclass(frozen=True)
class Stop:
    """A cause the run stopped on, and why, scanned with the run's
    redactor."""

    outcome: Stoppage
    reason: Redacted


@dataclass(frozen=True)
class ReplayEnd:
    """What a run knows once replay returned or raised: its record, whose scan
    decides what is withheld and which holds the intents the run left
    unresolved; the spec and the compiled script it replayed; replay's result,
    None when replay raised; the egress blocks and the gate's infrastructure
    events as the run left them, which hold whether or not replay returned or
    its record took them, as explore's do; each cause it stopped on; and, in a
    run that heals, the blob SHA of the compiled file it replayed
    (`heal_patch.blob_sha`), None in a strict replay, which never heals."""

    record: RunRecord
    spec: Spec
    script: CompiledScript
    result: RunResult | None
    blocks: EgressBlocks
    infrastructure_events: tuple[InfrastructureEvent, ...]
    stops: tuple[Stop, ...] = ()
    base: str | None = None

    @property
    def stopped_on(self) -> tuple[Stop, ...]:
        """Each cause the run stopped on, and each stop of the whole run a
        repair hit, which ended its handoffs."""
        repairs = () if self.result is None else self.result.repairs
        return (
            *self.stops,
            *(
                Stop(basis.stop, basis.said)
                for repaired in repairs
                if (basis := repaired.basis) is not None and basis.stop is not None
            ),
        )


def final_outcome(end: ReplayEnd) -> Outcome:
    """The outcome `end` exits with: of every cause that holds, the first in
    `EXIT_CODES`. An unresolved intent with `side_effect: true` makes the run
    non-resumable whatever else holds. A run with no result never passes."""
    holding: set[Outcome] = {stop.outcome for stop in end.stopped_on}
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
    if "drift_consistent" in verdicts:
        holding.add("drift_consistent")
    holding.add("passed")
    return next(outcome for outcome in EXIT_CODES if outcome in holding)


def rolled_up(outcomes: Iterable[Outcome]) -> Outcome:
    """The outcome several runs exit with together: the first in
    `EXIT_CODES` of any run's."""
    held = set(outcomes)
    return next(outcome for outcome in EXIT_CODES if outcome in held)


def present(end: ReplayEnd) -> dict[str, object]:
    """`end` as `replay.json` holds it, JSON-ready: schema 1. Every
    non-resumable step is listed whatever the outcome. A run that heals says so
    in its `mode`, and adds `heal_proposal`, null unless it ended
    `drift_consistent`."""
    outcome = final_outcome(end)
    withheld = not end.record.redactor.empty
    events = end.infrastructure_events
    judged = _judged(end)
    return {
        "schema_version": 1,
        "run_id": end.record.run_id,
        "spec_id": end.spec.frontmatter.id,
        "mode": "strict" if end.base is None else "run",
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
        **({} if end.base is None else {"heal_proposal": _proposal(end, outcome)}),
    }


def _proposal(end: ReplayEnd, outcome: Outcome) -> dict[str, object] | None:
    if end.base is None or end.result is None or outcome != "drift_consistent":
        return None
    final = final_script(end.script, end.result.repairs)
    return proposal_document(end.spec.frontmatter.id, end.base, end.script, final)


def unrecorded(
    spec: Spec, redactor: Redactor, reason: str, *, healing: bool = False
) -> dict[str, object]:
    """The document of a run of `spec` whose record couldn't be made, which
    no file holds: `present`'s fields, with no run ID, nothing that ran, and
    `reason`, scanned with `redactor`, the run's, its one reason. `healing` says
    it was a run that heals."""
    withheld = not redactor.empty
    return {
        "schema_version": 1,
        "run_id": None,
        "spec_id": spec.frontmatter.id,
        "mode": "run" if healing else "strict",
        "outcome": "record_error",
        "exit_code": EXIT_CODES["record_error"],
        "withheld": withheld,
        "error_code": None,
        "reasons": [redactor.redact(reason)],
        "non_resumable": [],
        "infrastructure_events": {"count": 0, "events": None if withheld else []},
        "verdicts": [],
        **({"heal_proposal": None} if healing else {}),
    }


def _non_resumable(end: ReplayEnd) -> list[int]:
    return [intent.seq for intent in end.record.unresolved() if intent.side_effect]


@dataclass(frozen=True)
class _Subject:
    """What one verdict is on, by its kind and its name: a step's seq, a check's
    id or an invariant's name. A check's id can be any string, `step 1` too, so
    only the two together say which subject it is."""

    kind: Literal["step", "assertion", "invariant"]
    name: int | str

    @property
    def label(self) -> str:
        """What the run's reasons call it: `step 1`, `a2`, `invariant http_5xx`."""
        return (
            str(self.name) if self.kind == "assertion" else f"{self.kind} {self.name}"
        )


@dataclass(frozen=True)
class _Judged:
    """One verdict: its subject, the verdict, its document entry, and each
    locator's miss when its target drifted, else None."""

    subject: _Subject
    verdict: Verdict
    entry: dict[str, object]
    drift: tuple[Miss, ...] | None = None

    def cited(self, heal: dict[str, object] | None) -> _Judged:
        """This verdict, as it is, citing `heal`."""
        return replace(self, entry=self.entry | {"heal": heal})

    def ruled(
        self, verdict: Verdict, reason: str | None, heal: dict[str, object] | None
    ) -> _Judged:
        """This subject with the verdict a run that heals rules on it."""
        ruling = {"verdict": verdict, "reason": reason, "heal": heal}
        return replace(self, verdict=verdict, entry=self.entry | ruling)

    def listing(self, rebound_steps: list[int | str]) -> _Judged:
        """This verdict, whose finding, if it has one, lists each step heal
        rebound, since a wrong rebinding could be the finding's cause (#245)."""
        finding = self.entry["finding"]
        if not isinstance(finding, dict):
            return self
        listed = finding | {"rebound_steps": rebound_steps}
        return replace(self, entry=self.entry | {"finding": listed})


def _judged(end: ReplayEnd) -> list[_Judged]:
    """Each step's verdict, then each assertion's and each enabled
    invariant's. An assertion a stopped step left unevaluated has none: the
    step's verdict says why. A run that stopped, as the executor stops, on
    a step that didn't complete or an infrastructure event, gives a held
    invariant none either, since what it held so far establishes nothing; a
    violation is established. A run with no result, or with an egress
    block, has none at all."""
    result = end.result
    if result is None or end.blocks.blocked:
        return []
    stopped = result.steps[-1].outcome != "completed" or bool(end.infrastructure_events)
    last = result.steps[-1].seq
    judging = _Judging(
        end.record.redactor,
        [step.seq for step in result.steps],
        last,
        last if result.evidence is None else result.evidence.seq,
        result.shots,
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
    return judged if end.base is None else _Healed.of(end, result).judged(judged)


@dataclass(frozen=True)
class _Judging:
    """What one run's verdicts are judged with: its scan; the seqs of the
    steps that ran, each finding's repro steps; the last step, whose
    evidence is the page the assertions were evaluated on; the step whose
    evidence a violated invariant cites, the page that first showed a
    violation (`RunResult.evidence`); and each step's screenshot, by seq
    (`RunResult.shots`)."""

    redactor: Redactor
    ran: list[int]
    last: int
    violated_at: int
    shots: Mapping[int, Shot]

    def step(self, step: StepResult) -> _Judged:
        judged = self._judged(
            _Subject("step", step.seq),
            _located(step.locator_index, step.misses),
            _step_verdict(step),
            step.seq,
        )
        return replace(judged, drift=step.misses if step.outcome == "drifted" else None)

    def assertion(
        self, result: AssertionResult, check: Assertion, expectation: str
    ) -> _Judged:
        category: Category = (
            "visual" if isinstance(check, VisibleUnoccluded) else "functional"
        )
        judged = self._judged(
            _Subject("assertion", result.id),
            _located(result.locator_index, result.misses),
            _assertion_verdict(result),
            self.last,
            about=(category, expectation),
        )
        drifted = result.outcome == "binding_unresolved"
        return replace(judged, drift=result.misses if drifted else None)

    def invariant(self, result: InvariantResult) -> _Judged:
        name = result.name
        held = result.outcome == "held"
        return self._judged(
            _Subject("invariant", name),
            {},
            ("pass" if held else "expectation_violated", None),
            self.last if held else self.violated_at,
            about=(_CATEGORIES[name], f"invariant {name} violated"),
        )

    def _judged(
        self,
        subject: _Subject,
        located: dict[str, object],
        judgement: tuple[Verdict, str | None],
        evidence: int,
        *,
        about: tuple[Category, str] | None = None,
    ) -> _Judged:
        """The verdict on `subject`, where it was `located`, and its reason,
        citing step `evidence`'s evidence and that step's screenshot, if its
        evidence asked for one (DATA_MODEL §7). A violation's finding is
        `about` its category and title."""
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
            subject,
            verdict,
            {
                subject.kind: subject.name,
                **located,
                "verdict": verdict,
                "reason": None if reason is None else scan(reason),
                "evidence": {
                    "path": f"evidence/{evidence}",
                    "screenshot": _shot(self.shots.get(evidence), self.redactor),
                },
                "finding": finding,
            },
        )


def _shot(shot: Shot | None, redactor: Redactor) -> dict[str, object] | None:
    """A screenshot as a verdict cites it: the file it was saved as, or why it
    was withheld, scanned as every reason is; None when none was asked for.
    The file's name is ours, so it isn't scanned, as `evidence.path` isn't."""
    if shot is None:
        return None
    withheld = None if shot.withheld is None else redactor.redact(shot.withheld)
    return {"path": shot.path, "withheld": withheld}


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


def _drift(misses: tuple[Miss, ...], why: str = "strict replay doesn't heal") -> str:
    said = f" ({', '.join(misses)})" if misses else ""
    return f"drift: {why}{said}"


@dataclass(frozen=True)
class _Healed:
    """What a run that heals changes in its verdicts (ADR-0024's #244
    amendment). A subject is on the rebound set when it is a step that ran
    through a target whose locators differ between the committed script and the
    final one, or an assertion of any kind on such a target. `by_subject` is each
    repair by what it was handed, `first` each target's first repair to change
    it, `targets` what each subject reads, `dispatched` each step that completed,
    that keeps its settle window, as one a repair inserted that failed once it
    settled does (#246), or whose intent has no completion, so not one refused
    before dispatch or one that drifted, which write none, and `untroubled` says
    nothing stopped, blocked or left unresolved the run."""

    redactor: Redactor
    rebound: frozenset[str]
    by_subject: Mapping[_Subject, Repaired]
    first: Mapping[str, Repaired]
    targets: Mapping[_Subject, str]
    dispatched: frozenset[_Subject]
    untroubled: bool

    @classmethod
    def of(cls, end: ReplayEnd, result: RunResult) -> _Healed:
        final = final_script(end.script, result.repairs)
        unresolved = {intent.seq for intent in end.record.unresolved()}
        targets = {
            _Subject("step", step.seq): step.target
            for step in final.steps
            if isinstance(step, Click | Fill | FillSecret | Select)
        } | {
            _Subject("assertion", check.id): check.target
            for check in end.script.assertions
            if isinstance(check, TextInTarget | NotVisible | VisibleUnoccluded)
        }
        return cls(
            end.record.redactor,
            frozenset(
                name
                for name, target in end.script.targets.items()
                if final.targets[name].locators != target.locators
            ),
            {_subject(repaired): repaired for repaired in result.repairs},
            dict(reversed(list(rebinds(end.script, result.repairs)))),
            targets,
            frozenset(
                _Subject("step", step.seq)
                for step in result.steps
                if step.outcome == "completed"
                or step.window is not None
                or step.seq in unresolved
            ),
            not (
                end.stopped_on
                or end.blocks.blocked
                or end.infrastructure_events
                or _non_resumable(end)
            ),
        )

    def judged(self, judged: list[_Judged]) -> list[_Judged]:
        """`judged`, each ruled on. Only when every one of the run's own verdicts
        passes and nothing troubled it is a subject on the rebound set
        `drift_consistent`; with none on it, the run just passed. Each finding lists
        the steps on the set that reached dispatch."""
        passed = self.untroubled and all(each.verdict == "pass" for each in judged)
        rebound_steps = [
            each.subject.name
            for each in judged
            if each.subject in self.dispatched
            and self.targets.get(each.subject) in self.rebound
        ]
        return [
            self._marked(each, passed=passed).listing(rebound_steps) for each in judged
        ]

    def _marked(self, judged: _Judged, *, passed: bool) -> _Judged:
        """`judged`, with its `heal`: a drift says what became of its repair; a
        subject on the rebound set cites the repair that first changed its
        target. There a completed step in a run that didn't pass is `inconclusive`
        (Q12), and an assertion keeps its own verdict (#245)."""
        target = self.targets.get(judged.subject)
        if judged.drift is not None:
            first = None if target is None else self.first.get(target)
            repair = self.by_subject.get(judged.subject) or first
            said = _drift(judged.drift, _why_drifted(repair))
            cited = (
                None if repair is None else _cited(repair, self.redactor, bound=False)
            )
            return judged.ruled("inconclusive", self.redactor.redact(said), cited)
        if target is None or target not in self.rebound:
            return judged.cited(None)
        cited = _cited(self.first[target], self.redactor, bound=True)
        if passed:
            return judged.ruled("drift_consistent", None, cited)
        if judged.subject.kind == "step" and judged.verdict == "pass":
            return judged.ruled(
                "inconclusive", self.redactor.redact(_NOT_PASSED), cited
            )
        return judged.cited(cited)


def _subject(repaired: Repaired) -> _Subject:
    """What `repaired` was handed."""
    at = repaired.at
    if isinstance(at, StepAt):
        return _Subject("step", at.step.seq)
    return _Subject("assertion", at.check.id)


def _why_drifted(repair: Repaired | None) -> str:
    if repair is None:
        return "it went to no heal"
    if repair.problems:
        return f"replay refused the heal's repair: {'; '.join(repair.problems)}"
    if repair.script is not None:
        return "drifted again after its repair"
    said = "" if repair.basis is None else f": {repair.basis.said}"
    return f"heal gave up{said}"


def _cited(repaired: Repaired, redactor: Redactor, *, bound: bool) -> dict[str, object]:
    """Where a verdict's heal is: its repair's record, the heal's own files if
    it wrote any, and, for a binding the verdict stands on, what it bound and
    what its model said of that, which is the model's own report and decides
    nothing (CONTEXT.md, "Verdict"); and the screenshot of its first look,
    scanned with `redactor`, if it took one."""
    basis = repaired.basis
    reported = None if basis is None or not bound else basis.model_reported
    return {
        "repair": f"repairs/{repaired.at.key}.json",
        "record": None if basis is None else basis.record,
        "bound": None if basis is None or not bound else basis.said,
        "model_reported": None
        if reported is None
        else {"confidence": reported.confidence, "rationale": reported.rationale},
        "screenshot": None if basis is None else _shot(basis.shot, redactor),
    }


def _reasons(end: ReplayEnd, judged: list[_Judged]) -> Iterator[Redacted]:
    """Why the run ended as it did: each stop's reason, `_UNREACHED` after
    an infrastructure event, then each verdict but a pass, with its reason,
    each scanned whole, so a value spanning a join is found."""
    scan = end.record.redactor.redact
    yield from (scan(stop.reason) for stop in end.stopped_on)
    if end.infrastructure_events:
        yield scan(_UNREACHED)
    for verdict in judged:
        # A proposal has its own block, so it isn't why the run didn't pass.
        if verdict.verdict not in ("pass", "drift_consistent"):
            reason = verdict.entry["reason"]
            said = "" if reason is None else f", {reason}"
            yield scan(f"{verdict.subject.label}: {verdict.verdict}{said}")
