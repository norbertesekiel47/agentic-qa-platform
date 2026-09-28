# ADR-0008: One runner image, two locations; AWS Lambda for hosted runs

- Status: Accepted
- Date: 2026-09-27

## Context
Runs happen from customer CI (which can reach private preview deployments and localhost) and from the dashboard (which needs hosted compute). The product must be on AWS and as cheap as possible, with strong isolation between tenants.

## Options
Where runs execute:
1. **One container image, two locations** — customer CI for pipeline runs; our hosted fleet for dashboard runs.
2. Hosted-only — can't reach private previews without building a tunnel.
3. Customer-side only — skips the hardest infrastructure.

Hosted compute on AWS:
1. **Lambda container images** — per-invocation Firecracker microVM, zero idle cost, always-free tier (1M requests + 400,000 GB-s/month), images up to 10 GB; 15-minute cap, cold starts of seconds.
2. ECS Fargate Spot — no time cap; ~30–60 s start; interruptions.
3. EC2 Spot warm pool — cheapest at scale; idle cost and server ops.

## Decision
One image, two locations; hosted runs on **Lambda**. Runners run **outside the VPC**, hold only a run-scoped token, and perform all I/O through the API (including LangGraph checkpoints) and presigned S3 URLs. Long runs continue across invocations via checkpoints (max 3 continuations).

## Consequences
- A compromised runner cannot reach RDS or other tenants' data; runners need no NAT.
- Headless Chromium packaging for Lambda is a known risk — spike in M1; a compute-backend interface keeps a Fargate adapter possible.
- Warm environment reuse requires wiping `/tmp` browser state between runs.
- Cost: ~2,270 runs/month of 60 s at 3,008 MB fit in the always-free compute allowance.

## Amendment — 2026-09-27 (external design review)

"One microVM per run" overstated the boundary. Lambda may **reuse an execution environment** — including `/tmp` and surviving processes — across invocations (potentially different tenants), and a timeout doesn't clear `/tmp`. The isolation guarantee is therefore enforced by the runner:
- startup hygiene on every invocation before any tenant data is fetched (terminate leftover processes, wipe and verify `/tmp`);
- fresh Chromium process and randomized profile per run; service workers blocked; secrets in memory only;
- crash-reuse tests on real Lambda.

Residual risk (a sandbox-escaping browser exploit persisting in a warm environment) is documented in SECURITY §6. A one-task-per-run **Fargate adapter** behind the compute-backend interface is on the roadmap for customers who need a hard per-run boundary.

### Amendment addendum — 2026-09-27 (verification review): sandbox decision gate

Playwright launches Chromium without its sandbox unless `chromium_sandbox=True`, and community evidence indicates the sandbox usually can't start on Lambda ("No usable sandbox!"). Without it, a renderer exploit runs with the runner's privileges, and warm-environment reuse could expose a later tenant's run. **The M1 spike is therefore a decision gate:** if sandboxed Chromium is proven on Lambda, hosted runs stay on Lambda; otherwise hosted multi-tenant runs move to a **Fargate one-task-per-run** adapter (fresh microVM per run, Spot pricing, ~30–60 s startup) and this ADR is superseded by a new one. Cross-tenant hosted execution isn't offered until one of these holds.
