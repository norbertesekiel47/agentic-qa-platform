# ADR-0013: GitHub OIDC for Actions, prefixed API keys elsewhere

- Status: Accepted
- Date: 2026-09-27

## Context
The CLI and GitHub Action upload results to the SaaS and need authentication.

## Options
1. Project API keys only — familiar; long-lived secrets that can leak.
2. GitHub OIDC only — no stored secrets; works only in GitHub Actions.
3. **OIDC for GitHub Actions, API keys for everything else.**

## Decision
Option 3. The Action requests a GitHub OIDC token (`aud=agentic-qa`), which the API validates (issuer, audience, repository, ref) and exchanges for a ≤15-minute run-scoped token. Elsewhere (local CLI, other CI), project API keys formatted `aqa_live_<prefix>_<secret>`: argon2id-hashed, shown once, revocable, and prefixed so secret scanners can detect leaks.

## Consequences
- In GitHub Actions the customer stores only their LLM provider key.
- API keys need scoping (per project, per scope) and last-used tracking.
- Mirrors current industry practice for CI publishing (e.g., PyPI/npm trusted publishing).

## Amendment — 2026-09-27 (external design review)

OIDC validation is tightened into an explicit per-repository **trust policy**:
- match **immutable** `repository_id` and `repository_owner_id` (names are display-only);
- allowlist `event_name` values; `workflow_ref` (optionally pinned `workflow_sha`) for ordinary workflows, or `job_workflow_ref` only when a reusable workflow is required; and refs;
- enforce **single use per `jti`** (`oidc_exchanges` table) and short expiry;
- record the token's `sha` claim as the **execution** SHA (for `pull_request` events, GitHub's merge commit) and the PR **head** SHA separately; heal staleness checks compare the head SHA.

Fork pull requests don't receive OIDC tokens and therefore can't upload; the Action runs locally and reports in the job log. Upload authority never implies heal-commit authority — heal commits require a human's Checks requested action verified against their repository permission.
- CI run tokens can't retrieve org-stored provider keys or test secrets; only dispatcher-issued hosted-execution tokens (`secrets:read`) can.
