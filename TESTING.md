# Testing — Agentic QA Platform

Last updated: 2026-09-27 (revised after external review). Two different things are tested here: **the software is correct** (unit → E2E) and **the agent is good** (the benchmark). Both gate releases.

## 1. Test layers

| Layer | Scope | Tools | Runs |
|---|---|---|---|
| Unit | Spec parser, **expectation-coverage compiler rules** (every `expect` item maps to ≥ 1 establishing check; unsupported clauses fail by name), target resolution, compiled-script patching + heal-patch validator (target locators and non-side-effect steps only), deterministic visual checks, network/probe checks, cost math, verdict schemas, token verification | pytest, Hypothesis | Every commit |
| Integration | API + Postgres (real RLS), checkpointer over HTTP, SQS dispatch, S3 presign | pytest + testcontainers (Postgres, LocalStack for S3/SQS) | Every commit |
| Tenant isolation | Cross-org access attempts on every table and endpoint | pytest (parametrized over the table/endpoint registry) | Every commit — **blocking** |
| Checkpointer conformance | Full saver interface (sync + async), pending writes, idempotent retries, HTTP failure injection, size limits | pytest, test cases ported from LangGraph's upstream Postgres saver suite | Every commit |
| Continuation & step intents | Forced cutoff mid-run: replay-safe resume succeeds; crash after dispatching a `side_effect` step but before completion → unresolved intent → `non_resumable`; stale-lease writes rejected; `reset` restarts as a new attempt | pytest + fixture app + fault injection | Every commit |
| Binding vs expectation | An assertion whose target won't resolve → heal (binding repair); a resolved target whose check is false → `expectation_violated`; a heal patch touching `/assertions`, target meanings, side-effect steps, or replay-safety flags is rejected | pytest + fixture pages | Every commit |
| **Runner hygiene** | Crash mid-run with secrets loaded → next invocation in the same environment finds no processes, files, or profile residue | Lambda runtime emulator + fault injection | Every commit — **blocking** |
| **Egress & secrets** | Through the egress proxy + routing: hostile pages attempting fetch/XHR/form/WebSocket exfiltration, redirect chains to disallowed hosts, DNS rebinding, service-worker registration, cross-origin iframes, non-HTTP schemes; secret reflection into text/attributes/pixels in explore, heal, and verified modes | pytest + fixture pages + local DNS fixture | Every commit — **blocking** |
| **Browser sandbox** | Runner verifies Chromium's sandbox is active at startup; M1 spike records whether it works on Lambda (decision gate for hosted compute) | Spike harness + startup check | M1 spike; every runner start |
| Strict-mode guarantee | Model client is never constructed in `strict` replay; `visual: model` assertions rejected in strict compile | pytest | Every commit |
| Agent unit | Graph transitions and tool handling with **recorded LLM responses** | VCR.py cassettes, fake chat model | Every commit |
| Contract | OpenAPI schema vs generated TS client; runner ↔ API payloads; cross-project trace fixture (`contracts/agentic-qa-trace.v1`) | Schemathesis-style fuzzing, client type check, fixture validation | Every commit |
| GitHub App | Checks limits (≤ 3 actions, ≤ 20-char identifiers), stale-proposal rejection, permission checks, fork fallback | pytest + recorded webhooks | Every commit |
| Realtime | Subscribe authorization, membership revocation, `since_seq` replay, stale connection cleanup | pytest + API Gateway emulation | Every commit |
| Dashboard | Components and pages | Vitest, Playwright Test | Every commit |
| Visual | Screens at 390px and 1440px, dark + light | Playwright screenshots, reviewed in PR | UI PRs |
| E2E system | CLI → API → runner → GitHub (sandbox org) on a demo app | Playwright + GitHub test org | Nightly + pre-release |
| Benchmark | Agent quality on the dev split (§5) | `bench/` harness | Smoke every PR; full dev split nightly |
| Load | Concurrent hosted runs, API p95, WebSocket fan-out | k6 / Locust against staging | Pre-release |
| Security | Authz matrix, injection fixtures, redaction, dependency audit | pytest, pip-audit, pnpm audit, Trivy | Every commit (audit weekly) |

## 2. Test-driven development scope

Written test-first (red → green → refactor): replay engine, locator resolution, deterministic visual checks, expectation-coverage compiler rules, heal-verdict schema and heal-patch validator (target locators and non-side-effect steps only), step intents and lease fencing, RLS policies and `set_config` context, token/OIDC/webhook verification, domain verification, API-key hashing, secret redaction and origin binding, egress enforcement, runner startup hygiene.

## 3. Tenant isolation tests

- A registry lists every tenant table and tenant-scoped endpoint. **CI fails if a new table/endpoint is not registered** (introspection compares `information_schema` and FastAPI routes to the registry).
- For each entry: seed orgs A and B; as A, attempt select/insert/update/delete of B's rows at the DB layer (as `app_user` with `app.org_id=A`) and via the API. Expect zero rows / `404` (never `403`, to avoid leaking existence).
- Unset-context test: queries with no `app.org_id` return zero rows. Unmapped Clerk `o.id` is rejected before any query.
- Definer-function test: pre-tenant lookup functions return only identifiers, never tenant rows.
- Run-token test: a token for run X cannot read or write run Y, even in the same org; an expired lease's token is rejected.

## 4. Agent tests without spending money

- **Cassettes:** LLM calls recorded once against real providers, replayed in CI. Keyed by prompt hash; a changed prompt fails loudly and must be re-recorded deliberately (`make record-cassettes`, requires keys).
- **Fake model:** scripted chat model for graph-transition tests (e.g., "heal returns `expectation_violated`" → assert finding created, no patch).
- **Fixture pages:** small static apps under `tests/fixtures/pages/` for each drift type (moved element, renamed label, removed element, new modal, canvas widget) and each hostile behavior (§1 Egress & secrets).

## 5. Benchmark

Location: `bench/`. Two open-source apps with **60 planted bugs** behind feature flags across six categories (functional, visual/layout, backend 5xx, JS error, broken flow, data display) and **20 benign UI changes** (moved/restyled/relabeled elements, reordered nav — every `expect` still holds).

**Pilot first (M0):** one app, ~5 bugs and 2 benign changes, development-only — enough to validate the compiler, the expectation-coverage rules, and the heal contract before investing in the full benchmark. Pilot cases join the dev split. The full benchmark is written and its split frozen at the start of M3, before any tuning on it.

### Splits (frozen before any tuning)
| Split | Bugs | Benign changes | Use |
|---|---|---|---|
| **dev** | 20 | 6 | Building and tuning prompts, routing, heuristics; PR smoke subset |
| **test (frozen)** | 40 | 14 | Release numbers only; never run in CI, never used for tuning |

- Related bugs (same code path or same "family") are assigned to the same split so the test set isn't leaked through near-duplicates.
- The split manifest is committed with a hash before M1. Once test results influence a design decision, that test set is **burned**: the next release uses a freshly written test set (v2), and the README says which version produced its numbers.

### Procedure
1. Explore each spec on the clean app → compiled scripts committed to `bench/apps/<app>/qa/.compiled/`.
2. For each bug flag: enable, run `aqa run` (strict mode), record verdicts. Ground truth: "expectation violated in spec S".
3. For each benign change: enable, run, record. Ground truth: `drift_consistent`.
4. Flake measurement (clean apps): **30 specs × 20 repeated replays = 600 replays**. Replays of the same spec are not independent, so two quantities are reported (see table).
5. Compute metrics with CIs, per category, on the relevant split.

### Metrics, targets, and statistical conventions
| Metric | Target | Reported as |
|---|---|---|
| Planted-bug detection rate | ≥ 85% | Point + two-sided 95% Wilson CI, per category (test split: 40 bugs) |
| False-positive rate (findings filed on clean runs) | ≤ 5% | Point + two-sided 95% Wilson CI |
| Heal classification accuracy (benign set → `drift_consistent`) | ≥ 90% | Point + two-sided 95% Wilson CI (test split: 14 changes — the interval will be wide, and we say so) |
| Flake rate | Observed < 1% of replays | (a) **Pooled replay rate** with a one-sided exact 95% upper bound computed *under an explicitly stated assumption* that replays are conditionally independent (0/600 → ≤ 0.50% under that assumption); (b) **per-spec flake incidence** — the fraction of specs with ≥ 1 flake in 20 repeats — with a one-sided exact bound over 30 specs as the assumption-free unit (0/30 → ≤ 9.5%). Both are published; (b) is the conservative claim. No bootstrap bound is reported for all-zero data (it's degenerate) |
| LLM cost per strict replay | $0 | Exact (no model client constructed) |
| Median LLM cost per heal | < $0.05 | Median + bootstrap CI |
| 10-step replay time | < 30 s | Measured locally at M3 and on Lambda (warm) at M6 — reported separately |

Every interval states its method and sidedness. With small splits, intervals are wide — publishing them honestly is the point.

Outputs: `bench/results/<date>-<sha>-<split>.json` + generated Markdown table. **README numbers are copied only from a test-split results file whose SHA is cited next to them.** Ablations (dev split): perception mode (tree-only / vision-only / hybrid) and per-role model comparisons; `verified`-mode results reported as a separate table with its own cost.

PR smoke: 8 dev-split cases (≈ 2 min, recorded cassettes). Nightly: full dev split with live models on a budget-capped key.

## 6. Flake policy

- A replay that fails then passes on retry is recorded as **flaky**, not passed. Flakes are tracked per spec and surfaced in the dashboard.
- No blanket retries in CI. Any retry is explicit, counted, and reported.

## 7. Dashboard quality

- Every screen has Playwright tests for its loading/empty/error/permission states.
- Visual snapshots at 390px and 1440px in dark and light themes; UI PRs attach before/after screenshots (project acceptance rule).
- Axe accessibility checks on core flows (no serious/critical violations).

## 8. Quality gates (CI)

A change is mergeable only when all pass:
`ruff check` · `ruff format --check` · `mypy --strict` · `pytest` (unit, integration, isolation, checkpointer, continuation, hygiene, egress/secrets, strict-mode) · `pnpm lint` · `pnpm typecheck` · `vitest` · Playwright component/page tests · benchmark smoke (dev split) · dependency audit (no new high/critical).

**Never** silence a check to go green: no new `# type: ignore`, `@ts-expect-error`, `eslint-disable`, `pytest.skip`, weakened assertions, or lowered thresholds without an ADR.

## 9. Evidence rule

Test and benchmark evidence produced before a rebase or merge is stale. Re-run the gate on the final HEAD and record the SHA in the PR description.
