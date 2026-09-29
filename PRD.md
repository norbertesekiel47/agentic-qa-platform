# PRD — Agentic QA Platform (v1)

Status: Draft (revised after external review) · Owner: Norbert Esekiel · Last updated: 2026-09-29

Related: [VISION](VISION.md) · [ARCHITECTURE](ARCHITECTURE.md) · [DATA_MODEL](DATA_MODEL.md) · [API](API.md) · [UX_SPEC](UX_SPEC.md) · [ROADMAP](ROADMAP.md)

## 1. Summary

A multi-tenant SaaS, CLI, GitHub Action, and GitHub App that turns structured natural-language specs into deterministic, self-healing E2E tests. An agent explores once, compiles the path into a script, replays in strict mode with zero LLM calls, and heals on UI drift by determining whether **the UI merely moved (every expectation still holds)** or **an expectation is violated (a bug)**.

## 2. Personas

| Persona | Goal | Pain today |
|---|---|---|
| **Priya — product engineer** | Ship PRs without breaking user flows | Flaky E2E suite; selector churn after every redesign |
| **Marcus — QA engineer** | Keep coverage high across repos; triage fast | Maintenance eats the week; failures lack evidence |
| **Dana — engineering manager** | Trust the signal from CI; control spend | Red builds get ignored; AI-tool costs are opaque |

## 3. User stories (v1)

**Authoring**
- As Priya, I write a spec in `qa/checkout.spec.md` describing a goal, preconditions, and expected outcomes in plain English, and commit it with my feature.
- As Priya, I run `aqa explore qa/checkout.spec.md --url http://localhost:3000` locally and get a compiled script plus a run report.

**CI / GitHub**
- As Priya, when I open a PR, the GitHub Action replays all compiled specs in strict mode against my preview deployment and posts a check run per spec.
- As Priya, when a step breaks because an element moved but every expectation still holds, I see the evidence and click **Accept heal** to commit the locator update to my branch.
- As Priya, when an expectation is violated, the check fails and links to the run viewer, showing exactly what happened.
- As Priya, if I intentionally changed what the app should do, I edit the spec's expectations in my PR — heals never change expectations.

**Dashboard**
- As Marcus, I watch a hosted run live, then scrub through it step by step in the run viewer.
- As Marcus, I triage an inbox of bugs and pending heal proposals with keyboard shortcuts.
- As Dana, I see LLM spend by project, model role, mode, and day, and the share of runs that cost $0.

**Tenancy & admin**
- As Dana, I create an organization, invite teammates with roles, install the GitHub App, verify our domains, add our own model provider keys, and store test-account credentials for hosted runs.

## 4. Functional requirements

### 4.1 Specs
- FR-1 Specs are Markdown files with YAML frontmatter under `qa/**/*.spec.md` (format in [DATA_MODEL §6](DATA_MODEL.md#6-spec-file-format)).
- FR-2 Fields: `goal` (required), `preconditions` (test account refs, start URL as a path, optional `reset` hook that runs before every attempt, optional read-only probes), optional `steps` hints, `expect` (≥1; each may declare `visual: deterministic|model`), `invariants`, optional `allowed_origins`, and optional `browser` settings. A project config next to the specs declares model roles, browser defaults, subresource and expected-blocked hosts, and test-secret bindings (DATA_MODEL §9).
- FR-3 Default invariants: no uncaught JS exceptions, no console errors, no HTTP 5xx, no broken images. Each can be disabled per spec.
- FR-4 Test credentials are referenced by name (`secret: TEST_PASSWORD`), never inlined.

### 4.2 Execution engine
- FR-5 **Explore mode (ADR-0024).**
  - *Coverage plan:* the agent first writes a coverage plan from the spec alone.
  - *Driving the browser:* it then drives the browser until every planned check passes, or it gives up with a reason. Hybrid perception applies: the accessibility tree for actions, screenshots for visual verification, and vision fallback for canvas, iframes and unlabeled widgets. M1 uses the tree only.
  - *Confirmation:* the compiled path is confirmed by a strict replay with no model calls before it is written. A path with side-effect steps whose spec has no reset hook is written unconfirmed, unless the person running explore authorizes one confirmation that repeats them. In M1, specs with `visual: model` expectations are rejected as a spec error; M2 defines how they compile and confirm.
  - *Never a bug report:* explore never files findings. An expectation it saw fail is a reason it gave up.
- FR-6 **Compile:** a successful exploration produces `qa/.compiled/<spec>.json`. It contains:
  - **targets:** each element's meaning, which never names its current label, plus multi-strategy locators;
  - ordered steps, each carrying a required `side_effect` flag (`false` marks a replay-safe step and needs positive evidence);
  - assertions;
  - the coverage plan and the browser settings it was explored under (ADR-0025).

  **Every expectation must map to ≥ 1 check that actually establishes it** (UI text/content, URL, the browser's network traffic, read-only state probes, deterministic visual checks). Unsupported expectations fail compilation by name rather than being replaced by a weaker proxy.
- FR-7 **Strict replay (default):** executes compiled steps with **zero LLM calls**. Every assertion must compile to a deterministic check, including deterministic visual checks (in viewport, not occluded, minimum size/contrast, optional baseline pixel-diff). **Verified replay (opt-in):** adds model-assisted checks for `visual: model` assertions; its cost and results are reported separately and never counted as strict replays.
- FR-8 **Heal on drift:** when a step's or an assertion's target can't be resolved, the agent re-observes, repairs target bindings and/or non-side-effect steps, and emits: `drift_consistent` (after repair every assertion evaluates and passes and every invariant holds → patch proposal), `expectation_violated` (a resolved check evaluated false; finding with evidence), or `inconclusive` (escalate). Heal patches can never modify assertions, target meanings, side-effect steps, `side_effect` flags, browser settings, the coverage plan, or invariants. PR context may be supplied as hints; the platform never claims to know author intent.
- FR-9 Every verdict carries evidence references: before/after screenshots, accessibility snapshot diff, network and console logs, and the model's rationale.
- FR-10 **Step intents and continuation:** every action writes an intent record before dispatch and a completion after, fenced by the current lease. A run cut off by the hosted time limit resumes in a new invocation (new lease + token from the dispatcher, max 3) by restoring browser storage state and re-executing only replay-safe steps up to the last completed step; it never re-executes a `side_effect` step and never continues past an unresolved side-effect intent (`errored: non_resumable`). A declared `reset` hook runs before every attempt; a new attempt from step 1 repeats side-effect steps only after the hook succeeds.
- FR-11 Secrets are filled via `fill_secret` only into fields and origins the secret is bound to.
  - *Where bindings live:* the project config for local and CI runs; the stored test secrets for hosted runs.
  - *What the model sees:* our tools never give the model a secret value. In every model-using mode, text observations are scrubbed of secret values, and screenshots are masked and OCR-checked. This is best effort: a hostile page can reflect a value in forms that scanning may miss, which is documented.
  - *What's saved:* request bodies, HAR files and browser traces are never saved.

### 4.3 Models
- FR-12 Model-agnostic: any provider supported by the configured chat-model layer (OpenRouter, OpenAI, Anthropic, DeepSeek, …).
- FR-13 Role-based routing: `navigator`, `verifier`, `healer`, `vision_fallback`. Each role declares required capabilities (tool calling, structured output, vision); config validation rejects incompatible models, reading capabilities and prices from a pinned copy of LiteLLM's price map (ADR-0007 amendment).
- FR-14 Every LLM call records provider, model, role, mode, tokens, latency, and computed cost.

### 4.4 CLI & GitHub Action
- FR-15 CLI (`aqa`) commands: `init`, `explore`, `replay`, `heal`, `heal apply`, `run` (replay → heal as needed), `report`, `login`, `upload`.
- FR-16 GitHub Action runs the runner container in the customer's CI and authenticates via GitHub OIDC (no stored secret), subject to the repo's trust policy (immutable repo/owner IDs, allowed events, `workflow_ref`/`workflow_sha` for ordinary workflows or `job_workflow_ref` for reusable ones, single-use tokens). The tested (execution) SHA and the PR head SHA are recorded separately. Fork PRs run locally without uploading.
- FR-17 Outside GitHub Actions, the CLI authenticates with a project API key.

### 4.5 GitHub App
- FR-18 Posts one check run per spec plus a summary check.
- FR-19 A spec with a heal proposal offers **Accept heal** (Checks requested action with a short opaque identifier). Acceptance verifies the user's write access, rejects stale proposals (head SHA or compiled blob changed), and commits the locator update to the PR branch. Fork PRs get a patch + `aqa heal apply` instructions.
- FR-20 Dashboard spec edits open a PR; the repo remains the source of truth.

### 4.6 Hosted runs (SaaS)
- FR-21 Dashboard-triggered runs execute on hosted compute chosen by the M1 **sandbox decision gate**. The compute qualifies only if it runs sandboxed Chromium and gives every run a fresh VM. Candidates are a Lambda MicroVM, a Fargate task and, measured only for the record, a Lambda function (ADR-0008 amendment). Browser egress goes through a local proxy that enforces the allowlist at the connection level. Isolation guarantees are stated precisely in [SECURITY §6](SECURITY.md#6-runner-isolation-precise-boundary).
- FR-22 Hosted runs may only target **verified domains** (DNS TXT or `/.well-known/` file). In every mode, the browser's egress is restricted to the run's allowed origins and the project's subresource hosts (ADR-0026). CI runs need no domain verification.
- FR-23 Per-org rate limits and concurrency caps.
- FR-24 BYOK: org provider keys encrypted with KMS envelope encryption; decrypted only in memory for the duration of a run.
- FR-25 **Test secrets:** write-only, KMS-encrypted, scoped per project with allowed origins and field hints; decryptable only by dispatcher-issued hosted-execution tokens (never CI/upload tokens); every retrieval audited.

### 4.7 Dashboard
- FR-26 Nine screens: Onboarding, Projects, Runs, Live run, Run viewer, Triage inbox, Specs, Usage & cost, Settings (details in [UX_SPEC](UX_SPEC.md)).
- FR-27 Triage actions (accept heal / reject / not a bug) are recorded as labeled data for evaluation.

### 4.8 Tenancy & identity
- FR-28 Clerk Organizations for sign-in, members, invites, and roles (`admin`, `member`, `viewer`); Clerk org IDs are mapped to internal UUIDs.
- FR-29 Every tenant-scoped row is protected by Postgres row-level security keyed on the internal org ID set per transaction.

### 4.9 Benchmark
- FR-30 A published benchmark: two established open-source web apps with **60 flag-toggled planted bugs** across six categories and **20 benign UI changes**, split before tuning into **dev (20 bugs + 6 benign, including an early one-app pilot)** and a **frozen test split (40 bugs + 14 benign)** used only for release numbers.
- FR-31 One command reproduces all headline metrics; results include commit SHA, split version, model config, and confidence intervals with stated method and sidedness.

## 5. Non-functional requirements

| Area | Requirement |
|---|---|
| Determinism | Strict replay of an unchanged app is reproducible; flakiness measured over 30 specs × 20 replays |
| Latency | 10-step strict replay < 30 s locally and on the chosen hosted compute; API p95 < 300 ms |
| Cost | $0 LLM spend on strict replays; median heal < $0.05; infra ~$20–30/month at portfolio traffic |
| Isolation | Cross-tenant read/write impossible at the DB layer; runners hold no DB or AWS data-plane credentials; runner startup hygiene on every invocation |
| Security | See [SECURITY](SECURITY.md); our tools never give secret values to the model; logs/artifacts scrubbed |
| Accessibility | Dashboard meets WCAG 2.2 AA on core flows |
| Observability | OpenTelemetry traces across CLI → API → runner; CloudWatch for infra |
| Portability | Runner is one container image used in both CI and Lambda |

## 6. Release criteria (v1 targets, measured on the frozen test split)

| Metric | Target | Measured at |
|---|---|---|
| Planted-bug detection rate | ≥ 85% | M3 (local) |
| False-positive rate (findings on clean runs) | ≤ 5% | M3 |
| Heal classification accuracy (benign changes → `drift_consistent`) | ≥ 90% | M3 |
| Flake rate (30 specs × 20 replays) | Observed < 1% of replays; reported as a pooled rate (with its independence assumption stated) and as per-spec flake incidence with a one-sided exact bound | M3 |
| LLM cost per strict replay | $0 | M3 |
| Median LLM cost per heal | < $0.05 | M3 |
| 10-step strict replay time | < 30 s | M3 (local), M6 (hosted compute) |

All results are published per bug category with confidence intervals (method and sidedness stated), including misses. The test split is small (40 bugs, 14 benign changes), so intervals will be wide — reported as-is. If a target is not met, the result is published with analysis; targets are not quietly lowered.

## 7. Out of scope (v1)

Native mobile · load testing · non-GitHub SCM integrations beyond CLI + API key · Stripe billing · exploratory (spec-less) mode · accessibility-audit mode · export to Playwright code · SSO/SCIM beyond what Clerk provides.

## 8. Open questions

1. ~~Which two open-source apps for the benchmark?~~ **Resolved (ADR-0020):** RealWorld Conduit (Angular + Nitro/Prisma/Zod) first, in M0; Medusa second, in M3, confirmed by a spike at M3 start.
2. **Hosted compute with a sandboxed browser** (decision gate). The M1 spike tests a Lambda MicroVM, a Fargate task and a Lambda function against one rule: sandboxed Chromium and a fresh VM per run (ADR-0008 amendment, 2026-09-29). It also measures packaging, start time, memory and cost.
3. Clerk client-only auth in a Next.js static export — proven by the M7 skeleton task.
4. Pixel-diff baselines for deterministic visual checks: where baseline images live (repo vs artifact store) — decide in M2.
5. What a fallback-locator match means for verdicts, side-effect steps and benign-case scoring — decide in M2, before M3's numbers (#25).
