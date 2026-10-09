# Data Model — Agentic QA Platform

Last updated: 2026-10-08 (a reply the model didn't finish is `invalid`, #53; explore's run-record parts, cost lines, unresolved intents and exact-text checks, #53 P5); 2026-10-07 (each step's evidence in the local run record, #50 B; a usable coverage plan: no target holds what its check asserts, #161; network and visual evaluators, #48; probes as `GET <path>`, `probe_equals`'s fields, JSON paths and probe reads, #48; the M1 executor's assertion outcomes, #46; the M1 executor's step and run outcomes, and its local steps record, #46; the coverage plan's format and hash, and local run records under the spec root, #41; `ws://` to a subresource host on port 443, corrected, #43; loading a compiled script and bounding its text searches, #46; model roles and cost records, #40; M1 design decisions, ADR-0024–0026). PostgreSQL 16+ on RDS. Internal IDs are UUIDv7 (time-ordered). External identifiers (Clerk org/user IDs, GitHub IDs) are stored as their native strings/integers and mapped to internal IDs. All timestamps `timestamptz` UTC.

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
| `llm_calls` | `id`, `org_id`, `run_id`, `role`, `mode` (`explore`\|`heal`\|`verified`), `provider`, `model`, `input_tokens` (every input token, the cached ones included), `output_tokens`, `cached_input_tokens`, `latency_ms`, `price_map_version` (the pinned map's upstream commit, also on a record priced from the config), `price_source` (`map`\|`config`), `applied_prices` (jsonb: the per-million-token rates used for this call: input, output and cached input), `cost_usd` (an exact decimal), `status` (`ok`\|`refusal`\|`invalid`: a `refusal` is a `stop_reason` of `refusal`, and `invalid` is a response that arrived and then failed parsing or validation, or that the model didn't finish (cut off at the output bound, say), tool calls included (ADR-0007's 2026-10-08 #53 amendment). Both were billed, so both are recorded) |

A call that gets no response, such as a transport error, has no usage and records nothing. `aqa_core.model_costs.CostRecord` holds every column but `id`, `org_id`, `run_id` and the row's creation time, which ingestion assigns (M4).

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
  probes:                        # optional read-only GETs on the start origin, for observing app state
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
| `console_errors` | The console records an error-level message: the page's own `console.error` calls, and the browser's own error entries such as "Failed to load resource" for a 4xx or 5xx response or a failed load |
| `js_exceptions` | The page throws an uncaught exception or leaves a promise rejection unhandled |
| `http_5xx` | Any response to the browser has a 5xx status |
| `broken_images` | An image fails to load: an `<img>` fires its `error` event, whether its load failed or what loaded isn't an image |

`console_errors` and `js_exceptions` never overlap: an uncaught exception is not a console error, even though DevTools prints it in the console. In Playwright terms, `console_errors` counts `console` events of type `error` and `js_exceptions` counts `pageerror` events. The benchmark manifest's invariant ground truth (§8) relies on this split. A 5xx response or a missing image in the page's own documents also logs the browser's "Failed to load resource" entry, so it fires `console_errors` too, as conduit-bug-003 names it in §8.

**How the invariants are observed (M1, `aqa_runner.invariants`, ADR-0024's #47 amendment).** The browser session installs the observers on its page before the first navigation, and they collect until the session ends, reloads included. Once the run is over, the spec's `invariants` decide which count: each invariant is `held`, `violated` or `disabled`, with the first 100 things it saw, each cut to 200 characters, and how many there were.
- A replay judges them once, after its assertions and inside the session, for errored runs too. When the script has a `fill_secret` step, `RunResult.invariants` retains each outcome and total but has `seen == ()`. Observation text is withheld until #50 redacts it, since the page can log or throw a filled value.
- *Where:* every frame of the page and its dedicated workers, as Playwright reports them on the page: console messages, page errors and responses. A dedicated worker's failed load logs no console entry there (Playwright 1.63). Broken images count only in documents on an allowed origin, so not in a subresource host's frame or a `data:` frame. Popups are never observed: the session closes them.
- *Broken images* are seen from a script in an isolated world of the page's own, which reports every trusted `error` event of an `<img>`. The page's scripts can't reach that world, and a made-up `error` event is no broken image. They can hide a report only by taking the image out of the document before it fails, or by opening the document anew and adding a listener that stops the event before the reporter listens again (ADR-0024's #47 amendment). An image inside a shadow root isn't seen (its `error` event doesn't leave the root), and neither is one in a frame on an allowed origin of another site, which runs out of the page's process.

**Egress blocks and invariants (ADR-0026, and its #47 amendment).**
- *What counts as a block:* a request to a host that is neither an allowed origin nor a subresource host (§9). It is read from both of the run's records, routing's and the egress gate's refusals of a host, each host and port once. Also a block, since it may hide one: routing's record overflowing, and an attempt that names no host an origin could write, such as a scheme the proxy can't carry. A refusal by the IP policy (an allowed host resolving to an address it may not use) is no block, and no infrastructure event either.
- *Effect on the run:* the run refuses the request, and the block alone keeps the run from passing. It is not an invariant failure and not a finding.
- *Expected-blocked hosts* (§9) are the exception: their blocks don't keep the run from passing.
- *Direct symptoms:* the symptoms of a request the run refused never count against `console_errors` or `broken_images`, an expected-blocked host's or any other's: the browser's console entry for it, and a broken image whose load it was, straight or at a redirect hop. Both are matched by the request's URL, read as routing reads it, never by console or failure text; a load refused at a redirect hop excuses the broken images at the URL it started at until a newer load of that URL starts. A console call of the page's own with arguments always counts, whatever URL it names; one with none never reaches the observers.
- *Indirect effects* always count, such as app code failing because a blocked script never loaded.

**Parsing (M1).** Specs are read before a browser or a model is involved, and every problem is reported at once, each naming the file, the key and the problem. An invalid project config doesn't hide the specs' problems.
- *YAML:* the file is UTF-8 (a byte order mark is skipped), and the frontmatter is YAML 1.2's core schema (ADR-0030). Aliases and tags that change a value's type are errors, `yes` and `no` are strings, and a date is a string.
- *Paths:* a spec or the project config that is a directory is a spec error naming the path (`<path>: a directory, not a file`). A project load skips a path named `*.spec.md` that isn't a file. Any other read error, such as a permission error, isn't a spec error: it propagates as the system's own error, for the caller to report as an infrastructure error (API.md §7).
- *Keys:* unknown and duplicate keys are errors, and so is a value of another type, such as a quoted `"3"` for a number.
- *Required:* `id`, `goal`, `preconditions.start_url` and at least one `expect` item. `invariants` defaults to `inherit: true`; `disable` names distinct invariants from the table above, and an `inherit: false` with `disable` is an error.
- *`id`* must equal the file name without `.spec.md`. Spec IDs must be unique across the project, subdirectories of the spec root included; a duplicate is a spec error before anything is written.
- *`start_url`* is a path: one leading `/`, then no whitespace, control characters or backslashes, and no empty, `.` or `..` segment (`%2e` is a dot). A browser could read any of those as a path starting `//`, another origin. A query and a fragment are allowed.
- *`account`* takes `email` and `password`, each a string or a secret reference, `{ secret: NAME }`. A reference must name a secret the project config declares (§9).
- *A probe* is `GET`, one space and a path held to `start_url`'s rules, in ASCII and with no fragment: the runner sends the path as written, so anything else is percent-encoded. A probe only reads, and only from the start origin (#48).
- *The reset hook* is `POST`, one space and a path under the same rules, so the runner posts it only to the start origin (#53 P8). Any other method or path is a spec error naming `preconditions.reset.http` and the rule, never the hook, which may hold a token. The runner sends it through the run's egress gate, and only a whole 2xx response within its bound (`budgets.resolve_seconds` when explore sends it) passes; a redirect fails (ADR-0024's #53 P8 amendment).
- *Fencing:* a completed request proves only that the app answered. An app that commits work after answering must make its reset hook wait for that work or cancel it; the runner can't check this.
- *An expectation* is a string, or `{ text, visual }`. `visual: model` is a spec error in M1 (ADR-0024).
- *`allowed_origins` and `browser`* are checked as §9 checks origins and `browser`.

## 7. Compiled script format

`qa/.compiled/checkout-expired-card.json` (machine-written, human-reviewable diffs). **Targets** (what an element *means* + how to find it) are separated from **assertions** (what must be true), so heals can repair bindings without touching expectations. Explore writes it after a confirmation replay passes, or marked unconfirmed (`confirmed: false`) when the path has side-effect steps and the spec has no reset hook (ADR-0024).

```json
{
  "schema_version": 1,
  "spec_id": "checkout-expired-card",
  "spec_hash": "sha256:bcb054dbf5a73870f5ddfb8fd99045b028ce9bec953a4f60de8cdd7c964f4db6",
  "compiled_at": "2026-10-12T14:03:22Z",
  "compiled_by": { "mode": "explore", "models": { "navigator": "claude-sonnet-5-5" }, "price_map": "<upstream commit>", "subject_contracts": "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945" },
  "confirmed": true,
  "browser": { "timezone": "UTC", "locale": "en-US", "viewport": [1440, 900], "device_scale_factor": 1, "color_scheme": "light" },
  "coverage": {
    "plan_hash": "sha256:d8442e4d3e7f02414e6b7e8104794970554481c806c210d7e7a868fcd593b898",
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
- **Coverage plan first (ADR-0024).** Explore writes the plan from the spec alone, before the browser opens, and keeps it frozen for the run (`aqa_core.coverage_plan`).
  - *As written:* one entry per expectation, in the spec's order, each with its `expect_index`, `subject` and `claim`. Then either `checks`, at least one and none twice, or `unsupported`, with a `reason`, plus `needs` when an M2 check would establish the claim (`pixel_diff`, `contrast_min` or `model_verify`). The plan also lists `requires`, the conditions, each `id` once. A plan fits its spec only with one entry per expectation and only with probes the spec declares. It is usable only when it fits and no check's `target_meaning` holds what the check asserts: its literal, found as the check finds text, or its pattern (ADR-0025: finding an element by the claim it must verify is circular). Explore asks for a plan that isn't usable once more, with its reasons (ADR-0024's #161 amendment).
  - *A planned check* has one of M1's nine check types: *Check types* below, less `pixel_diff`, `contrast_min` and `model_verify`. Its fields are its assertion's, less what compiling adds. It has no `id` or `expect_index`, and no `min_size_px`, `in_viewport` or baseline capture. Instead of a `target`, it has a `target_meaning`: what the element it reads is for and where it sits. That is the expectation's subject, or the part of the subject the check reads when the claim names several elements, such as one of three header links. `probe_equals` takes a `probe` and a `value`, an integer or a string. A `text` is normalized and a `pattern` is a Python regex, as in a compiled script.
  - *Compiling* turns each planned check into the assertion of the same type (#53), and each `target_meaning` into a target's `semantic` (#52). Compiling a `probe_equals` adds the `json_path` of the value its probe reads (#48).

  The compiled script stores the plan as `coverage`:
  - for each expectation: its subject, its claim and the assertions that establish it;
  - `requires`: conditions the goal or expectations need, such as `{ "id": "c1", "condition": "checked after reloading the article page" }`. The step that satisfies a condition carries `"satisfies": ["c1"]`. Compilation fails if a required condition is left without a satisfying step before the assertions;
  - `plan_hash`: identifies the plan the run froze, as written before the browser opened (subjects, claims, planned checks and required conditions). It is `sha256:` and the sha256 of the plan's canonical JSON, by `spec_hash`'s rules, leaving out every field whose value is null. Assertion IDs and `satisfies` marks are compile-time links to it.
- **Subject and claim.** An expectation's subject says what it is about ("jake's comment", "the Pay button"). That is a target's meaning: fixed when the spec is explored, and not re-checked on replay. Everything the expectation says about its subject (text, state, position, destination, count) is its claim, and its assertions must establish all of it. So an expectation that claims what no check can establish, such as "shown above the article list", fails compilation by name.
- **Check types:** `text_visible`, `text_in_target`, `not_visible`, `url_matches`, `network_none` / `network_seen` (method + URL pattern + status class, from the browser's own traffic), `probe_equals_baseline` / `probe_equals` (read-only GET to a declared probe on the start origin), deterministic visual checks — `visible_unoccluded` (hit-test at the element's center returns the element or a descendant; in viewport; minimum size), `pixel_diff` (region vs committed baseline image, threshold), `contrast_min` — and `model_verify` (only for `visual: model`; rejected in `strict` mode; explore rejects `visual: model` expectations as a spec error until M2 defines how they confirm).
- **Text parameters (ADR-0025).**
  - `text` is a literal. It matches case-insensitively, as whole words (no word character may touch an edge of the literal that is itself a word character; so a literal starting with a sign or a decimal point, such as `-1` or `.5`, is found inside `10-1` or `10.5`, and a claim about such a number uses a `pattern`), against the element's normalized rendered text: private-use glyphs, soft hyphens and zero-width spaces stripped, whitespace collapsed (ADR-0025, "reading a compiled script").
  - `pattern` is a Python regex (`re.search`, flags written out) for claims that need one, such as part of text written without spaces between words. It searches the same normalized rendered text; `url_matches` searches the page's URL as it is (ADR-0025, "resolving a target per use"). The page controls the text, and Python's `re` can't be interrupted, so each search, a `text` literal's included, runs in a child process that gets no environment and is killed after 2 s (`aqa_runner.text_search`). A search that runs out of time is the check's outcome, `check_timed_out` (*Replay outcomes*).

  The compiler prefers `text`. A claim that depends on case uses a `pattern` without `(?i)`.
- **Network reads (ADR-0024's #48 amendment).** `network_seen` and `network_none` match a method, a `url_pattern` and a status class against the browser's own responses (`aqa_runner.network.held`). Runner-side probes never enter this traffic.
  - `url_pattern` is a Python regex, searched in the complete URL as Playwright reports it, without decoding or normalization. All kept responses of the matching method and status class share one search child and its 2 s bound (`aqa_runner.text_search`). An empty candidate list starts no child.
  - A response joins its request's settle window, including each redirect hop. Each window keeps its first 100 responses and counts all. The browser session retains every window, starting before navigation. A kept match establishes `network_seen` and disproves `network_none`. With no kept match, any window that discarded responses raises `WindowsOverflowError`, naming the window and counts, instead of claiming absence.
  - Replay evaluates these checks against every window after the final step. An undecidable overflow makes the assertion and those after it `not_evaluated`, and the run `errored`. A search timeout is `check_timed_out`; later assertions still run.
  - Each exchange holds only method, URL and status. Methods and URLs are page-chosen text, so a window holds them scanned for every bound value, a URL cut to 2048 characters after its scan; policy events and popups hold their URLs and origins the same way (ADR-0026's A2 amendment). Network checks read `Traffic`'s private complete copies of the same kept responses, so a bound value never hides one. The window contains only plain data, including integer identities for requests still open. Live Playwright requests stay private to `Traffic`.
- **Visible and unoccluded (ADR-0024's #48 amendment).** `BrowserSession.unoccluded` checks the page and the element's frame against allowed origins first.
  - M1 supports only elements in the session page's main frame. A child-frame target, including an allowed-origin one, raises `UnsupportedVisualFrameError` with the fixed reason `visible_unoccluded does not support elements inside frames`. It establishes neither a pass nor an expectation failure.
  - Replay requires `in_viewport: true` before opening the browser. It resolves the target for assertion use and bounds the observation by `resolve_seconds` plus the executor's 1 s margin, disposing the handle afterward. A missing target is `binding_unresolved`; a resolved false geometry check is `failed`. An unsupported frame or observation error makes this and later assertions `not_evaluated`, and the run `errored`.
  - For a supported target, one evaluation reads its bounding box and hit test without scrolling. The box must meet both minimum dimensions and, when `in_viewport` is true, fit entirely within the viewport.
  - The hit test uses the center of the first non-empty client rect through the element's own root, as action resolution does below. An uncovered element or its descendant must receive that point. A covered assertion target still resolves, so the observation returns false.
- **Probe reads (ADR-0024's #48 amendment).** A probe check, and a baseline, reads its probe with runner-side requests (§6, ADR-0026) until two reads in a row, 0.5 s apart, select the same value; the whole read, every request in it, gets 10 s (`aqa_runner.probes`).
  - *What is read:* only a 200, whose body is one JSON object or array, in UTF-8, nested at most 256 deep, with no repeated key, no `NaN` or `Infinity` and no fraction or exponent a float can't hold, too large or so small that a nonzero number reads as 0. An integer is read exactly; a number with a fraction or exponent is read as a double (RFC 8259 §6), so digits past about 17 significant ones don't count. A body the connection's close ends can be cut short unseen, and an object or array cut short never parses, where a bare `12` cut to `1` would.
  - *Never still:* when the 10 s pass after two reads or more, none the same as the one before, the value never held still. When they pass before a second read, the probe didn't answer, which is a probe that can't be read, as any other response above is.
  - *Compared:* as canonical JSON (`spec_hash`'s rules), so `2`, `2.0`, `true` and `"2"` are four values: two reads, to tell whether the value held still; the selected value and `probe_equals`'s `value`; and the selected value and the baseline, for `probe_equals_baseline`.
  - *Replay preflight:* probe assertions and baseline definitions, including unused definitions, must name probes declared by the spec. All undeclared names are reported before opening the browser.
  - *Capture:* each baseline is read before resolving or recording the step whose `seq` is `capture_before_seq`. Only successful captures are stored, in memory for that replay. JSON null is a captured value. A failed or unstable capture fails the step without writing its intent or dispatching it. Later assertions are `not_evaluated` at that step.
  - *Interruption:* after capture, an egress block or infrastructure event stops the run before the next action, targeted or untargeted. Existing egress precedence and invariant recording still apply.
  - *Read errors:* unreadable, refused or unreachable probes make the assertion and all following assertions `not_evaluated`, and the run `errored`. Runner-request errors in `StepResult.error` and `AssertionResult.error` use fixed messages that contain no response text. Captured values are never saved in the run record.
- **Meanings (ADR-0025).** A target's `semantic` says what the element is for and where it sits, never its current label. If an expectation claims a label, the label belongs in an assertion.
- **Locators (ADR-0025).**
  - *Kinds:* `role` + `name` (the name normalized, then matched exactly), `label`, `placeholder`, `testid`, and `css`. A `css` locator uses a stable id, or stable classes, attributes and custom-element tags: never positional, never generated class names.
  - *Scope:* any locator may carry a `scope` (another locator), and it must be unique inside it.
  - *Order:* action targets list `role` + `name` first.
  - *Generation* (`aqa_runner.locator_generation`, ADR-0025's "generating locators"): from the element a use put the target to, with the role and name its ref's line in the snapshot gives. An action target gets one locator of each kind that finds it, in the order role and name, label, placeholder, test ID, stable id (`#id`), structure. An assertion target gets test ID, stable id, role, then structure, and no label or placeholder.
    - *Claims:* when an assertion checks its target's text (`text_in_target`), the target's role carries no name: a name and the rendered text can share words without either accepting the other. An assertion that checks no text, such as `visible_unoccluded`, keeps a control's name.
    - *Names:* only a control's name locates it (button, link, textbox, checkbox, radio, switch, tab, combobox, searchbox, spinbutton, slider, option, menu items, tree items). A container's name is its text.
    - *Stable:* an id, class, tag or attribute value is one plain CSS identifier (a letter, then letters, digits, `-` or `_`), not generated (`css-`, `sc-`, `jsx-`, `emotion-`, `svelte-` or `ng-` first, or a part of five or more characters mixing letters and digits) and not a state class such as `active`. Attributes are `name` and `type` only.
    - *Structure:* the element's custom-element tag, a stable attribute, a stable class, or its tag, alone or under its nearest custom-element ancestor (`app-favorite-button button.btn`).
    - *Scope:* when a locator isn't unique on the page, the nearest ancestor that makes it unique, by its stable id, its custom-element tag, or its tag with a stable attribute or one stable class, never `html` or `body`. One level. Only the nearest 16 ancestors and the first 16 stable classes of a node are tried, and one element costs at most 400 tries, each a resolution or a count. Tries that run out keep the locators already found.
    - *Kept:* a locator only if, alone, it resolves for the use to the element used, on the live page. Which element it found is judged by a selector engine in Playwright's utility world, where the page's scripts can't reach, holding the used element; whoever opens the browser session registers it first (`register_identity_engine`). With none, generating fails by name, as it does when the page breaks the reading of the element's facts, gives them in a form no element has, or moves the element out of its document.
  - *Assertion targets are never located by what their claim says.*
  - *Validation:* every locator must resolve to the element the agent used, at every use, both at compile time and on the confirmation replay. A target that can't do that at every use is split into separate targets (`aqa_runner.locator_generation.TargetUses`, one per meaning, ADR-0025's "generating locators").
    - *Across uses:* a later use joins the meaning's current target only if every one of its locators holds there. That means it is a kind the use allows: an assertion's target has no label or placeholder, a text check's has no name, and a negative check's is scoped. It also means that, alone, it resolves for the use to the element used, or, at a negative check, finds its scope on the page with nothing visible in it. Otherwise the use starts a new target with the same meaning, generated there. A locator is never dropped to keep a target whole. So the favorite toggle splits: it is clicked by a name that favoriting changes, then its text is checked. A class that flips with state, such as `btn-outline-primary`, splits a target too.
    - *Negative checks:* a `not_visible` target is generated where the element was seen, from an assertion's kinds, with every locator scoped. Each locator is kept under every scope that finds the element there. At the check, every one of them is looked at. Of each kind, the first whose scope is on the page with nothing visible in it is kept, so the scope holds both where the element was seen and where it is checked. If any of them finds something visible in its scope, one match or several, the element is still shown: compiling fails by name, rather than keep only the locators that miss it. It also fails by name when the page breaks a look, when no scope it was seen under is on the page, or when the element wasn't seen. Every negative check of a meaning, one that joins a target included, is judged against its latest sighting.
- **Subject contracts on targets (ADR-0025's #53 P7b-II amendment).** A target whose meaning a config row governs (§9) carries `contract: {region, part, leaf}`, exactly that row's contract, never one taken from the page. Any other target carries none. Its locators stay relative: resolution applies the region, so no locator can be written without it.
  - *At every use* (`TargetUses(meaning, checks_text=..., contract=...)`), the subject's verdict (`aqa_runner.binding.binding_verdict`) judges the element the use offers before anything else. A refusal raises `BindingRefusedError` with a fixed reason, such as `outside_region` or `part_absent`, starts no target and keeps no sighting. A meaning with no contract is refused where copies of the element are detected (`unlisted_copies`); one whose copies aren't detected binds the element offered, never another.
  - *Generation* resolves every candidate, sighting and look inside the region, counts a scope as unique inside it, and gives the contract to every target the meaning starts. A negative check's locators stay scoped. A negative check whose region isn't the region selector's one match fails by name.
- **Resolution per use.** Locators are tried in order, and what counts as a match depends on the use:
  - an action target needs the first unique, actionable match. *Actionable* means visible, enabled and receiving pointer events: a hit test at the center of the element's first box (a wrapped link has several), through its own root so an open shadow root counts, finds the element or a descendant. If it misses, the element is scrolled into view at once and tested again. It is the same for every step that targets an element, `fill` and `select` included;
  - an assertion's target needs a unique match attached to the page, and doesn't have to receive pointer events;
  - a negative check (`not_visible`) resolves its scope, and zero matches inside it is a result. Absence must be unanimous: any locator's unique match resolves the element instead, so a relabeled element a fallback still finds is seen. If no locator finds one element, none finds several and at least one finds its scope empty, the element is absent; otherwise it is drift. An unscoped locator's scope is the page. A negative check counts only visible matches, and its role locators also see past the accessibility tree (`aria-hidden`). So a hidden element with the target's old name can't stand in for the visible one a fallback finds. A resolved element is on screen, and an absent one is not.

  The replay records which locator resolved each target. Details (`aqa_runner.locators`, ADR-0025's 2026-10-02 amendments):
  - *A scope* must match exactly one element on the page, and the locator is unique inside it.
  - *One look:* resolution judges the page once. Waiting within `resolve_seconds` (§9) is the executor's loop around it.
  - *Role and name:* Playwright computes the role and the accessible name. The name matches when it normalizes to the locator's `name`, with private-use glyphs anywhere (astral ones included) and any run of whitespace where `name` has a space.
  - *Other kinds:* `label` and `placeholder` match exactly, and `testid` matches `data-testid`. `css` is sent as `css=<value>`, so another engine's syntax (`xpath=`, `text=`) is a parse error, never a match. That error ends resolution, because a broken script isn't drift. Playwright's own pseudo-classes, such as `:has-text()` or `:nth-match()`, would still work there, so the format refuses them (below).
  - *A contracted target* holds its region's one element, the region selector's only match, for the look, and each locator looks inside the selector's match. Every scope's element must be inside the held region (`outside region`). A found element must still be the held region's one part match, childless when `leaf` (`not the part`, `not a leaf`), judged last, after actionability for an action. A negative check is absent only when, after every locator's zero result, one read in Playwright's utility world proves the held region is still the selector's only match and shows none of its parts. A region that is missing, repeated, replaced, relabelled or removed, that another document adopted (a detached document or a frame's), or that contains a shadow host is `no region` for every locator, never absence. A navigation during the look is a document change, so the executor looks again.
  - *An unresolved target* gives each locator's reason, in order: no scope, no match, ambiguous or not actionable, and for a contracted target no region, outside region, not the part or not a leaf.
- **Every step carries `side_effect`:** `true` for a step that changes app state (submit, purchase, delete), `false` for a **replay-safe** step (navigation, reads, idempotent fills). The flag is required and never defaulted — a missing flag fails validation, because a default of `false` would let a continuation re-execute a purchase (ADR-0006 amendment).
  - *Inference (ADR-0025):* `false` needs positive evidence. The step's settle window must end in idle, with no write request to any host and no WebSocket message sent, and the model must agree. A write that arrives before the next action counts against the step.
  - *Otherwise* the flag is `true`, and `side_effect_basis` says why. A step whose flag is `false` has no `side_effect_basis`.
  - *Only a person lowers it,* by editing this file, and removes the basis with it.
- **Actions:** `navigate`, `reload`, `click`, `fill`, `fill_secret`, `select`, `press`. A `navigate` to the start origin stores a path.
- **Browser settings.** `browser` records the settings the script was explored under. Replay uses them rather than the current config (ADR-0025).
- **`spec_hash`** is the sha256 of the canonical JSON of the parsed frontmatter without `tags`, written `sha256:<hex>`. The Markdown body never counts. A mismatch makes the script stale: `aqa explore` redoes it, and replay refuses it.
  - *Canonical JSON:* sorted keys, no whitespace, and non-ASCII characters escaped, as Python's `json.dumps(…, sort_keys=True, separators=(",", ":"))` writes it.
  - *As parsed:* the hash covers the YAML's values, not normalized ones, so `HTTPS://Pay.test` and `https://pay.test` hash differently. A field the model gains later never changes an existing hash.
- **`confirmed`** is `true` when the confirmation replay passed. It is `false` when a path with side-effect steps was written without one because the spec has no reset hook (ADR-0024).
- **Location:** `<spec root>/.compiled/<spec id>.json`. The spec root is the directory holding `config.yaml` (§9), and spec IDs are unique per project (§6).

### Reading a compiled script (schema version 1)
A compiled script is read as strictly as a spec (§6): an unknown field is an error, and no value changes type, so `true` is not `1` and `"1"` is not a number (`aqa_core.compiled`).
- **Required:** every field the example shows, `browser`'s five settings included, except those this list makes optional. `schema_version` is the integer 1. `spec_hash` and `plan_hash` are `sha256:` and 64 lowercase hex digits. `compiled_at` has a time zone.
  - The example's `spec_hash` is the hash of §6's example. Its `plan_hash` is illustrative: no plan was written for it (*Coverage plan first* defines the hash).
- **Locators:**
  - each names exactly one kind: `role`, `label`, `placeholder`, `testid` or `css`;
  - `name` goes only with `role`, and is optional there;
  - `role` is a WAI-ARIA role that Playwright's `get_by_role` accepts;
  - a `name` is written normalized (no private-use glyphs, soft hyphens or zero-width spaces, single spaces, none at either end), because it is compared normalized;
  - a `css` value is one CSS selector, not only whitespace and with no `>>`. Resolution must send it to Playwright as `css=<value>` (#45), and Playwright still reads a `>>` as a chain into other selector engines there. Inside an attribute value, write `\>\>`;
  - a `css` value leaves no quote or escape open by Playwright's count. Playwright counts quotes even inside a CSS comment, which CSS ignores, and an open one would swallow a locator scoped under the value;
  - a `css` value is written trimmed: neither end has a character JavaScript's `trim()` removes. Playwright trims each selector part (`css=<value>`), which reaches the value's end, before it reads the value as CSS, and the start is held to the same rule;
  - a `css` value uses none of Playwright's own pseudo-classes: `:has-text()`, `:text()`, `:text-is()`, `:text-matches()`, `:visible`, `:light()`, `:nth-match()`, `:above()`, `:below()`, `:left-of()`, `:right-of()` and `:near()`. The value is read as Playwright's CSS tokenizer reads it: comments dropped, strings read whole, escapes decoded and each name lowercased. So `:HAS-TEXT(` and `:has\2d text(` are refused, and a name inside a quoted attribute value, a comment or after an escaped colon (`.a\:visible`) is no pseudo-class. Standard pseudo-classes stay allowed, positional ones such as `:nth-child()` included, though the compiler never writes those (ADR-0025, "generating locators");
  - `scope` is optional, and is itself a locator;
  - a target lists at least one locator, and none twice.
- **Steps:** each has `seq` (1 or more), `side_effect`, `side_effect_basis` exactly when `side_effect` is `true`, and optionally `satisfies`. The other fields depend on the action:

  | Action | Fields |
  |---|---|
  | `navigate` | `url`: a path on the start origin, held to `start_url`'s rules (§6). The executor joins it to the start origin. Schema version 1 records no navigate to another allowed origin |
  | `reload` | none |
  | `click` | `target` |
  | `fill` | `target`, `value` (may be empty) |
  | `fill_secret` | `target`, `secret` (a secret name, §9) |
  | `select` | `target`, `option` |
  | `press` | `key`, with no target, as the agent's `press(key)` tool has none (ARCHITECTURE §3.4) |

- **Assertions:** each has an `id` and an `expect_index`. Schema version 1 gives fields to these checks. The other check types above are refused by name until their fields are defined: `pixel_diff`, `contrast_min` and `model_verify` in M2.

  | Check | Fields |
  |---|---|
  | `text_visible` | `text` or `pattern`, exactly one |
  | `text_in_target` | `target`, and `text` or `pattern`, exactly one |
  | `not_visible` | `target` |
  | `url_matches` | `pattern` |
  | `network_none`, `network_seen` | `method` (`GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE` or `OPTIONS`), `url_pattern`, `status_class` (`1xx` to `5xx`) |
  | `probe_equals` | `probe`, `json_path`, `value` (an integer or a non-empty string, never `true` or `2.0`) |
  | `probe_equals_baseline` | `probe` |
  | `visible_unoccluded` | `target`, `min_size_px` (width and height, each 1 or more), `in_viewport` |

  A `text` is written normalized, as a `name` is. A `pattern` or `url_pattern` must compile as a Python regex, in a compiled assertion or a planned check. A `json_path`, here and in `probe_baselines`, is `$`, then a step for each level: `.name` for an object's key (ASCII letters, digits, `_` or `-`) or `[index]` for an array's item (an integer from 0, no leading zero, at most nine digits), as in `$.count` or `$.orders[0].total`. Nothing else, so no path reads two ways.
- **Also enforced:** `coverage.expectations` and `assertions` are not empty, and neither is an expectation's `assertions`. An expectation's `assertions` and a step's `satisfies` name each ID once. `compiled_by.mode` is `explore`, and `compiled_by.models` is keyed by model role (navigator, verifier, healer, vision_fallback). `compiled_by.subject_contracts` is required and is a `sha256:` fingerprint of the config rows for this script's spec (§9). A script with no rows still carries the canonical hash of `[]`, as the example does.
- **Checked by the loader, not the format** (`aqa_core.project.load_compiled`, #46). The file reader delegates to `parse_compiled(text, config, source=path)`, which applies the same strict JSON and cross-field checks without file I/O. In-memory benchmark rebindings use this parser too (#51). Every problem is reported at once, each naming the file and the key, as a spec error (exit 5, API.md §7). Reading the file is as for a spec (§6): a directory, or text that isn't UTF-8, is a spec error, and any other read error, a missing file included, propagates for the caller to handle.
  - that the JSON repeats no key, wherever it is: a reader keeps a key's last value, so a repeat could hide a lowered `side_effect`. Objects and arrays nest at most 256 deep;
  - that every name a part of the script uses exists, and is unique where it is defined: targets, assertion IDs, the expectations `expect_index` names, conditions, probes and step numbers (`capture_before_seq` names a step's `seq`);
  - that each secret a `fill_secret` step names is declared in the project config (§9);
  - that every locator of a `not_visible` check's target has a `scope`. Unscoped, the target is absent from any page without a match, such as a wrong page or an app's 404, so the check would pass whatever the page (#52). A scope every page has, such as `html` or `body`, protects nothing either, so the generator picks one particular to the page; the loader can't tell the two apart.

**Subject-contract agreement before replay (ADR-0025's #53 P7a and P7b-II amendments).** `aqa_core.project.contract_problems(script, config)` compares the recorded fingerprint with `contracts_fingerprint(config, script.spec_id)`. Replay raises a spec error before accepting steps, binding secrets, opening the browser or writing an intent when they differ: "compiled under other subject contracts: explore it again". It also refuses a listed expectation whose governing target lacks its subject contract, and every target sharing that target's semantic meaning without the same contract. A listed expectation checked without a target is refused too. A governed target passes only when its `contract` equals its row's, and a target carrying a contract that no row gives it is refused: "targets.<name>: carries a subject contract no row gives it". A correctly fingerprinted script whose targets carry no contract remains admissible for an unlisted spec.

### Replay outcomes
- **Binding unresolved:** no locator gives the match its use needs (see *Resolution per use*) within the wait budget → drift → heal path.
- **Expectation failed:** the target resolved and the check evaluated false → `expectation_violated` (after invariants and all assertions are evaluated, so the report is complete).
- **Check timed out** (`check_timed_out`): the check's text or URL search ran past its 2 s bound (*Text parameters*), or a probe assertion never stabilized within its 10 s bound (*Probe reads*). It established neither a pass nor a failure; later assertions still run. A run with one can't pass. M2 maps it to `inconclusive`, never to `expectation_violated` (ADR-0024's 2026-10-02 amendment).
- **Step outcomes** (the M1 executor, ADR-0024's #46 amendment): `completed`, dispatched and settled, `idle` or `timeout`; `drifted`, its target never resolved within `resolve_seconds`, so nothing was dispatched; `failed`, capturing a baseline, looking up its target, dispatching or settling raised, or `fill_secret` refused where its binding doesn't allow (once dispatched, its intent stays unresolved). In a run that fills a test secret, a reason keeps nothing of Playwright's message (ADR-0026's fill_secret amendment). A drifted or failed step stops the run, and every assertion is then `not_evaluated`, naming that step.
- **Assertions** (the M1 executor): each is evaluated once, in order, after the last step settled. `not_visible` fails at once on a target that is there, and waits out only drift within `resolve_seconds` (ADR-0024's #46 amendment weighs the false pass against the false failure). The page must first answer one bounded look (a read of its visible text); if it doesn't, every assertion is `not_evaluated`. An assertion whose look at the page raised (a page off the allowed origins, a crashed page, a page that didn't answer within its budget) is `not_evaluated`, with its reason, and so is every one after it, naming that assertion. `text_in_target` reads nothing from an element the page doesn't render (no box, `display: none`, `visibility: hidden`), so hidden text never satisfies it; reading an element that isn't HTML (SVG's) is a look that raises.
- **Run outcome:** `passed` when every step completed, every assertion passed, and every enabled invariant held. An egress block takes precedence over every other outcome and ends the run `errored` with `error_code: egress_blocked`. An infrastructure event, a failed step, or an assertion whose look raised also ends it `errored`, with no error code. Anything else is `failed`. A popup's policy event alone does not stop the run. Every other policy event raises where the session observes or acts, failing its step or assertion's look.
  - A step's or an assertion's `error` is scanned for every bound value, the values the script never fills too, by `aqa_runner.redaction.error_text`: the executor's own message, or a probe's fixed diagnostic, or Playwright's class and first line (only its class and call when the script fills a test secret). It is scanned three times, before a line, call, escape or cut is taken, over the assembly, and over the escaped line before its cut to 200 characters. Every reason `error_text` presents is therefore one line of at most 200 characters; the two the executor writes itself (an assertion left unevaluated, an action that timed out) are scanned whole. Each `RunResult.infrastructure_events` entry has its host and cause scanned; port, order and count stay. A cause never holds text the upstream sent: an exchange that broke off records fixed wording with h11's error class or the system's message (#160). Egress-block hosts and invariant texts stay withheld in a run that fills a secret (slice D).
- **Local run record (M1):** `<spec root>/.aqa/runs/<run_id>/steps.jsonl`, one ASCII JSON line per intent and per completion, as `run_steps` rows without a lease or an org. An intent, `{seq, state: "intent", action, side_effect, target_used, at}`, is on disk before its action is dispatched; a completion, `{seq, state: "completed", locator_used, settled, at}`, follows once it settled. Seq 0 is the start URL's navigation. An intent with no completion after it is unresolved. A `fill_secret` intent's action names the secret, never its value.
  - An egress-blocked run also writes `egress.json` once, with `error_code: "egress_blocked"` and `overflowed`; when the record refuses it, the counts-only form below, and when that is refused too, none. When the script has no `fill_secret` step, it records `refused: [{host, port}, ...]` and `hosts_and_ports_withheld: false`. It records no URL, path, or query.
  - Each step the executor keeps also leaves `evidence/<seq>/`, as its settle window stood after it settled: `a11y_snapshot.yaml`, the scanned snapshot (absent when the page couldn't be looked at); `console_log.json`, `{entries: [{type, text}], total}`; and `network_log.json`, `{entries: [{method, url, status, timing: {start, duration_ms}, size}], total}`, each request kept in start order. In a run that binds any test secret, entries keep only `type`, or `status`, `timing` and `size`. Every file but the two journals, `steps.jsonl` and `costs.jsonl` (below), is refused while a supported spelling of a bound value is left, and one written without a check is scanned first (ADR-0026's #50 B amendment).
  - When the script has a `fill_secret` step, the file contains only `{error_code, refused_count, overflowed, hosts_and_ports_withheld: true}`. It contains neither hosts nor ports. `refused_count` counts the distinct refused host-port pairs, expected-blocked hosts left out. The same predicate withholds Playwright's error messages and invariant observation text for the whole run, including steps before a fill. #50 replaces these withholdings with redaction. `RunResult.egress_blocks` retains hosts and ports in memory.
- **Egress block:** a request to an undeclared host keeps the run from passing without producing a finding. The run ends `errored` with `error_code: egress_blocked`, with no verdict, and `egress.json` records the block as above. Expected-blocked hosts do not count. The CLI exits 6 (§6, API.md §7, ADR-0026).

### Explore's run record (M1, #53)

ADR-0024's #53 P5 amendment; `aqa_runner.run_record`.

- **Parts.** Each phase of an explore goes to a part of its run record, `attempt-1/` or `confirmation/`: a record of its own, with the run's `run_id` and scan, its own `steps.jsonl` and its own `evidence/`. A part's directory is made once; any other name, or a link in its place, is refused. It is on disk once its first journal line is, since that line forces every directory from the part's up to the spec root.
- **Cost lines.** `costs.jsonl` holds one ASCII JSON line per priced model call: the `CostRecord`'s fields (§2's `llm_calls` row less `id`, `org_id`, `run_id` and its creation time), with no time of their own, so each line reads back as a `CostRecord`. It is on disk when `RunRecord.cost` returns.
- **Unresolved intents.** A record keeps in memory each step whose intent it wrote with no completion written after it, shared by every copy of the record at the same path (such as replay's), and only by them. `unresolved()` lists them, with their `side_effect` flags, and never reads the journal. A second intent for a step still unresolved is refused.
- **Exact-text checks.** A journal line, and a document written with a check, goes to the writer's check before its file is opened, as the exact text written: wrapper fields and the line's time included, with only the final newline added. Explore's check is `require_clean(redactor, place)`, which refuses any supported spelling of a bound value, naming the place, never the value. A checked document is written as checked, never scanned into other text, or not at all.
- **A failed journal write.** Once a write, flush, fsync, close or directory fsync of a journal fails, or anything else interrupts it once its file is open, the journal takes no further line from any copy of its record (`BrokenJournalError`). A check's refusal leaves it open. A cost line that is refused, or whose write fails, raises `UnrecordedCostError`, which keeps the call in memory and never in its message.

### What a heal patch may change (validator-enforced)
- **Allowed:** `/targets/<id>/locators` (re-binding what an element *is* to how to find it), and actions/values of steps whose `side_effect` is `false` (including inserting or removing replay-safe steps, e.g., dismissing a new modal).
- **Forbidden:** anything under `/assertions`; any target's `semantic`; any `side_effect` step other than via its target's locators; adding or removing `side_effect` steps; changing any step's `side_effect` flag; `spec_hash`; `/compiled_by` provenance, including `subject_contracts`; any target's `contract`, which comes only from the reviewed config; `/browser`; `/coverage`; invariants; probes; and any patch that leaves a required condition without a satisfying step (a heal may move a `satisfies` mark only onto a step that still meets the condition).
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

`qa/config.yaml` is committed next to the specs. The directory that holds it is the **spec root**: compiled scripts live in its `.compiled/` (§7), and local run records in its `.aqa/runs/`, which git ignores (ARCHITECTURE §3.3). The file must exist, but every key is optional, so an empty file is valid. It is read as specs are (§6, ADR-0030): unknown and duplicate keys are errors, and every problem is reported at once.

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
  expected_blocked: [ "analytics.example.test" ]             # refused; their blocks don't keep a run from passing
  private_origins: [ "http://staging.internal.test:8080" ]   # local and CI runs only: may resolve to private addresses
secrets:                      # bindings only; values come from AQA_SECRET_<NAME>
  TEST_PASSWORD: { origins: [ start ], field: password }
  API_TOKEN: { origins: [ start ], field: { role: textbox, name: "API token" } }
models:                       # a model the pinned price map lacks, or a replacement for its entry (ADR-0007 amendment)
  "example-provider/example-model": { capabilities: [tools, structured_output], input_usd_per_mtok: 0.50, output_usd_per_mtok: 1.50 }
budgets:                      # per explore run (ADR-0024)
  attempts: 3
  actions_per_attempt: 40
  model_usd: 3.00
  minutes: 15
  resolve_seconds: 10
```

**Subject contracts (ADR-0025's #53 P7a amendment).** Optional `subjects` is a list of reviewed rows. Each row has `spec`, `expect`, `region`, required `part`, and optional `leaf` defaulting to `false`. `spec` is a non-empty spec ID; `expect` is a strict nonnegative integer indexing that spec's expectations from zero; `leaf` is a strict boolean. No `(spec, expect)` pair may occur twice, even with different contract fields. For example:

```yaml
subjects:
  - { spec: checkout-expired-card, expect: 0, region: app-payment-step, part: button#pay }
```

- A `region` is one CSS compound: a lowercase tag starting with a letter, followed by lowercase letters, digits or hyphens, then at most eight `.class` or `#id` parts. Each suffix is `[A-Za-z][A-Za-z0-9_-]*`. It has at most 200 characters and no whitespace, combinator, attribute, pseudo-class, quote, comma, pipe or `>>`.
- A `part` is one to four compounds of the same grammar, joined only by a descendant space or ` > `, at most 200 characters. It cannot use `:scope`, a sibling combinator or any other selector syntax. It names the subject within its region without choosing by the subject's value. `leaf` additionally requires a childless element when the contract is enforced.
- `load_config` validates row shapes, grammars and duplicate subject keys. `load_project` additionally checks that each spec exists and each expectation index is in range, reporting spec errors before any call. `subject_contracts(config, spec_id)` returns that spec's contracts keyed by expectation index.
- `contracts_fingerprint(config, spec_id)` hashes the canonical JSON (§7) of `[{expect, region, part, leaf}]` sorted by expectation index, with defaults included. Other specs' rows and row order do not affect it. No rows means `sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`, the hash of `[]`.
- The pilot's config (`bench/apps/conduit/qa/config.yaml`) lists five reviewed rows, all in `div.banner`: read-article 1 (`a.author`) and 2 (`span.date`), publish-article 3 (`a.author`), and favorite-article 0 (`app-favorite-button > button`) and 1 (`span.counter`). Every part but the button is a leaf. Replay enforces them through each target's contract (§7).

**Browser time zone (ADR-0025's 2026-10-02 amendment).** `browser.timezone`, in the project config, in a spec and in a compiled script's `browser`, is a name in the `tzdata` package's list, written as the IANA database spells it: case counts, so `utc` is an error. Backward-compatible aliases such as `Asia/Calcutta` and `US/Pacific` are accepted, because Chromium accepts them, and are passed on as written. The list is the pinned package's (TECH_STACK §1), never the host's: `localtime` and any zone only the host lists are errors, and the answer is the same on every machine (`aqa_core.browser.time_zones`).

**Test secrets (ADR-0026).** A spec may reference only secrets declared here.
- `origins` lists where the browser may fill the secret. `start` is the run's start origin, which comes from the invocation (`aqa explore --url`). Any other entry is an origin, and must also be one of the run's allowed origins, because a secret's destinations are the intersection of its binding and the run's allowed origins. A run checks each secret its spec references once its start origin is known, and a bound origin the run doesn't allow is an error, never dropped.
- `field` is either `password`, meaning an `<input type="password">`, or a role and accessible name. The role must be one Playwright's `get_by_role` takes (`aqa_core.schema.AriaRole`), so a typo fails the config's load rather than every fill (#49).
- `load_spec` and `load_project` give each spec the bindings of exactly the secrets it references, from the config they read it with (`Spec.secret_bindings`). `aqa_core.project.secret_destinations(spec, start)` gives each one's destinations in a run; `fill_secret` (#49) takes them from it, through `aqa_runner.bound_secrets.bound_secrets`, and never reads `Spec.secret_bindings`, where `start` is unresolved (ADR-0026's secret bindings and fill_secret amendments).
- Values come from `AQA_SECRET_<NAME>` environment variables, locally and in CI, so a name is capital letters, digits and underscores, starting with a letter. Hosted runs use the test-secrets API (API.md §3).
  - A run reads the value of every secret its spec references before the browser starts, whichever of them it fills (`aqa_runner.bound_secrets.bound_secrets`, #49). A variable that is unset or empty stops the run as an infrastructure error, exit code 12 (API.md §7): GitHub Actions gives a secret that isn't set as an empty string. So does Playwright logging that would print a value (`DEBUGP`, or `DEBUG` naming `pw:protocol` or `pw:browser`).
- Which revision of this file a CI run trusts, the base branch or the pull request, is decided at M5 (#27).

**Start origin.** `base_url` must be an origin: `http` or `https`, a host and an optional port, with no path, query or user (a lone trailing `/` is allowed). `aqa explore --url` overrides it and is held to the same form. Origins compare lowercase and without the scheme's default port. A host is a DNS name, a dotted-decimal IPv4 address or a bracketed IPv6 address; the other IPv4 spellings a browser reads (`0x7f000001`, `127.1`) and IPv4-mapped IPv6 addresses are refused, so a stored origin is the one the browser reports. With neither, the run fails before the browser starts.

**Start URL.** A run's first navigation goes to the start origin followed by the spec's `start_url` as written: joined as text, never resolved and never percent-decoded, so `/%2f%2fevil.test` stays a path on the start origin (ADR-0026's start URL amendment). `aqa_core.project.start_url` builds it, and the executor's first navigation (#46), explore's and the confirmation replay's (#53) call it rather than building it. A compiled `navigate` path (§7) is joined to the start origin by the same rule: its single leading `/`, then the path as written, never decoded. One that breaks §6's segment rule can read as a path starting `//`, as `/..//evil.test` does.

**Egress hosts.** `subresource_hosts` and `expected_blocked` list bare host names or IP addresses (an IPv6 address in brackets), with no scheme, port or wildcard. A subresource host is reached only on its scheme's default port: 80 for http, 443 for https and wss, and never over `ws://` on its default port 80; `ws://` written to port 443 tunnels as wss does (ADR-0026 amendment, 2026-10-01, corrected 2026-10-02). `private_origins` lists origins; declaring one lets an allowed origin or a subresource host resolve to a private address, and doesn't make it reachable.

**Model roles (ADR-0007 amendment, 2026-10-01).** Each of `navigator`, `verifier`, `healer` and `vision_fallback` defaults to `claude-sonnet-5-5` on `anthropic`, and `roles` overrides a role's `provider`, `model`, `effort` and `fallback`. Config load checks every role and reports every role problem at once, each as `roles.<role>.<field>`, after the file's own shape problems (roles are resolved from a valid file):
- *Needs:* the model, and the fallback, must have the capabilities the role needs (TECH_STACK §3).
- *Priced:* the model must be in the pinned price map or declared under `models`.
- *Belongs:* a map model's provider (`litellm_provider`) must be the role's. A map model the map prices in tiers above a token threshold (`input_cost_per_token_above_200k_tokens` and the like) can't be used from the map, because cost records don't apply the tiers: declare it under `models` with the flat rates to record.
- *Provider:* only `anthropic` has an adapter in M1; another provider is an error that says so, and the role's only one. `fallback` names a model of the same provider.
- *Effort:* `low`, `medium`, `high`, `xhigh` or `max`.

**Model overrides.** A model missing from the pinned price map must be declared under `models`, with its capabilities and prices; otherwise config validation rejects it. A `models` entry also wins over the map for a model the map has, so a negotiated rate can be stated. Either way its cost records carry `price_source: config` and the rates they applied (`applied_prices`, §2), so a later change to this file doesn't change what past costs meant. A declared model has no cache-read rate: cached input tokens cost its input rate.

**Private origins** apply to local and CI runs only. Hosted runs reach public addresses only (ADR-0026).
