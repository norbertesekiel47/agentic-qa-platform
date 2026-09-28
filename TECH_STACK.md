# Tech Stack — Agentic QA Platform

Versions are the latest stable releases as of **2026-09-27** (checked on PyPI/npm). Pin exact versions in lockfiles at scaffold time; bump deliberately.

## 1. Backend, runner, CLI (Python)

| Concern | Choice | Version | Why |
|---|---|---|---|
| Language | Python | 3.14 | Matches existing tooling; flagship in the language most AI-engineer postings require (ADR-0005) |
| Package/env | uv | 0.11.x | Fast, lockfile-based |
| Browser automation | Playwright for Python | 1.63.0 | Accessibility snapshots, tracing, robust locators |
| Agent orchestration | LangGraph | 1.2.12 | Run graph and checkpointing (interrupts deferred past v1; ADR-0006) |
| Checkpointer | Custom `BaseCheckpointSaver` over our API | — | Runners have no DB access (ADR-0008) |
| Chat-model layer | LangChain provider packages: `langchain-openai` 1.6.6 (OpenAI + OpenRouter via OpenAI-compatible base URL), `langchain-anthropic` 1.7.4, `langchain-deepseek` 1.1.1 | — | Native tool binding and structured output in LangGraph (ADR-0007) |
| Cost table | LiteLLM model price map | 1.103.0 | Per-call cost for many providers without hand-maintained prices |
| API framework | FastAPI | 0.141.1 | OpenAPI generation → typed TS client |
| Lambda adapter | Mangum | 0.22.0 | ASGI on Lambda behind API Gateway |
| Validation | Pydantic | 2.13.5 | Typed tools, verdicts, API models |
| DB access | SQLAlchemy 2.1.1 + psycopg 3.3.6 | — | Async, typed; raw SQL for RLS policies |
| Migrations | Alembic | 1.20.0 | Versioned schema, RLS policies in migrations |
| CLI | Typer 0.27.2 + Rich 15.0.0 | — | Ergonomic CLI and terminal output |
| Tracing | OpenTelemetry SDK 1.45.0 + LangSmith 0.14.1 | — | Portable traces; LangSmith for LLM debugging |
| Logging | structlog | 26.1.0 | JSON logs with redaction processors |
| JWT verification | PyJWT | 2.15.0 | Clerk JWKS + run tokens |
| AWS SDK | boto3 | 1.43.x | KMS, SQS, S3, Lambda invoke |

**Quality tooling:** Ruff 0.16.9, mypy 2.3.1 (strict), pytest 9.1.1, testcontainers 4.15.0, Hypothesis 6.168.2, VCR.py 8.3.0 (recorded LLM/HTTP fixtures), respx 0.23.1.

## 2. Dashboard (TypeScript)

| Concern | Choice | Version |
|---|---|---|
| Framework | Next.js (App Router) | 16.3.6 |
| UI runtime | React | 19.3.0 |
| Language | TypeScript (`strict`) | 7.0.2 |
| Auth | `@clerk/nextjs` | 7.9.7 |
| API client | Generated from FastAPI's OpenAPI via `@hey-api/openapi-ts` | 0.99.0 |
| Data fetching | TanStack Query | 5.104.0 |
| Styling | Tailwind CSS | 4.3.3 |
| Runtime validation | zod | 4.6.5 |
| Unit tests | Vitest | 5.0.2 |
| E2E + visual | Playwright Test | 1.63.0 |
| Codebase intelligence (dead code, duplication, complexity) | fallow — gates agent commits via `.claude/hooks/fallow-gate.sh` (ADR-0018) | 3.30.0 |
| Package manager | pnpm | 11.x |

Node 24 LTS. **Hosting: Next.js static export (`output: 'export'`) on S3 + CloudFront** (ADR-0017). As of 2026-09-27, AWS Amplify Hosting's SSR support lists Next.js 12–15 only; the dashboard needs no server rendering (the FastAPI API enforces all authorization), so a static, client-rendered app avoids the version lag and the SSR compute cost. Clerk runs client-side (no Next.js middleware in static export); proven by the M7 skeleton task.

## 3. Model routing defaults

Model-agnostic by design (ADR-0007): any provider can fill any role that its model's capabilities satisfy. Defaults ship configured for Claude; every role is overridable per org in config.

| Role | Frequency | Needs | Default model (ID) | Price in/out per 1M tokens* |
|---|---|---|---|---|
| `navigator` | High (explore only) | tools, structured output | Claude Haiku 4.5 (`claude-haiku-4-5`) | $1 / $5 |
| `verifier` | Medium (visual assertions) | vision, structured output | Claude Sonnet 5 (`claude-sonnet-5`) | $2 / $10 |
| `healer` | Low (on drift) | tools, vision, structured output | Claude Sonnet 5 (`claude-sonnet-5`) | $2 / $10 |
| `vision_fallback` | Rare | vision, coordinate actions | Claude Sonnet 5 (`claude-sonnet-5`) | $2 / $10 |

\*Anthropic first-party list prices from the Anthropic API reference (cached 2026-06-24). **Re-verify before publishing any cost figure**; cost accounting reads live prices from the LiteLLM price map at runtime, not from this table.

Example non-Claude configs to include in docs: OpenRouter (any model, one key), DeepSeek (navigator), OpenAI models including Codex-family models (healer). The benchmark publishes a per-role model comparison table.

Notes:
- Anthropic's own computer-use and browser tools are **not** used as the action layer; our tools are provider-neutral. (For reference: Claude Opus 5.5 computer use requires `computer_toolset_20260801`.)
- Capability validation happens at config load: e.g., assigning a text-only model to `vision_fallback` fails fast with a clear error.

## 4. Infrastructure

| Concern | Choice |
|---|---|
| Cloud | AWS (ADR-0008, ADR-0009) |
| IaC | Terraform |
| Container registry | Amazon ECR |
| Runner compute | **Decided by the M1 sandbox gate:** Lambda container images (≤10 GB image, ≤15 min, up to 10,240 MB memory, outside VPC) if Chromium's sandbox runs there; otherwise ECS Fargate (Spot) one task per run — still pay-per-use, ~30–60 s startup |
| Browser egress | In-runner forward proxy (allowlist, validated-IP connects, redirect and WebSocket coverage) + Playwright routing |
| API | Lambda + API Gateway HTTP API; WebSocket API for live runs |
| Queue | SQS (standard) + dispatcher Lambda |
| Database | RDS PostgreSQL, db.t4g.micro, single-AZ, gp3 20 GB |
| NAT | fck-nat on t4g.nano (no managed NAT Gateway) |
| Object storage | S3 + gateway VPC endpoint; lifecycle expiry |
| Dashboard hosting | S3 + CloudFront (Next.js static export; ADR-0017) |
| Keys | KMS customer-managed key (BYOK envelope encryption) |
| Identity | Clerk (external) |
| CI for this repo | GitHub Actions on GitHub-hosted runners; runner label read from repo variable `CI_RUNNER`, so moving to Blacksmith once the repo is in an organization needs no workflow edit (ADR-0019). OIDC to AWS for deploys — no long-lived AWS keys |
| Observability | CloudWatch (logs/metrics/alarms), LangSmith (non-customer traces) |

## 5. AWS services and cost

Portfolio-traffic estimate (us-east-1, approximate — validate in the AWS Pricing Calculator before launch):

| Item | ~Monthly |
|---|---|
| RDS db.t4g.micro single-AZ + 20 GB gp3 | $13–15 |
| fck-nat t4g.nano + public IPv4 | $6–7 |
| KMS customer-managed key | $1 |
| Route 53 hosted zone | $0.50 |
| CloudFront + S3 (dashboard, artifacts), SQS, API Gateway, CloudWatch | $1–5 |
| Lambda | $0 within the always-free tier (1M requests + 400,000 GB-s/month) |
| **Total** | **≈ $20–30** |

Free-tier facts (verified 2026-09-27): accounts created on/after 2025-07-15 receive $100 in credits at signup plus up to $100 more for guided activities (one is setting up an AWS Budget); the Free Plan lasts 6 months or until credits run out. RDS has **no** separate free allowance for these accounts — it draws from credits. Lambda's 1M requests + 400,000 GB-seconds per month is always free. Sources: [AWS announcement](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-free-tier-credits-month-free-plan/), [Lambda pricing](https://aws.amazon.com/lambda/pricing/).

Runner budget math: a 3,008 MB runner for 60 s ≈ 176 GB-s → ~2,270 runs/month within the always-free compute allowance.

## 6. Alternatives considered

| Area | Rejected | Reason |
|---|---|---|
| Language | TypeScript end-to-end | Less signal for Python-heavy AI postings; team fluency (ADR-0005) |
| Agent framework | Pydantic AI, custom loop | LangGraph chosen for checkpointing/interrupts and market signal (ADR-0006) |
| Runners | Fargate Spot, EC2 Spot | Slower start / idle cost (ADR-0008) |
| DB | Aurora Serverless v2, DynamoDB | ~15 s resume on demo; no relational model/RLS (ADR-0009) |
| Auth | Cognito, WorkOS | No org primitives / preference for Clerk DX (ADR-0010) |
| Tracing | Langfuse, AWS-only | LangSmith pairs with LangGraph (ADR-0014) |

## 7. Version policy

- Lockfiles committed (`uv.lock`, `pnpm-lock.yaml`); Dependabot/Renovate weekly.
- Majors are upgraded in dedicated PRs with the full test + benchmark smoke gate.
- OpenTelemetry GenAI semantic conventions are still marked *Development*; pin the convention version and note it in traces.
