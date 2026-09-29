# Lab Notes — Agentic QA Platform

Running log of non-obvious failures and their root causes. One entry per lesson; newest first. This is the authoritative home for these facts — don't duplicate them elsewhere; link here instead.

**Format:** `- [YYYY-MM-DD] WHAT FAILED → WHY → CORRECT APPROACH`

**Add an entry when:** debugging revealed a root cause that wasn't obvious from the error; a library/platform behaved differently than documented; or the same surprise happened twice.

**Don't add:** routine bugs with obvious fixes, TODOs (use issues), or decisions between alternatives (use an ADR).

## Log

- [2026-09-28] Upstream's Conduit API spec suite passed 13/13 once, then failed `pagination.hurl` on the same code (2 of 20 runs of that file alone) → the file creates two articles back to back, which often get the same millisecond `createdAt`, and upstream lists articles by `createdAt desc` with no tiebreak, so equal timestamps come back oldest-first; every failure had equal timestamps → treat that file as flaky upstream, judge vendored apps by repeated runs, and never write a benchmark spec that asserts the order of items created within the same millisecond.
- [2026-09-28] Running the RealWorld E2E suite against the vendored Conduit created users and articles, XSS payloads included, on the **public** demo API, and 49 tests failed → the suite's helpers take `API_BASE` from the environment and default to `https://api.realworld.show/api` → always set `API_BASE` to the local stack (`http://frontend/api`); the command is in `bench/apps/conduit/README.md`.
- [2026-09-28] A scraped E2E summary said a spec file "passed 15" while it also failed 16, leading to a wrong conclusion → Playwright's line reporter writes cursor-control codes (`ESC[1A ESC[2K`) even when output isn't a terminal, so `^\s+N failed` never matched → take counts from `--reporter=json` (`.stats.expected` / `.stats.unexpected`), never from scraped text.
- [2026-09-28] An audit of Conduit's external hosts (a grep of the build output for `https?://`) reported only Google Fonts and the demo API, and missed Ionicons → both stylesheet links in `index.html` are protocol-relative (`//host/...`), and Angular inlines Google Fonts at build time but leaves other links alone, so Ionicons never appeared in the output → audit a vendored app's network use in a real browser (a Playwright request log per page, asserting that every request hits the app's origin); if you must grep, match `(https?:)?//`.
- [2026-09-28] gitleaks did not report a planted fake GitHub token (`ghp_` followed by `A1b2` repeated) → gitleaks drops matches below each rule's entropy threshold, and a repeated pattern has almost none → test secret scanners with random, realistic fakes. The flip side: obviously fake fixtures don't need allowlisting.

## Watch list (known risks to verify early)

- A heal can route around a broken UI path and still satisfy every `expect`. In the pilot, `conduit-bug-005` covers the header's "New Article" link, so `publish-article` can't click it; a heal that replaces the click with `navigate("/editor")` passes every clause while real users stay stuck. Decide at M2 whether the heal-patch validator may replace an interaction step with direct navigation. Until then, the manifest leaves such specs unscored (DATA_MODEL §8).
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
