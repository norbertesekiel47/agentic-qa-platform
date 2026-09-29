# Architecture — Agentic QA Platform

Last updated: 2026-09-27 (revised after external review). Decisions referenced as ADR-NNNN live in [`ADRs/`](ADRs/).

## 1. System context

```
                    ┌──────────────────────────────┐
  Developer ──git──►│ Customer repo (GitHub)       │  qa/*.spec.md + qa/.compiled/*.json
                    └───────┬──────────────▲───────┘
                  PR event  │              │ check runs, "Accept heal" commits
                            ▼              │
┌──────────────────────────────────┐   ┌───┴────────────────────────────────────────────┐
│ Customer CI (GitHub Actions)     │   │ AWS (our account)                              │
│  runner container (same image)   │──►│  API Gateway (HTTP + WebSocket) → API Lambda   │
│  auth: GitHub OIDC → run token   │   │  RDS Postgres (RLS)   S3 artifacts   SQS       │
│  LLM: customer key (GH secret)   │   │  Dispatcher Lambda → Runner Lambdas (no VPC)   │
└──────────────────────────────────┘   │  KMS (BYOK, test secrets)  CloudWatch  fck-nat │
                                       └───┬──────────────────────────▲─────────────────┘
                                           │                          │
                         ┌─────────────────▼─────┐          ┌─────────┴─────────┐
                         │ Dashboard (Next.js    │          │ Clerk (identity,  │
                         │ static export, S3 +   │          │ orgs, webhooks)   │
                         │ CloudFront)           │          └───────────────────┘
                         └───────────────────────┘
       External: LLM providers (BYOK) · Apps under test (verified domains for hosted runs) · LangSmith (non-customer traces)
```

## 2. Components

| Component | Responsibility | Tech |
|---|---|---|
| **CLI (`aqa`)** | Local explore/replay/heal, report viewer, upload | Python, Typer |
| **Runner** | Executes one run: the LangGraph run graph + Playwright browser | Python container image (ADR-0008) |
| **Run graph** | State machine for a run (replay → heal → verify → report) | LangGraph (ADR-0006) |
| **Model router** | Role-based model selection, capability checks, cost accounting | LangChain chat models + LiteLLM price map (ADR-0007) |
| **API** | REST control plane, result ingestion, auth, presigned uploads | FastAPI on Lambda via API Gateway HTTP API |
| **Realtime** | Live run events to the dashboard | API Gateway WebSocket API + Lambda |
| **Dispatcher** | Pulls run requests from SQS, enforces per-org concurrency, issues continuation leases, invokes runners | Lambda |
| **GitHub App** | Webhooks, check runs, heal commits, spec PRs | Part of API; GitHub App auth |
| **Database** | System of record; tenant isolation via RLS | RDS PostgreSQL (ADR-0009) |
| **Artifact store** | Screenshots, accessibility snapshots, traces, HTML reports | S3 with lifecycle expiry |
| **Dashboard** | Nine-screen web app (client-rendered; the API enforces all authorization) | Next.js static export on S3 + CloudFront (ADR-0017) |
| **Identity** | Users, orgs, roles | Clerk Organizations (ADR-0010) |

## 3. The run graph (LangGraph)

```
            ┌──────────────┐
  start ──► │ load_spec    │  spec + compiled script (if any) + config
            └──────┬───────┘
         compiled? │ no ─────────────────────────────┐
                   ▼ yes                              ▼
            ┌──────────────┐  step fails     ┌──────────────┐
            │ replay_step  │────────────────►│ heal         │ (subgraph)
            └──────┬───────┘                 │  observe     │ a11y snapshot + screenshot + logs
                   │ all steps ok            │  classify    │ drift_consistent | expectation_violated | inconclusive
                   ▼                         │  repair/file │ binding/step patch or finding with evidence
            ┌──────────────┐                 └──────┬───────┘
            │ verify       │◄───────────────────────┘ (resume after binding repair)
            │  expect[]    │  deterministic checks (incl. deterministic visual checks);
            │  invariants  │  an assertion whose target won't resolve → heal (binding repair only)
            └──────┬───────┘  console / 5xx / exceptions / broken images
                   ▼
            ┌──────────────┐
            │ report       │  verdicts + evidence refs → API
            └──────────────┘
   explore (no compiled script) = agent loop: observe → act (tool call) → check expects → compile on success
```

### 3.1 Replay modes (ADR-0003 amendment)
- **Every expectation is covered:** each expectation compiles to ≥ 1 check that actually establishes it (UI text, target content, URL, the browser's own network traffic, read-only state probes, deterministic visual checks). Expectations with no establishing check fail compilation by name — no weaker proxies (DATA_MODEL §7).
- **`strict` (default; CI):** zero LLM calls. Every assertion must compile to a deterministic check: text/URL/visibility checks, **deterministic visual checks** (element in viewport, not occluded — hit-test at the element's center returns the element or a descendant — minimum size/contrast, optional pixel-diff against a baseline region). A spec whose assertions cannot compile deterministically fails compilation in strict mode with a clear error. A test asserts the model client is never constructed in strict mode.
- **`verified` (opt-in per spec):** strict checks plus model-assisted visual verification for assertions marked `visual: model`. Reported separately: its own cost line, its own benchmark denominators, never mixed into strict-mode "$0 replay" figures.

### 3.2 What healing may change (ADR-0003 amendment)
The healer cannot observe *intent* — only behavior. So it classifies what it *can* observe:
- **`drift_consistent`** — the UI changed (element moved/renamed/restyled), and after repairing **target bindings** (locators) and/or non-side-effect steps, **every `expect` assertion evaluates and passes and every invariant holds**. Output: a patch proposal. This covers drift in both action targets and assertion targets.
- **`expectation_violated`** — an assertion or invariant fails even with the best repair. Output: a finding (bug) with evidence.
- **`inconclusive`** — neither can be established within the budget. Escalated to a human.

The compiled format separates **targets** (an element's meaning + locators) from **assertions** (what must be true). **Heal patches may change target locators and non-side-effect steps only; assertions, target meanings, side-effect steps, `side_effect` flags, and invariants are immutable in heals** — enforced by the patch validator (DATA_MODEL §7). A binding that can't be resolved is drift (heal path); a resolved target whose check evaluates false is `expectation_violated`. Changing what "correct" means requires a human edit to the spec in the PR. PR context (title, description, spec diff) may be passed to the healer as *hints*, but the platform never labels a change "intentional."

### 3.3 Checkpointing and continuation (ADR-0006 amendment)
A checkpoint preserves graph state, not the browser. Resuming therefore requires rebuilding the session:
- Every compiled step carries a required `side_effect` flag: `true` for submits, purchases and deletes; `false` for **replay-safe** steps (navigation, reads, idempotent fills). It is never defaulted. The explorer infers the flag; humans can override in the spec.
- At a checkpoint the runner also saves Playwright **storage state** (cookies, localStorage) — encrypted, via the API, deleted when the run ends.
- **Step intents:** before any action is dispatched, the runner writes a `run_steps` **intent** row (with the current `lease_id`); completion updates it. The API rejects writes carrying a stale lease, so a superseded invocation can't record progress. An unresolved intent on a `side_effect` step means the outcome is unknown (e.g., crash after clicking Pay, before recording) — that run is **non-resumable**.
- **Continuation:** a fresh invocation restores storage state and re-executes replay-safe steps up to the last completed step to rebuild page state, then continues. **It never re-executes a `side_effect` step automatically**, and never continues past an unresolved side-effect intent; those runs end `errored: non_resumable` with evidence.
- **Reset:** if the spec declares a `reset` hook, a non-resumable run may restart as a **new attempt from step 1** with a fresh browser, after calling the hook. The hook undoes only what the app's reset endpoint undoes; the report records both attempts.
- **Leases:** the dispatcher (control plane) issues a new run token + continuation lease per invocation (max 3); runners never renew their own tokens.
- Most specs finish well inside one 15-minute invocation; continuation is the exception path, and v1 supports it only for specs meeting the rules above.

### 3.4 Agent tools

| Tool | Purpose |
|---|---|
| `navigate(url)` | Only URLs on the run's allowed origin set (re-validated after redirects) |
| `click(ref)`, `fill(ref, text)`, `select(ref, option)`, `press(key)` | Act on accessibility-tree element refs |
| `fill_secret(ref, name)` | Inject a named secret at the browser layer, only into a field and origin the secret is bound to |
| `screenshot(region?)` | Visual observation for verification |
| `vision_click(x, y)` | Vision fallback, only when the tree lacks a usable ref |
| `assert_*` | Deterministic checks that compile directly into the script |
| `finish(verdict)` | Structured verdict (Pydantic-validated) with evidence refs |

Page content is **untrusted data**: observations enter the model context inside delimited blocks after secret-value redaction; browser-wide egress is enforced independently of the agent's tools (see [SECURITY §4, §7](SECURITY.md#4-prompt-injection)).

## 4. Key flows

### 4.1 CI run (customer's GitHub Actions)
1. PR opened → workflow runs the runner container with the `aqa` Action.
2. Action requests a GitHub OIDC token (`aud=agentic-qa`) and exchanges it at `POST /v1/auth/oidc/exchange`. The API validates the token and the project's trust policy — immutable `repository_id`/`repository_owner_id`, allowed `event_name` values, allowed `workflow_ref` (and optionally pinned `workflow_sha`) for ordinary workflows — or `job_workflow_ref` when a reusable workflow is required — single use per `jti` — and returns a **run-scoped token** (≤15 min). The token's `sha` claim is recorded as `execution_sha` (for `pull_request` events, GitHub's merge commit); the PR **head** SHA is recorded separately as `head_sha` (from the webhook/API) and is what heal staleness checks compare. Fork PRs don't receive OIDC tokens and can't upload (documented).
3. Runner replays each compiled spec in strict mode against the preview URL; heals only on drift using the customer's LLM key from GitHub secrets (never sent to us).
4. Runner uploads step records and requests presigned S3 URLs for artifacts.
5. API writes results; GitHub App posts **one check run per spec** (see §4.3).

### 4.2 Hosted run (dashboard-triggered)
1. `POST /v1/runs` → API checks domain verification, rate limits → enqueues to SQS.
2. Dispatcher enforces per-org concurrency (DB counter) → issues a run token + lease → invokes Runner Lambda asynchronously with `{run_id, run_token}` only.
3. Runner startup hygiene (§5) → fetches the spec bundle, the provider key, and any test secrets the spec references over HTTPS using the **hosted-execution** run token (held in memory only; CI run tokens can't retrieve org-stored secrets) → runs the graph → streams events via the API → WebSocket fan-out.

### 4.3 Checks and Accept heal
- Each spec gets its own check run (`agentic-qa / <spec_id>`), keeping every heal within GitHub's limit of 3 actions per check run and 20-character action identifiers.
- A spec with a heal proposal gets `conclusion: action_required` and one action: label **Accept heal**, identifier `ah_<12-char token>` (opaque, mapped server-side to the proposal). "Open run" is an ordinary link in the check output / `details_url`, not an action.
- On click (`check_run.requested_action` webhook): verify signature → verify the clicking user has write access → load proposal → **reject if stale**: the proposal is bound to the PR head SHA it was computed on and the compiled file's blob SHA; if either changed, the proposal is marked `stale` and a new run is requested → commit the patch via the GitHub App.
- **Fork PRs** (App can't push to the fork): the check output includes the patch and a one-line CLI command (`aqa heal apply <proposal>`) for the author to apply.
- The heal commit triggers CI again, which replays the healed script in strict mode — closing the loop deterministically.

### 4.4 Tenancy chain (identity → database)
```
Clerk session JWT ─► verify signature (JWKS), iss, exp, azp
                  ─► read org claim o.id (e.g. "org_2abc…", a Clerk string ID — not a UUID)
                  ─► resolve o.id → organizations.id (UUID) via clerk_org_id; reject if absent/unmapped
                  ─► per-request transaction:
                        SELECT set_config('app.org_id', %s, true);   -- parameterized; transaction-local
                        SET LOCAL ROLE app_user;
                  ─► every tenant table: RLS USING (org_id = current_setting('app.org_id', true)::uuid)
```
Credentials that arrive before the tenant is known (API keys, OIDC exchanges, GitHub webhooks) resolve through narrowly privileged `SECURITY DEFINER` lookup functions that return only the identifiers needed (`org_id`, `project_id`) — never tenant rows. The request then proceeds under RLS like any other.

## 5. Network & isolation

- **Chromium sandbox decision gate (M1 spike):** the runner launches Chromium with its sandbox **enabled** (`chromium_sandbox=True`) and verifies it at startup. Community evidence indicates Chromium's sandbox usually can't start on Lambda ("No usable sandbox!", so deployments fall back to `--no-sandbox`). Without it, a renderer exploit runs with the runner process's privileges — and because Lambda reuses warm environments across invocations, that could reach a later tenant's run. **Decision rule:** if the M1 spike proves sandboxed Chromium on Lambda, hosted runs use Lambda as below; if not, **hosted multi-tenant runs use the one-task-per-run Fargate adapter** (a fresh microVM per run — no cross-tenant reuse), and ADR-0008 is superseded. CI runs (customer machines, single tenant) attempt the sandbox and warn if it's unavailable.
- **Isolation boundary (stated precisely, if the gate selects Lambda):** each run executes in a Lambda invocation. Lambda **may reuse an execution environment** (including `/tmp` and any surviving processes) for later invocations of the same function — possibly a different tenant's run — and a timeout does not clear `/tmp`. So isolation is enforced by the runner, not assumed from Lambda:
  - **Startup hygiene (every invocation, before any tenant data is fetched):** kill any leftover child processes, wipe `/tmp`, verify the wipe.
  - A **fresh browser process and a fresh, randomized profile directory** per run; no persistent contexts; downloads disabled.
  - Secrets and provider keys held in memory only; never written to disk by our code.
  - Teardown on normal completion; startup hygiene covers crash/timeout paths.
  - **Crash-reuse tests** (TESTING §1) prove a run that crashes mid-way leaves nothing readable by the next invocation.
  - **Residual risk:** a compromised browser that escapes its sandbox inside a warm environment could observe a later run in that environment. For stronger isolation, the compute-backend interface supports a one-task-per-run Fargate adapter (roadmap).
- **Runner Lambdas run outside the VPC**, reach the internet directly (apps under test, LLM providers) at no NAT cost, and have **no route or credentials** to RDS. Their execution role has only CloudWatch Logs permissions.
- **API Lambda runs inside the VPC** (private subnets) to reach RDS. Outbound (Clerk JWKS, GitHub API, KMS, SQS, API Gateway management API) goes through a **fck-nat** instance (~$3/month) instead of a managed NAT Gateway (~$32/month + data). S3 uses a free **gateway VPC endpoint**.
- **Connection management:** no RDS Proxy (cost). API Lambda reserved concurrency is capped and each container reuses one pooled connection across invocations.

## 6. Durability & failure modes

| Failure | Handling |
|---|---|
| Runner hits the Lambda 15-min limit | Continuation per §3.3 (new lease + token, max 3); non-resumable specs end `errored: non_resumable` |
| Runner crash mid-step | One retry from the last checkpoint **only if** no `side_effect` step is in flight; otherwise `errored` with logs |
| LLM provider 429/5xx | Exponential backoff; optional per-role fallback model from org config |
| Duplicate SQS delivery | Idempotency key on dispatch; runner claims the run atomically |
| GitHub webhook redelivery | `webhook_events` dedupe by delivery ID |
| Flaky page load | Bounded waits on network idle + element stability; flake counted, never silently retried away |
| WebSocket disconnect | Client reconnects with `since_seq`; missed events re-sent from `run_events` |

## 7. Realtime design

- **Connect:** a 60-second ticket (minted by the API for an authenticated Clerk user) is checked by the `$connect` authorizer — API Gateway authorizes WebSockets only at connect time.
- **Stored state:** `ws_connections` (connection ID, org, user, expires_at ≤ 2 h — API Gateway's hard connection limit) and `ws_subscriptions`.
- **Every `subscribe` is authorized** against the user's current membership and the run's org. Membership removal (Clerk webhook) deletes that user's connections via the `@connections` management API.
- **Delivery:** events carry a per-run `seq`, persisted in `run_events` (7-day retention); reconnecting clients send `since_seq`. `GoneException` responses delete stale connections. Clients send heartbeats inside the 10-minute idle timeout.
- IAM: only the realtime/API functions hold `execute-api:ManageConnections`, scoped to the WebSocket API.

## 8. Cost design (AWS)

No NAT Gateway · runners bill per ms with zero idle · Lambda always-free tier covers portfolio traffic · S3 lifecycle rules expire artifacts (30 days default) · AWS Budgets alarm from day one · single-AZ db.t4g.micro · static dashboard on S3 + CloudFront. Estimated ~$20–30/month at portfolio traffic (see [TECH_STACK §5](TECH_STACK.md#5-aws-services-and-cost)).

## 9. Observability

OpenTelemetry spans across CLI → API → dispatcher → runner, using GenAI semantic conventions for LLM calls (pinned convention version). Export: **LangSmith** for benchmark/demo/dev runs only; **CloudWatch** for infra metrics and logs. Customer production runs are never exported to third parties by default.

## 10. Cross-project trace contract

Runs emit OTel traces that the Calibrated Eval Toolkit ingests. The contract is versioned (`contracts/agentic-qa-trace.v1.json` + a fixture trace shared by both repos):

| Attribute (span) | Meaning |
|---|---|
| `eval.item_id` (root) | Benchmark case ID (e.g., `conduit-bug-017`); for non-benchmark runs, `<spec_id>` |
| `eval.repeat` (root) | Repeat index (0-based) |
| `aqa.run_id`, `aqa.spec_id`, `aqa.mode` (root) | Run identity; `strict` / `verified` / `explore` |
| `aqa.verdict` (heal span) | `drift_consistent` \| `expectation_violated` \| `inconclusive` |
| `aqa.step.seq`, `aqa.step.kind` (step spans) | Step identity |

**Ground truth never travels in traces.** It lives in the benchmark manifest (`bench/manifest.v1.json`, keyed by case ID), which the toolkit joins on `eval.item_id`.

The runner's browser tools can optionally be exposed as MCP tools and routed through the MCP Defense Gateway (demo configuration, not a runtime dependency).

## 11. Decision index

ADR-0001 product choice · 0002 hybrid perception · 0003 explore–compile–heal (amended: strict/verified replay, drift classification) · 0004 specs in repo · 0005 Python + Next.js · 0006 LangGraph (amended: continuation contract; one required `side_effect` step flag) · 0007 model routing · 0008 runner image & Lambda (amended: isolation boundary; sandbox decision gate) · 0009 RDS + RLS · 0010 Clerk · 0011 BYOK · 0012 domain verification · 0013 CI auth (amended: OIDC trust policy) · 0014 observability · 0015 benchmark (amended: dev/test split) · 0016 build order & license · 0017 dashboard hosting · 0018 fallow commit gate (amended: CI) · 0019 CI with a switchable runner · 0020 benchmark apps (amended: localization scope) · 0021 bench/apps gate carve-out · 0022 benchmark flags, test-only endpoints and manifest (amended: invariant ground truth; residual exposure) · 0023 pilot toggle checks and dry compile review (amended: sandboxed checks image; every check under every flag).
