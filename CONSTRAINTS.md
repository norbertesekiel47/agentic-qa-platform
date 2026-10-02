# Constraints

Last reviewed: 2026-09-30 (#37: `spikes/` joins the type check and the coverage scope).

This file owns the project's quality bar: the floor every change keeps and the thresholds the gates hold. The floor applies to every file policy_guard checks. The thresholds cover the Python code; fallow gates TypeScript (ADR-0018) once the dashboard exists.
- The tool configs in `pyproject.toml` mirror it, and `tests/test_constraints.py` fails when a config drifts from a number here.
- TESTING.md §8 keeps the list of gates.
- [ADR-0028](ADRs/0028-quality-bar.md) records why each choice was made.

Read this file before writing code. Never weaken it to make a change pass. A threshold moves only in a change of its own, with the maintainer's approval: policy_guard asks before any edit to this file.

## Floor

Always enforced. Each rule has exactly one enforcer.

| Rule | Enforced by | Runs at |
|---|---|---|
| No new suppression comments: `# type: ignore`, `# noqa`, `pyright:` and `mypy:` pragmas, `@ts-ignore`, `@ts-expect-error`, `eslint-disable`, `fallow-ignore`, coverage pragmas | policy_guard, refused | Each edit and shell write in Claude Code; the Stop hook's tree scan; `--scan` (CI's `guardrails` job runs it) |
| No skipped, focused or rerun-until-green tests | policy_guard, refused | Same |
| No tautological assertions (`assert True`, `expect(true).toBe(true)`) | policy_guard, refused | Same |
| No unimplemented stubs outside tests (`raise NotImplementedError`, `throw new Error("Not implemented")`) | policy_guard, refused | Same |
| No empty bodies in functions that return a value (`...` or `pass`) | mypy's `empty-body` | Both mypy runs in the Types row below |
| No `TODO`, `FIXME`, `XXX` or `HACK` comments | Ruff `FIX` | `uv run ruff check .` |
| No broad exception handlers that swallow the error: a bare `except`, `except Exception` or `except BaseException` that doesn't re-raise | Ruff `E722`, `BLE001`, `S110`, `S112` | `uv run ruff check .` |
| No `suppress(Exception)` or `suppress(BaseException)`, the rewrite Ruff's `SIM105` suggests for `try`-`except`-`pass` | policy_guard, refused | Each edit and shell write in Claude Code; the Stop hook's tree scan; `--scan` |
| No deleted or renamed-away tests, stripped assertions, or changed gate configs or thresholds without the maintainer's approval | policy_guard, asks | Each edit and shell command in Claude Code only, until the `--diff` follow-up brings it to CI |

A refused line passes when it cites an existing `ADR-NNNN`, so write the ADR first. Abstract and Protocol methods use `...` as their body, which mypy allows. Ruff's `PLR0124` and `PLR0133` also flag a comparison of a value with itself or of two constants, wherever it appears, so the `x == x` form of a tautological assertion has a second, broader check.

## Thresholds

Every row holds at the local gate run, [AGENTS.md §4](AGENTS.md#4-commands), which runs the same gates as CI (ADR-0029).

| Dimension | Threshold | Measured as | Enforced by | Why this number |
|---|---|---|---|---|
| Lint | 0 findings | Ruff's rule set in `pyproject.toml` `[tool.ruff.lint]` | `uv run ruff check .` | A finding either gets fixed or gets a per-file ignore with its reason in the config |
| Format | 0 files to reformat | `ruff format` | `uv run ruff format --check .` | Formatting is never a review topic |
| Types | 0 errors | mypy `strict`, in two runs. `packages`, `spikes` and `tests` have only each package's `src` as a base, plus the spike's tests directory for its dry-run harness, so package code can't import a script. The scripts in `bench/harness` and `.claude/hooks` are named by file | `uv run mypy`, then `uv run mypy --strict --no-explicit-package-bases .claude/hooks bench/harness` | AGENTS.md rule 1 |
| Tests | 0 failures | pytest in `strict` mode, with every warning an error; it also collects the guard's unittest tests | `uv run pytest --cov` | AGENTS.md rule 1 |
| Coverage, overall | ≥ 94% | Line and branch coverage of `packages/`, `spikes/`, `bench/harness/` and `.claude/hooks/`, rounded to a whole percent. Tests and `toggle_checks.py` (ADR-0023) are excluded, subprocesses are measured, and a module no test imports counts as uncovered | `uv run pytest --cov` | `uv run pytest --cov` measured 94.09% at `4d372fd`: hold it, and raise it by hand as it rises |
| Cyclomatic complexity per function | ≤ 10 | Ruff `C901` (mccabe) | `uv run ruff check .` | Ruff's default, and today's maximum |
| Return statements per function | ≤ 6 | Ruff `PLR0911` | `uv run ruff check .` | Ruff's default; no function exceeds it |
| Branches per function | ≤ 12 | Ruff `PLR0912` | `uv run ruff check .` | Ruff's default; no function exceeds it |
| Arguments per function | ≤ 6 | Ruff `PLR0913`, keyword-only arguments included | `uv run ruff check .` | Today's maximum (`toggle()`), one above Ruff's default: a parameter object for one function adds a concept |
| Positional arguments per function | ≤ 5 | Ruff `PLR0917` | `uv run ruff check .` | Ruff's default; past five, arguments go keyword-only |
| Statements per function | ≤ 50 | Ruff `PLR0915` | `uv run ruff check .` | Ruff's default; no function exceeds it |
| Dependency audit | 0 high or critical advisories | osv-scanner's highest CVSS score per advisory group in each audited lockfile: `uv.lock` and `bench/harness/checks-requirements.txt`, the checks image's hash-locked set (ADR-0023). A score of 7.0 or more is high or critical, and an advisory with no score counts as one. A waiver is an `[[IgnoredVulns]]` entry in the root `osv-scanner.toml`, the only waiver file the audit lets osv-scanner read, with a reason and an `ignoreUntil` date, plus a row in Exceptions below with the advisory's ID, `Dependency audit` as its rule and the lockfile as its path | CI's `dependency-audit` job, on every pull request, every push to `main` and weekly. `tests/test_constraints.py` checks the script's cut and the waivers against this row | SECURITY.md §11. 7.0 is where CVSS v3 starts "High" |

## Planned

Recorded now, enforced once the code or the pipeline they need exists.

| What | Threshold | Starts with |
|---|---|---|
| Coverage of each test-first module (TESTING.md §2) | 100% line and branch, via `uv run coverage report --include=<the module's paths> --fail-under=100` | The ticket that adds the first test-first module, which also adds the command |
| Mutation testing | Chosen then | The replay engine, the first test-first module |
| The floor in CI: deleted tests, stripped assertions, changed gate configs | `policy_guard.py --diff <base>` finds none, or the maintainer approved them | A follow-up ticket, which first splits the guard, now near its 1000-line limit |

## Exceptions

| ID | Rule | Path | Reason | Owner | Expires |
|---|---|---|---|---|---|

None yet. An exception is a temporary waiver of a floor rule or a threshold: it names its rule, path, reason, owner and an expiry at most 90 days out, and changes this file, so it needs the maintainer's approval. A reviewed false positive that stays one, such as the guard's `S104`, is a per-file ignore in `pyproject.toml` with its reason instead.
