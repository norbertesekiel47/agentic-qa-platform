"""An explore run's outcome and its documents (ADR-0024's and ADR-0026's
#53 P12 amendments).

`present` projects a run's results into `explore.json`, whose `egress`
member is the top-level `egress.json`; the CLI's line reads from that
document. It is not every way a run's results leave the process: tool
errors and turn records are `aqa_runner.reasons`', `confirmation/egress.json`
is replay's, and evidence is `aqa_runner.evidence`'s. It copies an
allow-list, field by field, never a result object or a reflection of one
(`dataclasses.asdict`, `vars`, `model_dump`), so a field a result gains
later stays in memory until this list names it.

The run's record decides what is withheld: its scan, its session's, binds
every test secret the spec references, filled or not, and when it binds
any, no host, port or cause leaves, only counts, the predicate replay and
`aqa_runner.reasons` share. Every reason copied is scanned again with that
scan, since a `Redacted` mark doesn't say which run's scan made it, and so
is every reason assembled here. A copied reason otherwise keeps what replay
gave it: in a run that binds nothing, a Playwright message's first line
(`redaction.error_text`).

`final_outcome` picks the outcome when several causes hold at once. Spec and
secret errors (5, 12) and a run record that can't be created (15) stop
before the egress proxy opens, so they never reach here.

Neither touches a browser, model, graph, file or clock: a record is read
for its ID, path, scan and the intents it holds unresolved in memory."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from aqa_runner.egress import InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks, PhaseTraffic
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import RunResult, StepResult
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.run_record import PARTS, PendingIntent, RunRecord

# How an explore run that opened its egress proxy ends, with its exit code
# (API.md §7). The order is the precedence, `final_outcome` taking the first
# that holds: a block, which the user must answer before anything else the
# run says means much; side effects whose outcome is unknown, which nothing
# may repeat (AGENTS.md §6); the infrastructure errors; a provider that gave
# no response; giving up; and compiling, only when nothing else holds.
type Outcome = Literal[
    "egress_blocked",
    "non_resumable",
    "no_sandbox",
    "reset_unsafe",
    "unreachable",
    "record_error",
    "no_response",
    "gave_up",
    "compiled",
]
EXIT_CODES: Final[Mapping[Outcome, int]] = {
    "egress_blocked": 6,
    "non_resumable": 4,
    "no_sandbox": 10,
    "reset_unsafe": 14,
    "unreachable": 13,
    "record_error": 15,
    "no_response": 11,
    "gave_up": 3,
    "compiled": 0,
}

# The causes the explore graph stops on itself. The others follow from what
# the run left: its egress blocks, unresolved intents, uncertain traffic and
# infrastructure events.
type Stoppage = Literal[
    "no_sandbox", "reset_unsafe", "record_error", "no_response", "gave_up"
]

# Whether a run published its compiled script, confirmed or not.
type Published = Literal["confirmed", "unconfirmed"]


@dataclass(frozen=True)
class Stop:
    """A cause the explore graph stopped on, and why: fixed text, or a
    `reasons.reason`, scanned with the run's redactor either way."""

    outcome: Stoppage
    reason: Redacted


@dataclass(frozen=True)
class Phase:
    """How one phase of the run ended: its part of the run's record, which
    holds the phase's unresolved intents (`RunRecord.unresolved`), what
    ending its listener found uncertain (`EgressProxy.end_phase`), and how
    many policy events its browser session recorded, None for a
    confirmation whose replay raised, which reports none."""

    record: RunRecord
    traffic: PhaseTraffic
    policy_events: int | None


@dataclass(frozen=True)
class Confirmation(Phase):
    """The confirmation's phase, and its replay's result when replay
    returned one."""

    result: RunResult | None


@dataclass(frozen=True)
class ExploreEnd:
    """What an explore run knows once its last phase has ended: its record,
    whose ID it has and whose scan, its session's, decides what is withheld;
    each phase, in its part of that record; the egress blocks and the gate's
    infrastructure events as the last phase's end left them, which supersede
    what replay read; each planned check by ID with its last evaluation in
    the attempt, None while never bound; every cause the graph stopped on;
    and whether it published a compiled script, confirmed or not.

    A phase whose record isn't this record's part of its name, made from
    this record, so holding the intents this record's run left unresolved
    there, and scanning with its redactor, is refused (`ValueError`), and
    so is a confirmation result of another run. Whether this record is the
    run's own, scanning with the redactor its session opens with, is its
    maker's to check: no session is here."""

    record: RunRecord
    attempt: Phase
    confirmation: Confirmation | None
    blocks: EgressBlocks
    infrastructure_events: tuple[InfrastructureEvent, ...]
    checks: Mapping[str, AssertionResult | None]
    stops: tuple[Stop, ...]
    published: Published | None

    def __post_init__(self) -> None:
        for part, phase in _phases(self):
            if (
                phase.record.path != self.record.path / part
                or phase.record.run_id != self.record.run_id
                or phase.record.redactor is not self.record.redactor
                or not self.record.shares_journals(phase.record)
            ):
                raise ValueError(
                    f"{part} isn't a part made from this run's record, scanning "
                    "with its redactor"
                )
        replayed = self.confirmation.result if self.confirmation else None
        if replayed is not None and replayed.run_id != self.record.run_id:
            raise ValueError("the confirmation's result is another run's")


def final_outcome(end: ExploreEnd) -> Outcome:
    """The outcome `end` exits with: of every cause that holds, the first
    in `EXIT_CODES`. An unresolved intent never lets the run compile; one
    with `side_effect: true` makes it non-resumable, and a read-only one is
    terminal but resumable, so its cause decides. A run that published
    nothing gives up unless something more decisive holds."""
    holding: set[Outcome] = {stop.outcome for stop in end.stops}
    unresolved = [intent for _, intent in _unresolved(end)]
    if end.blocks.blocked:
        holding.add("egress_blocked")
    if unresolved:
        side_effect = any(intent.side_effect for intent in unresolved)
        holding.add("non_resumable" if side_effect else "gave_up")
    if any(_uncertain(phase.traffic) for _, phase in _phases(end)):
        holding.add("reset_unsafe")
    if end.infrastructure_events:
        holding.add("unreachable")
    holding.add("gave_up" if end.published is None else "compiled")
    return next(outcome for outcome in EXIT_CODES if outcome in holding)


def present(end: ExploreEnd) -> dict[str, object]:
    """`end` as `explore.json` holds it, JSON-ready: the allow-list
    ADR-0026's #53 P12 amendment names. Its `egress` is `egress.json` as
    replay writes it (DATA_MODEL §7), None without a block. Every non-
    resumable intent is listed whatever the outcome."""
    outcome = final_outcome(end)
    redactor = end.record.redactor
    withheld = not redactor.empty
    events = end.infrastructure_events
    replayed = end.confirmation.result if end.confirmation else None
    return {
        "run_id": end.record.run_id,
        "outcome": outcome,
        "exit_code": EXIT_CODES[outcome],
        "withheld": withheld,
        "published": end.published,
        "reasons": list(_reasons(end, replayed)),
        "non_resumable": [
            {"part": part, "seq": intent.seq}
            for part, intent in _unresolved(end)
            if intent.side_effect
        ],
        "phases": [
            {
                "part": part,
                "plain_uncertain": phase.traffic.plain_uncertain,
                "tunnel_uncertain": phase.traffic.tunnel_uncertain,
                "policy_events": phase.policy_events,
            }
            for part, phase in _phases(end)
        ],
        "egress": _egress(end.blocks, withheld=withheld),
        "infrastructure_events": {
            "count": len(events),
            "events": None
            if withheld
            else [
                {"host": event.host, "port": event.port, "cause": event.cause}
                for event in events
            ],
        },
        "confirmation": None if replayed is None else _replayed(replayed, redactor),
    }


def _phases(end: ExploreEnd) -> list[tuple[str, Phase]]:
    attempt, confirmation = PARTS
    phases: list[tuple[str, Phase]] = [(attempt, end.attempt)]
    if end.confirmation is not None:
        phases.append((confirmation, end.confirmation))
    return phases


def _unresolved(end: ExploreEnd) -> list[tuple[str, PendingIntent]]:
    return [
        (part, intent)
        for part, phase in _phases(end)
        for intent in phase.record.unresolved()
    ]


def _uncertain(traffic: PhaseTraffic) -> bool:
    return bool(traffic.plain_uncertain or traffic.tunnel_uncertain)


def _egress(blocks: EgressBlocks, *, withheld: bool) -> dict[str, object] | None:
    if not blocks.blocked:
        return None
    if withheld:
        return {
            "error_code": "egress_blocked",
            "refused_count": len(blocks.refused),
            "overflowed": blocks.overflowed,
            "hosts_and_ports_withheld": True,
        }
    return {
        "error_code": "egress_blocked",
        "refused": [{"host": host.host, "port": host.port} for host in blocks.refused],
        "overflowed": blocks.overflowed,
        "hosts_and_ports_withheld": False,
    }


def _replayed(result: RunResult, redactor: Redactor) -> dict[str, object]:
    """The confirmation's outcome, and each step's, assertion's and
    invariant's, in outcome words, each reason scanned again: never a
    settle window, a policy event, an infrastructure event, an egress block
    or what an invariant saw."""
    return {
        "outcome": result.outcome,
        "error_code": result.error_code,
        "steps": [
            {
                "seq": step.seq,
                "outcome": step.outcome,
                "locator_index": step.locator_index,
                "settled": step.settled,
                "misses": list(step.misses),
                "reason": _rescanned(step.error, redactor),
            }
            for step in result.steps
        ],
        "assertions": [
            {
                "id": assertion.id,
                "outcome": assertion.outcome,
                "misses": list(assertion.misses),
                "stopped_at": assertion.stopped_at,
                "reason": _rescanned(assertion.error, redactor),
            }
            for assertion in result.assertions
        ],
        "invariants": [
            {
                "name": invariant.name,
                "outcome": invariant.outcome,
                "total": invariant.total,
            }
            for invariant in result.invariants
        ],
    }


def _rescanned(reason: str | None, redactor: Redactor) -> Redacted | None:
    return None if reason is None else redactor.redact(reason)


def _reasons(end: ExploreEnd, replayed: RunResult | None) -> Iterator[Redacted]:
    """Why the run ended as it did, in the executor's outcome words:
    each stop's reason, scanned again; each planned check that never passed
    in the attempt; and each step, assertion and invariant of the
    confirmation that didn't. Each assembly is scanned whole, so a value
    spanning a join is found, as `redaction.error_text` scans its own."""
    scan = end.record.redactor.redact
    yield from (scan(stop.reason) for stop in end.stops)
    for check, result in end.checks.items():
        if result is None:
            yield scan(f"{check}: never bound")
        elif result.outcome != "pass":
            yield scan(f"{check}: {assertion_words(result)}")
    if replayed is not None:
        yield from map(scan, _confirmation_words(replayed))


def _confirmation_words(replayed: RunResult) -> Iterator[str]:
    for step in replayed.steps:
        if step.outcome != "completed":
            yield f"confirmation step {step.seq}: {_step_words(step)}"
    for assertion in replayed.assertions:
        if assertion.outcome != "pass":
            yield f"confirmation {assertion.id}: {assertion_words(assertion)}"
    for invariant in replayed.invariants:
        if invariant.outcome == "violated":
            yield f"confirmation invariant {invariant.name}: violated"


def assertion_words(result: AssertionResult) -> str:
    if result.outcome == "binding_unresolved":
        return f"binding_unresolved ({', '.join(result.misses)})"
    if result.outcome == "not_evaluated" and result.error is not None:
        return f"not_evaluated: {result.error}"
    if result.outcome == "not_evaluated":
        return f"not_evaluated: step {result.stopped_at} stopped the run"
    return result.outcome


def _step_words(step: StepResult) -> str:
    if step.outcome == "drifted":
        return f"drifted ({', '.join(step.misses)})"
    return step.outcome if step.error is None else f"{step.outcome}: {step.error}"
