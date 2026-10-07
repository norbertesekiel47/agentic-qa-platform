"""Replay compiled pilots on the clean app and under each dev case's flag,
one attempt at a time, and write the acceptance report (ADR-0023, #51).
Every input is admitted before the first Docker call (bench/README.md).

Run: uv run python bench/harness/pilot.py conduit --out DIR [--spec ID ...]
"""

import argparse
import asyncio
import hashlib
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import flags
import manifest
import pilot_report as report
from aqa_core.project import SpecError
from aqa_core.spec import canonical_hash
from manifest import DRIFT, Case
from pilot_inputs import PilotInput, UnusableSecretError, load_pilots, validate_pilot
from pilot_rebinding import apply_rebinding
from pilot_replay import replay_pilot
from pilot_report import Attempt, Halt, Pair, Report

type Replay = Callable[[PilotInput, Path], Awaitable[Attempt]]


# HEAD's commit and tree, and whether the working tree differs from them.
type Source = tuple[str, str, bool]
type ReadSource = Callable[[Path, Path], Source]


@dataclass
class HaltError(Exception):
    halt: Halt


def git_source(root: Path, out: Path) -> Source:
    """The Source at `root`, untracked files counted, except under `out`."""

    def git(*args: str) -> str:
        command = ["git", "-C", str(root), *args]
        return subprocess.run(
            command, capture_output=True, text=True, check=True
        ).stdout

    here, there = root.resolve(), out.resolve()
    inside = there.is_relative_to(here)
    outside = [f":(exclude){there.relative_to(here)}"] if inside else []
    status = git("status", "--porcelain", "--untracked-files=all", "--", ".", *outside)
    commit, tree = git("rev-parse", "HEAD", "HEAD^{tree}").split()
    return commit, tree, bool(status)


@dataclass
class Run:
    """One command's run: `all` changes the app, `finish` writes the report."""

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

    async def all(self) -> int:
        """The clean app, then each dev case, up to the first fatal pair; the
        report is written inside the loop, so a hung shutdown can't lose it."""
        try:
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
                attempts.append(await self._attempt(pilot))
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
        first, patched = await self._attempt(pilot), None
        drift = row is not None and row.verdict == DRIFT
        if row and drift and patch and report.admits_patch(first, row, settings):
            patched = await self._attempt(patch)
        return report.judge_case(case.id, spec, row, first, patched, settings=settings)

    async def _attempt(self, pilot: PilotInput) -> Attempt:
        try:
            return await self.replay(pilot, self.out)
        except asyncio.CancelledError as error:
            raise HaltError("interrupted") from error
        except Exception as error:
            # Whatever replay_pilot couldn't classify: its browser may be open.
            raise HaltError("unexpected") from error

    def _switch(self, *flag_ids: str, build: bool = False) -> None:
        app = self.base.app
        try:
            if build:
                flags.build(self.root, app, self.docker)
            flags.switch(self.root, app, flag_ids, self.docker)
        except flags.FlagError as error:
            print(f"error: {error}", file=sys.stderr)
            raise HaltError("switch_failed") from error

    def _kept(self, pair: Pair) -> bool:
        """Keep `pair` and save its receipts; whether it stops the run."""
        self.pairs.append(pair)
        script = next(p.script for p in self.pilots if _id(p) == pair.spec)
        for entry in pair.attempts:
            self.receipts += 1
            path = self.out / "attempts" / f"{self.receipts:03}.json"
            report.write_once(path, report.receipt(pair, entry, script))
        return pair.status == "fatal"

    def finish(self) -> int:
        """Switch back if every resource closed, write the report, print it."""
        if self.code is not None:
            return self.code
        switched = report.releasable(self.pairs, self.halt)
        if switched:
            try:
                self._switch()
            except HaltError as halted:
                self.halt, switched = halted.halt, False
        unchanged = self.source(self.root, self.out) == self.start
        ended = replace(self.base, unchanged=unchanged, pairs=tuple(self.pairs))
        ended = replace(ended, halt=self.halt, switched_back=switched)
        doc = report.document(ended, {_id(p): p.script for p in self.pilots})
        report.write_once(self.out / "report.json", doc)
        for pair in self.pairs:
            print(f"{pair.case or 'clean'}  {pair.spec}  {pair.status}")
        self.code = cast(int, doc["exit"])
        print(f"{self.out / 'report.json'}: exit {self.code}")
        return self.code


def _id(pilot: PilotInput) -> str:
    return pilot.spec.frontmatter.id


def _prepare(
    args: argparse.Namespace, docker: flags.Docker, replay: Replay, source: ReadSource
) -> Run:
    """Every input admitted before anything changes, or a fixed ValueError."""
    root, app, out = args.root, args.app, args.out
    start = source(root, out)
    if start[2]:
        raise ValueError(f"{root}: uncommitted changes, so no report could cite it")
    if out.exists():
        raise ValueError(f"{out}: already exists")
    flags.stack_for(app)
    loaded = sorted(manifest.load(root).cases.items())
    cases = tuple(c for _, c in loaded if c.app == app and c.split == "dev")
    qa = root / manifest.APPS / app / "qa"
    pilots = load_pilots(qa, args.compiled_dir or qa / ".compiled", args.spec)
    patches = _patches(args.patches, cases, pilots)
    hashes = {"manifest": _sha256(root / manifest.MANIFEST)}
    for pilot in pilots:
        spec = _id(pilot)
        hashes[f"spec:{spec}"] = pilot.spec.spec_hash
        hashes[f"script:{spec}"] = canonical_hash(pilot.script.model_dump(mode="json"))
    for key in patches:
        hashes["patch:" + "/".join(key)] = _sha256(
            args.patches / f"{'.'.join(key)}.json"
        )
    selected = tuple(_id(p) for p in pilots)
    base = Report(
        app, *start[:2], True, args.repeat, selected, (), hashes, (), None, False
    )
    (out / "attempts").mkdir(parents=True)
    return Run(root, out, base, pilots, cases, patches, start, docker, replay, source)


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
        except (OSError, ValueError, SpecError):
            candidate = None
        # Raised outside the handler, so the patch's own error isn't kept.
        if candidate is None:
            raise ValueError(f"{path}: invalid patch")
        patches[key] = validate_pilot(pilot.spec, pilot.config, candidate, source=path)
    return patches


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _positive(text: str) -> int:
    if (value := int(text)) < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


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
    parser.add_argument("--repeat", type=_positive, default=3)
    parser.add_argument("--patches", type=Path)
    args = parser.parse_args(argv)
    try:
        run = _prepare(args, docker or flags.LocalDocker(), replay, source)
    except (ValueError, OSError, flags.FlagError, manifest.ManifestError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 12 if isinstance(error, UnusableSecretError) else 2
    try:
        return asyncio.run(run.all())
    except OSError:
        print(f"error: evidence under {run.out} couldn't be written", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        run.halt = "interrupted"
    except SystemExit:
        run.halt = "system_exit"
    return run.finish()


if __name__ == "__main__":
    sys.exit(main())
