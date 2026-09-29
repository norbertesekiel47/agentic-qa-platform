# Roadmap — Agentic QA Platform

Last updated: 2026-09-27 (revised after external review). **Core first, then SaaS** (ADR-0016): prove the agent on the benchmark before building the platform around it. Each milestone is one vertical slice, one PR series, with an explicit exit criterion. No dates — quality over speed; milestones are sequential.

**Release checkpoints** (added after review): full v1 scope is unchanged, but two public, independently defensible releases ship along the way so the strongest hiring signal doesn't wait for the whole SaaS:
- **Release 0.1 — "CLI + benchmark"** after M3: open-source CLI, frozen benchmark, local run viewer, measured test-split numbers.
- **Release 0.2 — "CI-native"** after M5: GitHub Action + GitHub App with Accept heal on real PRs.
- **Release 1.0 — "SaaS"** after M8.

## M0 — Benchmark pilot (development only)
- Choose the two open-source apps (license, realistic flows, easy seeding); vendor **the first** under `bench/apps/`. *Chosen (ADR-0020): Conduit first, Medusa at M3.*
- Feature-flag harness and ground-truth manifest format (`bench/manifest.v1.json`, keyed by case ID).
- **Pilot:** ~5 planted bugs + 2 benign UI changes on one app, with specs written to the expectation-coverage rules (probes where UI alone can't establish an expectation). Pilot cases are dev-only.
- **Exit:** pilot cases toggle reliably; specs pass a dry compile-rules review; apps run via `docker compose`.
  *Met 2026-09-28:* 7 dev-split pilot cases (5 bugs, 2 benign) in `bench/manifest.v1.json`, proved by `bench/harness/toggle.py`; 5 specs with `bench/apps/conduit/qa/REVIEW.md`; Conduit under Compose. The evidence and its SHAs are in PRs #7–#14. Findings to carry into M1/M2 are in `qa/REVIEW.md` and the LAB_NOTES watch list.

## M1 — Local explore & compile
- Repo scaffold (uv, Ruff, mypy strict, pytest, CI gates, AGENTS.md rules live).
- Spec parser + schema; **browser wrapper with the full egress contract** (local egress proxy with validated-IP connects, Playwright HTTP + WebSocket routing installed before page creation, service workers blocked) and **sandbox-enabled launch with a startup check**; accessibility snapshot with element refs.
- LangGraph explore loop with navigator role; tools; compiler producing targets + assertions with expectation coverage (unsupported expectations fail by name) and a required `side_effect` flag on every step.
- Model router with capability validation and cost accounting.
- **Spike (decision gate):** headless Chromium in a Lambda container image — memory, cold start, startup-hygiene time, and **whether Chromium's sandbox runs**. Outcome recorded in an ADR: Lambda (sandbox works) or the Fargate one-task-per-run adapter for hosted runs.
- **Exit:** `aqa explore` compiles all pilot specs; egress tests (redirects, rebinding, WebSockets, service workers) pass; the sandbox gate is decided.

## M2 — Replay, heal & verdicts
- `strict` replay with multi-strategy locators, bounded waits, and deterministic visual checks (`visible_unoccluded`, `pixel_diff`, `contrast_min`); `verified` mode behind a flag.
- Invariant checks (console, 5xx, exceptions, broken images).
- Step intents before dispatch with lease fencing; non-resumable detection for unresolved side-effect intents.
- Heal subgraph: observe → classify (`drift_consistent` / `expectation_violated` / `inconclusive`) → binding/step patch or finding; binding-unresolved vs expectation-failed distinction for assertions; heal-patch validator (target locators and non-side-effect steps only); `fill_secret` with origin/field binding; secret-value redaction of observations in every model-using mode.
- Hybrid perception: screenshot verification and vision fallback.
- Local HTML report viewer (`aqa report`).
- **Exit:** `aqa run` produces correct verdict types on fixture pages; strict replay makes zero LLM calls (asserted); hostile-page fixtures pass.

## M3 — Full benchmark, first numbers → **Release 0.1**
- Vendor the second app; write the full benchmark (**60 planted bugs, 20 benign UI changes** including the pilot cases) and **freeze the dev/test split immediately** (dev 20 bugs + 6 benign; test 40 bugs + 14 benign), grouping related bugs; commit the split hash before any tuning on the new cases.
- `bench/` runner, metrics with stated-sidedness CIs, per-category breakdown, results JSON + Markdown.
- Iterate on heal classification **using the dev split only**; ablations on dev (tree-only vs vision-only vs hybrid; per-role models; strict vs verified).
- Run the frozen test split once for release numbers.
- **Exit (local targets):** a test-split results file with measured detection, false positives, heal accuracy, flake metrics (pooled rate under a stated independence assumption + per-spec incidence), strict replay cost, heal cost, and local replay time — published honestly including misses. Hosted replay time is deferred to M6.
- **Release 0.1:** CLI on PyPI, benchmark repo, run viewer, README with cited numbers.

## M4 — SaaS foundation
- Terraform: VPC, RDS, fck-nat, S3 (+ lifecycle), KMS, API Gateway (HTTP + WebSocket), Lambdas, SQS, Budgets alarm.
- FastAPI app: projects, runs, ingestion (≤ 1 MB bodies), checkpointer backend (full saver contract), presigned uploads, test-secrets API.
- Alembic schema with RLS on every tenant table, definer functions for pre-tenant lookups, and the isolation test registry.
- Clerk integration (JWKS verification, `o.id` → internal UUID mapping, webhooks → orgs/users/memberships).
- OIDC exchange with per-repo trust policy + API keys; CLI `login`/`upload`.
- **Exit:** CI runs upload results to the SaaS; isolation suite green for all tables/endpoints; checkpointer conformance suite green.

## M5 — GitHub App → **Release 0.2**
- App registration, webhooks with dedupe, one check run per spec + summary check.
- Accept heal via Checks requested actions (short opaque identifiers), staleness checks, fork fallback (`aqa heal apply`).
- Spec indexing on push; dashboard edit → PR (API side).
- Docker-based GitHub Action using the runner image.
- **Exit:** end-to-end on a sandbox repo: PR → check run → Accept heal → re-run passes; stale proposals rejected.

## M6 — Hosted runners
- Hosted compute per the M1 sandbox gate: **Lambda** (outside VPC, startup hygiene) if sandboxed Chromium works there, otherwise the **Fargate one-task-per-run** adapter; dispatcher with per-org concurrency, continuation leases, and hosted-execution tokens (`secrets:read`).
- Domain verification (DNS TXT + well-known) with periodic re-checks; SSRF guard with pinned resolution.
- BYOK: KMS envelope encryption, key delivery to runners, rotation. Hosted test secrets.
- Continuation per the replay-safe rules; storage-state restore; WebSocket live events with `run_events` catch-up on reconnect.
- **Exit:** a hosted run on a verified domain streams live; a forced cutoff on a replay-safe spec resumes; an unresolved side-effect intent ends `non_resumable`; isolation tests pass on the chosen compute (crash-reuse on Lambda, or fresh-task verification on Fargate); **10-step replay time measured on the chosen hosted compute**.

## M7 — Dashboard
- **First task:** deploy an authenticated skeleton (Next.js static export on S3 + CloudFront, Clerk sign-in, one API call through the generated client) — proves hosting and auth before any screen is built (ADR-0017).
- Screens: Onboarding, Projects, Runs, Live run, **Run viewer**, **Triage inbox**, Specs, Usage & cost, Settings.
- Visual snapshots at 390/1440 (dark + light); axe checks.
- **Exit:** full onboarding-to-triage flow works for a new org; the run viewer and triage inbox meet the polish bar.

## M8 — Hardening & launch → **Release 1.0**
- Load test (concurrency, API p95, WebSocket fan-out); fix bottlenecks.
- Security pass: authz matrix, injection/egress/secret fixtures, redaction audit, dependency/image scans, SBOM.
- Public read-only demo org seeded with benchmark runs; rate-limited.
- README with measured headline numbers (each cited to a test-split results file + SHA), architecture diagram, 2-minute demo video.
- **Exit:** a stranger can go from the README to a run in the run viewer in one click, and reproduce the benchmark with one command.

## Later (not v1)
- **Exploratory mode:** spec-less bug hunting.
- **Accessibility mode:** keyboard/screen-reader-style traversal.
- **Export to Playwright** (`.spec.ts`) for portability.
- **Fargate one-task-per-run compute adapter** as an option for customers requiring a hard isolation boundary (if the M1 gate chose Lambda).
- **Interactive agent interrupts** in live dashboard runs (persisted interrupt records, response endpoint, expiry, and the same side-effect-safe continuation rules).
- **Stripe billing** (per-seat or per-run tiers); platform-paid LLM credits option.
- GitLab/Bitbucket apps; Slack notifications.
- SSO/SCIM via Clerk enterprise features.
- Feed triage labels into Calibrated Eval Toolkit for continuous judge calibration.
- Benchmark test set v2 (after v1 is burned).

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Heal classification accuracy below target | Core claim weak | M3 before SaaS; iterate on dev split; publish test-split results honestly |
| Small test split → wide intervals | Less impressive-looking numbers | Report honestly; grow the test set in v2 |
| Chromium's sandbox unavailable on Lambda (likely, per community evidence) | Lambda unsafe for multi-tenant hosted runs | M1 decision gate; Fargate one-task-per-run adapter behind the compute interface |
| Full benchmark authored before the compiler is proven | Wasted mutation work | M0 pilot on one app first; full benchmark at M3 |
| Continuation limits (non-replay-safe specs) | Some long specs can't resume | Most specs fit in one invocation; `reset` hooks; clear `non_resumable` status |
| Clerk client-only auth in a static export | Dashboard auth friction | Proven by the M7 skeleton before screens are built |
| LLM price changes | Misstated cost metrics | Cost from live price map; figures re-measured per release |
| Scope (SaaS + GitHub App + dashboard) | Long build | Release checkpoints 0.1 and 0.2 ship defensible artifacts early |
