"""Replay compiled pilots on the clean app and under each dev case's flag, one
attempt at a time, admitting every input first (ADR-0023, bench/README.md).

Run: uv run python bench/harness/pilot.py conduit --out DIR [--spec ID ...]
"""

import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import NamedTuple, cast

import flags
import manifest
import pilot_report as report
from aqa_core.project import SpecError, load_project
from aqa_core.spec import canonical_hash
from manifest import DRIFT, Case
from pilot_inputs import PilotInput, UnusableSecretError, load_pilots, validate_pilot
from pilot_rebinding import apply_rebinding
from pilot_replay import replay_pilot
from pilot_report import Attempt, Halt, Pair, Report, Role

type Replay = Callable[[PilotInput, Path], Awaitable[Attempt]]


class Source(NamedTuple):
    """HEAD's commit and tree, and whether the working tree differs from them."""

    commit: str
    tree: str
    dirty: bool


type ReadSource = Callable[[Path, Path], Source]
# What reading the source or the evidence directory can raise.
FAILURES = (OSError, subprocess.SubprocessError)


@dataclass(eq=False)
class HaltError(Exception):
    halt: Halt


@contextmanager
def _halting(halt: Halt) -> Iterator[None]:
    """Any error but a HaltError, as HaltError(`halt`), keeping no error text."""
    try:
        yield
    except HaltError:
        raise
    except Exception as error:
        raise HaltError(halt) from error


def git_source(root: Path, out: Path) -> Source:
    """The Source at `root`, untracked files counted, except under `out`. Git
    runs without inherited GIT_* variables: GIT_DIR would outrank `root`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True, env=env)

    here, there = root.resolve(), out.resolve()
    inside = there.is_relative_to(here)
    outside = [f":(exclude,literal){there.relative_to(here)}"] if inside else []
    status = git("status", "--porcelain", "--untracked-files=all", "--", ".", *outside)
    commit, tree = git("rev-parse", "HEAD", "HEAD^{tree}").split()
    return Source(commit, tree, bool(status))


@dataclass
class Run:
    """One command's run: `execute` changes the app, `finish` writes the report."""

    root: Path
    out: Path
    base: Report
    pilots: tuple[PilotInput, ...]
    cases: tuple[Case, ...]
    patches: Mapping[tuple[str, str], PilotInput]
    start: Source
    docker: flags.Docker
    replay: Replay
    source: ReadSource
    pairs: list[Pair] = field(default_factory=list)
    halt: Halt | None = None
    receipts: int = 0
    code: int | None = None

    async def execute(self) -> int:
        """The clean app, then each dev case, up to a fatal pair or an error; the
        report is written in the loop, so a hung shutdown can't lose it."""
        try:
            with _halting("unexpected"):
                await self._pairs()
        except HaltError as halted:
            self.halt = halted.halt
        return self.finish()

    async def _pairs(self) -> None:
        self._switch(build=True)
        for pilot in self.pilots:
            attempts: list[Attempt] = []
            settings = pilot.spec.frontmatter.invariants
            for _ in range(self.base.repeat):
                attempts.append(await self._attempt(pilot, None, "clean"))
                if report.classify(attempts[-1], settings) == "fatal":
                    break
            if self._kept(report.judge_clean(_id(pilot), attempts, settings)):
                return
        for case in self.cases:
            self._switch(case.flag)
            for pilot in self.pilots:
                if self._kept(await self._judged(case, pilot)):
                    return

    async def _judged(self, case: Case, pilot: PilotInput) -> Pair:
        spec, settings = _id(pilot), pilot.spec.frontmatter.invariants
        row = next((e for e in case.expected if e.spec == spec), None)
        patch = self.patches.get((case.id, spec))
        drift = row is not None and row.verdict == DRIFT
        role: Role = "diagnostic" if drift else "unscored" if row is None else "scored"
        first, patched = await self._attempt(pilot, case.id, role), None
        if row and drift and patch and report.admits_patch(first, row, settings):
            patched = await self._attempt(patch, case.id, "acceptance")
        return report.judge_case(case.id, spec, row, first, patched, settings=settings)

    async def _attempt(
        self, pilot: PilotInput, case: str | None, role: Role
    ) -> Attempt:
        """One attempt, its receipt saved at once so a later halt can't lose it."""
        try:
            attempt = await self.replay(pilot, self.out)
        except asyncio.CancelledError as error:
            raise HaltError("interrupted") from error
        kind = report.classify(attempt, pilot.spec.frontmatter.invariants)
        self.receipts += 1
        path = self.out / "attempts" / f"{self.receipts:03}.json"
        entry = report.Entry(role, attempt, kind)
        report.write_once(path, report.receipt(case, _id(pilot), entry, pilot.script))
        return attempt

    def _switch(self, *flag_ids: str, build: bool = False) -> flags.Switch:
        with _halting("switch_failed"):  # a switch failing part-way leaves it unknown
            if build:
                flags.build(self.root, self.base.app, self.docker)
            return flags.switch(self.root, self.base.app, flag_ids, self.docker)

    def _kept(self, pair: Pair) -> bool:
        """Keep `pair`; whether it stops the run."""
        self.pairs.append(pair)
        return pair.status == "fatal"

    def finish(self) -> int:
        """Read the source before and after any switch back, then write and print
        the report; an error halts it (`unexpected`), twice is main's exit 3."""
        if self.code is not None:
            return self.code
        read = partial(self.source, self.root, self.out)
        unchanged = self._held(read) == self.start
        released = report.releasable(self.pairs, self.halt)
        switched = released and self._held(self._switch) is not None
        unchanged = unchanged and self._held(read) == self.start
        write = partial(self._write, unchanged, switched)
        doc = self._held(write) or write()
        for pair in self.pairs:
            print(f"{pair.case or 'clean'}  {pair.spec}  {pair.status}")
        self.code = cast(int, doc["exit"])
        return self.code

    def _held[T](self, call: Callable[[], T]) -> T | None:
        """What `call` returns, or None once its error halts the run."""
        try:
            with _halting("unexpected"):
                return call()
        except HaltError as halted:
            self.halt = self.halt or halted.halt
            return None

    def _write(self, unchanged: bool, switched: bool) -> report.Json:
        """The report, with the run's halt, written once as `report.json`."""
        ended = replace(self.base, unchanged=unchanged, pairs=tuple(self.pairs))
        ended = replace(ended, halt=self.halt, switched_back=switched)
        doc = report.document(ended, {_id(p): p.script for p in self.pilots})
        report.write_once(self.out / "report.json", doc)
        return doc


def _id(pilot: PilotInput) -> str:
    return pilot.spec.frontmatter.id


def _prepare(
    args: argparse.Namespace, docker: flags.Docker, replay: Replay, source: ReadSource
) -> Run:
    """Every input admitted before anything changes, or a fixed ValueError."""
    root, app, out, folder = args.root, args.app, args.out, args.patches
    start = source(root, out)
    if start.dirty:
        raise ValueError(f"{root}: uncommitted changes, so no report could cite it")
    if out.exists() or args.repeat < 1:
        raise ValueError(f"{out}: exists" if out.exists() else "--repeat below 1")
    flags.stack_for(app)
    loaded = sorted(manifest.load(root).cases.items())
    cases = tuple(c for _, c in loaded if c.app == app and c.split == "dev")
    qa = root / manifest.APPS / app / "qa"
    pilots = load_pilots(qa, args.compiled_dir or qa / ".compiled", args.spec)
    patches = _patches(folder, cases, pilots)
    hashes = {"manifest": _sha256(root / manifest.MANIFEST)}
    for pilot in pilots:
        spec = _id(pilot)
        hashes[f"spec:{spec}"] = pilot.spec.spec_hash
        hashes[f"script:{spec}"] = canonical_hash(pilot.script.model_dump(mode="json"))
    hashes |= {f"patch:{c}/{s}": _sha256(folder / f"{c}.{s}.json") for c, s in patches}
    selected = tuple(_id(p) for p in pilots)
    omitted = tuple(sorted(_spec_ids(qa).difference(selected)))
    base = Report(app, start.commit, start.tree, args.repeat, selected, omitted, hashes)
    (out / "attempts").mkdir(parents=True)
    return Run(root, out, base, pilots, cases, patches, start, docker, replay, source)


def _spec_ids(qa: Path) -> set[str]:
    """Every spec ID in the QA project, or a ValueError without the read's error."""
    with suppress(OSError, SpecError):
        return set(load_project(qa).specs)
    raise ValueError(f"{qa}: invalid pilot input")


def _patches(
    directory: Path | None, cases: Sequence[Case], pilots: Sequence[PilotInput]
) -> dict[tuple[str, str], PilotInput]:
    """Each file in `directory`, named `<case>.<spec>.json` for a selected
    spec's drift_consistent row, applied in memory, then admitted."""
    if directory is None:
        return {}
    by_id = {_id(p): p for p in pilots}
    rows = {
        f"{c.id}.{e.spec}.json": (c.id, e.spec)
        for c in cases
        for e in c.expected
        if e.verdict == DRIFT and e.spec in by_id
    }
    patches = {}
    for path in sorted(directory.iterdir()):
        if path.name not in rows:
            raise ValueError(f"{path}: not a patch for a selected drift_consistent row")
        key = rows[path.name]
        pilot = by_id[key[1]]
        try:
            text = path.read_text()
            candidate = apply_rebinding(pilot.script, text, pilot.config, source=path)
        except (OSError, ValueError, SpecError, RecursionError):
            candidate = None
        # Raised outside the handler, so the patch's own error isn't kept.
        if candidate is None:
            raise ValueError(f"{path}: invalid patch")
        patches[key] = validate_pilot(pilot.spec, pilot.config, candidate, source=path)
    return patches


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(
    argv: Sequence[str] | None = None,
    docker: flags.Docker | None = None,
    replay: Replay = replay_pilot,
    source: ReadSource = git_source,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=manifest.REPO_ROOT)
    parser.add_argument("app")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--spec", action="append", default=[])
    parser.add_argument("--compiled-dir", type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--patches", type=Path)
    args = parser.parse_args(argv)
    try:
        run = _prepare(args, docker or flags.LocalDocker(), replay, source)
    except (ValueError, flags.FlagError, manifest.ManifestError, *FAILURES) as error:
        # Ours name a path and a fixed category; the others can quote input.
        shown = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"error: {shown}", file=sys.stderr)
        return 12 if isinstance(error, UnusableSecretError) else 2
    try:
        with _halting("unexpected"):  # evidence that can't be written
            try:
                return asyncio.run(run.execute())
            except KeyboardInterrupt:
                run.halt = "interrupted"
            except SystemExit:
                run.halt = "system_exit"
            return run.finish()
    except HaltError:
        print(f"error: the run under {run.out} couldn't be completed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
