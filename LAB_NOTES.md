# Lab Notes — Agentic QA Platform

Running log of non-obvious failures and their root causes. One entry per lesson; newest first. This is the authoritative home for these facts — don't duplicate them elsewhere; link here instead.

**Format:** `- [YYYY-MM-DD] WHAT FAILED → WHY → CORRECT APPROACH`

**Add an entry when:** debugging revealed a root cause that wasn't obvious from the error; a library/platform behaved differently than documented; or the same surprise happened twice.

**Don't add:** routine bugs with obvious fixes, TODOs (use issues), or decisions between alternatives (use an ADR).

## Log

_No entries yet._

## Watch list (known risks to verify early)

- Headless Chromium inside a Python Lambda container image: memory floor, cold start, required launch flags, startup-hygiene overhead (M1 spike).
- Warm Lambda environment reuse: `/tmp` and surviving processes persist across invocations (and timeouts don't clear `/tmp`) — startup hygiene must run before any tenant data is fetched; crash-reuse tests must pass on real Lambda, not only the emulator.
- LangGraph checkpoint serialization size through the API: the binding limit is Lambda's **6 MB** synchronous payload (not API Gateway's 10 MB); keep checkpoints ≤ 1 MB compressed and large values in S3.
- Playwright request routing and service workers: service workers can bypass routing — keep them blocked; verify WebSocket routing coverage in the Playwright version pinned.
- Clerk client-only auth inside a Next.js static export (no middleware) — proven in the M7 skeleton.

## Resolved during design review (2026-09-27)

- Clerk session token v2 carries the active org as `o.id`, a string like `org_2abc…` — not a UUID. Map via `organizations.clerk_org_id` → internal UUID before setting `app.org_id`.
- psycopg 3 can't bind parameters in `SET LOCAL`; use `SELECT set_config('app.org_id', %s, true)`.
- Amplify Hosting SSR supports Next.js 12–15 only (as of 2026-09-27) → dashboard ships as a static export on S3 + CloudFront (ADR-0017).
- GitHub Checks: ≤ 3 actions per check run; action identifier and label ≤ 20 characters; description ≤ 40 → per-spec check runs with short opaque action tokens.
- API Gateway WebSocket: authorizers run only on `$connect`; 2-hour max connection, 10-minute idle timeout → authorize every subscribe in app code; heartbeats; reconnect with `since_seq`.
- Playwright launches Chromium with `--no-sandbox` unless `chromium_sandbox=True`; on Lambda the sandbox typically fails ("No usable sandbox!") → M1 decision gate between Lambda and per-run Fargate for hosted runs.
- GitHub OIDC: `job_workflow_ref` identifies a *reusable* workflow; ordinary workflows are identified by `workflow_ref`/`workflow_sha`. For `pull_request` events the `sha` claim is the merge commit, not the PR head → store execution SHA and head SHA separately.
- Zero flakes in 30 specs × 20 replays only bounds the pooled rate at ≤ 0.50% if replays are independent; with full within-spec dependence it's 30 independent outcomes (≤ 9.5%). Report both; bootstrap bounds on all-zero data are degenerate.
- Playwright page/context routing alone doesn't give connection-level egress control (WebSockets need `route_web_socket`, service workers bypass routing, DNS can rebind) → in-runner egress proxy with validated-IP connects as the primary control.
