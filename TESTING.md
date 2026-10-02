# Testing — Agentic QA Platform

Last updated: 2026-10-02 (settle windows, settling and the browser session's text reads, #46; WebRTC's mDNS responder off at launch, #43; packet capture for the egress tests, #43; the coverage plan's model, hash, request and cassette, and every model request's bounds, #41; the browser session's document-origin checks, #44; runner-side requests' tests, #42; routing, service workers and popups, and the launch's transport switches and fail-closed proxy, #43; the egress proxy's browser tests, #42; the model router, the Anthropic adapter's cassettes and the tracing guard, #40; model roles, capability checks at config load and cost records, #40; the Chromium launch test, #77; the pinned price map's load check and refresh, #40; spec and project-config parsing, #39; browser session tests, #36; browser tests in CI, #35; M1 design decisions, ADR-0024–0026; CI gates, ADR-0029). Two different things are tested here: **the software is correct** (unit → E2E) and **the agent is good** (the benchmark). Both gate releases.

## 1. Test layers

| Layer | Scope | Tools | Runs |
|---|---|---|---|
| Unit | Spec and project-config parser (strict YAML, unknown and duplicate keys, `id` vs file name, `start_url` as a path, declared secrets, browser settings; ADR-0030) and what a run derives from them (effective browser settings, start origin, allowed origins, secret destinations), the compiled-script reader (schema version 1: unknown fields, a required `side_effect`, one kind per locator, normalized names and literals, navigate paths; DATA_MODEL §7) and its loader (repeated JSON keys, names that must exist or be unique, scoped `not_visible` targets), the coverage plan (each check type's own fields, covered or unsupported with what it needs, `plan_hash`, one entry per expectation and only declared probes; DATA_MODEL §7), **expectation-coverage compiler rules** (every expectation maps to ≥ 1 establishing check; unsupported expectations fail by name; required conditions survive path selection), locator generation and resolution per use (action, assertion, negative check), text matching (case-insensitive, whole-word literals vs regex patterns; each search in a child process with no environment, stopped at its deadline without blocking the event loop), `side_effect` inference (positive evidence only), compiled-script patching + heal-patch validator (target locators and non-side-effect steps only; `browser` and `coverage` immutable), deterministic visual checks, network/probe checks, the pinned price map's sha256 check and its refresh script, per-role capability checks at config load, cost math from the pinned price map or the config's rates (cached input included), verdict schemas, token verification | pytest, Hypothesis | Every commit |
| **Pilot acceptance** (M1 exit) | Each compiled pilot script replayed by the M1 executor on the clean app 3 times (deterministic), then under each case's flag. Each bug case must fail exactly the expectations and invariants its manifest entry names. For each benign case, the broken bindings (or fallback resolutions) are listed, and a hand-written rebinding patch restores every pass without touching an assertion (ADR-0024) | `bench/` harness + the M1 executor | M1 exit; again when the compiler changes |
| Integration | API + Postgres (real RLS), checkpointer over HTTP, SQS dispatch, S3 presign | pytest + testcontainers (Postgres, LocalStack for S3/SQS) | Every commit |
| Tenant isolation | Cross-org access attempts on every table and endpoint | pytest (parametrized over the table/endpoint registry) | Every commit — **blocking** |
| Checkpointer conformance | Full saver interface (sync + async), pending writes, idempotent retries, HTTP failure injection, size limits | pytest, test cases ported from LangGraph's upstream Postgres saver suite | Every commit |
| Continuation & step intents | Forced cutoff mid-run: replay-safe resume succeeds; crash after dispatching a `side_effect` step but before completion → unresolved intent → `non_resumable`; stale-lease writes rejected; `reset` restarts as a new attempt; no attempt (including an explore restart or confirmation replay) repeats a dispatched side-effect step unless the reset hook succeeded first or `--confirm-repeat` authorized that single confirmation | pytest + fixture app + fault injection | Every commit |
| Binding vs expectation | An assertion whose target won't resolve → heal (binding repair); a resolved target whose check is false → `expectation_violated`; a covered target still resolves, so `visible_unoccluded` evaluates false; a heal patch touching `/assertions`, target meanings, side-effect steps, `side_effect` flags, `/browser` or `/coverage` is rejected | pytest + fixture pages | Every commit |
| **Runner hygiene** | Crash mid-run with secrets loaded → next invocation in the same environment finds no processes, files, or profile residue | Lambda runtime emulator + fault injection | Every commit — **blocking** |
| **Egress & secrets** | Through the egress proxy and routing (ADR-0026):<ul><li>**Exfiltration:** hostile pages attempting fetch, XHR, form, WebSocket, WebRTC/UDP, QUIC and IPv6 exfiltration.</li><li>**Launch:** each launch's transport switches and fail-closed proxy, each check against a control launched without them: Chromium runs with every switch, WebRTC gathers no candidate and sends no UDP, a context's own lookups fail while the session's proxy stays reachable, and a context without the session's proxy reaches nothing, loopback included (`packages/runner/tests/test_transports.py`, #43). The last `--disable-features` switch is Playwright's list plus WebRTC's mDNS responder, so a Playwright upgrade that changes its list fails (same file), and a page's peer connection, handed remote candidates with `.local` names the page chose, holds no socket on UDP 5353 and, on Linux, joins no multicast group and sends nothing but TCP, no lookup of those names included (`tests/test_peer_connections.py`).</li><li>**Routing:** routes in place before the first page (a test fails if the page comes first); each blocked fetch, XHR, form post, WebSocket and popup aborted and recorded, with nothing reaching the host; a repeated attempt counted in place; an allowed socket's messages kept in order; a redirect hop routing can't see, and a dedicated worker's socket, refused by the proxy; service-worker registration refused each of eight ways a page can try (ADR-0026 names the residual), against a control context that registers one (`packages/runner/tests/test_routing.py`, #43).</li><li>**Redirects:** chains to disallowed hosts and to subresource hosts.</li><li>**Documents:** on subresource hosts and disallowed hosts, reached by clicks, redirects, `location` changes and popups, which the agent must not observe or act on; frames from other origins, nested ones included, left out of snapshots, and a snapshot discarded when a frame navigates or is removed during it; popups recorded with their URL and opener, and closed; actions refused on an element around another origin's frame (the page's root, a region, a nested frame, an open shadow root) and keys refused into one; `navigate` refusing URLs off the allowed origins before anything is requested, re-checking after redirects, and leading back after a policy event; clicks through a slot or a link into such a frame, `fill` beside a frame that takes the focus, and keys held down (`Tab+a`); settling and the text reads refusing a page off the allowed origins, and the page's text discarded when a frame changes while it is read (`packages/runner/tests/test_settling.py`, `test_text_reads.py`, #46).</li><li>**DNS:** rebinding within a run, private addresses behind allowlisted hostnames, and DNS prefetch.</li><li>**Other paths:** service-worker registration, cross-origin iframes, non-HTTP schemes.</li><li>**Egress blocks:** undeclared hosts end the run `errored` with `egress_blocked` (exit 6), never as a finding; expected-blocked hosts' direct symptoms don't count.</li><li>**Secret reflection:** into text, attributes and pixels, in explore, heal and verified modes.</li><li>**Secret isolation:** secrets stay out of saved evidence (masked screenshots; no request or response bodies, HAR files or Playwright traces; network logs hold metadata only, and a saved `trace` is only our OpenTelemetry trace), and the browser's environment carries no secrets.</li></ul>Traffic is observed at the packet level: on Linux, as CI runs it, each scenario runs in a user and network namespace of its own where every address is local, and every IP packet any interface sends is captured, without root; a capture that dropped a packet fails (`tests/packet_capture.py`, with controls in `tests/test_packet_capture.py` and `tests/test_packet_flows.py`; ADR-0026 amendment, 2026-10-02). macOS checks results and destinations only | pytest + fixture pages + local DNS fixture | Every commit — **blocking** (from M1) |
| **Browser sandbox** | The sandbox check proves that Chromium's sandbox is active before any page loads, comparing a renderer with the browser process, with negative controls. A test refuses a Chromium launch or connection in `packages/*/src` outside `aqa_runner.sandbox.launch`, which runs the check, as far as the code's spelling shows (ADR-0026). The M1 spike tests hosted candidates against the fresh-VM predicate (ADR-0008 amendment). Our CI records the runner's AppArmor state, then allows unprivileged user namespaces before the browser tests (ADR-0026) | Spike harness + sandbox check; pytest for the launch test | M1 spike; every runner start; the launch test on every commit |
| Strict-mode guarantee | Model client is never constructed in `strict` replay (the router builds none until a call, and `strict` is no call mode: #40), and the confirmation replay that ends an explore run makes no model calls; `visual: model` assertions rejected in strict compile | pytest | Every commit |
| Agent unit | Graph transitions and tool handling with **recorded LLM responses** | VCR.py cassettes, fake chat model | Every commit |
| Contract | OpenAPI schema vs generated TS client; runner ↔ API payloads; cross-project trace fixture (`contracts/agentic-qa-trace.v1`) | Schemathesis-style fuzzing, client type check, fixture validation | Every commit |
| GitHub App | Checks limits (≤ 3 actions, ≤ 20-char identifiers), stale-proposal rejection, permission checks, fork fallback | pytest + recorded webhooks | Every commit |
| Realtime | Subscribe authorization, membership revocation, `since_seq` catch-up, stale connection cleanup | pytest + API Gateway emulation | Every commit |
| Dashboard | Components and pages | Vitest, Playwright Test | Every commit |
| Visual | Screens at 390px and 1440px, dark + light | Playwright screenshots, reviewed in PR | UI PRs |
| E2E system | CLI → API → runner → GitHub (sandbox org) on a demo app | Playwright + GitHub test org | Nightly + pre-release |
| Benchmark | Agent quality on the dev split (§5) | `bench/` harness | Smoke every PR; full dev split nightly |
| Load | Concurrent hosted runs, API p95, WebSocket fan-out | k6 / Locust against staging | Pre-release |
| Security | Authz matrix, injection fixtures (explore-focused from M1: task hijack, decoy success, decoy binding after a failed confirmation, navigation and secret steering; heal-focused from M2), redaction, dependency audit | pytest, osv-scanner (ADR-0029), pnpm audit, Trivy | Every commit (audit weekly) |

**Shared egress fixtures.** The egress proxy's fixture servers, its local DNS fixture and the egress proxy every browser session needs live in `packages/runner/tests/egress_fixtures.py`. The runner's tests import them as `packages.runner.tests.egress_fixtures`, the path pytest names those test modules by, with no change to pytest's configuration. Fixtures in a `conftest.py` would turn about 20 helpers into fixture arguments threaded through some 40 tests, typed with names mypy can't import from a `conftest.py`.

## 2. Test-driven development scope

Written test-first (red → green → refactor): replay engine (including M1's confirmation-replay executor), locator generation and resolution, deterministic visual checks, text matching, expectation-coverage compiler rules, `side_effect` inference, heal-verdict schema and heal-patch validator (target locators and non-side-effect steps only), step intents and lease fencing, RLS policies and `set_config` context, token/OIDC/webhook verification, domain verification, API-key hashing, secret redaction and origin binding, egress enforcement (including the IP policy and document-origin checks), runner startup hygiene.

## 3. Tenant isolation tests

- A registry lists every tenant table and tenant-scoped endpoint. **CI fails if a new table/endpoint is not registered** (introspection compares `information_schema` and FastAPI routes to the registry).
- For each entry: seed orgs A and B; as A, attempt select/insert/update/delete of B's rows at the DB layer (as `app_user` with `app.org_id=A`) and via the API. Expect zero rows / `404` (never `403`, to avoid leaking existence).
- Unset-context test: queries with no `app.org_id` return zero rows. Unmapped Clerk `o.id` is rejected before any query.
- Definer-function test: pre-tenant lookup functions return only identifiers, never tenant rows.
- Run-token test: a token for run X cannot read or write run Y, even in the same org; an expired lease's token is rejected.

## 4. Agent tests without spending money

- **Cassettes:** LLM calls recorded once against real providers (the first, at M1, are hand-written: see below), played back in CI. Keyed by prompt hash (VCR.py's matcher compares a sha256 of the canonical request body, plus method and URL); a changed prompt fails loudly and must be re-recorded deliberately. The command, with the key read from the environment and never typed (`.scratch/` is gitignored):
  ```bash
  set -a; . .scratch/provider.env; set +a    # a file holding ANTHROPIC_API_KEY=…
  AQA_RECORD_CASSETTES=1 uv run pytest packages/runner/tests/test_anthropic_client.py packages/runner/tests/test_model_router.py packages/runner/tests/test_coverage_plan.py
  ```
  `AQA_RECORD_CASSETTES=1` without a key fails at once, and a key without the flag changes nothing. A recording keeps only the request's `content-type` and `anthropic-version` and the response's `content-type` and `request-id`, stores a compressed answer as the text it was, and writes the API's own address whatever endpoint it went through (a gateway's prefix and query are dropped). It is written to a scratch file that replaces the cassette when the `with` block ends without an error, so a failed request leaves the committed one as it was; an assertion after the block does not, so read `git diff` on the cassettes before committing them. The fixture also drops the tracing switches a run inherits, so a recording's answers never reach a developer's LangSmith. `test_cassettes.py` records against a local stand-in and tests all of this.
  - *The cassettes at M1 are hand-written.* No provider key was available when #40 landed, so each file in `packages/runner/tests/cassettes/` starts with a `HAND-WRITTEN RESPONSES` header: the request bodies are what the adapter really sent (to a local stub), and the responses were written by hand in the Messages API's shape. They prove what the adapter sends and how it reads a response of that shape; they don't prove that the API accepts the request or that its real responses read the same way.
  - *Re-recording replaces four of the nine.* `tools`, `structured_output`, `plain_with_effort` and `coverage_plan` are scenarios a live API produces on demand, and their tests check the adapter's reading against the recorded response itself, so a real recording passes them. `refusal`, `structured_invalid`, `structured_truncated`, `no_usage` and `refusal_then_fallback` are a refusal, an answer that doesn't validate or was cut off, a response with no usage, and a refusal then an answer: a live API won't produce them on demand, so they stay hand-written. Their header says `BY DESIGN`, which is what the cassette fixture reads to never record them, and their tests assert the values written in them.
  - *Editing a hand-written cassette.* When a request changes on purpose, a by-design cassette no longer matches and can't be re-recorded: the failure prints the request body that was sent, so replace the cassette's `request.body` with it and keep the response. A test that isn't about the API's answers (tracing, the fixture's own) passes `replay_only=True` and never records.
  - *Why the keys stay stable:* the navigator's prompts depend only on graph state, and the coverage plan's prompt depends only on the spec (ADR-0024). The plan's request is the static instructions, then the spec's frontmatter as validated, as JSON, without `tags`, the account or the reset hook, with the start URL and each probe cut at its query (`aqa_runner.coverage_plan.plan_request`): never the spec's path, its Markdown body, the start origin or the environment. `test_the_plan_cassette_replays_for_the_spec_run_from_anywhere` replays one cassette for the same spec from two spec roots and start origins.
  - *What else is in the plan's request:* the plan models' docstrings reach the model as the response format's descriptions (`aqa_core.coverage_plan`), so editing one, or the instructions, changes the request. Re-record `coverage_plan`, or edit its `request.body` as above.
  - *Bounds:* every request asks for at most 4096 output tokens, and each try waits at most 120 s for its answer, with the SDK's two retries after it, as the adapter states them (`aqa_runner.anthropic_client`, LAB_NOTES 2026-10-02). `test_every_request_in_every_cassette_asks_for_the_output_bound` holds every cassette to the first, and `test_a_request_that_gets_no_answer_times_out` shows the second against a server that never answers.
  - *Wire requests:* per provider, cassette tests also check what each adapter actually sends. For example, no request forces a tool choice (ADR-0007 amendment). Anthropic's are in `test_anthropic_client.py`, with a control that shows langchain-anthropic's default structured output would force one. The same file shows that an ambient `LANGSMITH_GATEWAY` can't take a call away from Anthropic's own endpoint, or put a LangSmith key on it.
  - *Tracing:* an ambient `LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2` or `LANGCHAIN_TRACING` exports nothing once a router exists; `test_tracing.py` sets each against a local stand-in for LangSmith's API, and shows that without the guard the run does reach it.
  - *Pages that change between runs:* a page whose content differs from run to run, such as one with a random slug, can't be replayed from a cassette. The pilot's full explores are live, recorded runs whose evidence cites the SHA.
- **Fake model:** scripted chat model for graph-transition tests (e.g., "heal returns `expectation_violated`" → assert finding created, no patch).
- **Pilot pages:** saved renderings of the pages the five pilot specs visit, from the clean local Conduit stack, in `packages/runner/tests/pilot_renderings/`. `capture.py` there captures them again, and `packages/runner/tests/pilot_pages.py` loads one with Conduit's own stylesheets. The locator generator's rules are tested on them (§2, ADR-0025).
- **Fixture pages:** small static apps under `tests/fixtures/pages/` for each drift type (moved element, renamed label, removed element, new modal, canvas widget) and each hostile behavior (§1 Egress & secrets).

## 5. Benchmark

Location: `bench/`. Two open-source apps with **60 planted bugs** behind feature flags across six categories (functional, visual/layout, backend 5xx, JS error, broken flow, data display) and **20 benign UI changes** (moved/restyled/relabeled elements, reordered nav — every expectation still holds).

**Flags and ground truth (ADR-0022):** each case is one opaque flag, set through `BENCH_FLAGS` when the app's containers are recreated (which also reseeds the database). Specs reach app state only through the app's `/test-api/*` reset hook and narrow read-only probes. Ground truth lives only in `bench/manifest.v1.json`, keyed by case ID.

**Browser settings:** every run pins its browser settings (time zone UTC, locale en-US, viewport 1280×800), and the compiled script records the settings it was explored under (ADR-0025). Benchmark runs use the defaults, as `bench/harness/toggle_checks.py` does. Apps render dates in the viewer's time zone. Conduit's seeded article times (10:00 UTC) show their seeded dates only between UTC−10 and UTC+13, so a replay in another zone would fail the clean app.

**Pilot first (M0):** one app, ~5 bugs and 2 benign changes, development-only — enough to validate the compiler, the expectation-coverage rules, and the heal contract before investing in the full benchmark. Pilot cases join the dev split. The full benchmark is written and its split frozen at the start of M3, before any tuning on it.

### Splits (frozen before any tuning)
| Split | Bugs | Benign changes | Use |
|---|---|---|---|
| **dev** | 20 | 6 | Building and tuning prompts, routing, heuristics; PR smoke subset |
| **test (frozen)** | 40 | 14 | Release numbers only; never run in CI, never used for tuning |

- Related bugs (same code path or same "family") are assigned to the same split so the test set isn't leaked through near-duplicates.
- The split manifest is committed with a hash before M1: `bench/manifest.v1.split.sha256` covers each case's split and family, and a unit test recomputes it (ADR-0022). Once test results influence a design decision, that test set is **burned**: the next release uses a freshly written test set (v2), and the README says which version produced its numbers.

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
- **Explore applies the same rule** (ADR-0024). If a confirmation replay fails and a later one passes with the identical compiled script, the spec is flaky: explore writes nothing and reports why.

## 7. Dashboard quality

- Every screen has Playwright tests for its loading/empty/error/permission states.
- Visual snapshots at 390px and 1440px in dark and light themes; UI PRs attach before/after screenshots (project acceptance rule).
- Axe accessibility checks on core flows (no serious/critical violations).

## 8. Quality gates (CI)

A change is mergeable only when every gate that runs passes. The jobs are in `.github/workflows/ci.yml` (ADR-0019, ADR-0029) and, for `floor`, `.github/workflows/floor.yml` (ADR-0028 amendment). Each job is a required check on `main`, and a gate with nothing to check yet is left out rather than faked. [AGENTS.md §4](AGENTS.md#4-commands) runs the same gates locally.

| Gate | CI job | Runs |
|---|---|---|
| `ruff check` · `ruff format --check` | `python` | Now |
| `mypy --strict`, in two runs: the packages, `spikes` and `tests`, then the scripts in `bench/harness` and `.claude/hooks` | `python` | Now |
| `pytest --cov`: every test in `packages/`, `spikes/`, `bench/harness/`, `.claude/hooks/` and `tests/`, including CONSTRAINTS.md's threshold checks and the coverage floor | `python` | Now. §1's layers (unit, integration, isolation, checkpointer, continuation, hygiene, egress/secrets, strict-mode) join as their code lands |
| Browser tests: the sandbox check, the browser session and the spike's trial (#37), then the egress fixtures | `python`, after steps that install Playwright's headless shell, log the runner's AppArmor setting and relax it (ADR-0026) | The sandbox check (#35), the browser session (#36), the trial (#37), and the egress proxy's and runner-side requests' (#42) now. #43's packet-level egress suite joins when it lands |
| Dependency audit of `uv.lock` and the checks image's `bench/harness/checks-requirements.txt` (osv-scanner) | `dependency-audit` | Now, and weekly |
| Guard tests · policy scan | `guardrails` | Now |
| The floor's moves: `policy_guard.py --diff refs/remotes/origin/main`, whose approval-class findings pass with the maintainer's `floor-change-approved` label (ADR-0028 amendment) | `floor` | Now, on pull requests, and again when a label is added or removed |
| Secret scan (gitleaks, full history) | `secrets` | Now |
| `fallow audit` | `fallow` | Now, on pull requests. It checks TypeScript only, so it finds nothing until M7 |
| `pnpm lint` · `pnpm typecheck` · `vitest` · Playwright component/page tests | — | M7, with the dashboard, and its dependency audit |
| Benchmark smoke (dev split) | — | M3, with the `bench/` runner. `aqa run` produces verdicts from M2 |

The thresholds these gates hold, coverage included, are in [CONSTRAINTS.md](CONSTRAINTS.md).

**Never** silence a check to go green: no new `# type: ignore`, `@ts-expect-error`, `eslint-disable`, `pytest.skip`, weakened assertions, or lowered thresholds without an ADR.

## 9. Evidence rule

Test and benchmark evidence produced before a rebase or merge is stale. Re-run the gate on the final HEAD and record the SHA in the PR description.
