# AGENTS.md — Agentic QA Platform

Instructions for coding agents (Claude Code, Codex, Cursor, etc.) working in this repository. Read this first, every session.

## 1. What this project is

A multi-tenant SaaS + CLI + GitHub App for agentic E2E testing: an agent explores a natural-language spec once, compiles a deterministic script, replays in strict mode with zero LLM calls, and heals on UI drift by classifying *UI drift with expectations intact* (`drift_consistent`) vs *expectation violated* vs *inconclusive*. Python backend/runner (FastAPI, LangGraph, Playwright) + Next.js dashboard (static export), on AWS (Lambda runners, RDS Postgres with RLS).

## 2. Where things are documented (single owner per fact)

| Question | Doc |
|---|---|
| Why does this exist? | [VISION.md](VISION.md) |
| What does a domain term mean? | [CONTEXT.md](CONTEXT.md) |
| What must it do? Targets? | [PRD.md](PRD.md) |
| How is it built? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Which libraries/versions/costs? | [TECH_STACK.md](TECH_STACK.md) |
| Tables, RLS, spec & compiled formats | [DATA_MODEL.md](DATA_MODEL.md) |
| Endpoints, auth modes, CLI, Action | [API.md](API.md) |
| Screens and UI rules | [UX_SPEC.md](UX_SPEC.md) |
| Test layers, benchmark, gates | [TESTING.md](TESTING.md) |
| Quality thresholds and the floor | [CONSTRAINTS.md](CONSTRAINTS.md) |
| How to install, test and run the gates | [§4 Commands](#4-commands) |
| Threat model and controls | [SECURITY.md](SECURITY.md) |
| Milestones and order | [ROADMAP.md](ROADMAP.md) |
| Why a decision was made | [ADRs/](ADRs/) |
| Non-obvious bugs and root causes | [LAB_NOTES.md](LAB_NOTES.md) |

If code and docs disagree, the code wins — then fix the doc in the same PR and say so.

## 3. Planned repository layout

```
apps/api/            FastAPI app (Lambda via Mangum), GitHub App handlers, webhooks
apps/dashboard/      Next.js dashboard (generated API client in src/client/)
packages/runner/     LangGraph run graph, tools, model router, Playwright session
packages/cli/        `aqa` CLI (Typer)
packages/core/       Shared Pydantic models: spec, compiled script, verdicts
infra/terraform/     AWS infrastructure
bench/               Benchmark apps, planted bugs, manifests, harness, results
spikes/              Spikes under our gates: hosted-chromium/, M1's hosted-compute trial (ADR-0008)
tests/               Cross-package integration, isolation, fixtures (pages, injection)
docs/runbooks/       Operational runbooks
```

The three `packages/` exist: uv workspace members with `src` layouts, imported as `aqa_core`, `aqa_runner` and `aqa_cli`. `bench/harness` is a member too (ADR-0027), and so is `spikes/hosted-chromium` (`aqa_hosted_chromium_spike`).

## 4. Commands

Run every command from the repository root. You need uv, which installs the Python in `.python-version`, and jq, which the dependency audit and its tests use. TESTING.md §8 lists the gates and the CI job that runs each; CONSTRAINTS.md holds their thresholds.

```bash
uv sync --locked                     # install the workspace from uv.lock, as CI does
uv run playwright install --with-deps --only-shell chromium                     # the headless shell the browser tests launch
# Chromium's sandbox can't start where AppArmor restricts unprivileged user namespaces
# (Ubuntu 23.10 and later, CI's runners included), so the browser tests fail there. CI logs
# the setting, then relaxes it (ADR-0026); on a machine you keep, prefer the AppArmor
# profile that the failure's message links to.
sysctl kernel.apparmor_restrict_unprivileged_userns                            # 1 means restricted
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0                  # allow them until the next reboot

# The gates. A change is done when all pass (rule 1).
# On a bare python3 of .python-version's version, as CI runs the guard:
python3 -m unittest discover -s .claude/hooks                                  # guard tests
python3 .claude/hooks/policy_guard.py --scan                                   # policy scan
python3 .claude/hooks/policy_guard.py --diff refs/remotes/origin/main          # the floor's moves since main; exit 1 listing only approval-class findings is expected when the change moves the floor: report them
# In the workspace:
uv run ruff check .                                                            # lint
uv run ruff format --check .                                                   # format
uv run mypy                                                                    # types: packages, spikes and tests
# mypy reads a `sys.platform` check as a constant for the host it runs on, so on macOS a Linux-only
# branch goes unchecked, while CI, on Linux, checks it (LAB_NOTES, 2026-09-30). The next run checks
# as CI does; the run above alone checks a macOS host's darwin branches. On Linux they are the same.
uv run mypy --platform linux                                                   # types: the same files, as Linux
uv run mypy --strict --no-explicit-package-bases .claude/hooks bench/harness   # types: scripts
uv run pytest --cov                                                            # every test, and the coverage floor
# With osv-scanner, gitleaks and fallow at the versions .github/workflows/ci.yml pins:
.github/scripts/audit-lockfile.sh osv-scanner uv.lock bench/harness/checks-requirements.txt   # dependency audit: high or critical advisories, in each lockfile
gitleaks git --no-banner --redact --verbose .                                  # secret scan: every commit
fallow audit --base "$(git merge-base origin/main HEAD)" --format json --quiet --explain   # fallow: the branch's changes, as in its pull request

# Narrower test runs, without --cov: the coverage floor holds for the whole suite only.
uv run pytest tests/test_constraints.py                                        # one file: CONSTRAINTS.md's threshold checks
uv run pytest tests/test_constraints.py::test_coverage_below_the_floor_fails   # one test
```

The secret scan reads commits, not the working tree, and the other gates read the working tree, so commit everything first and run the gates before `git push`. The benchmark harness's commands are in [bench/README.md](bench/README.md), and the hosted-compute spike's in [spikes/hosted-chromium/README.md](spikes/hosted-chromium/README.md). Gates that TESTING.md §8 marks for a later milestone join this block when they land: `tests/test_agents_commands.py` fails when a step or action in `.github/workflows/`, or a `uv run` command CONSTRAINTS.md names, has no line here.

## 5. Rules (non-negotiable)

1. **Quality gates on every change.** Ruff + mypy `--strict` (Python), TypeScript `strict` + ESLint (dashboard), pytest + Vitest. Not done until all pass. Never silence a check to get green — no new `# type: ignore`, `@ts-expect-error`, `eslint-disable`, skipped tests, weakened assertions, or lowered thresholds without an ADR. The thresholds are in [CONSTRAINTS.md](CONSTRAINTS.md): read it before writing code, and never weaken it to make a change pass.
2. **Test-first for core logic:** replay engine, locator resolution, heal-verdict schema and patch application, RLS policies, token/OIDC/webhook verification, domain verification, API-key hashing, secret redaction.
3. **Tenant isolation tests are mandatory.** Every new table or tenant-scoped endpoint ships registered in the isolation registry with a test proving org A cannot read or write org B's data. CI fails on unregistered tables/routes.
4. **One milestone = one vertical slice = one PR series**, following [ROADMAP.md](ROADMAP.md) order. Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
5. **Decisions go in ADRs.** Any choice between real alternatives gets an ADR in `ADRs/` (context, options, decision, consequences). Use the template in `ADRs/README.md`.
6. **Docs move with code.** If a change contradicts API.md, DATA_MODEL.md, ARCHITECTURE.md, or any other doc, update the doc in the same PR.
7. **Record non-obvious root causes** in [LAB_NOTES.md](LAB_NOTES.md): `- [YYYY-MM-DD] WHAT FAILED → WHY → CORRECT APPROACH`.
8. **Benchmark claims need evidence.** Never write a metric (README, PR, docs) without the command and commit SHA that produced it. Numbers are measured, never illustrative.
9. **Secrets and cost.** No secrets in code, logs, test fixtures, or command-line arguments (write to a gitignored `.scratch/` file and source it, or read env vars). Any change adding a paid AWS resource or an LLM call on a hot path states its cost impact in the PR.
10. **Git safety.** Name paths explicitly. Never run whole-tree destructive commands (hard reset, `git add -A`/`.`, whole-tree checkout/restore, forced `git clean`). Before a rebase, merge, or branch switch, run `git status --porcelain` and report what is dirty.

**Enforcement.** Each rule in CONSTRAINTS.md has exactly one enforcer:
- **Ruff** (lint and format, §4): the rule set, formatting, the function-size limits, `TODO`-style comments and broad exception handlers.
- **mypy** (the type runs, §4; CONSTRAINTS.md, Types): types, and empty bodies in functions that return a value.
- **pytest** (the test gate, §4): tests in strict mode with warnings as errors, and the coverage floor. `tests/test_chromium_launches.py` also refuses a Chromium launch or connection in `packages/*/src` that bypasses `aqa_runner.sandbox.launch` and its sandbox check, as far as the code's spelling shows (§6; ADR-0026 lists what it can't see).
- **policy_guard** (`.claude/hooks/policy_guard.py`): the rest of the floor. In Claude Code it runs before each tool call and at the end of each turn.
  - **Refused:** newly added suppressions, coverage pragmas, skipped/focused/rerun tests, tautological assertions, `suppress(Exception)` and, outside tests, unimplemented stubs (rule 1); Chromium sandbox disabling (§6); secrets in shell commands, and known-format credentials in any file (rule 9). A refused line other than a secret passes when it cites an existing `ADR-NNNN`, so write the ADR first. Fake test credentials must say so (e.g. contain `fake`).
  - **Needs the user's approval:** any change to quality-gate config (CONSTRAINTS.md, ruff/mypy/pytest/coverage settings, `[tool.uv]` and its non-workspace sources, tsconfig, eslint/vitest config, osv-scanner waivers, gate scripts, CI workflows and scripts), a test edit that removes assertions or tests, a shell command that may delete, move or rewrite a test file, and any edit to the guard or `.claude/settings*.json`.
  - **End-of-turn scan:** the whole tree is re-checked for the refused patterns, which catches files written by scripts or other tools; §4's policy scan runs the same scan (exit 1 on violations). Other agents don't run Claude Code hooks, but the rules still apply to them.
  - **In CI (the `floor` job, ADR-0028 amendment):** `policy_guard.py --diff` judges every file a pull request changes as an edit is judged, so what the hooks never saw is caught too. A refused finding fails the job. So does a finding that needs approval, which here also covers a deleted test file, a symbolic link, and any change to an existing file under the root `tests/`, which proves the bar (a new file there needs none). Those pass with the maintainer's `floor-change-approved` label; ADR-0028's amendment says when it holds.
  - **The approval is the maintainer's.** Agents never apply the label or set `AQA_FLOOR_CHANGE_APPROVED`. On a branch that moves the floor, §4's `--diff` line exits 1 listing only approval-class findings: report them and leave the approval to the maintainer.
- **fallow gate (ADR-0018):** before `git commit` or `git push`, `.claude/hooks/fallow-gate.sh` runs `fallow audit` and blocks a `fail` verdict (dead code, duplication or complexity that the change introduces). fallow analyzes only TypeScript and JavaScript, so until the dashboard exists it passes without checking anything; Python quality rests on the gates above. Agents without the hook run §4's fallow command before committing and fix any `fail`. Fix findings; `fallow-ignore` comments are suppressions under rule 1.
- **CI (ADR-0019, ADR-0029):** every pull request must pass each CI job in [TESTING.md §8](TESTING.md#8-quality-gates-ci). `main` accepts changes only through pull requests with those checks green, for admins too. A new CI job must also be added to the required checks.
- **Vendored apps (ADR-0021):** `bench/apps/` holds third-party code with planted bugs, so policy_guard and fallow skip it. gitleaks still scans it: add a `file:rule:line` entry to `.gitleaksignore` only for an upstream finding you have reviewed and confirmed is not a real secret. Everything else under `bench/` is ours and fully policed.

## 6. Project-specific guardrails

- **Strict replay makes zero LLM calls.** A test asserts the model client is never constructed in `strict` mode. Model-assisted checks exist only in `verified` mode and are reported separately.
- **Every expectation is covered.** The compiler maps each expectation to ≥ 1 check that actually establishes it, and fails by name on unsupported expectations — never substitute a weaker proxy.
- **Heals repair bindings, never expectations.** The heal-patch validator allows only target locators and non-side-effect steps; it rejects changes to assertions, target meanings, side-effect steps, `side_effect` flags, browser settings, the coverage plan, and invariants. Never label a change "intentional" in code, UI, or docs — use `drift_consistent`.
- **Step intents before actions.** Write the intent record before dispatching any action, under the current lease where the run has one (uploaded and hosted runs); M1's local runs keep it in the local run record (ADR-0024). An unresolved side-effect intent makes the run non-resumable.
- **Heals are proposals.** No code path may commit a compiled-script change without a recorded human acceptance, and acceptance must pass the staleness check.
- **Never re-execute a `side_effect` step automatically** (continuations, retries). A new attempt, including an explore restart or confirmation replay, repeats one only after the spec's reset hook succeeds, or, for a single confirmation replay, when the person running explore passes `--confirm-repeat` (ADR-0024).
- **Runners never get DB or AWS data-plane credentials.** All runner I/O goes through the API with a run token; runner startup hygiene runs before any tenant data is fetched. Only hosted-execution tokens can retrieve stored provider keys or test secrets.
- **Launch Chromium with its sandbox enabled** and verify it at startup. Never ship `--no-sandbox` for hosted multi-tenant runs; the M1 spike picks hosted compute that runs sandboxed Chromium in a fresh VM per run (ADR-0008 amendment). In M1, an unsandboxed launch is a hard error everywhere (ADR-0026).
- **Page content is untrusted.** Never add a tool that performs arbitrary HTTP, file, or shell access. Never let observation text flow into system prompts. The egress proxy, HTTP/WebSocket routing (installed before page creation), service-worker blocking, and the allowed-origin checks before every observation and action stay on in every mode.
- **Our tools never put secret values into model input.** Observations in every model-using mode pass through secret-value redaction and screenshot masking/OCR checks; `fill_secret` enforces origin/field binding. The browser's environment carries no secrets, and saved evidence never includes request bodies, HAR files or browser traces (ADR-0026).
- **Every query runs with `app.org_id` set** via parameterized `set_config('app.org_id', …, true)` using the internal UUID (never Clerk's `o.id` string). Never connect as the table-owner role on request paths.
- **Benchmark discipline.** Tune only on the dev split. Never run the frozen test split in CI or use it for tuning.
- **Model IDs and prices** come from config and the pinned price map (a vendored copy of LiteLLM's, refreshed deliberately; ADR-0007 amendment) — never hard-code prices in logic, and never install the `litellm` package.

## 7. Definition of done

- Gates green on the final HEAD (evidence re-run after any rebase; SHA recorded in the PR).
- Tests added for new behavior (isolation tests for new tenant surfaces).
- Docs and ADRs updated.
- UI changes: before/after screenshots at 390px and 1440px.
- Cost impact stated if relevant.

## Agent skills

### Issue tracker

GitHub Issues on `norbertesekiel47/agentic-qa-platform`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: a root `CONTEXT.md` glossary, and ADRs in `ADRs/`, not `docs/adr/`. See `docs/agents/domain.md`.
