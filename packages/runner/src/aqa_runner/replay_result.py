"""A replay's outcome and its run document, `replay.json` (ADR-0024's #238,
#244 and #256 amendments, DATA_MODEL §7), for a strict replay, a run that heals
and a verified replay.

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
heals or a verified replay can end 11, only a run that heals 7, and only its
document has `heal_proposal` and each verdict's `heal`. Only a verified
replay's has `verified_cost_usd` and each verdict's `verifier`.

Neither touches a browser, file or clock: a record is read for its ID, scan
and the intents it holds unresolved in memory."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import MAX_PREC, Decimal, localcontext
from typing import Final, Literal

from aqa_core.compiled import (
    Assertion,
    Click,
    CompiledScript,
    ContrastMin,
    Fill,
    FillSecret,
    ModelVerify,
    PixelDiff,
    Select,
    TargetCheck,
    VisibleUnoccluded,
)
from aqa_core.model_costs import CostRecord
from aqa_core.price_map import plain
from aqa_core.spec import InvariantName, Spec

from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks
from aqa_runner.evaluation import AssertionResult, Verified, VerifierSaid
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
# Why a completed step on a rebound target gets no verdict of its own (Q12),
# and why a completed step a repair inserted gets none (#325).
_NOT_PASSED: Final = "heal rebound its target, but the run didn't pass"
_INSERTED_NOT_PASSED: Final = "heal inserted this step, but the run didn't pass"


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
    its record took them, as explore's do; each cause it stopped on; in a
    run that heals, the blob SHA of the compiled file it replayed
    (`heal_patch.blob_sha`), None in a strict replay, which never heals; and,
    in a verified replay, the cost record of each response its verifier
    calls got, None in any other run."""

    record: RunRecord
    spec: Spec
    script: CompiledScript
    result: RunResult | None
    blocks: EgressBlocks
    infrastructure_events: tuple[InfrastructureEvent, ...]
    stops: tuple[Stop, ...] = ()
    base: str | None = None
    verified: tuple[CostRecord, ...] | None = None

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
    `drift_consistent`. A verified replay says so too, and adds
    `verified_cost_usd`, what its verifier calls cost."""
    outcome = final_outcome(end)
    withheld = not end.record.redactor.empty
    events = end.infrastructure_events
    judged = _judged(end)
    return {
        "schema_version": 1,
        "run_id": end.record.run_id,
        "spec_id": end.spec.frontmatter.id,
        "mode": _mode(healing=end.base is not None, verified=end.verified is not None),
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
        **({} if end.verified is None else {"verified_cost_usd": _usd(end.verified)}),
    }


def _mode(*, healing: bool, verified: bool) -> str:
    """What a run's document says it was: `run`, a run that heals; `verified`,
    a verified replay; or `strict`."""
    return "run" if healing else "verified" if verified else "strict"


def _usd(calls: Sequence[CostRecord]) -> str:
    """What `calls` cost, exactly, written as `costs.jsonl` writes a call's
    `cost_usd`: `0` for none."""
    with localcontext(prec=MAX_PREC):
        return str(plain(sum((call.cost_usd for call in calls), Decimal(0))))


def _proposal(end: ReplayEnd, outcome: Outcome) -> dict[str, object] | None:
    if end.base is None or end.result is None or outcome != "drift_consistent":
        return None
    final = final_script(end.script, end.result.repairs)
    return proposal_document(end.spec.frontmatter.id, end.base, end.script, final)


def unrecorded(
    spec: Spec,
    redactor: Redactor,
    reason: str,
    *,
    healing: bool = False,
    verified: bool = False,
) -> dict[str, object]:
    """The document of a run of `spec` whose record couldn't be made, which
    no file holds: `present`'s fields, with no run ID, nothing that ran, and
    `reason`, scanned with `redactor`, the run's, its one reason. `healing` says
    it was a run that heals, and `verified` a verified replay."""
    withheld = not redactor.empty
    return {
        "schema_version": 1,
        "run_id": None,
        "spec_id": spec.frontmatter.id,
        "mode": _mode(healing=healing, verified=verified),
        "outcome": "record_error",
        "exit_code": EXIT_CODES["record_error"],
        "withheld": withheld,
        "error_code": None,
        "reasons": [redactor.redact(reason)],
        "non_resumable": [],
        "infrastructure_events": {"count": 0, "events": None if withheld else []},
        "verdicts": [],
        **({"heal_proposal": None} if healing else {}),
        **({"verified_cost_usd": _usd(())} if verified else {}),
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
        result.before_shots,
        verifying=end.verified is not None,
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
    violation (`RunResult.evidence`); each step's screenshot and each failed
    step's "before", by seq (`RunResult.shots`, `RunResult.before_shots`);
    and whether the run is a verified replay, whose every verdict says what
    its verifier said, if anything."""

    redactor: Redactor
    ran: list[int]
    last: int
    violated_at: int
    shots: Mapping[int, Shot]
    before_shots: Mapping[int, Shot]
    verifying: bool = False

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
            "visual"
            if isinstance(
                check, VisibleUnoccluded | ContrastMin | PixelDiff | ModelVerify
            )
            else "functional"
        )
        judged = self._judged(
            _Subject("assertion", result.id),
            _located(result.locator_index, result.misses),
            _assertion_verdict(result),
            self.last,
            about=(category, expectation),
            verified=result.verified,
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
        verified: Verified | None = None,
    ) -> _Judged:
        """The verdict on `subject`, where it was `located`, and its reason,
        citing step `evidence`'s evidence, that step's screenshot, if its
        evidence asked for one, and its "before", if it failed and kept one
        (DATA_MODEL §7), or, for a `model_verify` check whose evidence was
        saved, its own folder and screenshot, with no "before". A violation's
        finding is `about` its category and title. In a verified replay, it
        says what the verifier said of it, null for all but a `model_verify`
        check whose reply parsed."""
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
        cited = {
            "path": f"evidence/{evidence}",
            "screenshot": _shot(self.shots.get(evidence), self.redactor),
            "before_screenshot": _shot(self.before_shots.get(evidence), self.redactor),
        }
        if verified is not None and verified.folder is not None:
            cited = {
                "path": verified.folder,
                "screenshot": _shot(verified.shot, self.redactor),
                "before_screenshot": None,
            }
        said = None if verified is None else verified.said
        return _Judged(
            subject,
            verdict,
            {
                subject.kind: subject.name,
                **located,
                "verdict": verdict,
                "reason": None if reason is None else scan(reason),
                "evidence": cited,
                "finding": finding,
                **({"verifier": _said(said, self.redactor)} if self.verifying else {}),
            },
        )


def _said(said: VerifierSaid | None, redactor: Redactor) -> dict[str, object] | None:
    """What a verifier said, as its verdict cites it, each text scanned again:
    its verdict, the references it cited, and its own report, which decides
    nothing (CONTEXT.md, "Verdict"); None when its reply didn't parse."""
    if said is None:
        return None
    return {
        "verdict": redactor.redact(said.verdict),
        "evidence_refs": [redactor.redact(ref) for ref in said.evidence_refs],
        "model_reported": {
            "confidence": said.confidence,
            "rationale": redactor.redact(said.rationale),
        },
    }


def _shot(shot: Shot | None, redactor: Redactor) -> dict[str, object] | None:
    """A screenshot as a verdict cites it: the file it was saved as, or why it
    was withheld, scanned as every reason is; None when none was asked for or
    kept.
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
    and #238 amendments). A `model_verify` check has the verdict and the
    fixed reason it came to (#256)."""
    if result.verified is not None:
        return result.verified.verdict, result.verified.reason
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
    """What a run that heals changes in its verdicts (ADR-0024's #244 and #246
    amendments). A subject is on the rebound set when it is a step that ran
    through a target whose locators differ between the committed script and the
    final one, an assertion of any kind on such a target, or a step a repair
    inserted, whatever it acts on, so the set is empty exactly when the final
    patch is. `by_subject` is each repair by what it was handed, `first` each
    target's first repair to change it, `inserted` each inserted step's repair
    (`_inserting`), `targets` what each subject reads, `dispatched` each step
    that completed, that keeps its settle window, as one a repair inserted that
    failed once it settled does (#246), or whose intent has no completion, so not
    one refused before dispatch or one that drifted, which write none, and
    `untroubled` says nothing stopped, blocked or left unresolved the run."""

    redactor: Redactor
    rebound: frozenset[str]
    by_subject: Mapping[_Subject, Repaired]
    first: Mapping[str, Repaired]
    inserted: Mapping[_Subject, Repaired]
    targets: Mapping[_Subject, str]
    dispatched: frozenset[_Subject]
    untroubled: bool

    @classmethod
    def of(cls, end: ReplayEnd, result: RunResult) -> _Healed:
        final = final_script(end.script, result.repairs)
        unresolved = {intent.seq for intent in end.record.unresolved()}
        committed = {step.seq for step in end.script.steps}
        targets = {
            _Subject("step", step.seq): step.target
            for step in final.steps
            if isinstance(step, Click | Fill | FillSecret | Select)
        } | {
            _Subject("assertion", check.id): check.target
            for check in end.script.assertions
            if isinstance(check, TargetCheck)
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
            {
                _Subject("step", step.seq): repaired
                for step in result.steps
                if step.seq not in committed
                and (repaired := _inserting(step.seq, final, result.repairs))
                is not None
            },
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
            and (
                each.subject in self.inserted
                or self.targets.get(each.subject) in self.rebound
            )
        ]
        return [
            self._marked(each, passed=passed).listing(rebound_steps) for each in judged
        ]

    def _marked(self, judged: _Judged, *, passed: bool) -> _Judged:
        """`judged`, with its `heal`: a drift says what became of its repair; an
        inserted step cites the repair that inserted it, with no binding, since
        what that repair bound is the drifted target's; any other subject on the
        rebound set cites the repair that first changed its target. There a
        completed step in a run that didn't pass is `inconclusive` (Q12), and an
        assertion keeps its own verdict (#245)."""
        target = self.targets.get(judged.subject)
        if judged.drift is not None:
            first = None if target is None else self.first.get(target)
            repair = self.by_subject.get(judged.subject) or first
            said = _drift(judged.drift, _why_drifted(repair))
            cited = (
                None if repair is None else _cited(repair, self.redactor, bound=False)
            )
            return judged.ruled("inconclusive", self.redactor.redact(said), cited)
        inserting = self.inserted.get(judged.subject)
        if inserting is not None:
            cited = _cited(inserting, self.redactor, bound=False)
            unpassed = _INSERTED_NOT_PASSED
        elif target is not None and target in self.rebound:
            cited = _cited(self.first[target], self.redactor, bound=True)
            unpassed = _NOT_PASSED
        else:
            return judged.cited(None)
        if passed:
            return judged.ruled("drift_consistent", None, cited)
        if judged.subject.kind == "step" and judged.verdict == "pass":
            return judged.ruled("inconclusive", self.redactor.redact(unpassed), cited)
        return judged.cited(cited)


def _inserting(
    seq: int, final: CompiledScript, repairs: tuple[Repaired, ...]
) -> Repaired | None:
    """The repair that inserted step `seq` as `final` holds it: the first taken
    one whose script holds that step exactly, so a later repair that rewrote it
    is the one cited. None when no repair holds it, which a step that ran has
    no way to be, since a taken repair keeps what ran."""
    step = next((each for each in final.steps if each.seq == seq), None)
    return next(
        (
            repaired
            for repaired in repairs
            if repaired.script is not None and step in repaired.script.steps
        ),
        None,
    )


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
