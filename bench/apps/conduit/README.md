# Conduit (benchmark app 1)

The RealWorld "Conduit" app (a Medium.com clone), vendored as the first benchmark app (ADR-0020). It is third-party code: `policy_guard.py` and fallow skip this directory, and gitleaks still scans it (ADR-0021).

## Run

```bash
cd bench/apps/conduit
docker compose up --build --wait     # first build takes a few minutes; later starts take seconds
open http://127.0.0.1:4100           # bound to loopback only
docker compose restart backend       # reset to the seeded state (about 2 s)
curl -X POST 'http://127.0.0.1:4100/test-api/reset?fixture=seed'   # the same reset in place (a few ms)
docker compose down
```

**Benchmark flags (ADR-0022).** Each planted change sits behind an opaque flag ID. Switch cases from the repository root with the harness, which recreates both containers with `BENCH_FLAGS` set (reseeding the database) and checks that both tiers got exactly that flag set:

```bash
python3 bench/harness/flags.py set conduit-bug-001   # one case on
python3 bench/harness/flags.py clean conduit         # the clean app
python3 bench/harness/flags.py show conduit          # which case is on
```

The harness doesn't rebuild images, so run `docker compose build` here after changing app code. A plain `docker compose up` takes `BENCH_FLAGS` from your shell; unset means the clean app.

The whole app is served from one origin: nginx serves the Angular build and proxies `/api` and `/test-api` to the backend. No request leaves the Compose network.

## Test-only endpoints (`/test-api/*`)

Ours, for spec `reset` hooks and `probes` (DATA_MODEL.md §6, ADR-0022). They sit outside upstream's `/api`, and flags are never exposed over HTTP.

| Endpoint | Returns |
|---|---|
| `POST /test-api/reset?fixture=seed` | `204` after restoring the fixture's pristine database in place (flags untouched). An unknown fixture returns `404`. Resets run one at a time; a request in flight during a reset may fail, so reset between attempts |
| `GET /test-api/articles/count?author=<username>` | `{"count": n}`: that user's articles (`0` for an unknown user). No `author` returns `400` |
| `GET /test-api/comments/count?article=<slug>` | `{"count": n}`: that article's comments (`0` for an unknown slug). No `article` returns `400` |

Probes are narrow and read-only: add one per question, in the same change as the spec that needs it.

## Specs (`qa/`)

The benchmark pilot specs (DATA_MODEL.md §6): `login`, `read-article`, `post-comment`, `favorite-article` and `publish-article`. `qa/REVIEW.md` is their dry compile-rules review (ADR-0023). Specs are system-under-test input, so neither they nor the review mention any case or planted change; ground truth lives only in `bench/manifest.v1.json`.

## Seeded data

`seed/seed.ts` runs once, when the backend image is built. It writes a pristine SQLite database (`/app/fixtures/seed.db`, the `seed` fixture), and every backend start copies that database into a `tmpfs`, so a restart is a full reset. Slugs, timestamps (January 2026, UTC) and insertion order are all fixed, so ids and rendered dates never change between runs.

| Account | Email | Notes |
|---|---|---|
| `jake` | `jake@conduit.test` | 5 articles; followed by anna and reader |
| `anna` | `anna@conduit.test` | 5 articles |
| `reader` | `reader@conduit.test` | 2 articles; follows jake; 2 favorites |

- All three accounts share the password `conduit-bench-password`. It is a **public benchmark fixture** for a local app, not a secret; specs receive it as the `TEST_PASSWORD` secret so it never reaches model input.
- The data: 12 articles (two pages of 10 in the global feed), 8 tags (one article is untagged), 4 comments, 4 favorites and 2 follows.
- Content created *during* a run gets upstream's random slug suffix and a real timestamp. Specs must not assert on either.

## Provenance

| Path | Upstream | Commit | License |
|---|---|---|---|
| `frontend/` | [realworld-apps/angular-realworld-example-app](https://github.com/realworld-apps/angular-realworld-example-app) | `dd99ed2` | MIT (`frontend/LICENSE`) |
| `frontend/realworld/` | [realworld-apps/realworld](https://github.com/realworld-apps/realworld) (subset: `assets/theme`, `assets/media/*.svg`, `specs/e2e`) | `ffbd690` | MIT |
| `backend/` | [realworld-apps/nitro-prisma-zod-realworld-example-app](https://github.com/realworld-apps/nitro-prisma-zod-realworld-example-app) | `c8c6685` | MIT (`backend/LICENSE`) |
| `backend/realworld/` | realworld-apps/realworld (subset: `specs/api`) | `450bbc5` | MIT |
| `frontend/src/vendor/fonts/` | Google Fonts: Lora, Source Sans Pro, Titillium Web (`latin` and `latin-ext` subsets, woff2) | fetched 2026-09-28 | OFL-1.1 (`licenses/`) |
| `frontend/src/vendor/ionicons/` | npm `ionicons@2.0.1` (tarball integrity verified against the registry; byte-identical to the CDN copy the app used to load) | 2.0.1 | MIT |

The two apps pin different `realworld` commits, so each carries only the subset it uses. Each tree is a `git archive` export of tracked files.

**Left out of the upstream trees:**
- `.github/`: upstream CI, which never runs from a subdirectory.
- `.gitmodules` and `.husky/`: repository plumbing.
- The frontend's `CLAUDE.md`: Claude Code loads nested `CLAUDE.md` files, so upstream's agent instructions would enter our sessions.
- Non-SVG media: the build copies only `*.svg`.
- The realworld repo's docs and CI.

## Changes to upstream files

1. **`frontend/src/app/core/interceptors/api.interceptor.ts`**: API requests go to the same origin (`/api`) instead of the public demo API `https://api.realworld.show`.
2. **`frontend/src/index.html`**: the Ionicons and Google Fonts stylesheets load from `vendor/` instead of `code.ionicframework.com` and `fonts.googleapis.com`. This also removes the build's own network fetch: Angular inlines Google Fonts at build time.
3. **`frontend/angular.json`**: one asset entry copies `src/vendor/` into the build.
4. **`backend/Makefile`**: upstream's `run` and `test-*` targets hard-code a random 44-character `JWT_SECRET`. It is replaced with `${JWT_SECRET:-conduit-bench-jwt-fixture}`, so a live-looking key is never republished here. The Compose setup does not use these targets.
5. **`backend/server/utils/prisma.ts`**: `closePrisma()` is added (and the client variable may now be `undefined`), so `/test-api/reset` can close the client before restoring the database file (ADR-0022).

## Our additions inside the upstream trees

New files only (ADR-0022); planted changes will add flag checks to upstream files and are listed per case in the manifest.

- **`backend/server/utils/bench-flags.ts`** (+ `bench-flags.test.ts`): `benchFlag(id)`, read once from `BENCH_FLAGS` at start. Nitro auto-imports it into routes.
- **`backend/server/routes/test-api/`**: the test-only endpoints above.
- **`frontend/src/app/bench/flags.ts`**: `benchFlag(id)`, read from the `<script id="app-flags" type="application/json">` element that the frontend container writes into `index.html` at start.

Everything else is ours and sits outside the upstream trees: `seed/`, `docker/` (including `bench-flags.sh`, which validates `BENCH_FLAGS` in both images, and `frontend-flags.sh`, which renders `index.html`), `compose.yaml`, `.dockerignore` and this README.

## Verification (2026-09-28)

- **Build and start:** `docker compose up --build --wait` reaches healthy on both services.
- **Through the single origin:** the SPA and deep links, the self-hosted fonts and icons, the 12 seeded articles, fixture login, and the tags all work.
- **Reset:** a write followed by `restart backend` returns byte-identical article data.
- **In-place reset (2026-09-28):** 20 rounds of writes (create and delete an article, comment, favorite, follow, edit a profile, register a user), each followed by `POST /test-api/reset?fixture=seed`, restored a fingerprint of 21 state entries (every article, comment list, tag, profile, and one user's feed and settings) to its pristine value every time. Median reset: 2 ms.
- **Flags (2026-09-28):** `flags.py selftest conduit --cycles 10` made 20 verified switches (median 2.67 s). A switch after creating an article returned jake's article count from 6 to 5. An invalid `BENCH_FLAGS` (uppercase, an empty item, `*`, `;`) stops the backend and the frontend from starting, and `bun test server/utils/bench-flags.test.ts` passes.
- **In Chromium** (Playwright 1.63 in a container on the Compose network), visiting `/`, an article, a profile and `/login`:
  - 131 requests, **all to the app's own origin**, with no failed requests and no console errors
  - Ionicons, Lora and Source Sans Pro loaded
  - The theme never uses Titillium Web, so its files are never requested.

### Upstream test suites against the vendored copy

| Suite | Result |
|---|---|
| Backend API spec suite (Hurl 7.1.0; upstream's backend CI gate) | **13/13 files, 154/154 requests pass**, except that **`pagination.hurl` is flaky upstream**. Over 30 full runs each, it failed 8 times at `24d0009` (before `/test-api` existed) and 8 times with `/test-api`, including an interleaved A/B of 20 runs each; no other file ever failed. Its two articles are created back to back, often in the same millisecond, and upstream lists articles by `createdAt desc` with no tiebreak; every failure had equal timestamps (LAB_NOTES.md) |
| Backend unit tests (`bun test`) | 13/13 pass (upstream's 11 plus our 2 for `bench-flags.ts`) |
| Frontend unit tests (Vitest) | **Broken upstream.** `src/test-setup.ts` imports `zone.js`, which `package.json` never declares. The pristine upstream clone fails the same way, and upstream CI doesn't run these tests. |
| Frontend E2E suite (Playwright 1.60; upstream's frontend CI gate) | **113/139 pass; 26 fail.** No retries, both as one run and file by file on fresh seed data. |

**Open item: the 26 E2E failures are not explained yet.**
- They are deterministic (the same set every run), and fresh seed data doesn't change them.
- By file: `error-handling` 16, `user-fetch-errors` 5, `social` 3, `comments` 1, `navigation` 1.
- Replaying one failing test (a mocked 400 on registration) step by step outside the suite passes: the mock fires and both errors render.
- Upstream's CI passed this suite on 2026-09-11, but with the Angular dev server against the public demo backend, not a production build against this backend.

Until the cause is known, **pilot specs avoid error-handling flows**. If these are real bugs in the clean app, a spec over those flows would count them as false positives.

**To run upstream's suites yourself,** start the stack, work on a *copy* of the app directories (so `node_modules` never lands in the repo), and join the Compose network:

```bash
# API spec suite: point Hurl at the backend on the Compose network
docker run --rm --network conduit-bench_default -v "$PWD/backend/realworld/specs/api:/specs" -w /specs \
  --entrypoint sh ghcr.io/orange-opensource/hurl:7.1.0 \
  -c 'hurl --test --jobs 1 --variable host=http://backend:3000 --variable uid=bench$(date +%s) hurl/*.hurl'

# E2E suite: API_BASE is REQUIRED. The suite's helpers default to the public demo
# API (https://api.realworld.show/api) and would create test data there.
# Its config expects `ng serve`, so pass a config that sets baseURL http://frontend and no webServer.
docker run --rm --network conduit-bench_default -v "<copy-of-frontend>:/app" -w /app \
  -e CI=1 -e API_BASE=http://frontend/api mcr.microsoft.com/playwright:v1.60.0-noble \
  npx playwright test -c <that-config> --retries=0 --reporter=json
```

Use the JSON reporter for counts. The line reporter writes cursor-control codes that hide its summary lines from a plain `grep`.

## Updating upstream

Re-export each tree at the new commit with the same exclusions, then re-apply the five changes above and restore our additions and planted changes. After that:
- Re-run the seed, the checks above, and `gitleaks dir .`.
- An upstream change that moves a reviewed finding makes CI fail until its `.gitleaksignore` entry is re-reviewed (ADR-0021).
