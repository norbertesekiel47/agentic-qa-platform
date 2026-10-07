"""The pilot acceptance report (ADR-0023, #51): each attempt sorted by what
its result admits, each case-and-spec pair judged against its manifest row or
left unscored, all written as outcomes, counts, IDs and hashes, never text."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from aqa_core.compiled import CompiledScript
from aqa_core.spec import Invariants
from aqa_runner.invariants import INVARIANTS
from manifest import DRIFT, Expected
from pilot_replay import FailedAttempt, ObservedAttempt
from pilot_results import Observation, compare

type Attempt = ObservedAttempt | FailedAttempt
# What an attempt's result admits: scoring; only a binding repair (a drift
# shape, `_binding_only`); neither, as with a real failure beside the drift;
# or nothing, since something unsafe or unknown happened (any FailedAttempt).
type Kind = Literal["eligible", "binding_only", "ineligible", "fatal"]
type Role = Literal["clean", "scored", "diagnostic", "acceptance", "unscored"]
type Judged = Literal["passed", "matched", "accepted_pending_C", "mismatch", "fatal"]
type Status = Judged | Literal["patch_missing", "unscored", "unscored_binding_blocked"]
# Why the command stopped short when no attempt's result says so.
type Halt = Literal["interrupted", "unexpected", "system_exit", "switch_failed"]
type Json = dict[str, object]


class Entry(NamedTuple):
    role: Role
    attempt: Attempt
    kind: Kind


@dataclass(frozen=True)
class Pair:
    """One spec on the clean app (`case` None) or under one case's flag."""

    case: str | None
    spec: str
    expected: Expected | None
    attempts: tuple[Entry, ...]
    status: Status


@dataclass(frozen=True)
class Report:
    """A command's run: its source, inputs, pairs and how it ended."""

    app: str
    commit: str
    tree: str
    unchanged: bool
    repeat: int
    selected: tuple[str, ...]
    hashes: Mapping[str, str]
    pairs: tuple[Pair, ...]
    halt: Halt | None
    switched_back: bool


def classify(attempt: Attempt, settings: Invariants) -> Kind:
    """What `attempt` admits under its spec's invariant `settings`."""
    if isinstance(attempt, FailedAttempt):
        return "fatal"
    seen, health = attempt.observation, attempt.health
    if seen.eligible:
        return "eligible"
    if (
        health.infrastructure
        or health.egress_blocked
        or seen.outcome == "errored"
        or any(s.outcome == "completed" and s.settled != "idle" for s in seen.steps)
        or any(a.outcome == "check_timed_out" for a in seen.assertions)
        or not _agrees(seen, settings)
    ):
        return "fatal"
    return "binding_only" if _binding_only(seen) else "ineligible"


def _agrees(seen: Observation, settings: Invariants) -> bool:
    """Whether each invariant's outcome follows from its count and the
    spec's settings, as A2's eligibility requires."""
    off = set(settings.disable) if settings.inherit else set(INVARIANTS)
    return all(
        i.outcome
        == ("disabled" if i.name in off else "violated" if i.total else "held")
        for i in seen.invariants
    )


def _binding_only(seen: Observation) -> bool:
    """No invariant violated, every earlier step completed, and then: each
    check passed or found no binding, one at least; or the run stopped at a
    drifted step, which dispatched nothing, before any check was evaluated."""
    *before, last = seen.steps
    if seen.violated_invariants or any(s.outcome != "completed" for s in before):
        return False
    if last.outcome == "drifted":
        return all(
            a.outcome == "not_evaluated" and a.stopped_at == last.seq
            for a in seen.assertions
        )
    return {a.outcome for a in seen.assertions} - {"pass"} == {"binding_unresolved"}


def _matches(attempt: Attempt, row: Expected) -> bool:
    return isinstance(attempt, ObservedAttempt) and compare(attempt.observation, row)


def admits_patch(original: Attempt, row: Expected, settings: Invariants) -> bool:
    """Whether a `drift_consistent` pair's patched attempt may run: its
    original passed as the row says, or only its bindings failed."""
    kind = classify(original, settings)
    return kind == "binding_only" or (kind == "eligible" and _matches(original, row))


def judge_clean(spec: str, attempts: Sequence[Attempt], settings: Invariants) -> Pair:
    """The clean app's repeats of `spec`: passed when every attempt is an
    eligible pass and all of them are equal."""
    entries = tuple(Entry("clean", a, classify(a, settings)) for a in attempts)
    seen = [a.observation for a in attempts if isinstance(a, ObservedAttempt)]
    passed = all(o.eligible and o.outcome == "passed" and o == seen[0] for o in seen)
    fatal = any(e.kind == "fatal" for e in entries)
    status: Status = "fatal" if fatal else "passed" if passed else "mismatch"
    return Pair(None, spec, None, entries, status)


def judge_case(
    case: str,
    spec: str,
    expected: Expected | None,
    original: Attempt,
    patched: Attempt | None = None,
    *,
    settings: Invariants,
) -> Pair:
    """`spec` under `case`'s flag. An omitted pair is unscored; a
    `drift_consistent` row accepts only a patched attempt that matches."""
    kind = classify(original, settings)
    if expected is None:
        blocked = kind == "binding_only"
        status: Status = "unscored_binding_blocked" if blocked else "unscored"
        entries: tuple[Entry, ...] = (Entry("unscored", original, kind),)
    elif expected.verdict != DRIFT:
        status = "matched" if _matches(original, expected) else "mismatch"
        entries = (Entry("scored", original, kind),)
    else:
        entries = (Entry("diagnostic", original, kind),)
        if patched is not None:
            entries += (Entry("acceptance", patched, classify(patched, settings)),)
        status = _accepted(entries, expected, settings)
    fatal = any(e.kind == "fatal" for e in entries)
    return Pair(case, spec, expected, entries, "fatal" if fatal else status)


def _accepted(legs: tuple[Entry, ...], row: Expected, settings: Invariants) -> Status:
    original, *patched = legs
    if not admits_patch(original.attempt, row, settings):
        return "mismatch"
    if not patched:
        return "patch_missing"
    return "accepted_pending_C" if _matches(patched[0].attempt, row) else "mismatch"


def _failed(pairs: Sequence[Pair]) -> list[FailedAttempt]:
    attempts = [e.attempt for p in pairs for e in p.attempts]
    return [a for a in attempts if isinstance(a, FailedAttempt)]


def releasable(pairs: Sequence[Pair], halt: Halt | None) -> bool:
    """Whether the run ended with every resource known closed, so the app may be released."""
    return halt is None and all(a.resources == "closed" for a in _failed(pairs))


def _exit_code(report: Report) -> int:
    """0 accepted, 1 a scored disagreement, 3 an operational failure, 12 a
    test secret that can't be used, 130 interrupted (bench/README.md)."""
    statuses = {p.status for p in report.pairs}
    if report.halt == "interrupted":
        return 130
    if any(a.failure == "secret_unusable" for a in _failed(report.pairs)):
        return 12
    if report.halt or not report.unchanged or "fatal" in statuses:
        return 3
    return 1 if statuses & {"mismatch", "patch_missing"} else 0


def document(report: Report, scripts: Mapping[str, CompiledScript]) -> Json:
    """`report` as the JSON the command writes; `scripts` name each step's
    and check's target. C, the assertions' locator provenance, is pending."""
    release = releasable(report.pairs, report.halt)
    return {
        "app": report.app,
        "source": {"commit": report.commit, "tree": report.tree},
        "source_unchanged": report.unchanged,
        "repeat": report.repeat,
        "diagnostic": report.repeat != 3,
        "selected": list(report.selected),
        "hashes": dict(report.hashes),
        "assertion_provenance": "pending",
        "pairs": [_pair(p, scripts[p.spec]) for p in report.pairs],
        "halt": report.halt,
        "reservation_release": "allowed" if release else "forbidden",
        "switched_back_clean": report.switched_back,
        "exit": _exit_code(report),
    }


def receipt(pair: Pair, entry: Entry, script: CompiledScript) -> Json:
    """One attempt with its case and spec, as the command saves it at once."""
    return {"case": pair.case, "spec": pair.spec} | _entry(entry, script)


def write_once(path: Path, document: Mapping[str, object]) -> None:
    """Write `document` to a new `path`; a document that can't be made
    into JSON leaves no file."""
    text = json.dumps(document, indent=2, sort_keys=True)
    with path.open("x", encoding="utf-8") as file:
        file.write(text + "\n")


def _pair(pair: Pair, script: CompiledScript) -> Json:
    row = pair.expected
    scored = [e.attempt for e in pair.attempts if e.role in ("scored", "acceptance")]
    differences: Json = {"missing": None, "unexpected": None}
    if row is not None and scored and isinstance(scored[0], ObservedAttempt):
        seen = scored[0].observation
        wanted = (frozenset(row.expect), frozenset(row.invariants))
        found = (seen.failed_expectations, seen.violated_invariants)
        differences = {
            "missing": _sets(wanted[0] - found[0], wanted[1] - found[1]),
            "unexpected": _sets(found[0] - wanted[0], found[1] - wanted[1]),
        }
    return {
        "case": pair.case,
        "spec": pair.spec,
        "expected": None if row is None else asdict(row),
        "status": pair.status,
        "attempts": [_entry(e, script) for e in pair.attempts],
    } | differences


def _sets(expect: frozenset[int], invariants: frozenset[str]) -> Json:
    return {"expect": sorted(expect), "invariants": sorted(invariants)}


def _entry(entry: Entry, script: CompiledScript) -> Json:
    attempt = entry.attempt
    head: Json = {"role": entry.role, "run_id": attempt.run_id, "kind": entry.kind}
    if isinstance(attempt, FailedAttempt):
        ended = {"failure": attempt.failure, "cleanup": attempt.cleanup}
        return head | ended | {"resources": attempt.resources}
    steps = {s.seq: s.model_dump().get("target") for s in script.steps}
    checks = {a.id: a.model_dump().get("target") for a in script.assertions}
    seen = attempt.observation
    return head | {
        "outcome": seen.outcome,
        "error_code": seen.error_code,
        "eligible": seen.eligible,
        "failed_expectations": sorted(seen.failed_expectations),
        "violated_invariants": sorted(seen.violated_invariants),
        "steps": [s._asdict() | {"target": steps.get(s.seq)} for s in seen.steps],
        "assertions": [a._asdict() | {"target": checks[a.id]} for a in seen.assertions],
        "invariants": [i._asdict() for i in seen.invariants],
        "health": asdict(attempt.health),
    }
