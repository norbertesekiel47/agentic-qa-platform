# Data Model — Agentic QA Platform

Last updated: 2026-09-29 (M1 design decisions, ADR-0024–0026). PostgreSQL 16+ on RDS. Internal IDs are UUIDv7 (time-ordered). External identifiers (Clerk org/user IDs, GitHub IDs) are stored as their native strings/integers and mapped to internal IDs. All timestamps `timestamptz` UTC.

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
| `runs` | `id`, `org_id`, `project_id`, `spec_version_id`, `trigger` (`ci`\|`hosted`\|`local_upload`), `mode` (`explore`\|`strict`\|`verified`), `status` (`queued`\|`running`\|`passed`\|`failed`\|`heal_proposed`\|`errored`\|`cancelled`), `error_code` (e.g. `non_resumable`, `egress_blocked`), `target_url`, `execution_sha` (the commit actually tested — for `pull_request` workflows, GitHub's merge commit), `head_sha` (the PR head commit, used for heal staleness), `pr_number`, `runner_location` (`ci`\|`hosted`), `attempt` (increments on `reset`), `continuations`, `lease_id`, `started_at`, `finished_at`, `llm_cost_usd`, `idempotency_key` (unique per org) |
| `run_steps` | `id`, `org_id`, `run_id`, `attempt`, `seq`, `lease_id`, `kind` (`action`\|`assert`\|`observe`\|`heal`), `action` (jsonb), `target_used`, `locator_used`, `side_effect` (bool), `state` (`intent`\|`completed`\|`failed`\|`skipped`), `intent_at`, `completed_at`, `duration_ms`, `error` — the **intent** row is written before the action is dispatched; completion updates it; writes carrying a stale `lease_id` are rejected |
| `run_events` | `run_id`, `seq` (per-run monotonic), `org_id`, `type`, `payload` (jsonb), `created_at` — catch-up for reconnecting live clients; 7-day retention |
| `artifacts` | `id`, `org_id`, `run_id`, `step_id` (nullable), `type` (`screenshot`\|`a11y_snapshot`\|`console_log`\|`network_log`\|`trace`\|`report_html`), `s3_key`, `bytes`, `sha256`, `expires_at`. A `network_log` is metadata only (method, redacted URL, status, timing, size), and a `trace` is our OpenTelemetry trace. Request and response bodies, HAR files and Playwright traces are never stored (SECURITY §5, ADR-0026) |
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
| `llm_calls` | `id`, `org_id`, `run_id`, `role`, `mode` (`explore`\|`heal`\|`verified`), `provider`, `model`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `latency_ms`, `price_map_version` (the pinned map's upstream commit), `price_source` (`map`\|`config`), `applied_prices` (jsonb: the per-million-token rates used for this call), `cost_usd`, `status` |

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
| Run events (reconnect catch-up) | 7 days | No |
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
  start_url: /                   # a path; the origin comes from the run (`aqa explore --url`), never from the spec
  account: { email: returning@example.test, password: { secret: TEST_PASSWORD } }   # the secret must be declared in the project config (§9)
  reset: { http: "POST /test-api/reset?fixture=returning-user-expired-card" }   # optional; called before every attempt, the first included (ADR-0024)
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
allowed_origins: [ "https://payments-sandbox.example.test" ]   # extra origins the agent may navigate to and act on (must be verified for hosted runs); CDN and font hosts are subresource hosts in the project config (§9)
browser: { viewport: [1440, 900] }   # optional; overrides the project's browser settings (ADR-0025)
tags: [checkout, payments]
---

Free-form notes for humans. The agent never reads the body; anything that affects a run belongs in the frontmatter.
```

**Invariants** are checked on every run on top of the spec's expectations. `inherit: true` turns on all of them, and `disable` lists the ones a spec turns off:

| Name | Fails when |
|---|---|
| `console_errors` | The console records an error-level message: the page's own `console.error` calls, and the browser's own error entries such as "Failed to load resource" for a 4xx or 5xx response |
| `js_exceptions` | The page throws an uncaught exception or leaves a promise rejection unhandled |
| `http_5xx` | Any response to the browser has a 5xx status |
| `broken_images` | An image fails to load |

`console_errors` and `js_exceptions` never overlap: an uncaught exception is not a console error, even though DevTools prints it in the console. In Playwright terms, `console_errors` counts `console` events of type `error` and `js_exceptions` counts `pageerror` events. The benchmark manifest's invariant ground truth (§8) relies on this split.

**Egress blocks and invariants (ADR-0026).**
- *What counts as a block:* a request to a host that is neither an allowed origin nor a subresource host (§9).
- *Effect on the run:* the run refuses the request, and the block alone keeps the run from passing. It is not an invariant failure and not a finding.
- *Expected-blocked hosts* (§9) are the exception. For them, the block's direct symptoms don't count against `console_errors` or `broken_images`: its console error and a broken image, matched by the failed request, never by console text.
- *Indirect effects* always count, such as app code failing because a blocked script never loaded.

**Parsing (M1).** Specs are read before a browser or a model is involved, and every problem is reported at once, each naming the file, the key and the problem.
- *YAML:* the frontmatter is YAML 1.2's core schema (ADR-0030). Anchors, aliases and tags are errors, `yes` and `no` are strings, and a date is a string.
- *Keys:* unknown and duplicate keys are errors, and so is a value of another type, such as a quoted `"3"` for a number.
- *Required:* `id`, `goal`, `preconditions.start_url` and at least one `expect` item. `invariants` defaults to `inherit: true`; `disable` names distinct invariants from the table above, and an `inherit: false` with `disable` is an error.
- *`id`* must equal the file name without `.spec.md`. Spec IDs must be unique across the project, subdirectories of the spec root included; a duplicate is a spec error before anything is written.
- *`start_url`* is a path: one leading `/`, then no whitespace, control characters or backslashes, which a browser could read as `//`, another origin. A query and a fragment are allowed.
- *`account`* takes `email` and `password`, each a string or a secret reference, `{ secret: NAME }`. A reference must name a secret the project config declares (§9).
- *An expectation* is a string, or `{ text, visual }`. `visual: model` is a spec error in M1 (ADR-0024).
- *`allowed_origins` and `browser`* are checked as §9 checks origins and `browser`.

## 7. Compiled script format

`qa/.compiled/checkout-expired-card.json` (machine-written, human-reviewable diffs). **Targets** (what an element *means* + how to find it) are separated from **assertions** (what must be true), so heals can repair bindings without touching expectations. Explore writes it after a confirmation replay passes, or marked unconfirmed (`confirmed: false`) when the path has side-effect steps and the spec has no reset hook (ADR-0024).

```json
{
  "schema_version": 1,
  "spec_id": "checkout-expired-card",
  "spec_hash": "sha256:…",
  "compiled_at": "2026-10-12T14:03:22Z",
  "compiled_by": { "mode": "explore", "models": { "navigator": "claude-sonnet-5-5" }, "price_map": "<upstream commit>" },
  "confirmed": true,
  "browser": { "timezone": "UTC", "locale": "en-US", "viewport": [1440, 900], "device_scale_factor": 1, "color_scheme": "light" },
  "coverage": {
    "plan_hash": "sha256:…",
    "expectations": [
      { "expect_index": 0, "subject": "the payment error message", "claim": "says the card has expired", "assertions": ["a1"] },
      { "expect_index": 1, "subject": "this user's orders", "claim": "no new order exists", "assertions": ["a2", "a3"] },
      { "expect_index": 2, "subject": "the cart summary's line items", "claim": "still include the Classic Hoodie in size M", "assertions": ["a4"] },
      { "expect_index": 3, "subject": "the user's place in checkout", "claim": "still the payment step", "assertions": ["a5"] },
      { "expect_index": 4, "subject": "the payment step's submit button", "claim": "visible and not covered", "assertions": ["a6"] }
    ],
    "requires": []
  },
  "targets": {
    "email_input":    { "semantic": "the login form's email field",
                        "locators": [ { "role": "textbox", "name": "Email" }, { "label": "Email" }, { "css": "#email" } ] },
    "password_input": { "semantic": "the login form's password field",
                        "locators": [ { "role": "textbox", "name": "Password" }, { "placeholder": "Password" } ] },
    "sign_in":        { "semantic": "the login form's submit button",
                        "locators": [ { "role": "button", "name": "Sign in" }, { "css": "form.login button[type=submit]" } ] },
    "cart_items":     { "semantic": "the line items in the cart summary",
                        "locators": [ { "testid": "cart-lines" }, { "css": "ul.cart-lines", "scope": { "css": "app-cart-summary" } } ] },
    "pay_button":     { "semantic": "the payment step's submit button",
                        "locators": [ { "role": "button", "name": "Pay", "scope": { "css": "app-payment-step" } }, { "css": "app-payment-step button[type=submit]" } ] }
  },
  "probe_baselines": { "orders_count": { "capture_before_seq": 9, "json_path": "$.count" } },
  "steps": [
    { "seq": 1, "action": "navigate", "url": "/login", "side_effect": false },
    { "seq": 2, "action": "fill", "target": "email_input", "value": "returning@example.test", "side_effect": false },
    { "seq": 3, "action": "fill_secret", "target": "password_input", "secret": "TEST_PASSWORD", "side_effect": false },
    { "seq": 4, "action": "click", "target": "sign_in", "side_effect": true, "side_effect_basis": "network: POST /api/users/login" },
    { "seq": 9, "action": "click", "target": "pay_button", "side_effect": true, "side_effect_basis": "network: POST /api/payments; model: submits the payment" }
  ],
  "assertions": [
    { "id": "a1", "expect_index": 0, "check": "text_visible", "text": "card has expired" },
    { "id": "a2", "expect_index": 1, "check": "network_none", "method": "POST", "url_pattern": "/api/orders", "status_class": "2xx" },
    { "id": "a3", "expect_index": 1, "check": "probe_equals_baseline", "probe": "orders_count" },
    { "id": "a4", "expect_index": 2, "check": "text_in_target", "target": "cart_items", "pattern": "Classic Hoodie.*\\bM\\b" },
    { "id": "a5", "expect_index": 3, "check": "url_matches", "pattern": "/checkout/payment" },
    { "id": "a6", "expect_index": 4, "check": "visible_unoccluded", "target": "pay_button", "min_size_px": [44, 24], "in_viewport": true }
  ]
}
```

### Compilation rules
- **Every expectation maps to ≥ 1 assertion** (`expect_index`). If the compiler can't produce a check that actually establishes an expectation, compilation **fails and names it** — it never substitutes a weaker proxy (e.g., "no confirmation heading" is not accepted as "no order created"). The author either adds a probe/observable or rewrites the expectation as the UI condition it really is.
- **Coverage plan first (ADR-0024).** Explore writes the plan from the spec alone, before the browser opens, and keeps it frozen for the run. The compiled script stores it as `coverage`:
  - for each expectation: its subject, its claim and the assertions that establish it;
  - `requires`: conditions the goal or expectations need, such as `{ "id": "c1", "condition": "checked after reloading the article page" }`. The step that satisfies a condition carries `"satisfies": ["c1"]`. Compilation fails if a required condition is left without a satisfying step before the assertions;
  - `plan_hash`: identifies the plan the run froze, as written before the browser opened (subjects, claims, planned checks and required conditions). Assertion IDs and `satisfies` marks are compile-time links to it.
- **Subject and claim.** An expectation's subject says what it is about ("jake's comment", "the Pay button"). That is a target's meaning: fixed when the spec is explored, and not re-checked on replay. Everything the expectation says about its subject (text, state, position, destination, count) is its claim, and its assertions must establish all of it. So an expectation that claims what no check can establish, such as "shown above the article list", fails compilation by name.
- **Check types:** `text_visible`, `text_in_target`, `not_visible`, `url_matches`, `network_none` / `network_seen` (method + URL pattern + status class, from the browser's own traffic), `probe_equals_baseline` / `probe_equals` (read-only GET to a declared probe on an allowed origin), deterministic visual checks — `visible_unoccluded` (hit-test at the element's center returns the element or a descendant; in viewport; minimum size), `pixel_diff` (region vs committed baseline image, threshold), `contrast_min` — and `model_verify` (only for `visual: model`; rejected in `strict` mode; explore rejects `visual: model` expectations as a spec error until M2 defines how they confirm).
- **Text parameters (ADR-0025).**
  - `text` is a literal. It matches case-insensitively, at word boundaries, against the element's normalized rendered text (whitespace collapsed, private-use glyphs stripped).
  - `pattern` is a Python regex (`re.search`, flags written out) for claims that need one.

  The compiler prefers `text`. A claim that depends on case uses a `pattern` without `(?i)`.
- **Meanings (ADR-0025).** A target's `semantic` says what the element is for and where it sits, never its current label. If an expectation claims a label, the label belongs in an assertion.
- **Locators (ADR-0025).**
  - *Kinds:* `role` + `name` (the name normalized, then matched exactly), `label`, `placeholder`, `testid`, and `css`. A `css` locator uses a stable id, or stable classes, attributes and custom-element tags: never positional, never generated class names.
  - *Scope:* any locator may carry a `scope` (another locator), and it must be unique inside it.
  - *Order:* action targets list `role` + `name` first.
  - *Assertion targets are never located by what their claim says.*
  - *Validation:* every locator must resolve to the element the agent used, at every use, both at compile time and on the confirmation replay. A target that can't do that at every use is split into separate targets.
- **Resolution per use.** Locators are tried in order, and what counts as a match depends on the use:
  - an action target needs the first unique, actionable match;
  - an assertion's target needs a unique match attached to the page, and doesn't have to receive pointer events;
  - a negative check (`not_visible`) resolves its scope, and zero matches inside it is a result.

  The replay records which locator resolved each target.
- **Every step carries `side_effect`:** `true` for a step that changes app state (submit, purchase, delete), `false` for a **replay-safe** step (navigation, reads, idempotent fills). The flag is required and never defaulted — a missing flag fails validation, because a default of `false` would let a continuation re-execute a purchase (ADR-0006 amendment).
  - *Inference (ADR-0025):* `false` needs positive evidence. The step's settle window must end in idle, with no write request to any host and no WebSocket message sent, and the model must agree. A write that arrives before the next action counts against the step.
  - *Otherwise* the flag is `true`, and `side_effect_basis` says why.
  - *Only a person lowers it,* by editing this file.
- **Actions:** `navigate`, `reload`, `click`, `fill`, `fill_secret`, `select`, `press`. A `navigate` to the start origin stores a path.
- **Browser settings.** `browser` records the settings the script was explored under. Replay uses them rather than the current config (ADR-0025).
- **`spec_hash`** is the sha256 of the canonical JSON of the parsed frontmatter without `tags`, written `sha256:<hex>`. The Markdown body never counts. A mismatch makes the script stale: `aqa explore` redoes it, and replay refuses it.
  - *Canonical JSON:* sorted keys, no whitespace, and non-ASCII characters escaped, as Python's `json.dumps(…, sort_keys=True, separators=(",", ":"))` writes it.
  - *As parsed:* the hash covers the YAML's values, not normalized ones, so `HTTPS://Pay.test` and `https://pay.test` hash differently. A field the model gains later never changes an existing hash.
- **`confirmed`** is `true` when the confirmation replay passed. It is `false` when a path with side-effect steps was written without one because the spec has no reset hook (ADR-0024).
- **Location:** `<spec root>/.compiled/<spec id>.json`. The spec root is the directory holding `config.yaml` (§9), and spec IDs are unique per project (§6).

### Replay outcomes
- **Binding unresolved:** no locator gives the match its use needs (see *Resolution per use*) within the wait budget → drift → heal path.
- **Expectation failed:** the target resolved and the check evaluated false → `expectation_violated` (after invariants and all assertions are evaluated, so the report is complete).
- **Egress block:** a request to an undeclared host keeps the run from passing without producing a finding. The run ends `errored` with `error_code: egress_blocked`, with no verdict, and the run record names the refused host. The CLI exits 6 (§6, API.md §7, ADR-0026).

### What a heal patch may change (validator-enforced)
- **Allowed:** `/targets/<id>/locators` (re-binding what an element *is* to how to find it), and actions/values of steps whose `side_effect` is `false` (including inserting or removing replay-safe steps, e.g., dismissing a new modal).
- **Forbidden:** anything under `/assertions`; any target's `semantic`; any `side_effect` step other than via its target's locators; adding or removing `side_effect` steps; changing any step's `side_effect` flag; `spec_hash`; `/browser`; `/coverage`; invariants; probes; and any patch that leaves a required condition without a satisfying step (a heal may move a `satisfies` mark only onto a step that still meets the condition).
- A heal is valid only if, after the patch, **every assertion evaluates** (none unresolved) **and passes**, and every invariant holds.

## 8. Benchmark manifest

`bench/manifest.v1.json` is the only home of the benchmark's ground truth (ADR-0022). Traces carry the case ID as `eval.item_id` and never the answer (ARCHITECTURE §10). `bench/harness/manifest.py` validates the file, and CI runs its tests.

The format, shown with a fictional `shop` app (these are not real cases):

```json
{
  "schema_version": 1,
  "cases": {
    "shop-bug-001": {
      "app": "shop", "kind": "bug", "category": "data_display",
      "split": "dev", "family": "shop-cart", "flag": "k3q9",
      "summary": "The cart total leaves out the last line item",
      "expected": [ { "spec": "checkout", "verdict": "expectation_violated", "expect": [1] } ]
    },
    "shop-benign-001": {
      "app": "shop", "kind": "benign",
      "split": "dev", "family": "shop-nav", "flag": "h3k8",
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
| `flag` | Opaque: 4 lowercase letters or digits, unique across the manifest. Never the case ID, because the frontend's flag list reaches the browser. `0000` is reserved for the harness self-test |
| `summary` | One line, for people. It never reaches the system under test |
| `expected` | One entry per scored spec: `spec` is a spec ID with a file at `bench/apps/<app>/qa/<spec>.spec.md`, and `verdict` is `expectation_violated` or `drift_consistent`. An `expectation_violated` entry names the violated `expect` indexes (0-based, as `expect_index` in §7), the violated `invariants` (§6), or both. Each index must exist in the spec file's `expect:` list, and the file's `id` must match its name. A bug needs at least one such entry, and it may also cause drift in other specs. List every spec in which the planted change violates an expectation or an invariant along that spec's steps. Leave out a spec where the change only blocks a step: its outcome depends on what healing may do, so it is not scored for that case (LAB_NOTES watch list). A benign case is `drift_consistent` in every entry |

Unknown keys and duplicate keys are errors. **Split freeze:** `bench/manifest.v1.split.sha256` holds the sha256 of the canonical JSON (sorted keys, no whitespace) of `{case_id: [split, family]}`. A test fails when the hash file and the manifest disagree, so moving a case is a deliberate edit in a reviewed pull request ([bench/README.md](bench/README.md) has the command that prints the new value).

## 9. Project config

`qa/config.yaml` is committed next to the specs. The directory that holds it is the **spec root**: compiled scripts live in its `.compiled/` (§7). The file must exist, but every key is optional, so an empty file is valid. It is read as specs are (§6, ADR-0030): unknown and duplicate keys are errors, and every problem is reported at once.

```yaml
base_url: "http://127.0.0.1:4100"   # the start origin when `aqa explore --url` is omitted; `--url` wins
roles:                        # overrides the defaults in TECH_STACK §3
  navigator: { provider: anthropic, model: claude-sonnet-5-5, effort: medium, fallback: claude-opus-5-5 }
browser:                      # settings for every run (ADR-0025); a spec may override them
  timezone: UTC
  locale: en-US
  viewport: [1280, 800]
egress:                       # ADR-0026
  subresource_hosts: [ "fonts.cdn.example.test" ]            # pages may load from these; no navigation, no secrets
  expected_blocked: [ "analytics.example.test" ]             # refused; their direct symptoms don't count against invariants
  private_origins: [ "http://staging.internal.test:8080" ]   # local and CI runs only: may resolve to private addresses
secrets:                      # bindings only; values come from AQA_SECRET_<NAME>
  TEST_PASSWORD: { origins: [ start ], field: password }
  API_TOKEN: { origins: [ start ], field: { role: textbox, name: "API token" } }
models:                       # only for models missing from the pinned price map (ADR-0007 amendment)
  "example-provider/example-model": { capabilities: [tools, structured_output], input_usd_per_mtok: 0.50, output_usd_per_mtok: 1.50 }
budgets:                      # per explore run (ADR-0024)
  attempts: 3
  actions_per_attempt: 40
  model_usd: 3.00
  minutes: 15
  resolve_seconds: 10
```

**Test secrets (ADR-0026).** A spec may reference only secrets declared here.
- `origins` lists where the browser may fill the secret. `start` is the run's start origin, which comes from the invocation (`aqa explore --url`). Any other entry is an origin, and must also be one of the run's allowed origins, because a secret's destinations are the intersection of its binding and the run's allowed origins. A run checks each secret its spec references once its start origin is known, and a bound origin the run doesn't allow is an error, never dropped.
- `field` is either `password`, meaning an `<input type="password">`, or a role and accessible name.
- Values come from `AQA_SECRET_<NAME>` environment variables, locally and in CI, so a name is capital letters, digits and underscores, starting with a letter. Hosted runs use the test-secrets API (API.md §3).
- Which revision of this file a CI run trusts, the base branch or the pull request, is decided at M5 (#27).

**Start origin.** `base_url` must be an origin: `http` or `https`, a host and an optional port, with no path, query or user (a lone trailing `/` is allowed). `aqa explore --url` overrides it and is held to the same form. Origins compare lowercase and without the scheme's default port. With neither, the run fails before the browser starts.

**Egress hosts.** `subresource_hosts` and `expected_blocked` list bare host names or IP addresses (an IPv6 address in brackets), with no scheme, port or wildcard. `private_origins` lists origins.

**Model overrides.** A model missing from the pinned price map must be declared under `models`, with its capabilities and prices; otherwise config validation rejects it. Its cost records carry `price_source: config` and the rates they applied (`applied_prices`, §2), so a later change to this file doesn't change what past costs meant.

**Private origins** apply to local and CI runs only. Hosted runs reach public addresses only (ADR-0026).
