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

## Amendment (2026-09-30): how the spike's trial measures (#37)

The program every candidate runs is the **trial**, since CONTEXT.md already gives "probe" another meaning. Its code, its packages and its commands are in `spikes/hosted-chromium/` (README). The spike's code is under the gates: mypy, pytest and coverage include `spikes/`.

- **The predicate's first half.** The trial launches Chromium through `aqa_runner.sandbox.launch`, so the sandbox check (ADR-0026) is what proves the sandbox. A sandbox that can't start, or that the check can't prove, is reported with its reason, never raised: it is the finding the spike looks for.
- **Fresh-VM evidence.** Each trial reports:
  - a random run ID;
  - the run IDs a marker file in `/tmp` already held;
  - the kernel's boot ID.

  An environment used again shows earlier run IDs. Whether the boot ID separates VMs on Fargate and on Lambda functions is for #38 to observe. It can't separate MicroVMs: every MicroVM restored from one snapshot reads the same boot ID (Firecracker, `docs/snapshotting/random-for-clones.md`). So a run's evidence is the marker plus the platform's own ID for it: the log stream, task ARN or MicroVM ID.
- **Time to a ready browser.** Inside the trial: from asking Playwright to launch until the sandbox check passes, with nothing else running in the trial. End to end: from the scripts' request until that moment (`ready_at`, by the candidate's clock). Sampling memory reads every Chromium process's memory map, which takes a real share of a small candidate's CPU, so a launch sampled while it runs reads slower than it is (LAB_NOTES, 2026-09-30). The platform's start latency comes on top.
- **Peak memory.** The summed PSS of the trial's process and its descendants, sampled every 50 ms for 1 s after the launch, with a blank page open. PSS counts memory that Chromium's processes share once, so the sum is comparable across candidates. The page is blank, so the peak is a lower bound for a real run.
- **One base for all three packages:** the container base Lambda publishes for MicroVMs, `public.ecr.aws/lambda/microvms:al2023-minimal` (Amazon Linux 2023, arm64 only). Lambda MicroVMs run on arm64 only, so every candidate is measured on arm64.
  - The alternative, Playwright's Ubuntu image for Fargate and the Lambda function, would measure two operating systems.
  - The Lambda function runs the shared image through awslambdaric, the runtime interface client, rather than on Lambda's Python base image, so that it measures the same image as the other two.
- **MicroVMs launch Chromium after the restore.** Lambda snapshots a MicroVM image with every running process once its `/ready` hook answers, and restores that snapshot into each MicroVM. So the trial's server starts no browser before a request: a browser in the snapshot would be one browser, its memory and random state included, in every MicroVM. Its lifecycle hooks only answer 200. The image keeps the default OS capabilities; `["ALL"]` isn't measured. The image's Python, from uv, links its own OpenSSL rather than the base image's snapshot-safe build; the trial makes no TLS call from the snapshotted process, but a runner that did would need the snapshot-safe one.
- **Cold and warm.**
  - *Lambda:* a cold start is the first invocation after a configuration change; a warm one is the next invocation, which may reuse that environment.
  - *Fargate:* every task starts cold.
  - *MicroVM:* the first MicroVM after an image build is cold. Later ones are warm: each is still a new VM from the same snapshot, which Lambda may have cached by then.
- **The scripts** (`spikes/hosted-chromium/scripts/`, AWS CLI only, us-east-1). `deploy.sh` and `invoke.sh` refuse to run until the budget alarm exists. Every resource has a fixed name and the tag `aqa-spike=hosted-chromium`. `teardown.sh` deletes what it lists under those names, budget last, and fails while anything remains. A dry run against fake commands proves both properties, and that no secret reaches a command line.
- **Cost per run** comes from list prices and the measured durations. No MicroVM API reports a snapshot's size, so snapshot storage and reads come from Cost Explorer's billed usage (`collect.sh`). The spike's README has the formulas.
