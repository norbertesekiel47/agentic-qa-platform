# Conduit (benchmark app 1)

The RealWorld "Conduit" app (a Medium.com clone), vendored as the first benchmark app (ADR-0020). It is third-party code: `policy_guard.py` and fallow skip this directory, and gitleaks still scans it (ADR-0021).

## Run

```bash
cd bench/apps/conduit
docker compose up --build --wait     # first build takes a few minutes; later starts take seconds
open http://127.0.0.1:4100           # bound to loopback only
docker compose restart backend       # reset to the seeded state (about 2 s)
docker compose down
```

The whole app is served from one origin: nginx serves the Angular build and proxies `/api` to the backend. No request leaves the Compose network.

## Seeded data

`seed/seed.ts` runs once, when the backend image is built. It writes a pristine SQLite database, and every backend start copies that database into a `tmpfs`, so a restart is a full reset. Slugs, timestamps (January 2026, UTC) and insertion order are all fixed, so ids and rendered dates never change between runs.

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

Everything else is ours and sits outside the upstream trees: `seed/`, `docker/`, `compose.yaml`, `.dockerignore` and this README.

## Verification (2026-09-28)

- **Build and start:** `docker compose up --build --wait` reaches healthy on both services.
- **Through the single origin:** the SPA and deep links, the self-hosted fonts and icons, the 12 seeded articles, fixture login, and the tags all work.
- **Reset:** a write followed by `restart backend` returns byte-identical article data.
- **In Chromium** (Playwright 1.63 in a container on the Compose network), visiting `/`, an article, a profile and `/login`:
  - 131 requests, **all to the app's own origin**, with no failed requests and no console errors
  - Ionicons, Lora and Source Sans Pro loaded
  - The theme never uses Titillium Web, so its files are never requested.

### Upstream test suites against the vendored copy

| Suite | Result |
|---|---|
| Backend API spec suite (Hurl 7.1.0; upstream's backend CI gate) | **13/13 files, 154/154 requests pass** |
| Backend unit tests (`bun test`) | 11/11 pass |
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

Re-export each tree at the new commit with the same exclusions, then re-apply the four changes above. After that:
- Re-run the seed, the checks above, and `gitleaks dir .`.
- An upstream change that moves a reviewed finding makes CI fail until its `.gitleaksignore` entry is re-reviewed (ADR-0021).
