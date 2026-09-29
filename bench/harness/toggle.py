"""Prove that each benchmark case's flag switches its planted change, and only it (ADR-0023).

The app's images are built from the working tree first, so the evidence can't
come from stale images. In each cycle, every case's check runs on the clean
app, and each must report "clean". Then the app is switched to each case in
turn and every check runs again: that case's must report "planted" and every
other one "clean", so a flag that also switches another case's change fails.
The run ends on the clean app. The checks (toggle_checks.py) run in the checks
image (checks.Dockerfile) on the app's Compose network, as a non-root user
with Chromium's sandbox on.

Run: python3 bench/harness/toggle.py conduit [--cycles N] [--case CASE_ID ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import flags
import manifest

CLEAN = "clean"
PLANTED = "planted"
HARNESS = Path("bench/harness")
IMAGE = "aqa-bench-checks:local"


class ToggleError(Exception):
    pass


@dataclass(frozen=True)
class Result:
    case: str
    state: str | None
    detail: str
    error: str | None


@dataclass(frozen=True)
class Row:
    """One check's result, with the case whose flag was on (None: the clean app)."""

    cycle: int
    case: str
    active: str | None
    result: Result

    @property
    def flag_on(self) -> bool:
        return self.active == self.case

    @property
    def ok(self) -> bool:
        want = PLANTED if self.flag_on else CLEAN
        return self.result.error is None and self.result.state == want


class Checks(Protocol):
    def registered(self) -> list[str]: ...

    def run(self, case_ids: Sequence[str]) -> list[Result]: ...


def parse_results(output: str, case_ids: Sequence[str]) -> list[Result]:
    """The checks' JSON lines, which must cover exactly ``case_ids`` in order."""
    results = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            raise ToggleError(
                f"unexpected output from the checks: {line[:200]}"
            ) from None
        results.append(
            Result(
                item["case"],
                item.get("state"),
                item.get("detail", ""),
                item.get("error"),
            )
        )
    reported = [r.case for r in results]
    if reported != list(case_ids):
        raise ToggleError(f"the checks reported {reported}, want {list(case_ids)}")
    return results


def fixture_password(root: Path, app: str) -> str:
    """The seeded accounts' public fixture password, from the app's compose.yaml."""
    arg = flags.stack_for(app).password_arg
    compose = (root / manifest.APPS / app / "compose.yaml").read_text()
    match = re.search(rf"^\s*{arg}:\s*(\S+)\s*$", compose, re.MULTILINE)
    if match is None:
        raise ToggleError(f"no {arg} in {manifest.APPS / app / 'compose.yaml'}")
    return match[1]


class DockerChecks:
    """Runs toggle_checks.py in the checks image on the app's Compose network."""

    def __init__(self, root: Path, app: str) -> None:
        self.root = root
        self.app = app
        self.stack = flags.stack_for(app)
        self.built = False

    def _docker(self, args: Sequence[str], env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["docker", *args],
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ToggleError(
                f"docker {args[0]} failed:\n{result.stderr.strip()[-2000:]}"
            )
        return result.stdout

    def _run(self, args: Sequence[str]) -> str:
        harness = self.root / HARNESS
        if not self.built:
            dockerfile = str(harness / "checks.Dockerfile")
            self._docker(["build", "-q", "-t", IMAGE, "-f", dockerfile, str(harness)])
            self.built = True
        # The password travels in the environment, never on the command line.
        env = {"BENCH_FIXTURE_PASSWORD": fixture_password(self.root, self.app)}
        command = [
            "run",
            "--rm",
            "--network",
            self.stack.network,
            "--security-opt",
            f"seccomp={harness / 'chromium-seccomp.json'}",
            "-e",
            f"BENCH_APP_URL={self.stack.internal_url}",
            "-e",
            "BENCH_FIXTURE_PASSWORD",
            "-v",
            f"{harness}:/harness:ro",
            "-w",
            "/harness",
            IMAGE,
            "python3",
            "toggle_checks.py",
            *args,
        ]
        return self._docker(command, env)

    def registered(self) -> list[str]:
        names: list[str] = json.loads(self._run(["--list"]))
        return names

    def run(self, case_ids: Sequence[str]) -> list[Result]:
        return parse_results(self._run(case_ids), case_ids)


def toggle(
    root: Path,
    app: str,
    cycles: int,
    docker: flags.Docker,
    checks: Checks,
    only: Sequence[str] = (),
) -> list[Row]:
    """Every check on the clean app and under each case's flag, ``cycles`` times.

    ``only`` limits which flags are switched on; every check still runs in every
    state. Builds the app's images first and ends on the clean app.
    """
    cases = sorted(c.id for c in manifest.load(root).cases.values() if c.app == app)
    unknown = sorted(set(only) - set(cases))
    if unknown:
        raise ToggleError(f"no case {', '.join(unknown)} for app {app}")
    if not cases:
        raise ToggleError(f"no cases for app {app} in {manifest.MANIFEST}")
    missing = sorted(set(cases) - set(checks.registered()))
    if missing:
        raise ToggleError(f"no toggle check for {', '.join(missing)}")
    switched = [c for c in cases if not only or c in only]

    flags.build(root, app, docker)
    rows: list[Row] = []
    for cycle in range(1, cycles + 1):
        flags.switch(root, app, (), docker)
        rows += [Row(cycle, r.case, None, r) for r in checks.run(cases)]
        for case_id in switched:
            flags.set_case(root, case_id, docker)
            rows += [Row(cycle, r.case, case_id, r) for r in checks.run(cases)]
    flags.switch(root, app, (), docker)
    return rows


def _describe(row: Row) -> str:
    under = row.active or "the clean app"
    outcome = row.result.error or f"{row.result.state} {row.result.detail}"
    mark = "ok " if row.ok else "BAD"
    return f"{mark} cycle {row.cycle}  {row.case:20} under {under:20}  {outcome}"


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(
    argv: list[str] | None = None,
    docker: flags.Docker | None = None,
    checks: Checks | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=manifest.REPO_ROOT, help="repository root"
    )
    parser.add_argument("app")
    parser.add_argument("--cycles", type=_positive, default=3)
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="switch on only this case's flag (every check still runs)",
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        rows = toggle(
            root,
            args.app,
            args.cycles,
            docker or flags.LocalDocker(),
            checks or DockerChecks(root, args.app),
            args.case,
        )
    except (ToggleError, flags.FlagError, manifest.ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for row in rows:
        print(_describe(row))
    bad = [row for row in rows if not row.ok]
    if bad:
        print(f"{len(bad)} of {len(rows)} checks not as expected")
        return 1
    print(f"{len(rows)} checks over {args.cycles} cycles, all as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
