"""Prove the read-article fixture from a pilot report (#51, ADR-0023): three
equal clean passes, then exactly the issue's outcome under each Conduit dev flag.

Run: uv run python bench/harness/pilot_read_article.py REPORT --report PROOF
"""

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import flags
import manifest
import pilot_report as report
from aqa_core.coverage_plan import CoveragePlan, misfits, plan_hash
from aqa_core.project import SpecError, load_project
from aqa_core.schema import ListOf, StrictModel
from aqa_core.spec import InvariantName, canonical_hash
from aqa_runner.invariants import INVARIANTS
from manifest import Case, _reject_duplicates
from pilot import git_source
from pilot_inputs import PilotInput, load_pilots
from pilot_replay import AttemptHealth, ObservedAttempt
from pilot_results import AssertionObservation as Checked
from pilot_results import InvariantObservation, Observation
from pilot_results import StepObservation as Stepped

SPEC, FIXTURES = "read-article", Path("bench/harness/fixtures")
type Want = tuple[frozenset[int], frozenset[InvariantName]]
NONE: Want = frozenset(), frozenset()
# The issue's outcomes (#51); every other dev case passes. Read-article rows in
# the manifest that say otherwise are a new contract, which the proof refuses.
OUTCOMES: dict[str, Want] = {
    "conduit-bug-001": (frozenset({2}), frozenset()),
    "conduit-bug-002": (frozenset(), frozenset({"js_exceptions"})),
}
HEALTHY = AttemptHealth(
    reset_completed=True, infrastructure=False, egress_blocked=False
)
# What reading a report or the source can raise; their text can quote either.
FAILURES = (OSError, ValueError, RecursionError, SpecError, manifest.ManifestError)


# What the proof reads from a report: each pair's run IDs and invariant counts.
class _Run(StrictModel, extra="ignore"):
    run_id: str
    invariants: ListOf[InvariantObservation] = ()


class _Pair(StrictModel, extra="ignore"):
    case: str | None
    spec: str
    attempts: ListOf[_Run]


class _Report(StrictModel, extra="ignore"):
    pairs: ListOf[_Pair]


def _attempt(pilot: PilotInput, want: Want, run: _Run) -> ObservedAttempt:
    """The attempt that proves `want`, with `run`'s ID and counts: healthy and
    eligible, each step settled on its first locator, failing exactly `want`."""
    script, (failed, violated) = pilot.script, want
    settings = pilot.spec.frontmatter.invariants
    off = set(settings.disable) if settings.inherit else set(INVARIANTS)
    counts = {i.name: i.total for i in run.invariants if i.name in violated | off}
    seqs = (0, *(step.seq for step in script.steps))
    steps = tuple(
        Stepped(n, "completed", "idle", None if n == 0 else 0, (), False) for n in seqs
    )
    checks = tuple(
        Checked(a.id, "failed" if a.expect_index in failed else "pass", (), None, False)
        for a in script.assertions
    )
    invariants = tuple(
        InvariantObservation(
            name,
            "disabled" if name in off else "violated" if name in violated else "held",
            counts.get(name, 0),
        )
        for name in INVARIANTS
    )
    seen = Observation(
        SPEC,
        "failed" if any(want) else "passed",
        None,
        steps,
        checks,
        invariants,
        *want,
        True,
    )
    return ObservedAttempt(run.run_id, Path(), seen, HEALTHY)


def _current(
    root: Path,
) -> tuple[PilotInput, tuple[str, ...], dict[str, str], list[Case]]:
    """The fixture as the pilot command admits it, with its plan checked, every
    other QA spec ID, the report's input hashes and the current Conduit dev cases."""
    qa = root / manifest.APPS / "conduit" / "qa"
    origin = flags.stack_for("conduit").host_origin
    (pilot,) = load_pilots(qa, root / FIXTURES, (SPEC,), origin=origin)
    plan = CoveragePlan.model_validate_json(
        (root / FIXTURES / f"{SPEC}.plan.json").read_bytes()
    )
    if plan_hash(plan) != pilot.script.coverage.plan_hash or misfits(
        plan, pilot.spec.frontmatter
    ):
        raise ValueError("the plan isn't the fixture's")
    hashes = {"manifest": _sha256((root / manifest.MANIFEST).read_bytes())}
    hashes[f"spec:{SPEC}"] = pilot.spec.spec_hash
    hashes[f"script:{SPEC}"] = canonical_hash(pilot.script.model_dump(mode="json"))
    omitted = tuple(sorted(load_project(qa).specs.keys() - {SPEC}))
    cases = sorted(manifest.load(root).cases.items())
    dev = [c for _, c in cases if c.app == "conduit" and c.split == "dev"]
    return pilot, omitted, hashes, dev


def prove_read_article(report_path: Path, *, root: Path) -> Mapping[str, object]:
    """The proof receipt for the pilot report at `report_path`, judged against
    `root`'s source as it is now. In canonical JSON, the report must be the one
    a proving run writes, but for its run IDs and violated invariants' counts.
    An unreadable report or source raises."""
    now = git_source(root, root.parent)
    pilot, omitted, hashes, cases = _current(root)
    raw = report_path.read_bytes()
    text = raw.decode()
    data = json.loads(
        text,
        object_pairs_hook=_reject_duplicates,
        parse_constant=_refuse,
        parse_float=_refuse,
    )
    listed = _Report.model_validate_json(text).pairs
    runs = {(p.case, p.spec): p.attempts for p in listed}
    ids = Counter(r.run_id for p in listed for r in p.attempts)
    shown = {(p["case"], p["spec"]): p for p in data["pairs"]}
    rows = {c.id: e for c in cases for e in c.expected if e.spec == SPEC}
    found = {
        k: (e.verdict, frozenset(e.expect), frozenset(e.invariants))
        for k, e in rows.items()
    }
    problems = (
        []
        if found == {k: (manifest.VIOLATED, *w) for k, w in OUTCOMES.items()}
        else ["contract"]
    )
    wants = {None: NONE} | {c.id: OUTCOMES.get(c.id, NONE) for c in cases}
    if len(runs) != len(data["pairs"]) or runs.keys() != {(k, SPEC) for k in wants}:
        problems.append("inventory")
    settings, pairs = pilot.spec.frontmatter.invariants, []
    for key, want in wants.items():
        attempts = [_attempt(pilot, want, run) for run in runs.get((key, SPEC), ())]
        if key is None:
            pairs.append(report.judge_clean(SPEC, attempts, settings))
        elif len(attempts) == 1:
            pairs.append(
                report.judge_case(
                    key, SPEC, rows.get(key), *attempts, settings=settings
                )
            )
    proving = report.Report(
        "conduit",
        now.commit,
        now.tree,
        3,
        (SPEC,),
        omitted,
        hashes,
        tuple(pairs),
        unchanged=True,
        switched_back=True,
    )
    doc = report.document(proving, {SPEC: pilot.script})
    rendered: Any = doc["pairs"]
    built = {(p["case"], p["spec"]): p for p in rendered}
    verdicts = {}
    for key, (_, violated) in wants.items():
        given = runs.get((key, SPEC), ())
        counted = all(
            i.total > 0 for r in given for i in r.invariants if i.name in violated
        )
        pair = (key, SPEC)
        alike = pair in built and pair in shown and _same(built[pair], shown[pair])
        # Each attempt is a run of its own, so no two may report the same ID.
        complete = len(given) == (3 if key is None else 1) and all(
            ids[r.run_id] == 1 for r in given
        )
        verdicts[key or "clean"] = (
            "passed" if counted and alike and complete else "failed"
        )
    differ = [
        k for k in doc if k != "pairs" and (k not in data or not _same(doc[k], data[k]))
    ]
    problems += [f"report.{k}" for k in differ]
    problems += ["report.extra"] if data.keys() - doc.keys() else []
    if now.dirty or git_source(root, root.parent) != now:
        problems.append("source")
    return {
        "spec": SPEC,
        "source": {"commit": now.commit, "tree": now.tree},
        "report": _sha256(raw),
        "hashes": hashes | {f"plan:{SPEC}": pilot.script.coverage.plan_hash},
        "cases": verdicts,
        "problems": problems,
        "proven": not problems and set(verdicts.values()) == {"passed"},
    }


def _same(built: object, shown: object) -> bool:
    return canonical_hash(built) == canonical_hash(shown)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _refuse(_text: str) -> object:
    raise ValueError("a report holds integers only")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", metavar="REPORT", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=manifest.REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        receipt = prove_read_article(args.source, root=args.root)
    except (*FAILURES, subprocess.SubprocessError):
        print(
            "error: the report or the fixture's inputs can't be read", file=sys.stderr
        )
        return 2
    try:
        report.write_once(args.report, receipt)
    except OSError:
        print("error: the proof can't be written as a new file", file=sys.stderr)
        return 2
    print("proven" if receipt["proven"] else "not proven: see the proof's problems")
    return 0 if receipt["proven"] else 1


if __name__ == "__main__":
    sys.exit(main())
