# ADR-0016: Core-first build order; Apache-2.0 license

- Status: Accepted
- Date: 2026-09-27

## Context
The scope is large (agent, benchmark, SaaS, GitHub App, hosted runners, dashboard). The agent's quality — especially heal classification — is the main unknown. Separately, recruiters can only judge public code, while the product could one day be a real SaaS.

## Options
Build order:
1. **Core first, then SaaS** — benchmark → local explore/compile → replay/heal → first numbers → SaaS foundation → GitHub App → hosted runners → dashboard → hardening.
2. Walking skeleton — thin end-to-end slice through every layer first, then deepen.

License:
1. **Fully public, Apache-2.0** — maximum visibility; patent grant; norm for developer infrastructure.
2. AGPL-3.0 — protects a hosted service; some companies ban it.
3. Open core — SaaS layer private or source-available; hides the most impressive parts.

## Decision
Core first (so a heal-design pivot happens early, not after the dashboard exists) and Apache-2.0 for the entire repository including the SaaS layer. The author retains copyright and can relicense future SaaS-only code if it becomes a business.

## Consequences
- Real benchmark numbers exist by M3, before any platform work.
- Integration risk is deferred to M4+; mitigated by the Lambda/Chromium spike in M1.
- Anyone may host a competing service; accepted for a portfolio project.
