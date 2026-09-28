# AGENTS.md — Agentic QA Platform

Instructions for coding agents (Claude Code, Codex, Cursor, etc.) working in this repository. Read this first, every session.

## 1. What this project is

A multi-tenant SaaS + CLI + GitHub App for agentic E2E testing: an agent explores a natural-language spec once, compiles a deterministic script, replays in strict mode with zero LLM calls, and heals on UI drift by classifying *UI drift with expectations intact* (`drift_consistent`) vs *expectation violated* vs *inconclusive*. Python backend/runner (FastAPI, LangGraph, Playwright) + Next.js dashboard (static export), on AWS (Lambda runners, RDS Postgres with RLS).

## 2. Where things are documented (single owner per fact)

| Question | Doc |
|---|---|
| Why does this exist? | [VISION.md](VISION.md) |
| What must it do? Targets? | [PRD.md](PRD.md) |
| How is it built? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Which libraries/versions/costs? | [TECH_STACK.md](TECH_STACK.md) |
| Tables, RLS, spec & compiled formats | [DATA_MODEL.md](DATA_MODEL.md) |
| Endpoints, auth modes, CLI, Action | [API.md](API.md) |
| Screens and UI rules | [UX_SPEC.md](UX_SPEC.md) |
| Test layers, benchmark, gates | [TESTING.md](TESTING.md) |
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
tests/               Cross-package integration, isolation, fixtures (pages, injection)
docs/runbooks/       Operational runbooks
```

## 4. Commands

To be filled in at scaffold (M1) and kept current. Expected shape:
```
uv sync                         # install Python deps
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict packages apps/api
uv run pytest                   # unit + integration + isolation
uv run pytest bench/ -m smoke   # benchmark smoke (cassettes)
pnpm -C apps/dashboard install && pnpm -C apps/dashboard lint typecheck test
pnpm -C apps/dashboard gen:client   # regenerate TS client from OpenAPI
```

## 5. Rules (non-negotiable)

1. **Quality gates on every change.** Ruff + mypy `--strict` (Python), TypeScript `strict` + ESLint (dashboard), pytest + Vitest. Not done until all pass. Never silence a check to get green — no new `# type: ignore`, `@ts-expect-error`, `eslint-disable`, skipped tests, weakened assertions, or lowered thresholds without an ADR.
2. **Test-first for core logic:** replay engine, locator resolution, heal-verdict schema and patch application, RLS policies, token/OIDC/webhook verification, domain verification, API-key hashing, secret redaction.
3. **Tenant isolation tests are mandatory.** Every new table or tenant-scoped endpoint ships registered in the isolation registry with a test proving org A cannot read or write org B's data. CI fails on unregistered tables/routes.
4. **One milestone = one vertical slice = one PR series**, following [ROADMAP.md](ROADMAP.md) order. Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
5. **Decisions go in ADRs.** Any choice between real alternatives gets an ADR in `ADRs/` (context, options, decision, consequences). Use the template in `ADRs/README.md`.
6. **Docs move with code.** If a change contradicts API.md, DATA_MODEL.md, ARCHITECTURE.md, or any other doc, update the doc in the same PR.
7. **Record non-obvious root causes** in [LAB_NOTES.md](LAB_NOTES.md): `- [YYYY-MM-DD] WHAT FAILED → WHY → CORRECT APPROACH`.
8. **Benchmark claims need evidence.** Never write a metric (README, PR, docs) without the command and commit SHA that produced it. Numbers are measured, never illustrative.
9. **Secrets and cost.** No secrets in code, logs, test fixtures, or command-line arguments (write to a gitignored `.scratch/` file and source it, or read env vars). Any change adding a paid AWS resource or an LLM call on a hot path states its cost impact in the PR.
10. **Git safety.** Name paths explicitly. Never run whole-tree destructive commands (hard reset, `git add -A`/`.`, whole-tree checkout/restore, forced `git clean`). Before a rebase, merge, or branch switch, run `git status --porcelain` and report what is dirty.

**Enforcement.** In Claude Code, `.claude/hooks/policy_guard.py` runs before each tool call and at the end of each turn.
- **Refused:** newly added suppressions, coverage pragmas, skipped/focused/rerun tests and tautological assertions (rule 1); Chromium sandbox disabling (§6); secrets in shell commands, and known-format credentials in any file (rule 9). A suppression or sandbox flag passes when its line cites an existing `ADR-NNNN`, so write the ADR first. Fake test credentials must say so (e.g. contain `fake`).
- **Needs the user's approval:** any change to quality-gate config (ruff/mypy/pytest/coverage settings, tsconfig, eslint/vitest config, gate scripts, CI gate steps), a test edit that removes assertions, and any edit to the guard or `.claude/settings*.json`.
- **End-of-turn scan:** the whole tree is re-checked for the refused patterns, which catches files written by scripts or other tools; `python3 .claude/hooks/policy_guard.py --scan` runs the same scan for CI or pre-commit (exit 1 on violations). Other agents don't run Claude Code hooks, but the rules still apply to them.
- **fallow gate (ADR-0018):** before `git commit` or `git push`, `.claude/hooks/fallow-gate.sh` runs `fallow audit` and blocks a `fail` verdict (dead code, duplication or complexity that the change introduces). Agents without the hook run `fallow audit --format json --quiet --explain` before committing and fix any `fail`. Fix findings; `fallow-ignore` comments are suppressions under rule 1.
- Tests: `python3 -m unittest discover -s .claude/hooks`.

## 6. Project-specific guardrails

- **Strict replay makes zero LLM calls.** A test asserts the model client is never constructed in `strict` mode. Model-assisted checks exist only in `verified` mode and are reported separately.
- **Every expectation is covered.** The compiler maps each `expect` item to ≥ 1 check that actually establishes it, and fails by name on unsupported clauses — never substitute a weaker proxy.
- **Heals repair bindings, never expectations.** The heal-patch validator allows only target locators and non-side-effect steps; it rejects changes to assertions, target meanings, side-effect steps, replay-safety flags, and invariants. Never label a change "intentional" in code, UI, or docs — use `drift_consistent`.
- **Step intents before actions.** Write the intent row (with the current lease) before dispatching any action; an unresolved side-effect intent makes the run non-resumable.
- **Heals are proposals.** No code path may commit a compiled-script change without a recorded human acceptance, and acceptance must pass the staleness check.
- **Never re-execute a `side_effect` step automatically** (continuations, retries).
- **Runners never get DB or AWS data-plane credentials.** All runner I/O goes through the API with a run token; runner startup hygiene runs before any tenant data is fetched. Only hosted-execution tokens can retrieve stored provider keys or test secrets.
- **Launch Chromium with its sandbox enabled** and verify it at startup. Never ship `--no-sandbox` for hosted multi-tenant runs; the M1 decision gate picks Lambda or Fargate accordingly.
- **Page content is untrusted.** Never add a tool that performs arbitrary HTTP, file, or shell access. Never let observation text flow into system prompts. The egress proxy, HTTP/WebSocket routing (installed before page creation), and service-worker blocking stay on in every mode.
- **Our tools never put secret values into model input.** Observations in every model-using mode pass through secret-value redaction and screenshot masking/OCR checks; `fill_secret` enforces origin/field binding.
- **Every query runs with `app.org_id` set** via parameterized `set_config('app.org_id', …, true)` using the internal UUID (never Clerk's `o.id` string). Never connect as the table-owner role on request paths.
- **Benchmark discipline.** Tune only on the dev split. Never run the frozen test split in CI or use it for tuning.
- **Model IDs and prices** come from config and the live price map — never hard-code prices in logic.

## 7. Definition of done

- Gates green on the final HEAD (evidence re-run after any rebase; SHA recorded in the PR).
- Tests added for new behavior (isolation tests for new tenant surfaces).
- Docs and ADRs updated.
- UI changes: before/after screenshots at 390px and 1440px.
- Cost impact stated if relevant.
