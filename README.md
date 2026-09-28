# Agentic QA Platform

**Natural-language E2E tests that run as deterministic replays and self-heal on UI drift. CLI, GitHub App, and multi-tenant SaaS.**

> **Status: design complete, pre-build.** This README is the public front page; sections marked *(planned)* will be filled with measured results as milestones land. No number appears here without the command and commit that produced it.

## Why

E2E suites break when the UI changes even though nothing is broken, and "AI testers" that call an LLM on every run are slow, costly, and non-deterministic. This platform uses the model only where judgment is needed:

1. **Explore once** — an agent reads a plain-English spec and drives a real browser.
2. **Compile** — the successful path becomes a deterministic script (robust locators + deterministic assertions, including visual checks like "the Pay button is visible and not covered").
3. **Replay for $0** — strict-mode runs make zero LLM calls.
4. **Heal on drift** — when a step breaks, the agent determines whether the UI merely moved (every expectation still holds → a locator update you accept on the PR) or an expectation is violated (a bug report with evidence). Heals never change what "correct" means.

## Headline results *(planned — frozen test split: 40 planted bugs, 14 benign UI changes)*

| Metric | v1 target | Measured |
|---|---|---|
| Planted-bug detection rate | ≥ 85% | — |
| False-positive rate | ≤ 5% | — |
| Heal classification accuracy (UI drift vs violation) | ≥ 90% | — |
| Flake rate (30 specs × 20 replays) | < 1% of replays observed (reported as pooled rate + per-spec incidence) | — |
| LLM cost per strict replay | $0 | — |
| Median LLM cost per heal | < $0.05 | — |
| 10-step replay (local / hosted) | < 30 s | — |

Results will be reported per bug category with confidence intervals (method and sidedness stated), including misses. Tuning happens only on a separate dev split.

## How it works

```
spec.md ──explore (LLM)──► compiled.json ──strict replay (no LLM)──► pass
                                   │
                                   └─ step drifts ──► heal (LLM) ──► UI moved, expectations hold → [Accept heal] on PR
                                                                  └─► expectation violated → finding + replay evidence
```

- **Hybrid perception:** accessibility tree for precise actions, screenshots for visual verification, vision fallback for canvas/iframes.
- **Specs live in your repo** (`qa/*.spec.md`), versioned with the code they test; heals arrive as locator-only PR commits you approve.
- **Model-agnostic:** role-based routing (navigator / verifier / healer / vision fallback) across OpenRouter, OpenAI, Anthropic, DeepSeek, and more — bring your own keys.
- **Runs anywhere:** the same runner image executes in your GitHub Actions (zero stored secrets via OIDC) or on AWS-hosted runners from the dashboard, with a sandboxed browser, per-run isolation, and connection-level egress restricted to verified domains.

## Architecture

Python (FastAPI, LangGraph, Playwright) · Next.js dashboard (static, on S3 + CloudFront) · AWS (Lambda runners outside the VPC, RDS Postgres with row-level security, S3, SQS, KMS) · Clerk Organizations. Details: [ARCHITECTURE.md](ARCHITECTURE.md).

## Releases *(planned)*

- **0.1 — CLI + benchmark:** open-source CLI, frozen benchmark, local replay viewer, measured numbers.
- **0.2 — CI-native:** GitHub Action + GitHub App with Accept heal.
- **1.0 — SaaS:** hosted runs, dashboard, multi-tenant orgs.

## Quickstart *(planned)*

```bash
uvx agentic-qa init
aqa explore qa/checkout.spec.md --url http://localhost:3000
aqa run --url http://localhost:3000
aqa report
```

## Documentation

[Vision](VISION.md) · [PRD](PRD.md) · [Architecture](ARCHITECTURE.md) · [Tech stack](TECH_STACK.md) · [Data model](DATA_MODEL.md) · [API](API.md) · [UX spec](UX_SPEC.md) · [Testing & benchmark](TESTING.md) · [Security](SECURITY.md) · [Roadmap](ROADMAP.md) · [Decisions (ADRs)](ADRs/) · [For coding agents](AGENTS.md)

## Part of a three-project portfolio

- **Agentic QA Platform** — builds agents and generates trajectories *(this repo)*
- **Calibrated Eval Toolkit** — measures agents with human-calibrated judges and bias-corrected confidence intervals
- **MCP Defense Gateway** — blocks MCP attacks, measured on a published attack benchmark

## License

Apache-2.0; see [LICENSE](LICENSE) (ADR-0016).
