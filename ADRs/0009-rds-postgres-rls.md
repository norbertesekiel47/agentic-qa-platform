# ADR-0009: RDS PostgreSQL with row-level security; fck-nat for egress

- Status: Accepted
- Date: 2026-09-27

## Context
The system of record is relational (orgs → projects → specs → runs → steps → verdicts) and multi-tenant. Budget is minimal. New AWS accounts (post 2025-07-15) get credits, not a free RDS allowance.

## Options
1. **RDS PostgreSQL db.t4g.micro, single-AZ** — ~$13–15/month, always warm, full Postgres (RLS, pgvector later).
2. Aurora Serverless v2 with scale-to-zero — near-$0 idle, but ~15 s resume on first request (bad demo impression) and pricier when active.
3. DynamoDB — $0 idle, serverless; loses relational modeling and database-enforced tenant isolation.

## Decision
Option 1. Tenant isolation is enforced by forced RLS policies keyed on `current_setting('app.org_id')`, set per transaction from the verified token; the app role is not the table owner and has no `BYPASSRLS`; unset context fails closed.

The API Lambda runs in the VPC; its outbound traffic (Clerk JWKS, GitHub, KMS, SQS) uses a **fck-nat** t4g.nano instance (~$3/month + public IPv4) instead of a managed NAT Gateway (~$32/month + data). S3 uses a free gateway endpoint.

## Consequences
- No RDS Proxy (cost): API Lambda concurrency is capped and connections are reused per container.
- fck-nat is a single instance — acceptable availability trade-off for a portfolio; documented upgrade path to managed NAT.
- Mandatory isolation tests for every tenant table (AGENTS.md rule 3).
