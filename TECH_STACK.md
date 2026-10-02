# Tech Stack — Agentic QA Platform

Versions are the latest stable releases as of **2026-09-27** (checked on PyPI/npm), except the tracked ones below. Pin exact versions in lockfiles at scaffold time; bump deliberately.

A version followed by an HTML comment, `renovate: NAME`, is tracked: it is the exact pin of the Python package NAME in a `pyproject.toml`, and the weekly Renovate pull request bumps both together (ADR-0031). `tests/test_dependency_updates.py` fails when a pin and its tracked version differ. A version without the comment is the pick as of the date above, until a ticket adopts it.

## 1. Backend, runner, CLI (Python)

| Concern | Choice | Version | Why |
|---|---|---|---|
| Language | Python | 3.14 | Matches existing tooling; flagship in the language most AI-engineer postings require (ADR-0005) |
| Package/env | uv, and its build backend `uv_build` | 0.11.x | Fast, lockfile-based; one workspace (ADR-0027) |
| Browser automation | Playwright for Python | 1.63.0<!-- renovate: playwright --> | Accessibility snapshots, tracing, robust locators |
| Egress proxy framing | h11 | 0.16.0<!-- renovate: h11 --> | HTTP/1.1 parsing for the in-runner egress proxy, so each request is read and checked whole and each response is framed correctly, and for runner-side requests; already locked through httpcore (ADR-0026 amendment, 2026-10-01) |
| Agent orchestration | LangGraph | 1.2.12<!-- renovate: langgraph --> | Run graph and checkpointing (interrupts deferred past v1; ADR-0006) |
| Checkpointer | Local runs (M1): LangGraph's in-memory saver. Uploaded and hosted runs (M4 onward): a custom `BaseCheckpointSaver` over our API | — | Runners have no DB access (ADR-0008) |
| Chat-model layer | LangChain provider packages: `langchain-openai` 1.6.6<!-- renovate: langchain-openai --> (OpenAI + OpenRouter via OpenAI-compatible base URL), `langchain-anthropic` 1.7.4<!-- renovate: langchain-anthropic -->, `langchain-deepseek` 1.1.1<!-- renovate: langchain-deepseek -->; the Anthropic SDK under langchain-anthropic, `anthropic` 1.9.0<!-- renovate: anthropic -->, pinned because the adapter catches its errors (#41) | — | Native tool binding and structured output in LangGraph (ADR-0007) |
| Cost and capability table | A vendored copy of LiteLLM's `model_prices_and_context_window.json`, pinned to an upstream commit and its sha256 | refreshed by script | Per-call cost and capability flags for many providers without hand-maintained prices. We don't install the `litellm` package, whose PyPI releases were compromised in March 2026 (ADR-0007 amendment) |
| API framework | FastAPI | 0.141.1 | OpenAPI generation → typed TS client |
| Lambda adapter | Mangum | 0.22.0 | ASGI on Lambda behind API Gateway |
| Validation | Pydantic | 2.13.5<!-- renovate: pydantic --> | Typed tools, verdicts, API models |
| YAML | PyYAML, narrowed to YAML 1.2's core schema | 6.0.3<!-- renovate: pyyaml --> | Spec frontmatter and the project config, with duplicate keys, aliases and tags refused; already locked through LangChain (ADR-0030) |
| Time zone data | `tzdata`, the IANA database as a PyPI package | 2026.4<!-- renovate: tzdata --> | The one list `browser.timezone` is checked against: the same answer on every machine, where the host's own list differs by OS (ADR-0025 amendment, 2026-10-02) |
| DB access | SQLAlchemy 2.1.1 + psycopg 3.3.6 | — | Async, typed; raw SQL for RLS policies |
| Migrations | Alembic | 1.20.0 | Versioned schema, RLS policies in migrations |
| CLI | Typer 0.27.2<!-- renovate: typer --> + Rich 15.0.0<!-- renovate: rich --> | — | Ergonomic CLI and terminal output |
| Tracing | OpenTelemetry SDK 1.45.0 + LangSmith 0.14.1<!-- renovate: langsmith --> | — | Portable traces; LangSmith for LLM debugging. The runner imports `langsmith` itself, to switch export off (SECURITY §10), so it is pinned |
| Logging | structlog | 26.1.0<!-- renovate: structlog --> | JSON logs with redaction processors |
| JWT verification | PyJWT | 2.15.0 | Clerk JWKS + run tokens |
| AWS SDK | boto3 | 1.43.x | KMS, SQS, S3, Lambda invoke |
| M1 spike images | Base `public.ecr.aws/lambda/microvms:al2023-minimal` (pinned by digest) for every candidate; awslambdaric runs the Lambda function's | awslambdaric 4.1.0<!-- renovate: awslambdaric --> | One Amazon Linux 2023 image for all three candidates, so they differ only in their platform (ADR-0008 amendment, 2026-09-30) |

**Quality tooling:** Ruff 0.16.9<!-- renovate: ruff -->, mypy 2.3.1<!-- renovate: mypy --> (strict), pytest 9.1.1<!-- renovate: pytest -->, pytest-cov 7.1.0<!-- renovate: pytest-cov --> with coverage 7.16.2<!-- renovate: coverage -->, testcontainers 4.15.0, Hypothesis 6.168.2<!-- renovate: hypothesis -->, VCR.py 8.3.0<!-- renovate: vcrpy --> (recorded LLM/HTTP fixtures), types-pyyaml 6.0.12.20260906<!-- renovate: types-pyyaml --> (PyYAML's stubs, for mypy), respx 0.23.1<!-- renovate: respx -->. CI installs uv 0.11.15 and runs osv-scanner 2.6.0, the dependency audit, as sha256-checked release binaries (ADR-0029).

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
| `navigator` | High while exploring, but once per spec version | tools, structured output | Claude Sonnet 5.5 (`claude-sonnet-5-5`) | $2 / $10 |
| `verifier` | Medium (visual assertions) | vision, structured output | Claude Sonnet 5.5 (`claude-sonnet-5-5`) | $2 / $10 |
| `healer` | Low (on drift) | tools, vision, structured output | Claude Sonnet 5.5 (`claude-sonnet-5-5`) | $2 / $10 |
| `vision_fallback` | Rare | vision, tools (coordinate actions are tool calls) | Claude Sonnet 5.5 (`claude-sonnet-5-5`) | $2 / $10 |

\*Anthropic first-party list prices from the Anthropic API reference (cached 2026-09-25). **Re-verify before publishing any cost figure**; cost accounting reads prices from the pinned price map, not from this table.

**Why these defaults (ADR-0007 amendment, 2026-09-29):**
- *Navigator:* moved up from Haiku 4.5 ($1 / $5, 200K context). Exploring runs once per spec version, and every later result depends on the compiled script's quality.
- *Other roles:* moved from Sonnet 5 to Sonnet 5.5 at the same price.
- *Benchmark:* M3's per-role ablation tests Haiku 4.5 and Opus 5.5 on the dev split.
- *Tool choice:* requests never force a tool choice. Sonnet 5.5 and Opus 5.5 reject it with a 400.

Example non-Claude configs to include in docs: OpenRouter (any model, one key), DeepSeek (navigator), OpenAI models including Codex-family models (healer). The benchmark publishes a per-role model comparison table.

Notes:
- Anthropic's own computer-use and browser tools are **not** used as the action layer; our tools are provider-neutral. (For reference: Claude Opus 5.5 computer use requires `computer_toolset_20260801`.)
- Capability validation happens at config load: e.g., assigning a text-only model to `vision_fallback` fails fast with a clear error. Config load reports every role problem at once, and holds a `fallback` model to the same needs (DATA_MODEL §9). Capabilities come from the pinned price map's `supports_function_calling`, `supports_response_schema` and `supports_vision`, or from the config's `models` entry (ADR-0007 amendment, 2026-10-01).

## 4. Infrastructure

| Concern | Choice |
|---|---|
| Cloud | AWS (ADR-0008, ADR-0009) |
| IaC | Terraform |
| Container registry | Amazon ECR |
| Runner compute | **Decided by the M1 spike (ADR-0008 amendment, 2026-09-29).** Hosted compute must run sandboxed Chromium and give every run a fresh VM. Candidates:<ul><li>**Lambda MicroVMs** (GA 2026-06-22): a Firecracker VM and kernel per session, snapshot starts, sessions up to 8 h. ARM compute costs $0.0000276944 per vCPU-second plus $0.0000036667 per GB-second, plus snapshot storage and reads.</li><li>**Fargate:** one task per run. Only `SYS_PTRACE` can be added and custom seccomp is unavailable, so the sandbox most likely fails.</li><li>**Lambda functions:** measured only, since they reuse environments.</li><li>**ECS on EC2:** qualifies only with a fresh instance per run.</li></ul> |
| Browser egress | In-runner forward proxy (allowlist, validated-IP connects pinned per run, IP policy by run location, redirect, WebSocket and UDP coverage) + Playwright routing + allowed-origin checks before each observation and action (ADR-0026) |
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
| Control-plane Lambdas (API, dispatcher, realtime) | $0 within the always-free tier (1M requests + 400,000 GB-s/month) |
| **Total (base infrastructure)** | **≈ $20–30** |
| Hosted-runner compute | Pending the M1 spike. MicroVM compute is about $0.004 per 60-second run at 2 vCPU / 4 GB, plus snapshot storage and reads |

Free-tier facts (verified 2026-09-27): accounts created on/after 2025-07-15 receive $100 in credits at signup plus up to $100 more for guided activities (one is setting up an AWS Budget); the Free Plan lasts 6 months or until credits run out. RDS has **no** separate free allowance for these accounts — it draws from credits. Lambda's 1M requests + 400,000 GB-seconds per month is always free. Sources: [AWS announcement](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-free-tier-credits-month-free-plan/), [Lambda pricing](https://aws.amazon.com/lambda/pricing/).

Runner budget math if the spike picked Lambda functions: a 3,008 MB runner for 60 s ≈ 176 GB-s, so ~2,270 runs/month fit in the always-free compute allowance.

The 2026-09-29 rule excludes Lambda functions, though. If Lambda MicroVMs win, compute has no free tier:
- 2 vCPU and 4 GB for 60 s costs about $0.004 at the list prices above, before snapshot storage and reads.
- The spike measures the real cost per run (ADR-0008 amendment).

## 6. Alternatives considered

| Area | Rejected | Reason |
|---|---|---|
| Language | TypeScript end-to-end | Less signal for Python-heavy AI postings; team fluency (ADR-0005) |
| Agent framework | Pydantic AI, custom loop | LangGraph chosen for checkpointing/interrupts and market signal (ADR-0006) |
| Runners | Fargate Spot, EC2 Spot | Slower start / idle cost (ADR-0008). Reopened by the M1 spike, which tests hosted candidates against the fresh-VM rule (ADR-0008 amendment, 2026-09-29) |
| DB | Aurora Serverless v2, DynamoDB | ~15 s resume on demo; no relational model/RLS (ADR-0009) |
| Auth | Cognito, WorkOS | No org primitives / preference for Clerk DX (ADR-0010) |
| Tracing | Langfuse, AWS-only | LangSmith pairs with LangGraph (ADR-0014) |
| Price map | The `litellm` package | A large dependency tree in a runner that holds provider keys, for one data file. Its PyPI releases 1.82.7 and 1.82.8 were malicious (2026-03-24) (ADR-0007 amendment) |

## 7. Version policy

- Lockfiles committed (`uv.lock`, `pnpm-lock.yaml`); Renovate weekly (ADR-0031): one pull request for minor and patch updates, one for each major, each pre-1.0 minor and Playwright, one for lock-file maintenance, and none for a bumped dependency's release under 3 days old. `pnpm-lock.yaml` joins at M7.
- Majors are upgraded in dedicated PRs with the full test + benchmark smoke gate.
- OpenTelemetry GenAI semantic conventions are still marked *Development*; pin the convention version and note it in traces.
- The vendored price map is refreshed only by its script, in a reviewed pull request that records the new upstream commit and sha256. Cost records cite the version they used. The script is `uv run python -m aqa_core.price_map_refresh [ref]` (default `main`): it needs network access, rewrites `packages/core/src/aqa_core/price_map_data/`, and loading the map afterwards checks the new pair (ADR-0007 amendment, 2026-10-01). A reviewer of a refresh pull request checks the pin against upstream: `curl -s https://raw.githubusercontent.com/BerriAI/litellm/<commit>/model_prices_and_context_window.json | shasum -a 256` must print the `sha256` in `pin.json`.
