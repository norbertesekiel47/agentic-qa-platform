# ADR-0028: The quality bar: coverage, complexity, the Ruff rule set and the floor guard

- Status: Accepted
- Date: 2026-09-29

## Context
#59 writes the quality bar down in CONSTRAINTS.md and makes each number fail a local run when it's crossed. ADR-0027 left it the Ruff rule set, pytest's strict mode and every threshold. The maintainer settled the coverage floor, the complexity checker, the rule set and the floor guard in an interview on 2026-09-29, and three more choices while it was built. Measurements:
- **Coverage,** at `2d8e328`: `uv run pytest --cov` measures 94.09%. Removing `toggle_checks.py` from `omit` gives 84.02%. Removing `patch = ["subprocess"]` gives 57.71%, with the guard at 0%, because its tests run it as a subprocess.
- **Complexity,** at `c54d955`: `ruff check --select C901 --config 'lint.mccabe.max-complexity=9' .` finds two functions at 10, `check_file_edit` and `_validate_case`, and none above. `uvx complexipy@8.0.1 packages bench/harness .claude/hooks` finds six functions with a cognitive complexity above 15, the highest at 24.
- **Ruff,** at `c54d955`: `ruff check --select ALL .` gives 754 findings. `ruff check --show-settings` lists 413 rules in Ruff 0.16.9's default set, BLE001 and S110 among them (LAB_NOTES, 2026-09-29).

## Options
- **Coverage tool:** (1) pytest-cov with coverage.py; (2) coverage.py alone: `coverage run -m pytest`, `coverage combine`, `coverage report`; (3) diff-cover, on changed lines.
- **Coverage floor:** (1) hold today's 94%; (2) a round 90%; (3) changed lines only.
- **Test-first modules** (TESTING.md §2): (1) 100% line and branch per module, from the first one; (2) the overall floor only.
- **`toggle_checks.py`:** (1) excluded; (2) measured, which puts the total at 84.02%.
- **A module in a workspace member that no test imports:** (1) counted, with `include_namespace_packages`; (2) coverage's default, which leaves it out of the total.
- **Complexity:** (1) Ruff's `C901` with its Pylint size rules; (2) complexipy's cognitive complexity at 15, fallow's limit for TypeScript; (3) both.
- **`toggle()`'s six arguments:** (1) `max-args = 6`; (2) a parameter object, keeping Ruff's default of 5.
- **Ruff rule set:** (1) a curated, explicit list; (2) `ALL` minus ignores; (3) Ruff's defaults plus more families (`extend-select`).
- **Default rules the curated families leave out:** (1) keep them in the list; (2) drop them.
- **Floor guard:** (1) extend policy_guard's per-call checks now, with a `--diff` mode for CI as a follow-up; (2) per-call checks only; (3) both now.
- **`[tool.uv.sources]`,** where a git or path source for a package overrides a `constraint-dependencies` ban on it: (1) an entry other than a workspace member asks; (2) sources never ask.
- **`contextlib.suppress(Exception)`,** which Ruff's `SIM105` suggests in place of `try`-`except`-`pass` and no Ruff rule flags: (1) policy_guard refuses it; (2) CONSTRAINTS.md names it as a gap.
- **Markdown in `ruff format`:** (1) keep Ruff's default `include`, which formats Python blocks in docs; (2) narrow `include` to Python files.
- **pytest:** (1) `strict` mode, with warnings as errors; (2) pytest's lenient defaults.

## Decision
- **pytest-cov 7.1.0 with coverage 7.16.2; the gate is `uv run pytest --cov`.** One command measures and fails, and pytest-cov combines the subprocess data itself; coverage alone needs three commands. diff-cover needs a base branch, which the local gate doesn't have. `--cov` stays out of `addopts`, so a run of one file isn't held to the whole suite's floor. `patch = ["subprocess"]` measures the guard and the CLI, whose tests run them as subprocesses.
- **Hold 94%, rounded to a whole percent** (`precision = 0`), and raise it by hand. A round 90% would let coverage fall four points unnoticed, and changed-line coverage needs a base branch too.
- **Test-first modules at 100% line and branch,** from the first one. Its ticket adds the per-module command (CONSTRAINTS.md, Planned).
- **`toggle_checks.py` is excluded.** It runs only in the checks image (ADR-0023), where no unit test reaches it.
- **`include_namespace_packages = true`** (the maintainer's call). Workspace members have no `__init__.py` above `src/`, so without the option an untested new module in `packages/` would drop out of the total instead of counting as 0% (LAB_NOTES, 2026-09-29). Today's total is unchanged.
- **Ruff `C901` ≤ 10 and the Pylint size rules:** returns 6, branches 12, arguments 6, positional arguments 5, statements 50. Each limit is set explicitly, so a Ruff upgrade can't move it. All but arguments are Ruff's defaults, and nothing exceeds them today. complexipy would be one more tool with its own config, and its six functions above 15 would each need a refactor or an exception.
- **`max-args = 6`,** today's maximum (`toggle()`). Its `only` became keyword-only, so positional arguments stay at 5. A parameter object for one function adds a concept.
- **A curated, explicit list of families.** `ALL` gives 754 findings and turns on every rule a Ruff upgrade adds; `extend-select` lets an upgrade change the base set. Not selected: D, COM, CPY, EM, TC, INP; ANN, because mypy `strict` covers annotations; FBT, because Typer options are bools; PGH, because policy_guard owns suppression comments and each floor rule has one enforcer. `E501`, `PLR2004`, `TRY003`, `S603` and `S607` are ignored, each with its reason in `pyproject.toml`. Per-file ignores cover unittest-style tests, pytest's plain asserts, scripts that print, a fake that keeps its protocol's signature, and four reviewed false positives.
- **The list keeps Ruff's default rules** that those families leave out (the maintainer's call): the YTT, EXE, INT, FA and PYI families, and PGH005, TC004, TC005, TC007, TC010 and D419. Otherwise 72 rules that ran before would stop: the difference between the enabled rules that `ruff check --show-settings` lists before and after, at `c54d955`. None has a finding today. PGH005 is `invalid-mock-access`, not a suppression rule.
- **policy_guard stays the floor's one enforcer beyond Ruff and mypy,** extended per call now: it refuses an unimplemented stub outside tests and a test disabled in code (`__test__ = False`, `self.skipTest()`). It asks before an edit that leaves fewer assertions or test definitions, before a shell command that may delete, move or rewrite a test file (`git checkout` or `git restore` from another ref included, even chained with a commit), and before any change to CONSTRAINTS.md, `[tool.uv]` or a `[tool.uv.sources]` entry other than a workspace member (the maintainer's call: such a source overrides the litellm ban). mypy's `empty-body` already rejects `...` and `pass` bodies that should return a value, so the stub rule covers only `raise NotImplementedError` and its TypeScript form. A `--diff` mode for CI is a follow-up: CI's `--scan` sees file contents, not a deleted test or a changed config.
- **policy_guard refuses a new `suppress(Exception)` or `suppress(BaseException)`** (the maintainer's call). Ruff's `BLE001`, `S110` and `S112` catch a broad `except` that swallows the error, but `SIM105` then suggests `contextlib.suppress(Exception)`, which no Ruff rule flags, so the linter's own fix would get around the floor. An ADR citation excuses it, as it does the other refused rules.
- **Markdown stays in Ruff's `include`.** No doc has a Python block today, and `ruff format` fixes any that appears.
- **pytest `strict = true` and `filterwarnings = ["error"]`.** Both pass today. Strict mode catches a mistyped marker or config key and an `xfail` test that starts passing, and a warning fails the test that raised it.

## Consequences
- CONSTRAINTS.md owns the numbers and `pyproject.toml` mirrors them. `tests/test_constraints.py` checks each number on both sides of its boundary, and changing either file asks the maintainer.
- A new ignore, per-file ignore or exception changes gate config, so it needs the maintainer's approval.
- CI runs none of this until #60 moves it onto `uv`. Until then CI lints and type-checks `.claude/hooks` and `bench/harness` with these settings, and runs the unittest suites and `--scan`.
- The `--diff` follow-up brings deleted tests, stripped assertions and config changes into CI.
- policy_guard is at 995 lines, so the `--diff` follow-up starts by moving its rule tables into a sibling module.
- An upgrade of Ruff can add rules to its default set that this list doesn't pick up. Compare `ruff check --show-settings` before and after each upgrade (LAB_NOTES, 2026-09-29).
