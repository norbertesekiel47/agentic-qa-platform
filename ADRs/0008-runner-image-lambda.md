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

## Amendment (2026-09-29): the spike tests three candidates against one predicate

**New facts, checked 2026-09-29:**
- **Lambda functions:** function code can't create user namespaces (aws/containers-roadmap#2102, open), so Chromium's sandbox fails there.
- **Fargate:** a task may add only `SYS_PTRACE` (ECS `KernelCapabilities`), with no privileged mode and no custom seccomp profile (aws/containers-roadmap#1957, open). The documented fallback most likely fails too.
- **Lambda MicroVMs** reached general availability on 2026-06-22. Each session gets its own Firecracker VM with its own kernel and full operating-system capabilities. Starts come from snapshots, and a session can last up to 8 hours.

**Predicate.** Hosted compute qualifies only if both hold:
1. Chromium runs with its sandbox on. The launch succeeds, and compared with the browser process, a renderer shows its own namespaces and seccomp filtering.
2. Every run gets a fresh VM.

What that means per candidate:
- **Lambda function:** can't win even if its sandbox works, because it reuses execution environments across invocations. It is measured only for the record.
- **ECS on EC2:** qualifies only with a fresh instance per run.
- **No candidate qualifies:** hosted runs wait.

**Candidates.** A Lambda MicroVM, a Fargate task and a Lambda function all run one probe program, packaged separately for each platform. MicroVM images build from a Lambda-managed base image.

**Measurements.**
- Time until the browser is ready, cold and warm.
- Peak memory.
- Cost per 60-second run, including snapshot storage and snapshot reads.

**Logistics.**
- The spike runs first in M1, right after the scaffold.
- Code lives in `spikes/hosted-chromium/`, under our gates.
- AWS CLI scripts only; Terraform arrives in M4.
- Region us-east-1, with a budget alarm set up first and a teardown script.
- No AWS resource is created without the maintainer's go-ahead.
- The outcome, with its commands and SHA, goes in a new ADR that supersedes this ADR's choice of Lambda.

**If MicroVMs win.** A run can last up to 8 hours in one VM, so the 15-minute cap no longer forces continuation. M6 decides what continuation keeps (#28).

**Unchanged.** One runner image in two locations, and runners holding no data-plane credentials.
