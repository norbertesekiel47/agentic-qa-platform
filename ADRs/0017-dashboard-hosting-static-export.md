# ADR-0017: Dashboard hosting — Next.js static export on S3 + CloudFront

- Status: Accepted
- Date: 2026-09-27

## Context
The dashboard was planned on AWS Amplify Hosting with Next.js 16. As of 2026-09-27, Amplify Hosting's SSR support lists Next.js **12–15** only. The dashboard needs no server rendering: all data comes from the FastAPI API, which enforces authorization (RLS, roles). Verification had been deferred until after the dashboard was built — a late, expensive surprise risk.

## Options
1. **Next.js 16 static export (`output: 'export'`) on S3 + CloudFront** — no SSR compute, no version lag, cheapest; Clerk runs client-side (no Next.js middleware in static exports); route guards are UX only.
2. Pin Next.js 15 on Amplify SSR — supported today, but ties the dashboard to Amplify's release cadence and adds SSR compute for no functional gain.
3. OpenNext/SST for full SSR on Lambda — more infrastructure to own.

## Decision
Option 1. The first M7 task is a deployed, authenticated skeleton (static export on S3 + CloudFront, Clerk sign-in, one call through the generated API client), proving hosting and auth before any screen is built.

## Consequences
- Server-only Next.js features (middleware, server actions, SSR) are unavailable — acceptable because the API is the security boundary.
- CloudFront needs SPA-style routing rules (fallback to the exported route files) and security headers (CSP, HSTS) configured in Terraform.
- If a future feature genuinely needs SSR, revisit with option 2 or 3 in a new ADR.
