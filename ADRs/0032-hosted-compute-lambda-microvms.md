# ADR-0032: Hosted compute: one Lambda MicroVM per run

- Status: Accepted
- Date: 2026-10-01

## Context
ADR-0008 put hosted runs on Lambda functions, then made that choice a decision gate (its amendments of 2026-09-27 and 2026-09-29). Hosted compute qualifies only if both hold:
1. Chromium runs with its sandbox on, proven by the sandbox check (ADR-0026).
2. Every run gets a **fresh VM** (CONTEXT.md).

#37 built the trial and its packages (`spikes/hosted-chromium/`, ADR-0008 amendment of 2026-09-30). #38 ran them on AWS on 2026-10-01, and ran the MicroVM's again on 2026-10-09 (*Current-package rerun, 2026-10-09*):
- **Go-ahead.** The maintainer's comment on #38 (2026-10-01T05:47:04Z) came before the first resource, the budget alarm (16:41:57Z).
- **Account and region.** An AWS project account (AWS's "projects" sign-up, Free plan). Its organization denies Lambda, ECS, ECR, EC2 and S3 outside the project's region, us-east-2, so the spike ran there, not in us-east-1 as ADR-0008's amendment planned. us-east-2's list prices equal us-east-1's for every item the cost formulas use (AWS Price List, checked 2026-09-30).
- **Versions.** AWS CLI 2.37.7, on both days. On 2026-10-01 every package was built at `36cf3dc6935c`, for arm64, and `deploy.sh` reported that commit for each candidate; the 2026-10-09 rerun's package was built at `dd05cb4781b1`. Each candidate ran with 2 GB: the MicroVM and the Fargate task with 1 vCPU, the Lambda function with about 1.16. The scripts ran at `36cf3dc` for the MicroVM and at `cf629e8` for Fargate and Lambda; among the scripts, `cf629e8` changes only the Fargate security group's description in `deploy.sh`.
- **Clock.** This machine's offset to `time.aws.com` was +0.0009 ± 0.0745 s before the trials and −0.0019 ± 0.0740 s after them (`sntp`, 2026-10-01). Around the rerun's trials it was −0.0048 ± 0.0692 s before and −0.0016 ± 0.0722 s after (`sntp`, 2026-10-09), so D3's 0.076-second allowance covers both runs.
- **Rows.** `invoke.sh <candidate> cold|warm`, once per row, back to back: the 11 MicroVM trials within 200 seconds on 2026-10-01, and within 314 seconds on 2026-10-09. October 1's 41 rows are in `spikes/hosted-chromium/results/2026-10-01.jsonl`, and the rerun's 11 in `spikes/hosted-chromium/results/2026-10-09.jsonl`. Both are redacted: the account ID masked; the MicroVM endpoint, the role ARN and the Fargate task's network details dropped. The rerun's rows hold the same fields as October 1's MicroVM rows, plus the trial's `sandbox_observed` (#81). No row was dropped.
- **Teardown.** The unchanged `teardown.sh` completed at 2026-10-04T08:57:28Z, exit 0. Independent native listings found no live spike compute, image versions, package stores, security group, custom logs, roles or budget; direct gets for image versions 1.0 and 2.0 returned `ResourceNotFoundException`. At 09:01:20Z the ECS cluster was `INACTIVE`, its task definition was still `DELETE_IN_PROGRESS`, and the tag index retained those two entries; no broader deletion was attempted. The session logged out successfully. The [failed-attempt evidence](../spikes/hosted-chromium/results/2026-10-04-failed-attempt.json) preserves the receipts and their limitations. By 2026-10-09T01:45Z the task definition was gone: `ecs list-task-definitions` listed none, `ACTIVE` or `INACTIVE`, and `describe-task-definition` for revision 1 failed with `ClientException`. The tag index (`resourcegroupstaggingapi get-resources`) still listed the old cluster, which `describe-clusters` no longer showed as `ACTIVE`. After the rerun, `teardown.sh` ran from 2026-10-09T03:37:45Z to 03:39:14Z, exit 0. Independent listings then found no image, live MicroVM, bucket, log group, role, function, repository, `ACTIVE` cluster or security group; image version 1.0 and the budget returned not-found, and the tag index listed only that old cluster. Whether the index ever drops it wasn't checked again.

## Current-package rerun, 2026-10-04

The maintainer approved rebuilding from `8ae62222e62e547cb4c2f3ed90acb6e41df4c03d` after importing main's current sandbox launch protections, then separately approved the image build and the one-cold/ten-warm trial sequence. The trial, Dockerfile and invocation behavior stayed unchanged. Image version 2.0 used `microvm-8ae62222e62e.zip` (457,649 bytes; SHA256 `1ba49e95a710994d144a97296a58a28377cb1d9d885f79e2219e49e6af8b3756`). Both its builds were `SUCCESSFUL` and the version was `ACTIVE`; the resources, hooks, CPU, logging, egress, environment and capabilities matched version 1.0. The accepted base-version input was `1`; saved version metadata reports `1.0`. LAB_NOTES records the rejected input and separately approved correction.

`spikes/hosted-chromium/scripts/invoke.sh microvm cold` at that HEAD began at 08:51:15Z and exited 28 at 08:53:21Z: its single trial POST timed out after 120.007 seconds with zero received bytes. It emitted no trial row. The stop condition prevented all ten warm attempts and any retry. The service reported version 2.0 `TERMINATED`, from 08:51:18.468Z to 08:52:23.726Z (65.258 seconds), with `maximumDurationInSeconds=900` and reason `MicroVM exceeded maximum lifetime.` The recorded idle policy was 60 seconds, zero suspended duration, auto-resume false. Idle termination is a hypothesis, not an established explanation of the stalled request or the service's reason. Two reads of the custom log group returned zero runtime events, and no matching default runtime log group existed. The clocks bracketed the attempt at +0.000187 ± 0.075675 seconds and −0.001894 ± 0.070562 seconds (`sntp time.aws.com`).

The [separate failure artifact](../spikes/hosted-chromium/results/2026-10-04-failed-attempt.json) is an evidence object, not a trial `Report`, and records the commands, source SHA, timestamps, terminal state, cleanup and raw-receipt hashes. It supplies no sandbox, isolation or ready-time measurement. The October 1 rows and figures below remain historical, and no successful rows were fabricated or appended to the old dataset. The 2026-10-09 rerun validated the current launcher on hosted MicroVMs, in rows of its own (*Current-package rerun, 2026-10-09*).

Version 2.0's build metadata spans 06:40:58.108Z–06:43:38.914Z (160.806 seconds). Its memory snapshots were 600,137,728 bytes on Graviton 4 and 584,867,840 bytes on Graviton 3; each build had 31,657,984 disk-snapshot bytes and 1,683,935,232 code-install bytes (`list-microvm-image-builds` and `get-microvm-image-build`, 2026-10-04, same HEAD). These inputs and the package's S3 size were preserved before deletion. The approved image estimate was $0.023–$0.125 and the planned eleven-trial estimate $0.034–$0.117; neither is a bill or the cost of this single failed attempt. The billed usage, collected on 2026-10-17, is under Consequences, **Cost**: version 2.0's storage billed a full week, and this day billed a snapshot write.

## Amendment 2026-10-04: bounded diagnostics before another hosted attempt

**Context and alternatives.** The current-package attempt returned no Report, and its service reason and timing do not identify the failure. Keeping the invocation unchanged would preserve the failed experiment's settings but still leave its missing phase evidence. The alternatives were to add bounded instrumentation and adjust the competing idle window, or have the maintainer choose the issue's hosted-wait fallback. The maintainer selected local instrumentation, including the existing test's idle-value change from 60 to 900; no additional AWS attempt was approved by that choice.

**Decision.** The spike emits fixed-schema, flushed stderr records around handler/body, Playwright startup, guarded launch plus sandbox observation, memory sampling, browser close and response writing. Only a closed `Stage` value and monotonic time enter a record. Health/hooks emit none, other errors retain their existing propagation, and the Report fields are unchanged. The spike README owns the phase vocabulary. Browser cleanup still runs if a close-start diagnostic write fails; no new exception is caught. Logging stays outside the existing ready-time interval and memory-sampling loop.

The MicroVM invocation explicitly names its already scoped runtime log group and sets idle duration to 900 seconds, matching the unchanged maximum lifetime. Suspended duration remains zero, auto-resume remains false, and the one trial POST's timeout remains 120 seconds. The termination trap and no-repeat behavior stand. No IAM, base-image, Dockerfile, dependency, runner-launch or sandbox control changes are part of this amendment.

**Consequences and limits.** These markers can distinguish the last observed handler/startup/launch/memory/close boundary; the launch and sandbox observation remain one guarded call. They do not prove the reason for a stall, client receipt or equivalence between local Docker and an AWS snapshot. Explicit runtime logging is proposed to obtain evidence, not proof that the previous log gap is solved ([MicroVM monitoring](https://docs.aws.amazon.com/lambda/latest/dg/microvms-monitoring.html), checked 2026-10-04). The idle change removes a shorter timer as a competing variable, not an established root cause. The local implementation incurs no AWS/model spend. Any next image must have its own reviewed source/artifact hash and exact build approval, followed by a separate hosted-trial approval with a refreshed cost estimate and teardown scope. The 2026-10-09 rerun, below, ran with these settings and settled current hosted qualification. What the cleanup checks found is under Context, **Teardown**, and billed cost under Consequences, **Cost**.

## Current-package rerun, 2026-10-09

The maintainer approved the rerun's twelve setup writes, the image among them, and then, after seeing the build and a refreshed estimate, the one-cold/ten-warm trial sequence and its teardown; the maintainer typed the trials and the teardown. The package was `microvm-dd05cb4781b1.zip` (465,194 bytes; SHA256 `e332a822b264f8ee31dc5cadb05d8cc5afe2d75ed8dcd4d5912d55c896aaa99a`), built at `dd05cb4781b1`: main `a9a1093`'s launcher, with this ADR's 2026-10-04 diagnostics. The launcher, `packages/runner/src/aqa_runner/sandbox.py`, is unchanged from `dd05cb4` to main at `57a7b25`, and so are `uv.lock` and the Dockerfile (`git diff --stat dd05cb4 57a7b25`, 2026-10-17).

`create-microvm-image` created a new image at 2026-10-09T02:01:05.925Z. Its version 1.0, on base version 1.0, was `SUCCESSFUL` and `ACTIVE` from 02:03:47Z (`get-microvm-image-version`), with two `SUCCESSFUL` builds: Graviton 3, with a 587,501,568-byte memory snapshot, and Graviton 4, with 589,606,912 bytes; each build also has a 31,657,984-byte disk snapshot and a 1,685,532,672-byte code install (`list-microvm-image-builds` and `get-microvm-image-build`, 2026-10-09). `invoke.sh microvm cold`, then `invoke.sh microvm warm` ten times, ran from 03:13:26Z to 03:18:44Z with the 2026-10-04 amendment's settings: a 900-second idle window, no suspension, and runtime logs in the spike's log group (each row's `idlePolicy`).

No trial failed. All 11 `invoke.sh` runs exited 0, and every row qualifies:
- in 11 of 11 the sandbox check proved the sandbox, with one renderer in user, PID and network namespaces of its own and more seccomp filters than the browser (D1, D10);
- every MicroVM was new, and no row lists an earlier run (D2);
- all 11 rows are clock-valid (D3).

Ready in the trial: cold 71.92 s, warm median 5.66 s (0.64–51.69 s; D4). **Results** holds the rest. Each MicroVM's runtime log, read before the teardown (`aws logs get-log-events`), held the 13 phase records in order; those logs went with the teardown and aren't committed. Trial 04's polling of `/health` got one 502, which `invoke.sh` retried before its single trial request.

The cold trial spent 71.92 s in the guarded launch, longer than October 4's 60-second idle window. With October 4's billed snapshot write (Consequences, **Cost**), that fits the idle-window hypothesis for October 4's attempt, without proving it [inferred].

## Options
1. **Lambda MicroVMs** (GA 2026-06-22): a Firecracker VM with its own kernel per MicroVM, restored from a snapshot of the image.
2. **Fargate, one task per run.**
3. **Lambda functions**, measured for the record: they reuse execution environments, so they can't qualify.
4. **ECS on EC2 with a fresh instance per run**: not measured, the last resort.
5. **No hosted runs** until something qualifies.

The plan the maintainer approved before the trials set a tie-break, in case more than one candidate qualified: the cheapest per 60-second run at 1,000 runs a month; within 20%, the faster cold end-to-end ready time.

## Results
These are October 1's results for all three candidates, and the 2026-10-09 rerun's for the MicroVM. Each figure names the derivation under **Evidence** that produced it from the committed rows: October 1's in `R`, the rerun's in `R2`. "Cold" and "warm" are `invoke.sh`'s labels.

| | MicroVM, 2026-10-01 | MicroVM, 2026-10-09 rerun | Fargate task | Lambda function |
|---|---|---|---|---|
| Trials | 1 cold, 10 warm | 1 cold, 10 warm | 10 cold | 10 cold, 10 warm |
| Sandbox on (D1) | 11 of 11 | 11 of 11 | 0 of 10 | 0 of 20 |
| What the sandbox check read (D10) | Not recorded: the rows predate #81 | In 11 of 11, one renderer, in its own user, PID and network namespaces, with 1 seccomp filter to the browser's 0 | Not recorded: the rows predate #81 | Not recorded: the rows predate #81 |
| Fresh VM (D2) | Yes: 11 MicroVM IDs, no earlier runs | Yes: 11 MicroVM IDs, no earlier runs | Yes: 10 task ARNs and 10 boot IDs, no earlier runs | No: every warm trial ran in its cold trial's environment |
| Ready browser in the trial (D4) | Cold 20.15 s. Warm: median 4.70 s, 0.98–24.60 s | Cold 71.92 s. Warm: median 5.66 s, 0.64–51.69 s | None | None |
| Ready browser end to end (D4) | Cold 28.99 s. Warm: median 8.12 s, 4.24–27.34 s | Cold 83.59 s. Warm: median 10.04 s, 4.04–54.93 s | None | None |
| Peak memory, summed PSS (D5) | 394.4–395.5 MB, with the browser and a blank page | 394.7–395.8 MB, with the browser and a blank page | 165.7–172.4 MB, no browser | 167.4–172.6 MB, no browser |
| Compute per 60-second run (D6–D8) | $0.0024 cold; $0.0022 warm (median); before burst, snapshot and data-transfer costs | $0.0025 cold; $0.0022 warm (median); before the same | At least $0.0011 | $0.0016 |
| Qualifies | **Yes** | **Yes** | No: no sandbox | No: no sandbox, and reuse |

**MicroVM, 2026-10-01.**
- *Clock.* All 11 rows are valid: `requested_at ≤ ready_at ≤ answered_at`, allowing for the offset (D3). That window is seconds wide, so figures that mix the MicroVM's clock with AWS's or this machine's (end to end, D8's `started_to_trial`) are only as exact as the clocks agree, which the rows don't measure.
- *Boot IDs.* The 11 MicroVMs, all from image version 1.0, read two boot IDs: one in the first nine, another in the last two (D9). A restore keeps its snapshot's boot ID, as the 2026-09-30 amendment predicted. Version 1.0 has two builds, one for Graviton 3 and one for Graviton 4, each with its own snapshot (`list-microvm-image-builds`, 2026-10-01), which would explain two; the rows don't record which chipset each MicroVM ran on. The first MicroVM on the second boot ID behaved like a cold start: 18.87 s in the trial, and the slowest warm `started_to_trial`, 4.96 s. Every run ID (`uuid4`, drawn from the kernel) was distinct.
- *Start.* `run-microvm` reached `startedAt` in 0.52 s cold and 0.53 s warm (median; 0.50–0.70 s). From `startedAt` to the trial's start took 8.33 s cold and 2.86 s warm (median; 2.13–4.96 s), including `invoke.sh`'s token call and its polling of `/health` (D8).
- *Spread.* Warm launches ranged from 0.98 s to 24.60 s; the slowest was on the first boot ID, so the tail isn't only the second build. Lambda documents the build's `/validate` hook as a way to have it prefetch the parts of the snapshot a request touches ([MicroVM images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html#microvms-images-build-hooks), checked 2026-10-01). The spike didn't configure it.
- *Back to back.* Every warm trial followed another within seconds. Whether Lambda still holds the snapshot ready when runs arrive minutes apart, as 1,000 runs a month would, wasn't measured; the one cold trial is the conservative figure.
- *Capabilities.* The sandbox started with the default OS capabilities, without `additionalOsCapabilities`.
- *Network.* Every MicroVM got Lambda's default egress, `INTERNET_EGRESS`, and the `ALL_INGRESS` connector that `invoke.sh` asked for. The image build had no connector configured, yet its log names `public.ecr.aws`, `ghcr.io`, `docker.io` and `cdn.playwright.dev`, and it installed 53 packages from Amazon Linux's repositories: builds reach the internet.

**MicroVM, 2026-10-09 rerun.**
- *Clock.* All 11 rows are valid (D3).
- *Boot IDs.* Two again, interleaved this time: the cold trial's in eight rows, another in trials 02, 06 and 07 (D9). This image version also had a Graviton 3 build and a Graviton 4 build. They are October 1's two boot IDs, on an image created eight days later from another package (D9 on `R` and on `R2`), so they were drawn before either image's build, presumably in a snapshot both builds start from [inferred]. What such a snapshot holds is shared beyond one image version, which **Uniqueness after restore** under Consequences already allows for: every per-run value comes after the restore. Every run ID and MicroVM ID was distinct (D2).
- *Start.* `run-microvm` reached `startedAt` in 1.56 s cold and 0.51 s warm (median; 0.47–0.58 s). From `startedAt` to the trial's start took 10.10 s cold and 2.66 s warm (median; 2.45–7.60 s) (D8).
- *Spread.* The three slowest launches, cold 71.92 s and warm 51.69 s and 38.39 s, were on the cold trial's boot ID; the other boot ID's took 0.64–8.89 s (D9). What slowed them isn't recorded.
- *Sandbox check (D10).* In every MicroVM the browser had no seccomp filter, and its one renderer had one, in user, PID and network namespaces other than the browser's. A process inherits its parent's filters, so nothing in the MicroVM filters the trial's processes before Chromium does; under Playwright's seccomp profile in Docker the counts are 1 and 2 (the spike's README).
- *Capabilities and network.* As on October 1: the default OS capabilities, `INTERNET_EGRESS` and `ALL_INGRESS`.

**Fargate.** In all 10 tasks, `launch` raised `SandboxUnavailableError`: the sandbox couldn't start. The rows keep only its message for the host, so the cause isn't recorded; the 2026-09-29 facts explain it (a task can't add a custom seccomp profile, and `SYS_PTRACE` is the only capability it may add). Median timeline (D7): 11.19 s from `createdAt` to the image pull, a 10.67 s pull, `startedAt` 25.92 s after the request, and 14.78 s from `stoppingAt` to `stoppedAt`. With no ready browser, the cost uses `startedAt` as the work's start (87.93 s billed, median), so it is a lower bound.

**Lambda function.** The same error in all 20 trials. Each of the 10 warm trials listed its cold trial's run ID in `earlier_runs`, with the same log stream and boot ID (D2). Cold `Init Duration`: median 488 ms (429–3,674 ms; the slowest was the first after the deploy). Warm `Duration`: median 1,572 ms. `Max Memory Used` for the whole environment: 331–355 MB (D6).

## Decision
**Hosted runs execute in Lambda MicroVMs, one MicroVM per run**, started from the runner's MicroVM image and terminated when the run ends. It is the only candidate that met both halves of the predicate, on 2026-10-01 and again, with the current launcher, on 2026-10-09, so the tie-break didn't apply.

This supersedes ADR-0008's choice of Lambda functions for hosted runs. These ADR-0008 decisions stand: runners run outside the VPC, hold only a run-scoped token, and do all I/O through the API. Its "one image, two locations" can't hold as written (Consequences); M5 decides what replaces it.

## Consequences
- **A fresh VM per run replaces environment hygiene as the isolation boundary.** It holds only if no MicroVM ever serves a second run, and only if a run's memory never leaves its VM:
  - one `run-microvm` per run, and `terminate-microvm` when it ends, whatever the outcome;
  - `maximumDurationInSeconds` set, so a lost MicroVM still ends;
  - no suspension: automatic suspension off, and no `suspend-microvm` call. Suspending writes the MicroVM's memory and disk, and with them the run's token and secrets, to storage that AWS keeps ([MicroVM lifecycle](https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html#microvms-lifecycle-overview)). The idle timer counts only traffic through the MicroVM's endpoint, so a runner that only calls out would look idle and be suspended mid-run ([Running MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html#microvms-launching-idle-policy), checked 2026-10-01).

  SECURITY §6 and ARCHITECTURE §5 state the boundary. TESTING §1's runner-isolation tests enforce it: on the dispatcher at every commit, and on real MicroVMs from M6.
- **Uniqueness after restore.** MicroVMs restored from one snapshot share everything it holds, and AWS says content generated during the build is shared by every MicroVM of that image version: IDs, secrets and random seeds ([Working with snapshots](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images-snapshots.html#microvms-images-snapshots-uniqueness), checked 2026-10-01). So:
  - every per-run value comes after the restore, from the run hook on;
  - the runner draws randomness from the kernel (`secrets`, `os.urandom`);
  - the browser launches after the restore, as the trial's does;
  - image-level environment variables are the same in every run, so they hold no secret.

  A process alive at snapshot time that makes TLS calls needs the base image's snapshot-safe OpenSSL. uv's Python links its own (ADR-0008 amendment, 2026-09-30).
- **Egress.** MicroVMs reach the public internet by default, and so do image builds. The platform isolates nothing on the network. The runner's egress proxy stays the control for the browser's traffic (ADR-0026), but it runs in the same VM, so it can't hold back an exploit that escapes the browser's sandbox (SECURITY §6). A VPC egress connector would route a MicroVM's traffic through our VPC and its NAT ([MicroVM networking](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html#microvms-networking-outbound)). M6 decides whether to use one.
- **Ingress and the run token.** Lambda forwards a request to a MicroVM's endpoint only with a JWE token scoped to that MicroVM and its ports. A runner that only calls out needs no inbound traffic (Lambda's `NO_INGRESS` connector), and no shell access. How the dispatcher hands a MicroVM its run token is M6's design:
  - `run-microvm` can hand each MicroVM a payload of up to 16 KB through its `/run` hook ([Running MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html#microvms-launching-payload), checked 2026-10-01). Whether it may carry a run token depends on where AWS records it, such as CloudTrail's request parameters. Unverified.
  - A request over ingress instead needs a role that can mint auth tokens for runner MicroVMs, and whoever holds that role can reach every live run.
- **Continuation.** A MicroVM may live up to 8 hours (`maximumDurationInSeconds`, 1–28,800 s), so Lambda's 15-minute cap no longer forces continuation. Run tokens still last at most 15 minutes, and runners never renew their own (SECURITY §3, ARCHITECTURE §3.3), so a longer run still needs a new token from the control plane. #28 decides how, and what continuation keeps.
- **Ready time.** The PRD's target is a 10-step strict replay in under 30 s on the hosted compute (PRD §5). The cold MicroVM took 28.99 s just to reach a ready browser on 2026-10-01 and 83.59 s in the 2026-10-09 rerun, and the slowest warm ones 27.34 s and 54.93 s (D4), so the target is at risk: the rerun's cold start alone missed it. M6 measures the runner's own ready time, tries the `/validate` prefetch, and then keeps or amends the target.
- **"One image, two locations" can't hold as written.** Lambda builds MicroVM images itself, from a Dockerfile, on its arm64-only base, and the build installs packages and the headless shell from the internet when it runs. Customer CI runs a container image on the customer's runner, often x86-64. M5 decides, with the Action, which base the CI image uses and how the two builds are kept equivalent for strict replay.
- **Quotas.** The spike's account allows 8 GB of ARM_64 MicroVM memory, running or suspended (four 2 GB MicroVMs at once), and one `RunMicrovm` call a second. AWS's defaults are 1,024 GB and five a second (`aws service-quotas list-service-quotas --service-code lambda` and `list-aws-default-service-quotas`, 2026-10-01). M6's concurrency needs increases in whatever account M4 picks (ROADMAP's risk table).
- **Cost.** Compute at the 2 GB, 1 vCPU baseline: $0.0024 per 60-second run cold, $0.0022 warm (median), billed from `startedAt` (D8); the rerun's rows give $0.0025 cold and $0.0022 warm. MicroVMs have no free tier. Cost Explorer's billed usage, collected on 2026-10-17 (B1–B4 under **Evidence**), gives the rest. Cost Explorer's own cost came back $0.00 for every usage type, to within $0.000000002, presumably net of the account's Free plan credits [inferred], so each cost below is the billed quantity at the us-east-2 Price List rate (**Evidence**).
  - *Compute, billed:* D8's October 1 MicroVMs never reached Cost Explorer (see *Fargate and the Lambda function*), so no bill checks D8 directly. Cost Explorer billed MicroVM compute on two days, each holding an image build and that day's MicroVMs, which its daily totals don't separate (B4):
    - 2026-10-04, version 2.0's build and the failed MicroVM: 243.742 vCPU-seconds and 417.052 GB-seconds, $0.0082795;
    - 2026-10-09, the rerun's image, built at `dd05cb4`, and its 11 MicroVMs (`R2`): 461.44 vCPU-seconds and 837.164 GB-seconds, $0.0158489, so at most $0.0014408 a MicroVM with the build's share included.

    At the baseline, the failed MicroVM's 65.258 seconds would bill 65.258 vCPU-seconds and 130.516 GB-seconds. That leaves at least 178.484 vCPU-seconds and 286.536 GB-seconds ($0.0059936) for the build and any burst [inferred]. These totals fit billing from `startedAt` but can't confirm it to the second, so the ADR keeps D8 for a run's compute and counts each build per image version.
  - *Burst:* Cost Explorer has no burst usage type. Burst bills as ordinary vCPU-seconds and GB-seconds, inside the totals above, and can't be separated from them.
  - *Snapshot reads:* 27.4395 GB on 2026-10-09: at most 2.4945 GB and $0.0038584 a MicroVM (B4), more than a 60-second run's compute. 2026-10-04 billed 6.3306 GB for one MicroVM and a build. The Price List measures reads by the memory and disk the snapshot uses, so a larger runner reads more.
  - *Snapshot writes:* 1.0879 GB ($0.0041314) on 2026-10-04, and none on 2026-10-09, a day with a build and 11 MicroVMs that all ended long before their 900-second idle window. The Price List describes this usage type as the snapshot a suspend writes. So builds don't bill writes, and October 4's failed MicroVM was most likely suspended by its 60-second idle window before it ended [inferred], its memory and disk written to storage that AWS keeps. Its trial held no secret; a runner's would, which is why the no-suspension rule above stands.
  - *Snapshot storage, with its one-week minimum:* billed by the hour, at a fixed size per image version, for 168 hours from the end of its build, even after its delete (B3). The minimum appears as those ordinary GB-hours continuing after the delete, not as a separate charge:
    - the October 1 version 1.0: 1.3509612 GB, deleted after about 64 hours, billed until 2026-10-08 at 17:04Z;
    - version 2.0: 1.3563575 GB, built 2026-10-04 at 06:43Z and deleted about two hours later, billed until 2026-10-11 at 06:43Z;
    - the rerun's image: 1.3534163 GB, built 2026-10-09 at 02:03Z and deleted within two hours, billed until 2026-10-16 at 02:03Z. Its week came to 227.3739 GB-hours, $0.0252638.

    In all, 672.8504 GB-hours, $0.0747611. Version 1.0's first 6.92 hours, on 2026-10-01, are missing (see *Fargate and the Lambda function*). The billed sizes are 8–9% larger than the memory and disk snapshot sizes `get-microvm-image-build` reports, summed over both builds: 1.2467, 1.2483 and 1.2404 GB. The Price List weights this usage type by the memory and disk the snapshot uses, and the ADR trusts the billed size. One version kept for a whole month (730 hours) costs $0.1097771: $0.00010978 a run at 1,000 runs a month, $0.000010978 at 10,000. Each new version also costs at least a week's storage, $0.0252638 at this size, however soon it's replaced. For the record, version 1.0's memory snapshots are 599,609,344 bytes (Graviton 4) and 584,867,840 bytes (Graviton 3); each build also has a 31,125,504-byte disk snapshot and a 1,673,818,112-byte code install (`get-microvm-image-build`, 2026-10-01). The 2026-09-30 amendment's "no MicroVM API reports a snapshot's size" was wrong, but that size doesn't set the bill.
  - *The zip in S3:* `collect.sh` groups by usage type only, so S3's storage and ECR's share `USE2-TimedStorage-ByteHrs`: 0.0541736 GB-months in all (B1), at most $0.0054174 at ECR's $0.10 per GB-month, almost all of it the Lambda function's and Fargate's images until 2026-10-04. The rerun's 465,194-byte zip alone, on 2026-10-08 and 2026-10-09, posted 0.0000139775 GB-months a day: about $0.00001 a month at S3's $0.023, nothing per run.
  - *Data transfer,* at standard AWS rates: 0.4736 GB in and 0.0020 GB out over the internet, and 0.3448 GB in and 0.0015 GB out through CloudFront, over every trial day (B1). A real run's, such as screenshots sent to model providers, is for M6 to measure.
  - *Fargate and the Lambda function:* no billed usage. Their trials ran only on 2026-10-01, and Cost Explorer shows no usage of any type in us-east-2 on that day (B2), although October 1's 11 MicroVMs and version 1.0's first hours were there too. Why isn't known. Their ECR images posted only as storage. D6 and D7 stay computed, not billed.
  - *The budget alarm* has no final posted total: `teardown.sh` deleted it at 2026-10-09T03:39Z, before that day's usage posted. Its last read (`aws budgets describe-budget`, 2026-10-09T01:45Z, at `dd05cb4`; AWS had updated it at 2026-10-08T06:37Z) showed $0.136 spent in October before credits. Cost Explorer's rows through 2026-10-07 come to at most $0.0627 at list price, so the rest isn't in them. October 1's missing usage and Cost Explorer's own $0.01 requests may account for it [inferred].
  - *Posting:* collected 2026-10-17T17:30:43Z, eight days after the rerun's image was created. Posting had finished for 2026-10-02 to 2026-10-16: every version's storage ran its full 168 hours, the last ending on 2026-10-16 at 02:03Z, and nothing of the spike was left to bill. Two things stay open. Cost Explorer marks every October day `Estimated` until the month's bill closes, so these figures can still change; and 2026-10-01 never posted.
  - *Total per run,* from D8's compute, the snapshot reads at their upper bound and one image version's storage, before burst and data transfer: $0.0062 warm and $0.0064 cold at 1,000 runs a month; $0.0061 warm and $0.0063 cold at 10,000. The rerun's compute gives the same totals. Snapshot reads are the largest part. Each image version adds its build's compute and a week's storage, whatever the number of runs. This is M6's cost impact.
- **Docs updated:** ARCHITECTURE §1, §4.2, §5, §6, §8 and §11; SECURITY §6; TECH_STACK §4, §5 and §6; ROADMAP M1, M6 and its risk table; PRD §4, §5 and §8; TESTING §1 and §5; AGENTS.md §1; LAB_NOTES' watch list; the spike's README; ADR-0008's status; the ADR index, whose template gains "Superseded in part".

## Evidence
Run from the repository root. `R` holds October 1's rows and `R2` the 2026-10-09 rerun's; `T` holds three helpers: ISO 8601 timestamps with an offset in seconds, a median, and rounding.

```bash
R=spikes/hosted-chromium/results/2026-10-01.jsonl
R2=spikes/hosted-chromium/results/2026-10-09.jsonl
T='def ts: capture("(?<d>.{19})(?<f>\\.[0-9]+)?(?<s>[+-])(?<h>[0-9]{2}):(?<m>[0-9]{2})") | ((.d + "Z") | fromdateiso8601) + ("0\(.f // "")" | tonumber) - (if .s == "-" then -1 else 1 end) * ((.h | tonumber) * 3600 + (.m | tonumber) * 60); def med: sort | if length % 2 == 1 then .[length / 2 | floor] else (.[length / 2 - 1] + .[length / 2]) / 2 end; def stats: [min, med, max] | map(. * 100 | round / 100);'

# D1 sandbox, per candidate and start
jq -r '[.candidate, .start, .trial.sandbox.on] | @tsv' $R | sort | uniq -c
# D2 fresh VM: rows with earlier runs, and distinct platform IDs, run IDs and boot IDs
jq -c -s 'group_by(.candidate)[] | {candidate: .[0].candidate, rows: length, earlier_runs: map(select(.trial.earlier_runs != [])) | length, platform_ids: map(.platform.microvm.microvmId // .platform.task.taskArn // .platform.log_stream) | unique | length, run_ids: map(.trial.run_id) | unique | length, boot_ids: map(.trial.boot_id) | unique | length}' $R
#    and each warm Lambda trial against the cold trial before it
jq -s '[.[] | select(.candidate == "lambda")] | [range(1; length; 2) as $i | .[$i].start == "warm" and .[$i].trial.earlier_runs == [.[$i - 1].trial.run_id] and .[$i].platform.log_stream == .[$i - 1].platform.log_stream and .[$i].trial.boot_id == .[$i - 1].trial.boot_id] | group_by(.) | map({(.[0] | tostring): length}) | add' $R
# D3 clock validity, allowing for the offset
jq -r 'select(.trial.ready_at) | .requested_at - 0.076 <= .trial.ready_at and .trial.ready_at <= .answered_at + 0.076' $R | sort | uniq -c
# D4 MicroVM ready times, in the trial and end to end: [min, median, max]
jq -c -s "$T"' [.[] | select(.candidate == "microvm")] | group_by(.start)[] | {start: .[0].start, ready: map(.trial.ready_seconds) | stats, end_to_end: map(.trial.ready_at - .requested_at) | stats}' $R
# D5 peak memory, in MB
jq -c -s 'group_by(.candidate)[] | {candidate: .[0].candidate, peak_mb: map(.trial.peak_memory_bytes / 1e6 | . * 10 | round / 10) | [min, max]}' $R
# D6 Lambda's REPORT lines
jq -c -s "$T"' [.[] | select(.candidate == "lambda") | {start} + (.platform.report | capture("\tDuration: (?<duration>[0-9.]+) ms.*Max Memory Used: (?<max_memory_mb>[0-9]+) MB(\tInit Duration: (?<init>[0-9.]+) ms)?") | map_values(tonumber? // null))] | group_by(.start)[] | {start: .[0].start, init_ms: (map(.init | values) | if length > 0 then stats else null end), duration_ms: map(.duration) | stats, max_memory_mb: map(.max_memory_mb) | [min, max]}' $R
# D7 Fargate's timeline, and its billed seconds (README formula, startedAt as the work's start)
jq -c -s "$T"' [.[] | select(.candidate == "fargate") | .platform.task as $t | {to_pull: (($t.pullStartedAt | ts) - ($t.createdAt | ts)), pull: (($t.pullStoppedAt | ts) - ($t.pullStartedAt | ts)), request_to_started: (($t.startedAt | ts) - .requested_at), stopping: (($t.stoppedAt | ts) - ($t.stoppingAt | ts)), billed_s: (($t.startedAt | ts) - ($t.pullStartedAt | ts) + 60 + ($t.stoppedAt | ts) - ($t.stoppingAt | ts))}] | {to_pull: map(.to_pull) | stats, pull: map(.pull) | stats, request_to_started: map(.request_to_started) | stats, stopping: map(.stopping) | stats, billed_s: map(.billed_s) | stats}' $R
# D8 the MicroVM's start, and its billed compute seconds (README formula)
jq -c -s "$T"' [.[] | select(.candidate == "microvm") | {start, request_to_started: ((.platform.microvm.startedAt | ts) - .requested_at), started_to_trial: (.trial.ready_at - .trial.ready_seconds - (.platform.microvm.startedAt | ts))}] | group_by(.start)[] | {start: .[0].start, request_to_started: map(.request_to_started) | stats, started_to_trial: map(.started_to_trial) | stats, billed_s: map(.started_to_trial + 60) | stats}' $R
# D9 the MicroVMs in the order they ran: boot ID, then the in-trial ready time
jq -r 'select(.candidate == "microvm") | [.start, .trial.boot_id[0:8], .trial.ready_seconds] | @tsv' $R

# The rerun: D1, D2's first command, D3, D4, D5, D8 and D9 as above, with $R2 in place of $R
# D10 what the sandbox check read in each MicroVM: the browser's seccomp filters, each renderer's,
#     and whether every renderer has a user, PID and network namespace other than the browser's
jq -c -s 'map(.trial.sandbox_observed as $o | {start, browser_filters: $o.browser.seccomp_filters, renderer_filters: [$o.renderers[].seccomp_filters], own_namespaces: ($o.browser.namespaces as $b | [$o.renderers[] | .namespaces.user != $b.user and .namespaces.pid != $b.pid and .namespaces.net != $b.net] | all)}) | group_by(.) | map(.[0] + {rows: length})' $R2
```

D10 on the rerun's rows prints:

```
[{"start":"cold","browser_filters":0,"renderer_filters":[1],"own_namespaces":true,"rows":1},{"start":"warm","browser_filters":0,"renderer_filters":[1],"own_namespaces":true,"rows":10}]
```

**Costs** apply the spike README's formulas, at its us-east-2 list prices, to these medians:
- *Lambda function:* 2 GB × (60 s + `Init Duration`) × $0.0000133334 per GB-second + $0.0000002 per request. That is $0.0016002 warm, and $0.0016132 cold at the median init of 0.488 s (D6).
- *Fargate:* 87.93 s (D7) × $0.0000123611 per second (1 vCPU, 2 GB and a public IPv4 address) = $0.0010869.
- *MicroVM compute:* the billed seconds (D8) × $0.0000350278 per second (1 vCPU and 2 GB). That is $0.0022018 warm (62.86 s) and $0.0023934 cold (68.33 s); on the rerun's rows, $0.0021948 warm (62.66 s) and $0.0024554 cold (70.10 s).

**Billed usage** comes from Cost Explorer through `collect.sh`, at `550ec7b`, run once on 2026-10-17 at 17:30:43Z (exit 0). Its JSON is the account's billing record, so it stays out of the repository (#38's plan, Q6): run `collect.sh` again to reproduce B1–B4.

```bash
spikes/hosted-chromium/scripts/collect.sh 2026-10-01 2026-10-18 > .scratch/billed.json
B=.scratch/billed.json
# B1 every usage type: total quantity, unit, and Cost Explorer's cost
jq -r '[.ResultsByTime[] | .Groups[] | {k: .Keys[0], q: (.Metrics.UsageQuantity.Amount | tonumber), u: .Metrics.UsageQuantity.Unit, c: (.Metrics.UnblendedCost.Amount | tonumber)}] | group_by(.k)[] | [.[0].k, (map(.q) | add), .[0].u, (map(.c) | add)] | @tsv' $B
# B2 each day: Estimated, and the usage types that posted
jq -r '.ResultsByTime[] | [.TimePeriod.Start, .Estimated, (.Groups | map(.Keys[0] | sub("^USE2-(Lambda-MicroVM-)?"; "")) | join(" "))] | @tsv' $B
# B3 snapshot storage, GB-hours by day
jq -r --arg k USE2-Lambda-MicroVM-Snapshot-Storage-GB-Hour '.ResultsByTime[] | [.TimePeriod.Start, (.Groups[] | select(.Keys[0] == $k) | .Metrics.UsageQuantity.Amount)] | select(length == 2) | @tsv' $B
# B4 MicroVM compute, snapshot reads and snapshot writes by day
jq -r '.ResultsByTime[] | .TimePeriod.Start as $d | .Groups[] | select(.Keys[0] | test("MicroVM-(vCPU|Memory|Snapshot-(Read|Write))")) | [$d, .Keys[0], .Metrics.UsageQuantity.Amount] | @tsv' $B
```

**Billed costs** price those quantities at the us-east-2 Price List's rates (AWSLambda, publication 2026-10-01T18:47:46Z): $0.0000276944 per vCPU-second, $0.0000036667 per GB-second, $0.0015467699 per GB read, $0.0037977138 per GB written and $0.0001111111 per GB-hour stored (the README's $0.08 per GB-month over 720 hours).
- *Storage sizes:* a version's size is a whole day's GB-hours (B3) ÷ 24, on a day it was stored alone or beside a version already known: version 1.0's from 2026-10-02 (32.4230700), version 2.0's from 2026-10-05 (64.9756495, less version 1.0's) and the rerun's from 2026-10-12 (32.4819919).
- *Start and end times:* a part day's hours are its GB-hours, less the other version's, ÷ the version's size. Version 2.0 starts at 06:43Z on 2026-10-04 (55.8510846), as its build ends, and stops at 06:43Z on 2026-10-11 (41.6068590); version 1.0 stops at 17:04Z on 2026-10-08 (55.6222097); the rerun's image starts at 02:03Z on 2026-10-09 (62.2421771) and stops at 02:03Z on 2026-10-16 (2.7923945). 2026-10-10's 65.0345716 is a whole day of version 2.0 and the rerun's image, to within 0.0000002.
- *Per MicroVM:* 2026-10-09's compute and reads (B4) ÷ its 11 MicroVMs.
