# ADR-0012: Domain verification for hosted runs

- Status: Accepted
- Date: 2026-09-27

## Context
Without controls, anyone could point AWS-hosted browser agents at sites they don't own — scraping, credential stuffing, or load generation — with our AWS account taking the blame.

## Options
1. **Domain verification** (DNS TXT or `/.well-known/` file) before hosted runs may target a hostname, plus rate limits and concurrency caps.
2. Rate limits only — throttles, doesn't prevent abuse.
3. Allowlist of preview domains (e.g., `*.vercel.app`) — anyone can deploy anything there.

## Decision
Option 1, the standard approach (as used by search consoles and hosting platforms). Verifying an apex covers subdomains for preview URLs. Re-verified every 7 days. CI runs (customer's machines) are unrestricted. Hosted runners also block navigation to private/link-local IP ranges.

## Consequences
- Onboarding gains a verification step (UX_SPEC §3.1).
- The public demo targets only our benchmark apps.
- Verification tokens are stored hashed; failure pauses hosted runs for that domain.
