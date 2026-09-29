# ADR-0022: Benchmark feature flags, test-only endpoints and the ground-truth manifest

- Status: Accepted
- Date: 2026-09-28

## Context
M0 task 3 (ROADMAP). The benchmark plants bugs and benign changes in vendored apps (ADR-0015, ADR-0020), and the harness must switch them on one case at a time. Four constraints shape the design:
- **No rebuild per case.** Conduit's frontend is a static Angular build behind nginx, so it can't read environment variables at runtime.
- **Specs need a reset hook and read-only probes** on the app's allowed origins (DATA_MODEL §6).
- **Ground truth lives only in `bench/manifest.v1.json`, keyed by case ID** (ARCHITECTURE §10), and the split is committed with a hash before M1 (TESTING §5).
- **The system under test must not learn the answers.** A flag must not reach what the model observes (the accessibility tree, screenshots, console output, the network log), and the runner must not need to know which case is active.

## Options
**Flag delivery:**
1. **An environment variable plus a container recreate.** Both tiers read `BENCH_FLAGS` once at start. The switch costs a recreate and also resets the database.
2. **Runtime flags** in a shared file or behind a control endpoint, with the frontend fetching them at startup. Switching is almost free, but the fetch appears in every page's network log (which the compiler reads), and flags become shared state that can change, so one case's flag can carry into the next. Flag state must also survive the spec's reset hook.
3. **A cookie or header on each request,** set by the runner. This allows parallel cases on one stack, but the runner under test would have to know the benchmark flags, they show in the browser's stored state, and the database is shared anyway.

**Probes:** narrow endpoints that each answer one question, or one general read-only query endpoint (flexible, but a broad database-read surface on the app origin, and harder-to-read specs).

**Split-freeze hash:** the split assignment only, or every ground-truth field (which also blocks quiet edits to `expected` after the freeze, but changes on every pilot refinement).

**CI for the harness:** extend the existing `guardrails` job, or add a `bench` job (which must also become a required check).

## Decision
**Flags: option 1.**
- `compose.yaml` passes `BENCH_FLAGS` (comma-separated flag IDs, empty by default) to both services. Switching cases means `BENCH_FLAGS=<ids> docker compose up -d --wait`.
- The backend reads the list once, at start. Planted code calls `benchFlag("<id>")` from `backend/server/utils/bench-flags.ts`.
- The frontend container writes the list into `index.html` at start as an inline, non-executed JSON script element. Planted code reads it through `frontend/src/app/bench/`. The list is validated against the flag-ID pattern before it is written, so nothing else can be injected.
- Flag IDs are **opaque**: four lowercase letters or digits, such as `k3q9`, not case IDs, because the frontend's list reaches the browser. The manifest maps each case to its flag. Code comments may cite the case ID: the production frontend build strips comments and ships no source maps, and backend source is never served.
- A flag has no observable effect except the planted change. No DOM attributes, class names, console output, cookies, URLs or requests of its own.
- Flags are neither readable nor writable over HTTP. The harness sets them through Compose and checks them from outside the app: the container environment, and the flag list in the served `index.html`. It refuses flag IDs the manifest doesn't know.

**Test-only endpoints (`/test-api/*`).**
- Served on the app's origin (nginx proxies `/test-api/` to the backend), because specs call them on allowed origins. They stay out of `/api`, which upstream's API spec suite covers.
- `POST /test-api/reset?fixture=<name>` restores that fixture's pristine database inside the running process: close the Prisma client, copy the pristine file over the live one, reopen. Flags are untouched. An unknown fixture returns 404. The first fixture is `seed`, the current seeded data.
- **Probes are narrow:** one read-only `GET` per question, returning a small JSON object (for example `GET /test-api/articles/count?author=jake` → `{"count": 5}`), added in the same change as the spec that needs it. No general query endpoint.
- Our code sits inside the upstream trees under paths that are clearly ours (`backend/server/routes/test-api/`, `backend/server/utils/bench-flags.ts`, `frontend/src/app/bench/`), and the app's README lists them. Planted bugs edit upstream files anyway, so copying our code in at build time wouldn't keep those trees pristine.

**Manifest.** `bench/manifest.v1.json` holds `schema_version` and a `cases` object keyed by case ID. Each case has:
- `app`, `kind` (`bug` or `benign`), and `category` (bugs only: one of the six `findings.category` values)
- `split` (`dev` or `test`), `family`, `flag`, and a one-line `summary`
- `expected`: a list of `{spec, verdict}` entries. A bug needs at least one `expectation_violated` entry that names the violated `expect` indexes (0-based, as `expect_index` in DATA_MODEL §7). Every entry on a benign case is `drift_consistent`.

The validator also checks that each case ID reads `<app>-<kind>-NNN` and agrees with its fields, that flags are unique, and that every case in a family shares one split. DATA_MODEL.md owns the full format once the validator lands.

**Split freeze: the split assignment only.** `bench/manifest.v1.split.sha256` holds the sha256 of the canonical JSON of `{case_id: [split, family]}` (sorted keys, no whitespace). A unit test recomputes it, so moving a case between splits or families means editing the hash file in a reviewed pull request.

**Harness code** lives in `bench/harness/`: Python 3.14, standard library only until M1 brings uv and Pydantic, tested with `unittest`. **CI: the `guardrails` job** runs ruff, `mypy --strict` and the tests over it as well.

## Evidence (2026-09-28, Docker Compose 5.1.4, arm64)
Recreating both Conduit containers with `docker compose up -d --force-recreate --wait` took 6.66–6.75 s over five runs. Restarting the backend alone took 1.2 s. Most of the recreate is health-check polling: 2 s intervals, and the frontend starts only after the backend is healthy.

## Consequences
- **Switching cases costs about 7 s** until the health checks start faster, which is small next to a run. The flake measurement replays clean apps and never switches.
- **Every switch starts from the seeded database,** so no case inherits another case's data.
- **One case at a time per stack.** Parallel cases need separate Compose projects and host ports (the port is fixed at 4100 today).
- **The runner never sees flags.** The M3 bench runner owns switching and imports the manifest loader.
- **Residual exposure:** the served HTML contains the flag list. The accessibility tree, screenshots and the network log don't, and our tools expose nothing else. A model that could read the page source would learn only that some opaque flag is on.
- **The spec `reset` hook works over HTTP,** and `docker compose restart backend` remains a full reset too.
- **Re-vendoring upstream** means re-applying our additions and the planted edits, as README "Updating upstream" already says for the four upstream changes.
- **Medusa (M3)** uses the same contract: `BENCH_FLAGS`, `/test-api/*` on its origin, and cases in the same manifest.
