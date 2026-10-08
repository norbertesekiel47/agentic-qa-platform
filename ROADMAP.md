# Roadmap — Agentic QA Platform

Last updated: 2026-10-08 (M1 saves no screenshots, ADR-0033); 2026-09-30 (the sandbox check, #35; M1 design decisions, ADR-0024–0026). **Core first, then SaaS** (ADR-0016): prove the agent on the benchmark before building the platform around it. Each milestone is one vertical slice, one PR series, with an explicit exit criterion. No dates — quality over speed; milestones are sequential.

**Release checkpoints** (added after review): full v1 scope is unchanged, but two public, independently defensible releases ship along the way so the strongest hiring signal doesn't wait for the whole SaaS:
- **Release 0.1 — "CLI + benchmark"** after M3: open-source CLI, frozen benchmark, local run viewer, measured test-split numbers.
- **Release 0.2 — "CI-native"** after M5: GitHub Action + GitHub App with Accept heal on real PRs.
- **Release 1.0 — "SaaS"** after M8.

## M0 — Benchmark pilot (development only)
- Choose the two open-source apps (license, realistic flows, easy seeding); vendor **the first** under `bench/apps/`. *Chosen (ADR-0020): Conduit first, Medusa at M3.*
- Feature-flag harness and ground-truth manifest format (`bench/manifest.v1.json`, keyed by case ID).
- **Pilot:** ~5 planted bugs + 2 benign UI changes on one app, with specs written to the expectation-coverage rules (probes where UI alone can't establish an expectation). Pilot cases are dev-only.
- **Exit:** pilot cases toggle reliably; specs pass a dry compile-rules review; apps run via `docker compose`.
  *Met 2026-09-28, re-verified 2026-09-29:* 7 dev-split pilot cases (5 bugs, 2 benign) in `bench/manifest.v1.json`, proved by `bench/harness/toggle.py`, which runs every case's check under every flag; 5 specs with `bench/apps/conduit/qa/REVIEW.md`; Conduit under Compose. The evidence and its SHAs are in PRs #7–#14 and #22 (the final review). Findings to carry into M1–M3 are in `qa/REVIEW.md`, the LAB_NOTES watch list and issues #18–#21.

## M1 — Local explore & compile
Design: ADR-0024 (explore runs), ADR-0025 (compiled scripts), ADR-0026 (the browser boundary for local and CI runs), and the 2026-09-29 amendments to ADR-0007 and ADR-0008.
- **Repo scaffold:** uv workspace (`packages/core`, `packages/runner`, `packages/cli`), Ruff, mypy strict, pytest, CI gates, AGENTS.md rules live. `bench/harness` joins the workspace under pytest.
- **Spike (decision gate), first after the scaffold.** Sandboxed Chromium on a Lambda MicroVM, a Fargate task and a Lambda function, measured against one rule: sandboxed Chromium **and** a fresh VM per run (ADR-0008 amendment). It records time to a ready browser, peak memory and cost per run, and an ADR records the outcome. Its logistics, including the maintainer's go-ahead before any AWS resource is created, are in the ADR-0008 amendment.
- **Spec parser and project config.** Strict parsing (`seed` removed; `start_url` must be a path), and `qa/config.yaml` with roles, browser settings, subresource and expected-blocked hosts, private origins, secret bindings and budgets (DATA_MODEL §9).
- **Browser wrapper with the full egress contract (ADR-0026):**
  - the local egress proxy: validated-IP connects pinned per run, the IP policy by location, no UDP bypass;
  - Playwright HTTP and WebSocket routing installed before page creation, and service workers blocked;
  - allowed-origin checks before each observation and action;
  - egress blocks and expected-blocked hosts;
  - a sandbox-enabled launch with a sandbox check, and a minimal browser environment;
  - an accessibility snapshot with element refs;
  - browser settings pinned for every run (ADR-0025).
- **Moved up from M2** (the pilot signs in, and explore uses a model):
  - `fill_secret` with origin and field binding, and secret-value redaction of observations;
  - saved evidence, scanned (no screenshots in M1, ADR-0033; bodies, HAR files and Playwright traces are never saved; network evidence is metadata only);
  - the four invariant observers;
  - a minimal strict executor (locator resolution per use, step-scoped waits, assertion evaluation);
  - a local record of side-effect dispatches.
- **LangGraph explore loop (ADR-0024):**
  - a frozen coverage plan written from the spec alone;
  - stateless navigator steps over the accessibility tree only;
  - tools, including `reload` and `restart`;
  - path selection that keeps the plan's required conditions;
  - a confirmation replay with no model calls, and unconfirmed scripts when a reset hook is missing;
  - outcomes and exit codes, and budgets.
- **Compiler (ADR-0025):**
  - label-free meanings, and the locator grammar with scopes and per-use resolution;
  - `text` and `pattern` checks, with every expectation covered (unsupported ones fail by name);
  - `side_effect` set on positive evidence only, with its basis;
  - the `browser`, `coverage` and `confirmed` fields.
- **Model router:** capability validation and cost accounting from the pinned price map, and Sonnet 5.5 defaults (ADR-0007 amendment).
- **Injection fixtures aimed at exploring:** task hijack, decoy success, decoy binding after a failed confirmation, and steering of navigation, documents and secrets.
- **Exit:**
  - `aqa explore` compiles and confirms all five pilot specs. Their compiled assertions are checked against `bench/apps/conduit/qa/REVIEW.md`: a checklist in the PR, plus a test that none of its "Not acceptable" proxies appear.
  - Each compiled pilot script replays 3 times on the clean app with no model calls.
  - Under each of the 7 case flags, the M1 executor reproduces the manifest: each bug fails exactly the expectations and invariants it names. Each benign case's broken bindings are listed, and a hand-written rebinding patch restores every pass without touching an assertion.
  - The egress tests pass: redirects, rebinding, WebSockets, WebRTC/UDP, service workers and document origins.
  - The sandbox gate is decided.

## M2 — Replay, heal & verdicts
- `strict` replay built on M1's executor: verdicts and full deterministic visual checks (`visible_unoccluded`, `pixel_diff`, `contrast_min`); `verified` mode behind a flag.
- Define how specs with `visual: model` expectations compile and confirm before verified mode ships (M1 rejects them as a spec error).
- **Before any verdict ships:** decide what a fallback-locator match means (verdict, safeguards for side-effect steps, benign scoring), before M3's numbers (#25).
- Step intents before dispatch with lease fencing; non-resumable detection for unresolved side-effect intents.
- Heal subgraph: observe → classify (`drift_consistent` / `expectation_violated` / `inconclusive`) → binding/step patch or finding; binding-unresolved vs expectation-failed distinction for assertions; heal-patch validator (target locators and non-side-effect steps only; `browser` and `coverage` immutable); redaction in heal and verified modes.
- Hybrid perception: screenshot verification and vision fallback, with bound-field masking proven against a hostile page and OCR checks before any image is saved, reaches a model or leaves the machine (ADR-0033, #171).
- Injection fixtures aimed at healing ("update the test").
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
- GitHub Action using the runner image. First decide how it runs Chromium's sandbox (Docker actions run as root under Docker's default seccomp profile, #26) and which config revision a CI run trusts for secret bindings (#27).
- **Exit:** end-to-end on a sandbox repo: PR → check run → Accept heal → re-run passes; stale proposals rejected.

## M6 — Hosted runners
- Hosted compute per the M1 spike's ADR: sandboxed Chromium with a fresh VM per run (ADR-0008 amendment). A Lambda MicroVM is the likely winner. Dispatcher with per-org concurrency, continuation leases (their scope decided once the compute is chosen, #28), and hosted-execution tokens (`secrets:read`). Hosted policy for subresource hosts (#29).
- Domain verification (DNS TXT + well-known) with periodic re-checks; SSRF guard with pinned resolution.
- BYOK: KMS envelope encryption, key delivery to runners, rotation. Hosted test secrets.
- Continuation per the replay-safe rules; storage-state restore; WebSocket live events with `run_events` catch-up on reconnect.
- **Exit:** a hosted run on a verified domain streams live; a forced cutoff on a replay-safe spec resumes; an unresolved side-effect intent ends `non_resumable`; isolation tests pass on the chosen compute (a fresh VM per run, verified); **10-step replay time measured on the chosen hosted compute**.

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
- ~~**Fargate one-task-per-run compute adapter** as an option for a hard isolation boundary~~: no longer needed. The M1 spike's rule already gives every hosted run a fresh VM (ADR-0008 amendment, 2026-09-29).
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
| Chromium's sandbox unavailable on Lambda functions (no user namespaces) and most likely on Fargate (no custom seccomp) | No hosted compute qualifies | M1 spike tests Lambda MicroVMs (a VM per session) against the fresh-VM rule; ECS on EC2 with a VM per run is the last resort; otherwise hosted runs wait (ADR-0008 amendment) |
| Full benchmark authored before the compiler is proven | Wasted mutation work | M0 pilot on one app first; full benchmark at M3 |
| Continuation limits (non-replay-safe specs) | Some long specs can't resume | Most specs fit in one invocation; `reset` hooks; clear `non_resumable` status |
| Clerk client-only auth in a static export | Dashboard auth friction | Proven by the M7 skeleton before screens are built |
| LLM price changes | Misstated cost metrics | Cost from the pinned price map (refreshed in reviewed PRs; each cost record cites its version); figures re-measured per release |
| Scope (SaaS + GitHub App + dashboard) | Long build | Release checkpoints 0.1 and 0.2 ship defensible artifacts early |
