# ADR-0029: CI on the uv workspace: jobs, installs, the mypy split and the dependency audit

- Status: Accepted
- Date: 2026-09-29

## Context
#60 moves CI onto the workspace, so it runs TESTING.md §8's gates at CONSTRAINTS.md's thresholds. Until now, CI pip-installed Ruff, mypy and Playwright, and checked only `.claude/hooks` and `bench/harness` (ADR-0019, ADR-0028). Four things constrain the design:
- **The Actions allowlist.** The repository allows only GitHub-owned actions plus `fallow-rs/fallow`, and requires SHA pinning (ADR-0019). `astral-sh/setup-uv` isn't on the list.
- **The mypy leak.** One mypy run names the flat script folders through `mypy_path`, so package code can import `manifest` or `policy_guard` and pass. That code then fails for users with `ModuleNotFoundError` (ADR-0027).
- **Severity.** The audit must fail on high or critical advisories only (TESTING.md §8, SECURITY.md §11). Measured on 2026-09-29:
  - `uv audit` (uv 0.11.15, a preview command) reads `uv.lock`, but its JSON has no severity.
  - pip-audit reports no severity either.
  - GitHub's dependency graph doesn't list `uv.lock` among its supported files, so `actions/dependency-review-action` can't see our dependencies.
  - osv-scanner v2.6.0 reads `uv.lock`, and gives each advisory group a CVSS `max_severity`. On a probe lock pinning `requests==2.19.1` and `jinja2==3.1.4`, it scored the groups from 4.4 to 8.9.
- **Stale lockfiles.** An advisory can be published against a lockfile no pull request touches.

## Options
- **Installing uv:** (1) the release tarball, checked against a pinned sha256, as the `secrets` job installs gitleaks; (2) `pip install --require-hashes uv==…`; (3) `astral-sh/setup-uv`, added to the allowlist.
- **Dependency audit:** (1) osv-scanner, with our script judging its JSON by score; (2) `uv audit` plus our own script that looks up each advisory's severity at OSV; (3) `uv audit`, failing on any severity.
- **Jobs:** (1) all the Python gates in `guardrails`; (2) a new `python` job for the workspace gates and a `dependency-audit` job, with `guardrails` keeping the guard tests and the scan.
- **mypy:** (1) one run, as today; (2) two runs: the packages and `tests` with only the `src` folders as bases, then the scripts by file.

## Decision
- **uv from its release tarball** (the maintainer's choice), version 0.11.15, sha256-checked, as gitleaks is. There's no settings change, and one more third-party action would widen the supply chain. setup-python (GitHub-owned) supplies Python 3.14. `UV_PYTHON_DOWNLOADS=never` makes uv use it, or fail if it doesn't match `.python-version`, rather than download another interpreter.
- **`uv sync --locked`, then `uv run` for each gate.** `--locked` fails when `uv.lock` doesn't match the pyproject files, so every gate runs on the locked versions.
- **osv-scanner v2.6.0** (the maintainer's choice), a sha256-checked release binary, run by `.github/scripts/audit-lockfile.sh`.
  - **What fails:** any advisory group with a CVSS score of 7.0 or more, where CVSS v3's "High" band starts. A group with no score fails too, until someone reviews it. Every finding is printed with its score.
  - **Failing closed:** a scanner error fails the audit, and so does JSON the filter can't read, even on a clean exit, and so does an exit 1 with no rows.
  - **Why a script:** `tests/test_constraints.py` can run it against a fake scanner. The tests pin each rule above, and read the 7.0 from CONSTRAINTS.md.
  - **Waivers:** a reviewed advisory goes in `osv-scanner.toml` at the repository root, which osv-scanner reads next to `uv.lock`. osv-scanner itself also accepts waivers with no end date, and `[[PackageOverrides]]` that cover every package. So a test holds the file to CONSTRAINTS.md's rule:
    - only `[[IgnoredVulns]]` entries, each with just an `id`, a `reason` and an `ignoreUntil`. osv-scanner's TOML decoder matches keys case-insensitively, so an extra `ID` could override the checked one;
    - an `ignoreUntil` from today to an Exception's longest expiry. osv-scanner reads a zero date as "never expires";
    - a row in CONSTRAINTS.md's Exceptions table with the advisory's ID and `Dependency audit` as its rule.
  - **Why not the others:** option 2 would build on uv's preview JSON, which "may change without warning", and add a network lookup of our own. Option 3 would block on moderate and low advisories, a stricter bar than the one CONSTRAINTS.md sets.
- **Jobs** (the approved plan's layout):
  - `guardrails` keeps the guard tests and the policy scan. The tests run on a bare interpreter, as Claude Code runs the guard, and they still report when `uv sync` fails.
  - A new `python` job runs Ruff, both mypy runs and `pytest --cov`, in parallel with `guardrails`. pytest collects:
    - the packages;
    - `bench/harness`, whose CI step moves from unittest to pytest;
    - the guard's tests;
    - `tests/`, which holds #59's threshold checks.
  - A new `dependency-audit` job runs osv-scanner. The audit gets its own job because its result can change with no change to the code. Its name keeps it apart from `fallow`'s `fallow audit`.
  - `secrets` and `fallow` are unchanged.
- **The mypy split** (the maintainer's ticket comment):
  - `[tool.mypy]` checks `packages` and `tests`, with only the three `src` folders on `mypy_path`. A test proves that a module importing `manifest` or `policy_guard` fails with `import-not-found`.
  - The scripts run as `mypy --strict --no-explicit-package-bases .claude/hooks bench/harness`, which names each file as a top-level module. Without that flag the same command finds 16 errors, at `295c707`.
  - pytest's `pythonpath = ["bench/harness"]` stays: mypy checks package tests too, so a leaked import fails there first.
- **Weekly runs** (the maintainer's call): a `schedule:` trigger runs the workflow every Monday at 06:17 UTC, so a new advisory against an unchanged `uv.lock` still fails a run. Every job but `fallow`, which runs on pull requests only, runs then.
- **The guard sees all of CI** (the maintainer's call):
  - policy_guard counted a workflow line as gate config only if it named a tool (`ruff`, `pytest`, …). So the audit's threshold, its exit, `--locked`, a trigger or a whole job could change without an ask.
  - Now every workflow line but comments and the top-level `name:` is gate config, and so is every file in `.github/scripts/`.

## Consequences
- **"New" means "not waived".** The ticket's "new high or critical findings" is read as advisories not yet waived. `dependency-audit` is a required check, so an advisory published against a dependency nobody touched turns every open pull request red, docs-only ones included. The way out is a bump, or a waiver (with the maintainer's approval) plus its Exceptions row.
- **Required checks:** `main` requires `guardrails`, `python`, `dependency-audit`, `secrets` and `fallow`. Only a repository admin can add a job there.
- **Bumps:**
  - a uv or osv-scanner bump edits the version and the sha256 in `ci.yml`, and TECH_STACK.md, together. uv's `required-version` in `pyproject.toml` still holds it to 0.11.x;
  - a new Python minor version also edits setup-python's `python-version` in `ci.yml` (ADR-0027's list); otherwise the `python` job fails to find an interpreter.
- **Runner labels:** a `CI_RUNNER` label must be Linux x64, because the jobs download linux x64 builds. The audit script uses `jq`, which GitHub's Ubuntu images include; check it on a Blacksmith image before switching. The tests need `jq` too, as macOS 15 and later ship it.
- **What the audit sees:** osv-scanner reads OSV's database, which includes GitHub's advisories and PyPI's. It doesn't catch a malicious package with no advisory yet (SECURITY.md §11).
- **Weekly failures:** GitHub notifies whoever last changed the cron line. GitHub also turns off scheduled workflows in a public repository after 60 days without activity.
- **Every CI edit asks** in Claude Code, a rename of the workflow aside.
- **Cost:** on GitHub-hosted runners, public repositories run free. On Blacksmith, the `python` and `dependency-audit` jobs' minutes would be paid (ADR-0019).
- **Not yet in CI:**
  - the dashboard's gates and its dependency audit, at M7;
  - the benchmark smoke, at M3 with the `bench/` runner;
  - the browser tests' own steps, with #35 (TESTING.md §8);
  - `bench/harness/checks-requirements.txt`, the checks image's hash-locked set, which the audit doesn't read yet.
