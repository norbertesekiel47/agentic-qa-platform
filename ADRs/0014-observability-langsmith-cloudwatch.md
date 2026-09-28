# ADR-0014: LangSmith for non-customer LLM traces, CloudWatch for infrastructure

- Status: Accepted
- Date: 2026-09-27

## Context
Separate from the product's replay feature (customer-facing, stored in our S3/Postgres), the team needs internal observability: agent debugging, LLM cost/latency, and infrastructure health. The product is multi-tenant, so sending customer data to third parties is a privacy concern.

## Options
1. **LangSmith** — built by the LangGraph team, graph-aware traces, free developer tier, market signal.
2. Langfuse Cloud — open source, OTel-native, familiar.
3. AWS-native only (CloudWatch/X-Ray) — cheapest; weak for inspecting prompts and tool calls.

## Decision
LangSmith for LLM/agent traces **from benchmark, demo, and development runs only**; CloudWatch for Lambda errors, API latency, queue depth, and alarms. All code is instrumented with OpenTelemetry (GenAI semantic conventions, pinned version), so exporters are configuration.

**Privacy rule:** customer production runs are never exported to LangSmith by default.

## Consequences
- Tracing export is gated by run origin (benchmark/demo/dev) in the runner config.
- OTel traces double as input for the Calibrated Eval Toolkit.
- If the free tier is exceeded, sample benchmark traces rather than disabling instrumentation.
