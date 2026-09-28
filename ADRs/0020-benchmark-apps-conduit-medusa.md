# ADR-0020: Benchmark apps — RealWorld Conduit first, Medusa second

- Status: Accepted
- Date: 2026-09-28

## Context
PRD open question #1: which two open-source apps host the benchmark (ADR-0015). The docs set the criteria:
- **Credibility:** established, externally authored apps, so the benchmark survives the "you wrote the apps" critique.
- **License:** permissive, since the apps are vendored into this Apache-2.0 repo.
- **Modifiable code, front and back:** the six bug categories include backend 5xx and JS errors, and specs need test-only endpoints such as `/test-api/reset` and `/test-api/orders/count`.
- **Accounts and seedable fixtures**, plus an offline payment path for the commerce app.
- **Determinism and no external network at runtime:** the flake target is < 1% of replays, and the agent's browser is egress-restricted.
- **Runs under `docker compose`** on both arm64 (development) and x64 (CI).

## Options (evaluated 2026-09-28)
| Candidate | License | Verdict |
|---|---|---|
| RealWorld Conduit: `realworld-apps` Angular frontend + Nitro/Prisma/Zod backend | MIT, both maintained in 2026 | **Chosen for app 1** |
| RealWorld backends `gothinkster/node-express-…`, `lujakob/nestjs-…` | No license (all rights reserved) | Excluded |
| `gothinkster/react-redux-realworld-example-app` | MIT, archived | Excluded |
| Medusa: `medusajs/dtc-starter` backend + Next.js storefront | MIT (repository Enterprise Edition materials excluded) | **Chosen for app 2** |
| Spree: `spree/spree` + `spree/storefront` | BSD-3 core, MIT dashboards and storefront | Fallback for app 2 |
| Saleor | BSD-3 core, but the storefront is FSL-1.1 | Excluded |
| Vendure / EverShop | GPLv3 / GPL-3.0 | Excluded |
| Sylius, Solidus, Bagisto | MIT / BSD-3 / MIT | Viable, not preferred: PHP or Ruby backends where Medusa keeps the benchmark in TypeScript |

## Decision
- **App 1 (M0 pilot): RealWorld Conduit**, vendored under `bench/apps/conduit/` with the upstream LICENSE files:
  - `realworld-apps/angular-realworld-example-app` @ `dd99ed2` (Angular 21)
  - `realworld-apps/nitro-prisma-zod-realworld-example-app` @ `c8c6685` (Nitro, Prisma on SQLite, Zod)
  - both of which include the `realworld-apps/realworld` spec submodule @ `ffbd690`
  - Both run on Bun inside containers.
- **App 2 (M3): Medusa**, from `medusajs/dtc-starter`, confirmed by a spike like the one below at the start of M3. Spree is the fallback.

## Evidence (Docker spike, 2026-09-28)
- **Backend:** `oven/bun:1` (Bun 1.4.2) installs, generates Prisma, pushes the schema and builds (6.3 MB). It served requests about 15 s after the container started.
  - These all succeed: register 201, login 200, create article 201, comment 201, favorite 200, list by tag 200, tags 200.
  - Errors are correct: no token → 401, invalid body → 422.
- **Frontend:** the production build takes 3.4 s and produces a 560 KB bundle.
  - External hosts in the bundle: `fonts.gstatic.com` (87 references, from the shared theme), `api.realworld.show` (1, the API interceptor), plus namespace strings and plain links (`w3.org`, `github.com`, `angular.dev`) that are never fetched.
- **Size:** about 1.4k lines of TS/HTML/CSS in the backend and 4.6k in the frontend. The frontend ships a Playwright E2E suite, and the backend has Hurl and Bruno API spec suites.

## Consequences
- **Localization before first use:** point the frontend's API interceptor at the local backend, and self-host the Google Fonts (OFL-licensed) so no runtime request leaves the compose network.
- **Determinism:** article slugs carry a random suffix and records carry wall-clock timestamps, so seed fixtures must set both explicitly. Reset means restoring a seeded SQLite file.
- **Bun is a benchmark-app runtime only,** inside containers. It is not part of this project's toolchain (TECH_STACK.md).
- **Our gates will flag vendored third-party code:** upstream suppressions, skipped tests, possible test keys, and deliberately planted dead code. `bench/apps/` needs a deliberate, narrow carve-out in `policy_guard.py`, fallow's `ignorePatterns` (ADR-0018) and gitleaks before vendoring. That is the next change.
- **Credibility:** the Nitro backend is young (few stars). Its standing comes from being the RealWorld organization's spec-compliant reference backend. Running the RealWorld API spec suite against our vendored copy with every flag off shows that the vendoring didn't change behaviour.
- **Medusa at M3:** use the npm packages, never the Enterprise Edition materials. Localize the seed images (currently on a public S3 bucket). Add a fake card provider for payment-failure specs. Plant backend bugs through `pnpm patch` or workflow hooks.
