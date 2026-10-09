"""Switch a benchmark app to one case's flag, or to the clean app (ADR-0022).

A switch recreates the app's containers with BENCH_FLAGS set, which also
reseeds the database. It then checks from outside the app that both tiers got
exactly that flag set: the backend container's environment, and the flag list
in the served index.html. Flags are never readable or writable over HTTP.
Switching doesn't rebuild images: after changing app code, run
`docker compose build` in the app's directory first.

Run:
  uv run python bench/harness/flags.py set conduit-bug-001
  uv run python bench/harness/flags.py clean conduit
  uv run python bench/harness/flags.py show conduit
  uv run python bench/harness/flags.py selftest conduit --cycles 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import manifest

SELFTEST = manifest.SELFTEST_FLAG
FLAG_ELEMENT = re.compile(
    r'<script id="app-flags" type="application/json">(.*?)</script>', re.DOTALL
)


@dataclass(frozen=True)
class Stack:
    """An app's Compose stack. The backend reads BENCH_FLAGS; the frontend serves it.

    ``network`` and ``internal_url`` are where the toggle checks reach the app
    from a container, and ``password_arg`` names the build argument in
    compose.yaml that holds the seeded accounts' public fixture password.
    ``host_origin`` is where compose.yaml publishes the frontend on the host,
    the only origin a pilot may start at or reach (ADR-0023).
    """

    backend: str
    frontend: str
    frontend_port: int
    network: str
    internal_url: str
    password_arg: str
    host_origin: str


STACKS = {
    "conduit": Stack(
        backend="backend",
        frontend="frontend",
        frontend_port=80,
        network="conduit-bench_default",
        internal_url="http://frontend",
        password_arg="CONDUIT_SEED_PASSWORD",
        host_origin="http://127.0.0.1:4100",
    )
}


class FlagError(Exception):
    pass


class Docker(Protocol):
    def compose(
        self, app_dir: Path, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> str: ...

    def fetch(self, url: str) -> str: ...


class LocalDocker:
    """The docker CLI, and plain HTTP to the app's published port."""

    def compose(
        self, app_dir: Path, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> str:
        result = subprocess.run(
            ["docker", "compose", *args],
            cwd=app_dir,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            command = " ".join(args)
            raise FlagError(
                f"docker compose {command} failed:\n{result.stderr.strip()}"
            )
        return result.stdout

    def fetch(self, url: str) -> str:
        with urllib.request.urlopen(url, timeout=10) as response:
            body: bytes = response.read()
        return body.decode()


@dataclass(frozen=True)
class Switch:
    app: str
    flags: tuple[str, ...]
    seconds: float


def served_flags(html: str) -> tuple[str, ...]:
    """The flag ids in a served index.html, which must hold exactly one flag element."""
    found = FLAG_ELEMENT.findall(html)
    if len(found) != 1:
        raise FlagError(f"found {len(found)} flag elements in the served page, want 1")
    try:
        ids = json.loads(found[0])
    except json.JSONDecodeError as exc:
        raise FlagError(f"the served flag list is not JSON: {exc}") from exc
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise FlagError(f"the served flag list is not a list of ids: {found[0]}")
    return tuple(ids)


def stack_for(app: str) -> Stack:
    stack = STACKS.get(app)
    if stack is None:
        raise FlagError(f"no stack for app '{app}'")
    return stack


def _container_ids(app_dir: Path, stack: Stack, docker: Docker) -> dict[str, str]:
    return {
        service: docker.compose(app_dir, ["ps", "-q", service]).strip()
        for service in (stack.backend, stack.frontend)
    }


def _backend_env(app_dir: Path, stack: Stack, docker: Docker) -> str:
    args = ["exec", "-T", stack.backend, "printenv", "BENCH_FLAGS"]
    return docker.compose(app_dir, args).strip()


def _frontend_flags(app_dir: Path, stack: Stack, docker: Docker) -> tuple[str, ...]:
    port_args = ["port", stack.frontend, str(stack.frontend_port)]
    address = docker.compose(app_dir, port_args).strip()
    return served_flags(docker.fetch(f"http://{address}/"))


def build(root: Path, app: str, docker: Docker) -> None:
    """Build ``app``'s images from the working tree (quick when nothing changed)."""
    stack_for(app)
    docker.compose(root / manifest.APPS / app, ["build", "--quiet"])


def switch(root: Path, app: str, flag_ids: Sequence[str], docker: Docker) -> Switch:
    """Recreate ``app`` with exactly ``flag_ids`` on, and verify both tiers."""
    stack = stack_for(app)
    for flag in flag_ids:
        if not manifest.FLAG.fullmatch(flag):
            raise FlagError(f"invalid flag id '{flag}'")
    wanted = tuple(flag_ids)
    value = ",".join(wanted)
    app_dir = root / manifest.APPS / app

    before = _container_ids(app_dir, stack, docker)
    start = time.monotonic()
    up = ["up", "-d", "--wait", "--force-recreate"]
    docker.compose(app_dir, up, env={"BENCH_FLAGS": value})
    seconds = time.monotonic() - start

    after = _container_ids(app_dir, stack, docker)
    for service, container in after.items():
        if container == before[service]:
            raise FlagError(
                f"{service} container was not recreated, so the database was not reseeded"
            )
    backend = _backend_env(app_dir, stack, docker)
    if backend != value:
        raise FlagError(f"backend has BENCH_FLAGS='{backend}', want '{value}'")
    served = _frontend_flags(app_dir, stack, docker)
    if served != wanted:
        raise FlagError(f"frontend serves {served}, want {wanted}")
    return Switch(app, wanted, seconds)


def set_case(root: Path, case_id: str, docker: Docker) -> Switch:
    """Switch the case's app to that case's flag alone."""
    case = manifest.load(root).cases.get(case_id)
    if case is None:
        raise FlagError(f"no case '{case_id}' in {manifest.MANIFEST}")
    return switch(root, case.app, (case.flag,), docker)


def selftest(root: Path, app: str, cycles: int, docker: Docker) -> list[Switch]:
    """Alternate the reserved self-test flag and the clean app, ending clean."""
    results: list[Switch] = []
    for _ in range(cycles):
        results.append(switch(root, app, (SELFTEST,), docker))
        results.append(switch(root, app, (), docker))
    return results


def show(root: Path, app: str, docker: Docker) -> str:
    """Which case is on, after checking that both tiers agree."""
    stack = stack_for(app)
    app_dir = root / manifest.APPS / app
    backend = tuple(f for f in _backend_env(app_dir, stack, docker).split(",") if f)
    served = _frontend_flags(app_dir, stack, docker)
    if backend != served:
        raise FlagError(
            f"tiers disagree: backend has {backend}, frontend serves {served}"
        )
    if not backend:
        return "clean (no flags)"
    loaded = manifest.load(root)
    names = []
    for flag in backend:
        case = loaded.case_for_flag(flag)
        if flag == SELFTEST:
            names.append(f"self-test (flag {flag})")
        elif case is None:
            names.append(f"unknown flag {flag}")
        else:
            names.append(f"{case.id} (flag {flag})")
    return ", ".join(names)


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=manifest.REPO_ROOT, help="repository root"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("set", help="switch to one case").add_argument("case_id")
    commands.add_parser("clean", help="switch to the clean app").add_argument("app")
    commands.add_parser("show", help="print the active case").add_argument("app")
    selftest_parser = commands.add_parser(
        "selftest", help="alternate the self-test flag and the clean app"
    )
    selftest_parser.add_argument("app")
    selftest_parser.add_argument("--cycles", type=_positive, default=10)
    return parser


def _run(args: argparse.Namespace, docker: Docker) -> str:
    root: Path = args.root
    if args.command == "set":
        result = set_case(root, args.case_id, docker)
        flag = result.flags[0]
        return f"{args.case_id}: flag {flag} on in {result.seconds:.1f} s, verified"
    if args.command == "clean":
        result = switch(root, args.app, (), docker)
        return f"{args.app}: clean (no flags) in {result.seconds:.1f} s, verified"
    if args.command == "show":
        return f"{args.app}: {show(root, args.app, docker)}"
    results = selftest(root, args.app, args.cycles, docker)
    lines = [
        f"{i:3d}  {','.join(r.flags) or 'clean':5}  {r.seconds:.2f} s"
        for i, r in enumerate(results, 1)
    ]
    seconds = [r.seconds for r in results]
    lines.append(
        f"{args.app}: {len(results)} switches, all verified; seconds "
        f"min {min(seconds):.2f}, median {statistics.median(seconds):.2f}, "
        f"max {max(seconds):.2f}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None, docker: Docker | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(_run(args, docker or LocalDocker()))
    except (FlagError, manifest.ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
