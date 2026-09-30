# Constraints

This file owns the project's quality thresholds. The tool configs in `pyproject.toml` mirror it, and `tests/test_constraints.py` fails when a config drifts from a number here.

## Thresholds

Every row runs in the local gate run (AGENTS.md §4).

| Dimension | Threshold | Measured as | Enforced by | Why this number |
|---|---|---|---|---|
| Lint | 0 findings | Ruff's rule set in `pyproject.toml` `[tool.ruff.lint]` | `uv run ruff check .` | A finding either gets fixed or gets a reasoned per-file ignore in the config |
| Format | 0 files to reformat | `ruff format` | `uv run ruff format --check .` | Formatting is never a review topic |
| Types | 0 errors | mypy `strict` over `packages`, `bench/harness`, `.claude/hooks` and `tests` | `uv run mypy` | AGENTS.md rule 1 |
| Tests | 0 failures | pytest in `strict` mode, with every warning an error | `uv run pytest --cov` | AGENTS.md rule 1 |
| Coverage, overall | ≥ 94% | Line and branch coverage of `packages/`, `bench/harness/` and `.claude/hooks/`, tests excluded, subprocesses measured, rounded to a whole percent | `uv run pytest --cov` | Measured 94.1% at `c54d955`: hold it, and raise it by hand as it rises |
| Cyclomatic complexity per function | ≤ 10 | Ruff `C901` (mccabe) | `uv run ruff check .` | Ruff's default, and today's maximum |
| Return statements per function | ≤ 6 | Ruff `PLR0911` | `uv run ruff check .` | Ruff's default; no function exceeds it |
| Branches per function | ≤ 12 | Ruff `PLR0912` | `uv run ruff check .` | Ruff's default; no function exceeds it |
| Arguments per function | ≤ 6 | Ruff `PLR0913`, keyword-only arguments included | `uv run ruff check .` | Today's maximum (`toggle()`), one above Ruff's default: a parameter object for one function adds a concept |
| Positional arguments per function | ≤ 5 | Ruff `PLR0917` | `uv run ruff check .` | Ruff's default; past five, arguments go keyword-only |
| Statements per function | ≤ 50 | Ruff `PLR0915` | `uv run ruff check .` | Ruff's default; no function exceeds it |
