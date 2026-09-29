# Architecture Decision Records

Each ADR captures one decision between real alternatives: the context, the options considered, what we chose, and the consequences. ADRs are immutable once accepted — to change a decision, write a new ADR that supersedes the old one and update the old one's status. Refinements that keep a decision intact are appended as dated **Amendment** sections.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-agentic-qa-product-choice.md) | Build an agentic QA tester, not a general web agent | Accepted |
| [0002](0002-hybrid-perception.md) | Hybrid perception: accessibility tree for actions, vision for verification | Accepted |
| [0003](0003-explore-compile-heal.md) | Explore once, compile, replay without LLM, heal on drift | Accepted (amended 2026-09-27) |
| [0004](0004-specs-in-repo.md) | Structured natural-language specs; repo is the source of truth | Accepted |
| [0005](0005-python-backend-nextjs-dashboard.md) | Python backend + Next.js dashboard with generated client | Accepted |
| [0006](0006-langgraph-run-graph.md) | LangGraph for the run graph with an API-backed checkpointer | Accepted (amended 2026-09-27) |
| [0007](0007-model-agnostic-role-routing.md) | Model-agnostic, role-based model routing | Accepted |
| [0008](0008-runner-image-lambda.md) | One runner image, two locations; Lambda for hosted runs | Accepted (amended 2026-09-27) |
| [0009](0009-rds-postgres-rls.md) | RDS PostgreSQL with row-level security; fck-nat | Accepted |
| [0010](0010-clerk-organizations.md) | Clerk Organizations for identity and tenancy | Accepted |
| [0011](0011-byok-envelope-encryption.md) | BYOK only, with KMS envelope encryption | Accepted |
| [0012](0012-domain-verification.md) | Domain verification for hosted runs | Accepted |
| [0013](0013-ci-auth-oidc-api-keys.md) | GitHub OIDC for Actions, prefixed API keys elsewhere | Accepted (amended 2026-09-27) |
| [0014](0014-observability-langsmith-cloudwatch.md) | LangSmith for non-customer LLM traces, CloudWatch for infra | Accepted |
| [0015](0015-benchmark-design.md) | Benchmark on real OSS apps with planted bugs and benign changes | Accepted (amended 2026-09-27) |
| [0016](0016-build-order-and-license.md) | Core-first build order; Apache-2.0 | Accepted |
| [0017](0017-dashboard-hosting-static-export.md) | Dashboard hosting: Next.js static export on S3 + CloudFront | Accepted |
| [0018](0018-fallow-agent-commit-gate.md) | fallow as the TypeScript codebase-intelligence gate, enforced at agent commit/push | Accepted (amended 2026-09-28) |
| [0019](0019-ci-switchable-runner.md) | CI on GitHub Actions with a switchable runner (Blacksmith-ready) | Accepted |
| [0020](0020-benchmark-apps-conduit-medusa.md) | Benchmark apps: RealWorld Conduit first, Medusa second | Accepted (amended 2026-09-28) |
| [0021](0021-bench-apps-gate-carve-out.md) | Vendored benchmark apps (`bench/apps/`): a narrow gate carve-out | Accepted |
| [0022](0022-bench-flags-test-api-manifest.md) | Benchmark feature flags, test-only endpoints and the ground-truth manifest | Accepted (amended 2026-09-28) |

## Template

```markdown
# ADR-NNNN: <Title>

- Status: Proposed | Accepted | Superseded by ADR-XXXX
- Date: YYYY-MM-DD

## Context
What forces are at play? What problem needs a decision?

## Options
1. **Option A** — pros / cons
2. **Option B** — pros / cons

## Decision
What we chose and the one-paragraph reason.

## Consequences
What becomes easier, harder, or required as a result. Follow-ups.
```
