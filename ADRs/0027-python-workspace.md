# ADR-0027: The Python workspace: layout, pins and tool settings

- Status: Accepted
- Date: 2026-09-29

## Context
M1's scaffold (#34) builds the uv workspace that every later ticket works in (AGENTS.md §3, TECH_STACK.md §1). Several of its choices had real alternatives:
- the import names of the three packages, `core`, `runner` and `cli`;
- how a plain `uv sync` installs every member, including `bench/harness`, which is a directory of scripts rather than a package;
- how `litellm` stays out for good (ADR-0007 amendment);
- how pytest and mypy name modules, when package tests may share file names and the harness's tests import each other by bare name (`from test_manifest import ...`);
- which Python version Ruff targets, when some of our code runs on older interpreters.

A measured fact forced the last choice: with the workspace at `>=3.14`, Ruff reformats code that runs on older interpreters into syntax they reject (LAB_NOTES, 2026-09-29).

## Options
- **Import names:** (1) three top-level packages, `aqa_core`, `aqa_runner` and `aqa_cli`; (2) one namespace package split across three distributions, `aqa.core`, `aqa.runner` and `aqa.cli`.
- **Workspace root:** (1) a virtual root project that depends on every member; (2) a root without dependencies, with `uv sync --all-packages` wherever the workspace is installed.
- **Pins:** (1) exact pins in each member's dependencies; (2) ranges, with exact versions only in `uv.lock`.
- **Keeping litellm out:** (1) a root constraint that no version satisfies (`litellm<0`); (2) an override that drops it from every dependency; (3) a test that `uv.lock` has no litellm.
- **pytest import mode:** (1) `prepend`, pytest's default and how unittest runs the harness; (2) `importlib`, with the harness on `pythonpath` and namespace-package resolution on.
- **mypy module names:** (1) mypy's default search, which names a test file outside a package by its basename; (2) `explicit_package_bases`, with each package's `src` and each script directory as a base.
- **Ruff target:** (1) 3.14 everywhere; (2) the oldest interpreter's version everywhere; (3) 3.14, with older targets for the paths that run elsewhere.

## Decision
- **Import names: `aqa_core`, `aqa_runner` and `aqa_cli`** (the maintainer's choice). Each package uses a `src` layout and the `uv_build` backend, and dependencies point one way: `cli` → `runner` → `core`. A namespace package reads better, but every tool would have to resolve one package spread over three directories, and it breaks the day anyone adds `aqa/__init__.py`.
- **A virtual root.** The root project (`aqa-workspace`, `package = false`) depends on every member, so a plain `uv sync` installs them all, plus the `dev` group of quality tools. `bench/harness` is a member with `package = false`: its dependencies are installed, and its scripts still run from their directory. The `workspace = true` sources live once, at the root.
- **Exact pins** that match TECH_STACK.md §1, so a version changes only when someone edits its pin. Nothing checks the pins against TECH_STACK.md, so a bump edits both. `.python-version` holds 3.14, and `required-version` holds uv to 0.11.x.
- **`constraint-dependencies = ["litellm<0"]`.** Any resolution that pulls litellm in, directly or transitively, fails and names it. An override would drop the dependency silently and leave the package that needs it broken. A test would only notice after the lock had changed. #34's PR records `uv add litellm` failing with the constraint and succeeding without it.
- **pytest: `importlib` mode,** pytest's advice for new projects, so test files in different packages may share a basename. `bench/harness` is on `pythonpath` and `consider_namespace_packages` is on, so each harness module is imported once, under its bare name, as under unittest. pytest runs the same harness tests unittest does; #34's PR compares the two lists.
- **mypy: `explicit_package_bases`,** with each package's `src`, `bench/harness` and `.claude/hooks` as bases. Package tests sit outside every base, so they are named by their path (`packages.cli.tests.test_version`). mypy's `files` also include `.claude/hooks`, so `uv run mypy` covers everything CI type-checks today.
- **Ruff: 3.14,** except for two paths:
  - the guard keeps Ruff's default, 3.10, because whatever `python3` Claude Code finds runs it;
  - `bench/harness` gets 3.12, because `toggle_checks.py` runs in the checks image's Python 3.12.
- **`bench/apps/`** is skipped by every tool that searches for files (ADR-0021): Ruff's `extend-exclude`, mypy's `exclude` and pytest's `norecursedirs`. Ruff's `force-exclude` also skips a file inside it that is named on the command line. mypy and pytest still check a file named that way, because their exclusions apply only while they search directories.

## Consequences
- `uv sync` installs the workspace from `uv.lock`, and `uv run aqa --version` prints the CLI's version.
- A new package is a `packages/<name>/` directory with `src/aqa_<name>/`. It joins the root's dependencies and sources, and mypy's `mypy_path`.
- A version bump edits the member's pin and TECH_STACK.md together. A new Python minor version edits `.python-version`, the `requires-python` of the root and the packages, mypy's `python_version` and Ruff's `target-version` together. The guard's and the harness's Ruff targets, and the harness's floor, follow the interpreters they run on.
- Test modules can't import each other, except in the harness. Shared test helpers go in fixtures or in the package under test.
- mypy's bases and pytest's `pythonpath` also let package code import the harness's and the guard's modules, which the installed packages can't. So a package that imports them passes every gate and fails for users, until #59 bans those imports in Ruff (`TID251`).
- The Ruff rule set, pytest's strict mode and every threshold belong to CONSTRAINTS.md (#59), and moving CI onto uv is #60. Until then, CI's pip-installed tools read these settings too, and its checks pass unchanged.
- Ruff 0.16's default `include` covers Markdown, so `ruff format --check .` also formats the Python code blocks in our docs. Once CI runs that command (#60), an unformatted Python snippet in a doc fails it. #59 decides whether to keep that or to narrow `include` to Python files.
- Pydantic's mypy plugin needs Pydantic installed wherever mypy runs, so adding it waits for CI to install from the lockfile (#60). mypy fails outright when it can't import a configured plugin.
