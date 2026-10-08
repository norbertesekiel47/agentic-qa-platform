# API — Agentic QA Platform

Last updated: 2026-10-08 (an interrupted plan call keeps its charges and exits 130, #161; unwritable run records exit 15 before planning, #53 P1; `aqa explore --plan-only`, its run record, and exit codes 3, 5, 10 and 11, #41; an invalid compiled script is a spec error, and so is one the M1 executor can't run, #46; `--url` is an origin, #39; explore exit codes and flags, ADR-0024). REST over HTTPS, JSON, base path `/v1`. The FastAPI app is the source of truth; its generated OpenAPI document produces the dashboard's typed client. This file is the design contract — update it in the same PR as any endpoint change.

## 1. Conventions

- **Errors:** RFC 9457 Problem Details (`application/problem+json`) with a stable `type` URI and `code` (e.g. `domain_not_verified`, `rate_limited`, `org_not_found`, `proposal_stale`).
- **Pagination:** cursor-based — `?limit=50&cursor=<opaque>`; responses include `next_cursor`.
- **Idempotency:** `Idempotency-Key` header required on `POST /runs` and result ingestion; a repeated request returns the original response for 24 h.
- **Rate limits:** per org and per credential; `429` with `Retry-After` and `RateLimit-*` headers.
- **Payload limits:** API Gateway → Lambda synchronous invocations cap request/response payloads at **6 MB**; ingestion endpoints accept ≤ 1 MB bodies (batched), and anything larger goes to S3 via presigned URLs.
- **Versioning:** additive changes within `/v1`; breaking changes → `/v2`.

## 2. Authentication modes

| Caller | Credential | How it resolves to an org |
|---|---|---|
| Dashboard | Clerk session JWT (`Authorization: Bearer`) | Verify via Clerk JWKS (`iss`, `exp`, `azp`); read the active-org claim `o.id` (a Clerk string ID like `org_2abc…`, session token v2); map via `organizations.clerk_org_id` → internal UUID; reject unmapped orgs |
| CLI / non-GitHub CI | Project API key `aqa_live_<prefix>_<secret>` | `auth_api_key(prefix)` definer function → argon2id verify → project → org |
| GitHub Actions | GitHub OIDC token → run token | `POST /v1/auth/oidc/exchange`: verify signature/`iss`/`aud=agentic-qa`/`exp`; match immutable `repository_id` + `repository_owner_id` to a linked repo; enforce the repo's `oidc_policy` (allowed `event_name`; `workflow_ref`/`workflow_sha` for ordinary workflows, `job_workflow_ref` only when a reusable workflow is required; refs); single use per `jti`; record `sha` as `execution_sha` (merge commit for `pull_request`) and the PR head as `head_sha` |
| Runner — CI | Run token (JWT, ≤ 15 min, `run_id`- and `lease_id`-bound, `runner_location=ci`, scope `run:write`) | Embedded org/run claims; cannot access other runs; **cannot** retrieve stored provider keys or test secrets |
| Runner — hosted | Run token issued by the dispatcher (`runner_location=hosted`, scopes `run:write secrets:read`) | Same, plus retrieval of the run's provider key and referenced test secrets; each continuation gets a new token + lease |
| GitHub | Webhook HMAC (`X-Hub-Signature-256`) | Installation → org via `auth_installation` |
| Clerk | Svix-signed webhooks | Clerk org ID → org |

Upload authority (run tokens) never implies authority to create heal commits; heal commits require a human's requested action verified against their repo permission (§5).

## 3. Endpoints

### Auth
| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/oidc/exchange` | GitHub OIDC token → run-scoped token (+ creates run shell) |
| POST | `/auth/cli/device` · `/auth/cli/token` | Device-code login for `aqa login` |

### Projects & repos
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/projects` | List / create |
| GET/PATCH/DELETE | `/projects/{id}` | Read / update settings (model routing, invariants, retention) / delete |
| GET | `/projects/{id}/repositories` | Linked GitHub repos |
| PATCH | `/repositories/{id}/oidc-policy` | Allowed events, workflows, refs for OIDC exchange |
| POST | `/github/installations/sync` | Refresh repos after App install |

### Specs
| Method | Path | Purpose |
|---|---|---|
| GET | `/projects/{id}/specs?branch=` | Specs indexed from the repo at a branch |
| GET | `/specs/{id}/versions/{version_id}` | Parsed spec + compiled script |
| POST | `/specs/{id}/edit-pr` | Dashboard edit → opens a PR with the modified spec |

### Test secrets (write-only values)
| Method | Path | Purpose |
|---|---|---|
| GET | `/projects/{id}/test-secrets` | Names, allowed origins, field hints, rotation dates (never values) |
| PUT | `/projects/{id}/test-secrets/{name}` | Create/replace `{value, allowed_origins[], allowed_field_hint}` — origins must be verified domains for hosted use |
| DELETE | `/projects/{id}/test-secrets/{name}` | Delete |

### Runs
| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` | Create a hosted run `{spec_version_id \| spec_ids[], target_url, mode: strict\|verified\|explore}` → `202` + run ID |
| GET | `/runs?project_id=&status=&branch=&pr=` | List |
| GET | `/runs/{id}` | Run detail with steps, verdicts, cost |
| POST | `/runs/{id}/cancel` | Cancel |
| GET | `/runs/{id}/artifacts/{artifact_id}` | Short-lived presigned download URL |

### Runner ingestion (run token only)
| Method | Path | Purpose |
|---|---|---|
| GET | `/runner/runs/{id}/bundle` | Spec, compiled script, config, allowed origins, mode |
| GET | `/runner/runs/{id}/provider-key/{role}` | Decrypted BYOK key (hosted runs only; `Cache-Control: no-store`; never logged) |
| GET | `/runner/runs/{id}/secrets/{name}` | Decrypted test secret — **hosted-execution tokens only** (`secrets:read`), only for names referenced by the run's spec; returned with allowed origins/field hint; each retrieval audited |
| POST | `/runner/runs/{id}/steps` | Write step **intent** rows before dispatch and completion updates after (batched, ≤ 1 MB); rejected if the token's `lease_id` is stale |
| POST | `/runner/runs/{id}/artifacts:presign` | Presigned S3 PUT URLs |
| POST | `/runner/runs/{id}/events` | Live events → `run_events` + WebSocket fan-out |
| PUT/GET | `/runner/runs/{id}/storage-state` | Encrypted Playwright storage state at checkpoints (continuation) |
| — | `/runner/runs/{id}/checkpoints/…` | LangGraph saver backend (§4) |
| POST | `/runner/runs/{id}/complete` | Final status, verdicts, findings, heal proposals, LLM usage |

### Triage
| Method | Path | Purpose |
|---|---|---|
| GET | `/triage?type=bug\|heal&status=open` | Inbox |
| POST | `/heal-proposals/{id}/accept` · `/reject` | Accept (commits via GitHub App after staleness check) / reject |
| POST | `/findings/{id}/status` | `resolved` \| `not_a_bug` |
| POST | `/verdicts/{id}/labels` | Human ground-truth label |

### Usage
| Method | Path | Purpose |
|---|---|---|
| GET | `/usage/costs?group_by=project\|role\|model\|mode\|day&from=&to=` | LLM spend (strict replays are always $0; verified-mode spend is its own line) |
| GET | `/usage/summary` | Share of runs with $0 LLM cost, runs, pass rate |

### Settings
| Method | Path | Purpose |
|---|---|---|
| GET/POST/DELETE | `/domains` | Add domain → returns TXT token / well-known file contents; delete |
| POST | `/domains/{id}/verify` | Trigger verification check |
| GET/POST/DELETE | `/provider-keys` | Add (write-only; returns `last4` only) / list / delete |
| POST | `/provider-keys/{id}/rotate` | Replace ciphertext |
| GET/POST/DELETE | `/api-keys` | Create (secret shown once) / list / revoke |
| GET | `/audit-events` | Org audit log |

### Webhooks (inbound)
| Path | Events |
|---|---|
| `/webhooks/github` | `pull_request`, `check_run` (incl. `requested_action`), `installation`, `installation_repositories`, `push` (spec indexing) |
| `/webhooks/clerk` | `organization.*`, `organizationMembership.*` (removal → drop that user's WebSocket connections), `user.*` |

## 4. Checkpointer backend contract

The runner's custom LangGraph saver implements the full `BaseCheckpointSaver` interface — sync **and** async variants (`get_tuple`/`aget_tuple`, `list`/`alist`, `put`/`aput`, `put_writes`/`aput_writes`, `delete_thread`/`adelete_thread`) — over these endpoints:

| Method | Path | Semantics |
|---|---|---|
| GET | `/runner/runs/{id}/checkpoints?thread_id=&checkpoint_ns=&checkpoint_id=` | `get_tuple`: latest (or specific) checkpoint + its pending writes |
| GET | `/runner/runs/{id}/checkpoints:list?thread_id=&before=&limit=&filter=` | `list`, newest first |
| PUT | `/runner/runs/{id}/checkpoints/{checkpoint_id}` | `put`: upsert checkpoint (≤ 1 MB compressed; large values must be artifact references) |
| PUT | `/runner/runs/{id}/checkpoints/{checkpoint_id}/writes/{task_id}` | `put_writes`: idempotent upsert keyed by `(task_id, idx)` |
| DELETE | `/runner/runs/{id}/checkpoints?thread_id=` | `delete_thread` |

All calls are idempotent and safe to retry; the saver retries transient failures with backoff and fails the run (never silently drops a write) after the retry budget.

## 5. Realtime (WebSocket API)

`wss://rt.<domain>/?ticket=<60-second ticket>` — minted by `POST /v1/realtime/ticket` (Clerk-authenticated). API Gateway authorizes only at `$connect`; everything after is authorized in application code.

Client → server: `{"action":"subscribe","run_id":"…","since_seq":42}` (each subscribe is authorized against the user's current membership) · `{"action":"unsubscribe","run_id":"…"}` · `{"action":"ping"}` (heartbeat, < 10 min)

Server → client events (all carry `seq`):
```json
{ "type": "step.started",   "run_id": "…", "seq": 7,  "action": {"kind":"click","target":"Pay"} }
{ "type": "step.finished",  "run_id": "…", "seq": 8,  "outcome": "failed", "screenshot_artifact_id": "…" }
{ "type": "agent.thought",  "run_id": "…", "seq": 9,  "text": "The Pay button moved into the order-summary drawer…" }
{ "type": "verdict",        "run_id": "…", "seq": 10, "verdict": "drift_consistent", "confidence": 0.86 }
{ "type": "run.finished",   "run_id": "…", "seq": 11, "status": "heal_proposed", "llm_cost_usd": 0.031 }
```
Connections live ≤ 2 hours (API Gateway limit); clients reconnect with `since_seq` and receive missed events from `run_events`.

## 6. GitHub App

- **Permissions (least privilege):** Checks: write · Contents: write (heal commits, spec PRs) · Pull requests: write · Metadata: read. No admin, no secrets, no workflows permission.
- **One check run per spec** (`agentic-qa / <spec_id>`), plus a summary check. Output includes the result table, cost, and a link to the run viewer (`details_url`).
- **Heal action:** a spec with a pending proposal gets `conclusion: action_required` and one action: `{label: "Accept heal", description: "Commit the locator update", identifier: "ah_<12 chars>"}` — within GitHub's limits (≤ 3 actions per check run; label and identifier ≤ 20 characters; description ≤ 40).
- **On `requested_action`:** verify signature → verify the clicking user has write permission → load proposal by `action_token` → **staleness check** (the PR's current **head** SHA — not the merge/execution SHA — and the compiled blob SHA must equal the proposal's `base_head_sha` / `base_compiled_blob_sha`; otherwise mark `stale` and request a new run) → commit authored by the App, message `chore(qa): heal <spec_id> (<proposal_id>)`, co-authored-by the accepting user.
- **Fork PRs:** the App can't push; the check output shows the patch and `aqa heal apply <proposal_id>` for the author.

## 7. CLI (`aqa`)

```
aqa init                          # scaffold qa/config.yaml + example spec
aqa explore <spec> [--url <url>]  # coverage plan → exploration → confirmation replay → qa/.compiled/<spec id>.json (ADR-0024)
            [--plan-only]         #   write the coverage plan to the run record and stop (no browser); required until #53
            [--force]             #   overwrite an up-to-date compiled script
            [--confirm-repeat]    #   allow one confirmation that repeats side effects when the spec has no reset hook
aqa replay  [<spec>...] --url     # strict replay (default), no LLM; --mode verified for model-assisted visual checks
aqa run     [<spec>...] --url     # replay; heal on drift; write proposals locally
aqa heal    <spec> --url          # force a heal pass from the first failing step
aqa heal apply <proposal_id>      # apply a proposal locally (fork PRs)
aqa report  [<run_id>]            # open the run viewer (local HTML)
aqa login                         # device-code auth to the SaaS
aqa upload  <run_dir>             # upload a local run
aqa --version                     # print "aqa <version>" and exit
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | All passed. For `explore`: compiled, including an unconfirmed script, which prints a warning |
| `1` | Expectation violated (bug). Never returned by `explore` |
| `2` | Heal proposals pending |
| `3` | Inconclusive. For `explore`: gave up (attempts or budget exhausted, a model refusal with no fallback, a coverage plan that didn't parse or was cut off at the output bound, a plan that still can't be used when asked for once more (it doesn't fit its spec, or a check's target holds what the check asserts), or a flaky confirmation) |
| `4` | Non-resumable run |
| `5` | Spec error: the spec, its compiled script or the project config is invalid, a required setting is missing (a start origin, the provider's key), an expectation has no establishing check, or the compiled script has a step or check M1 can't run (a `press` of more than one key after modifiers or `visible_unoccluded` with `in_viewport: false`), a probe assertion or baseline names a probe the spec doesn't declare or a `fill_secret` step names a secret the spec doesn't reference (ADR-0024 and its #46 amendment; DATA_MODEL §7, "Checked by the loader") |
| `6` | Policy: egress blocked. The page requested a host that is neither an allowed origin, a subresource host nor expected-blocked. No finding; the run record names the refused host (ADR-0026) |
| `10` | No sandbox: Chromium's sandbox can't start, or the sandbox check can't prove it (`aqa_runner.sandbox.SandboxUnavailableError.exit_code`, ADR-0026) |
| `11` | No model response: a model call got none (a provider outage, a timeout, a refused key), or a fallback got none after a billed refusal, whose cost record is kept (#41) |
| `12` | Test secrets can't be used: `AQA_SECRET_<NAME>` is unset, empty, whitespace-only, or shorter than 4 bytes after JavaScript whitespace trimming in UTF-8 or Latin-1 (when encodable) for a test secret the spec references, or the environment turns on Playwright logging that would print a value (`DEBUGP`, or `DEBUG` naming `pw:protocol` or `pw:browser`); found before the browser starts (`aqa_runner.bound_secrets.MissingSecretError.exit_code`, `SecretLoggedError.exit_code`, ADR-0026) |
| `13` | For `explore`: an allowed host can't be reached, the start origin included, or the egress proxy's listener stopped accepting the browser's connections after an accept error. Each is an infrastructure event (ADR-0026's #53 P8 amendment) |
| `14` | For `explore`: the reset hook failed (it answered other than 2xx, a redirect included, didn't answer within `budgets.resolve_seconds`, or couldn't be reached), or a phase ended with uncertain traffic. In M1 that includes every phase whose traffic used HTTPS or a WebSocket: the proxy can't see whether a tunnel's requests were answered (ADR-0026's #53 P8 amendment) |
| `15` | The run record could not be created: the spec root is unwritable or its record directory cannot be made. Found before planning or spend, including `explore --plan-only`; the diagnostic includes no raw OS error (#53 P1) |
| `10+` | Other infrastructure errors; their codes come with #53 |

**Interrupted while planning.** Ctrl-C during `explore --plan-only`'s plan call exits 130, the shell's code for SIGINT, and a cancellation of the calling task propagates as `asyncio.CancelledError`. Neither has a code of its own, and nothing is printed. The run record keeps what the plan call was billed before the interruption (ADR-0024's #161 amendment). A `plan.json` that can't be written never replaces the interruption and is not exit 15: stderr gets one fixed line, `interrupted: the run record could not be written, so what the plan call was billed is not kept`.

The run's start origin is `--url`, or the project config's `base_url` when `--url` is omitted (DATA_MODEL §9). Both must be origins: a `--url` with a path is an error, not cut back to its origin. A spec's `start_url` is only a path (ADR-0026). `<spec>` is a spec file's path, and its spec root is the nearest directory, from the spec's own up, that holds `config.yaml`. `explore` reads the whole project, so a problem in any spec of it stops the command. With `--plan-only`, the plan, its `plan_hash`, the outcome with its reasons, and the cost record of every model response go to `plan.json` in the run record, `<spec root>/.aqa/runs/<run_id>/`, whose `.aqa/` holds a `.gitignore` of `*`; the one-line summary names it. Test-secret values come from `AQA_SECRET_<NAME>` environment variables, and their bindings from the project config (DATA_MODEL §9).

## 8. GitHub Action

```yaml
on: pull_request
permissions: { id-token: write, contents: read }
steps:
  - uses: <owner>/agentic-qa-action@v1
    with:
      url: ${{ steps.preview.outputs.url }}
      specs: "qa/**/*.spec.md"
      provider-keys: |
        ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
      secrets: |
        TEST_PASSWORD=${{ secrets.TEST_PASSWORD }}
```
Docker-based action (runs the same runner image). No SaaS secret needed — OIDC handles auth. Fork PRs don't receive OIDC tokens; the action then runs locally and reports results in the job log only.

Each `secrets:` entry reaches the runner as `AQA_SECRET_<NAME>`, bound as the project config declares. Two questions are open for M5:
- how the action runs Chromium's sandbox, since Docker actions run as root under Docker's default seccomp profile (#26);
- which config revision a CI run trusts for secret bindings (#27).
