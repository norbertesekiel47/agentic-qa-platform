# Data Model — Agentic QA Platform

Last updated: 2026-09-27 (revised after external review). PostgreSQL 16+ on RDS. Internal IDs are UUIDv7 (time-ordered). External identifiers (Clerk org/user IDs, GitHub IDs) are stored as their native strings/integers and mapped to internal IDs. All timestamps `timestamptz` UTC.

## 1. Entity overview

```
organizations ─┬─< memberships >── users
               ├─< projects ─┬─< repositories (GitHub)
               │             ├─< specs ─< spec_versions
               │             ├─< runs ─┬─< run_steps ─< artifacts
               │             │         ├─< run_events
               │             │         ├─< verdicts ─┬─< findings (bugs)
               │             │         │             └─< heal_proposals
               │             │         ├─< llm_calls
               │             │         ├─< checkpoints ─< checkpoint_writes
               │             │         └─< run_storage_states
               │             ├─< test_secrets
               │             └─< triage_labels
               ├─< domains (verification)
               ├─< provider_keys (BYOK, encrypted)
               ├─< api_keys (hashed)
               ├─< github_installations
               ├─< ws_connections ─< ws_subscriptions
               └─< audit_events
webhook_events · oidc_exchanges (global, service role only)
```

## 2. Tables

### Tenancy & identity (mirrored from Clerk via webhooks)
| Table | Key columns |
|---|---|
| `organizations` | `id` (UUID), `clerk_org_id` (text, unique — e.g. `org_2abc…`), `name`, `slug`, `plan` (`free` in v1), `created_at` |
| `users` | `id`, `clerk_user_id` (text, unique), `email`, `name` |
| `memberships` | `org_id`, `user_id`, `role` (`admin`\|`member`\|`viewer`) |

### Projects & source
| Table | Key columns |
|---|---|
| `projects` | `id`, `org_id`, `name`, `default_branch`, `settings` (jsonb: model routing, invariants, retention days) |
| `github_installations` | `id`, `org_id`, `installation_id`, `account_id` (immutable), `account_login`, `permissions` (jsonb) |
| `repositories` | `id`, `org_id`, `project_id`, `installation_id`, `github_repo_id` (immutable, unique), `owner_id` (immutable), `full_name` (display only), `oidc_policy` (jsonb: allowed `event_name`s; allowed `workflow_ref`s — plus optional pinned `workflow_sha`s — for ordinary workflows; allowed `job_workflow_ref`s only when a reusable workflow is required; allowed refs) |
| `specs` | `id`, `org_id`, `project_id`, `path` (e.g. `qa/checkout.spec.md`), `latest_version_id` |
| `spec_versions` | `id`, `org_id`, `spec_id`, `commit_sha`, `branch`, `content_hash`, `spec_json` (parsed), `compiled_json` (nullable), `compiled_blob_sha`, `compiled_hash` |
| `test_secrets` | `id`, `org_id`, `project_id`, `name` (e.g. `TEST_PASSWORD`), `ciphertext`, `nonce`, `encrypted_dek`, `kms_key_id`, `allowed_origins` (text[]), `allowed_field_hint` (e.g. `password`), `created_by`, `rotated_at` — write-only via API; decryptable **only** by hosted-execution run tokens issued by the dispatcher (CI runs use GitHub secrets and can never retrieve these) |

### Runs
| Table | Key columns |
|---|---|
| `runs` | `id`, `org_id`, `project_id`, `spec_version_id`, `trigger` (`ci`\|`hosted`\|`local_upload`), `mode` (`explore`\|`strict`\|`verified`), `status` (`queued`\|`running`\|`passed`\|`failed`\|`heal_proposed`\|`errored`\|`cancelled`), `error_code` (e.g. `non_resumable`), `target_url`, `execution_sha` (the commit actually tested — for `pull_request` workflows, GitHub's merge commit), `head_sha` (the PR head commit, used for heal staleness), `pr_number`, `runner_location` (`ci`\|`hosted`), `attempt` (increments on `reset`), `continuations`, `lease_id`, `started_at`, `finished_at`, `llm_cost_usd`, `idempotency_key` (unique per org) |
| `run_steps` | `id`, `org_id`, `run_id`, `attempt`, `seq`, `lease_id`, `kind` (`action`\|`assert`\|`observe`\|`heal`), `action` (jsonb), `target_used`, `locator_used`, `side_effect` (bool), `state` (`intent`\|`completed`\|`failed`\|`skipped`), `intent_at`, `completed_at`, `duration_ms`, `error` — the **intent** row is written before the action is dispatched; completion updates it; writes carrying a stale `lease_id` are rejected |
| `run_events` | `run_id`, `seq` (per-run monotonic), `org_id`, `type`, `payload` (jsonb), `created_at` — realtime replay; 7-day retention |
| `artifacts` | `id`, `org_id`, `run_id`, `step_id` (nullable), `type` (`screenshot`\|`a11y_snapshot`\|`console_log`\|`network_har`\|`trace`\|`report_html`), `s3_key`, `bytes`, `sha256`, `expires_at` |
| `checkpoints` | `org_id`, `run_id`, `thread_id`, `checkpoint_ns`, `checkpoint_id`, `parent_checkpoint_id`, `type`, `checkpoint` (bytea, serialized, ≤ 1 MB compressed), `metadata` (jsonb) — PK `(thread_id, checkpoint_ns, checkpoint_id)` |
| `checkpoint_writes` | `org_id`, `thread_id`, `checkpoint_ns`, `checkpoint_id`, `task_id`, `idx`, `channel`, `type`, `value` (bytea) — PK `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)`; upserts are idempotent |
| `run_storage_states` | `run_id`, `org_id`, `checkpoint_id`, `ciphertext`, `nonce`, `encrypted_dek` — Playwright storage state for continuation; deleted at run end |

### Verdicts & triage
| Table | Key columns |
|---|---|
| `verdicts` | `id`, `org_id`, `run_id`, `step_id`, `type` (`pass`\|`drift_consistent`\|`expectation_violated`\|`inconclusive`), `confidence` (0–1, model-reported, uncalibrated), `rationale`, `evidence` (jsonb: artifact IDs, a11y diff, log excerpts) |
| `findings` | `id`, `org_id`, `verdict_id`, `category` (`functional`\|`visual`\|`backend_5xx`\|`js_error`\|`broken_flow`\|`data_display`), `title`, `repro_steps` (jsonb), `status` (`open`\|`resolved`\|`not_a_bug`) |
| `heal_proposals` | `id`, `org_id`, `verdict_id`, `spec_version_id`, `patch` (JSON Patch — restricted to target locators and non-side-effect steps; rules in §7), `base_head_sha`, `base_compiled_blob_sha`, `action_token` (`ah_` + 12 chars, unique), `status` (`pending`\|`accepted`\|`rejected`\|`stale`), `resolved_by`, `commit_sha` |
| `triage_labels` | `id`, `org_id`, `verdict_id`, `label` (`correct`\|`should_be_expectation_violated`\|`should_be_drift_consistent`\|`not_a_bug`), `user_id` — ground truth for evals |

### Models & cost
| Table | Key columns |
|---|---|
| `llm_calls` | `id`, `org_id`, `run_id`, `role`, `mode` (`explore`\|`heal`\|`verified`), `provider`, `model`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `latency_ms`, `cost_usd`, `status` |

### Security & admin
| Table | Key columns |
|---|---|
| `domains` | `id`, `org_id`, `hostname`, `method` (`dns_txt`\|`well_known`), `token_hash`, `status` (`pending`\|`verified`\|`failed`), `verified_at`, `last_checked_at` |
| `provider_keys` | `id`, `org_id`, `provider`, `label`, `ciphertext` (AES-256-GCM), `nonce`, `encrypted_dek` (KMS-wrapped data key), `kms_key_id`, `last4`, `created_by`, `rotated_at` |
| `api_keys` | `id`, `org_id`, `project_id`, `prefix` (`aqa_live_xxxx`), `hash` (argon2id of the secret part), `scopes`, `last_used_at`, `revoked_at` |
| `ws_connections` | `connection_id`, `org_id`, `user_id`, `connected_at`, `expires_at` (≤ 2 h) |
| `ws_subscriptions` | `connection_id`, `run_id`, `org_id`, `last_seq_sent` |
| `audit_events` | `id`, `org_id`, `actor_type` (`user`\|`api_key`\|`github`\|`system`), `actor_id`, `action`, `target`, `metadata`, `ip`, `created_at` (append-only; includes test-secret retrievals by runs) |
| `webhook_events` | `id`, `source` (`clerk`\|`github`), `delivery_id` (unique), `received_at`, `processed_at` (service role only) |
| `oidc_exchanges` | `jti` (unique), `repository_id`, `sha`, `exchanged_at`, `expires_at` — single-use enforcement (service role only) |

## 3. Row-level security

Every table with `org_id` has RLS enabled and forced:

```sql
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON runs
  USING      (org_id = current_setting('app.org_id', true)::uuid)
  WITH CHECK (org_id = current_setting('app.org_id', true)::uuid);
```

- The API connects as `app_user` (no `BYPASSRLS`, not the table owner). Each request runs in a transaction that begins with the **parameterized** call `SELECT set_config('app.org_id', %s, true)` (transaction-local). `SET LOCAL … = $1` cannot be parameterized server-side, so it is never used.
- `app.org_id` is always the **internal UUID**, resolved from Clerk's `o.id` (a string such as `org_2abc…`) via `organizations.clerk_org_id`. Unknown or missing orgs are rejected before any tenant query.
- If `app.org_id` is unset, `current_setting(..., true)` returns NULL and **no rows match** — fail closed.
- **Pre-tenant lookups** use `SECURITY DEFINER` functions owned by a restricted role, each returning only identifiers: `auth_api_key(prefix) → (org_id, project_id, hash, scopes)`, `auth_repo(github_repo_id, owner_id) → (org_id, project_id, oidc_policy)`, `auth_installation(installation_id) → org_id`. No request path reads tenant tables outside RLS.
- A separate `app_service` role owns tables; it is used only by migrations and global dedupe tables, never on request paths.
- Mandatory test: for every tenant table, a test proves org A cannot select, insert, update, or delete org B's rows (see [TESTING §3](TESTING.md#3-tenant-isolation-tests)).

## 4. Indexes (initial)

`runs (org_id, project_id, created_at desc)` · `runs (org_id, status)` · `run_steps (run_id, seq)` · `run_events (run_id, seq)` · `verdicts (run_id)` · `heal_proposals (org_id, status)` · unique `heal_proposals (action_token)` · `findings (org_id, status)` · `llm_calls (org_id, created_at)` · `spec_versions (spec_id, commit_sha)` · unique `api_keys (prefix)` · unique `domains (org_id, hostname)` · unique `test_secrets (project_id, name)` · `ws_subscriptions (run_id)`.

## 5. Retention

| Data | Default | Configurable |
|---|---|---|
| Artifacts (S3) | 30 days (lifecycle rule) | 7–90 days per project |
| Run rows & verdicts | 180 days | Yes |
| Run events (realtime replay) | 7 days | No |
| Checkpoints, checkpoint writes, storage states | Deleted when the run completes (storage states) / 7 days after (checkpoints) | No |
| LLM call records | 13 months (cost reporting) | No |
| Audit events | 1 year | No |

Org deletion: hard-deletes all tenant rows and S3 prefixes, and schedules KMS-wrapped keys for destruction; logged in a global deletion ledger.

## 6. Spec file format

`qa/checkout-expired-card.spec.md`:

```markdown
---
id: checkout-expired-card
goal: A returning user tries to buy a hoodie with an expired saved card and is told clearly why it failed.
preconditions:
  start_url: /
  account: { email: returning@example.test, password: { secret: TEST_PASSWORD } }
  seed: fixtures/returning-user-expired-card.json
  reset: { http: "POST /test-api/reset?fixture=returning-user-expired-card" }   # optional; restarts the run as a new attempt
  probes:                        # optional read-only GET endpoints on allowed origins, for observing app state
    orders_count: "GET /test-api/orders/count?email=returning@example.test"
steps:            # optional hints; the agent may deviate
  - Log in
  - Add "Classic Hoodie" (size M) to the cart
  - Check out with the saved card
expect:           # one observable claim per item; every item must compile to ≥ 1 check
  - An error message says the card has expired
  - No order is created for this user
  - The cart still contains the Classic Hoodie (size M)
  - The user remains on the payment step
  - text: The "Pay" button is visible and not covered by any overlay
    visual: deterministic        # deterministic (default) | model (requires verified mode)
invariants:
  inherit: true
  disable: [console_errors]   # this app logs an expected warning here
allowed_origins: [ "https://payments-sandbox.example.test" ]   # extra origins beyond start_url's (must be verified for hosted runs)
tags: [checkout, payments]
---

Free-form notes for humans (ignored by the agent unless referenced).
```

## 7. Compiled script format

`qa/.compiled/checkout-expired-card.json` (machine-written, human-reviewable diffs). **Targets** (what an element *means* + how to find it) are separated from **assertions** (what must be true), so heals can repair bindings without touching expectations.

```json
{
  "schema_version": 1,
  "spec_id": "checkout-expired-card",
  "spec_hash": "sha256:…",
  "compiled_at": "2026-10-12T14:03:22Z",
  "compiled_by": { "mode": "explore", "models": { "navigator": "claude-haiku-4-5" } },
  "targets": {
    "email_input":    { "semantic": "Email field on the login form",
                        "locators": [ { "role": "textbox", "name": "Email" }, { "testid": "login-email" }, { "css": "#email" } ] },
    "password_input": { "semantic": "Password field on the login form",
                        "locators": [ { "role": "textbox", "name": "Password" } ] },
    "sign_in":        { "semantic": "Sign in button", "locators": [ { "role": "button", "name": "Sign in" } ] },
    "cart_items":     { "semantic": "Line items in the cart summary", "locators": [ { "testid": "cart-lines" }, { "role": "list", "name": "Cart" } ] },
    "pay_button":     { "semantic": "Pay button on the payment step", "locators": [ { "role": "button", "name": "Pay" } ] }
  },
  "probe_baselines": { "orders_count": { "capture_before_seq": 9, "json_path": "$.count" } },
  "steps": [
    { "seq": 1, "action": "navigate", "url": "/login", "replay_safe": true },
    { "seq": 2, "action": "fill", "target": "email_input", "value": "returning@example.test", "replay_safe": true },
    { "seq": 3, "action": "fill_secret", "target": "password_input", "secret": "TEST_PASSWORD", "replay_safe": true },
    { "seq": 4, "action": "click", "target": "sign_in", "replay_safe": true },
    { "seq": 9, "action": "click", "target": "pay_button", "side_effect": true }
  ],
  "assertions": [
    { "id": "a1", "expect_index": 0, "check": "text_visible", "pattern": "(?i)card (has )?expired" },
    { "id": "a2", "expect_index": 1, "check": "network_none", "method": "POST", "url_pattern": "/api/orders", "status_class": "2xx" },
    { "id": "a3", "expect_index": 1, "check": "probe_equals_baseline", "probe": "orders_count" },
    { "id": "a4", "expect_index": 2, "check": "text_in_target", "target": "cart_items", "pattern": "Classic Hoodie.*\\bM\\b" },
    { "id": "a5", "expect_index": 3, "check": "url_matches", "pattern": "/checkout/payment" },
    { "id": "a6", "expect_index": 4, "check": "visible_unoccluded", "target": "pay_button", "min_size_px": [44, 24], "in_viewport": true }
  ]
}
```

### Compilation rules
- **Every `expect` item maps to ≥ 1 assertion** (`expect_index`). If the compiler can't produce a check that actually establishes a clause, compilation **fails and names the clause** — it never substitutes a weaker proxy (e.g., "no confirmation heading" is not accepted as "no order created"). The author either adds a probe/observable or rewrites the clause as the UI condition it really is.
- **Check types:** `text_visible`, `text_in_target`, `not_visible`, `url_matches`, `network_none` / `network_seen` (method + URL pattern + status class, from the browser's own traffic), `probe_equals_baseline` / `probe_equals` (read-only GET to a declared probe on an allowed origin), deterministic visual checks — `visible_unoccluded` (hit-test at the element's center returns the element or a descendant; in viewport; minimum size), `pixel_diff` (region vs committed baseline image, threshold), `contrast_min` — and `model_verify` (only for `visual: model`; rejected in `strict` mode).
- **Targets** are resolved by trying locators in order; the first unique, actionable match wins.

### Replay outcomes
- **Binding unresolved:** no locator resolves a target (for a step *or* an assertion) within the wait budget → drift → heal path.
- **Expectation failed:** the target resolved and the check evaluated false → `expectation_violated` (after invariants and all assertions are evaluated, so the report is complete).

### What a heal patch may change (validator-enforced)
- **Allowed:** `/targets/<id>/locators` (re-binding what an element *is* to how to find it), and actions/values of steps whose `side_effect` is `false` (including inserting or removing `replay_safe` steps, e.g., dismissing a new modal).
- **Forbidden:** anything under `/assertions`; any target's `semantic`; any `side_effect` step other than via its target's locators; adding or removing `side_effect` steps; changing any `replay_safe` / `side_effect` flag; `spec_hash`; invariants; probes.
- A heal is valid only if, after the patch, **every assertion evaluates** (none unresolved) **and passes**, and every invariant holds.

## 8. Benchmark manifest

`bench/manifest.v1.json` is the only home of the benchmark's ground truth (ADR-0022). Traces carry the case ID as `eval.item_id` and never the answer (ARCHITECTURE §10). `bench/harness/manifest.py` validates the file, and CI runs its tests.

```json
{
  "schema_version": 1,
  "cases": {
    "conduit-bug-001": {
      "app": "conduit", "kind": "bug", "category": "data_display",
      "split": "dev", "family": "conduit-article-meta", "flag": "k3q9",
      "summary": "Favorites count on the article page is off by one",
      "expected": [ { "spec": "favorite-article", "verdict": "expectation_violated", "expect": [1] } ]
    },
    "conduit-benign-001": {
      "app": "conduit", "kind": "benign",
      "split": "dev", "family": "conduit-nav", "flag": "h3k8",
      "summary": "Sign-in link relabeled",
      "expected": [ { "spec": "login", "verdict": "drift_consistent" } ]
    }
  }
}
```

| Field | Rule |
|---|---|
| case ID (key) | `<app>-<bug\|benign>-NNN`, agreeing with `app` and `kind` |
| `app` | A directory under `bench/apps/` |
| `kind` | `bug` or `benign` |
| `category` | Bugs only: one of the six `findings.category` values (§2) |
| `split` | `dev` or `test`. Every case in a `family` shares one split (TESTING §5) |
| `family` | `<app>-<name>`: cases on the same code path |
| `flag` | Opaque: 4 lowercase letters or digits, unique across the manifest. Never the case ID, because the frontend's flag list reaches the browser |
| `summary` | One line, for people. It never reaches the system under test |
| `expected` | One entry per scored spec: `spec` is a spec ID with a file at `bench/apps/<app>/qa/<spec>.spec.md`, and `verdict` is `expectation_violated` or `drift_consistent`. An `expectation_violated` entry lists the violated `expect` indexes (0-based, as `expect_index` in §7). A bug needs at least one such entry, and it may also cause drift in other specs. A benign case is `drift_consistent` in every entry |

Unknown keys and duplicate keys are errors. **Split freeze:** `bench/manifest.v1.split.sha256` holds the sha256 of the canonical JSON (sorted keys, no whitespace) of `{case_id: [split, family]}`. A test fails when the hash file and the manifest disagree, so moving a case is a deliberate edit in a reviewed pull request (`python3 bench/harness/manifest.py --split-hash` prints the new value).
