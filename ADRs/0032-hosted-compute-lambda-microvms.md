# ADR-0032: Hosted compute: one Lambda MicroVM per run

- Status: Accepted
- Date: 2026-10-01

## Context
ADR-0008 put hosted runs on Lambda functions, then made that choice a decision gate (its amendments of 2026-09-27 and 2026-09-29). Hosted compute qualifies only if both hold:
1. Chromium runs with its sandbox on, proven by the sandbox check (ADR-0026).
2. Every run gets a **fresh VM** (CONTEXT.md).

#37 built the trial and its packages (`spikes/hosted-chromium/`, ADR-0008 amendment of 2026-09-30). #38 ran them on AWS on 2026-10-01:
- **Go-ahead.** The maintainer's comment on #38 (2026-10-01T05:47:04Z) came before the first resource, the budget alarm (16:41:57Z).
- **Account and region.** An AWS project account (AWS's "projects" sign-up, Free plan). Its organization denies Lambda, ECS, ECR, EC2 and S3 outside the project's region, us-east-2, so the spike ran there, not in us-east-1 as ADR-0008's amendment planned. us-east-2's list prices equal us-east-1's for every item the cost formulas use (AWS Price List, checked 2026-09-30).
- **Versions.** AWS CLI 2.37.7. Every package was built at `36cf3dc6935c`, for arm64, and `deploy.sh` reported that commit for each candidate. Each candidate ran with 2 GB: the MicroVM and the Fargate task with 1 vCPU, the Lambda function with about 1.16. The scripts ran at `36cf3dc` for the MicroVM and at `cf629e8` for Fargate and Lambda; among the scripts, `cf629e8` changes only the Fargate security group's description in `deploy.sh`.
- **Clock.** This machine's offset to `time.aws.com` was +0.0009 ± 0.0745 s before the trials and −0.0019 ± 0.0740 s after them (`sntp`, 2026-10-01).
- **Rows.** `invoke.sh <candidate> cold|warm`, once per row, back to back: the 11 MicroVM trials within 200 seconds. All 41 rows are in `spikes/hosted-chromium/results/2026-10-01.jsonl`, redacted: the account ID masked; the MicroVM endpoint, the role ARN and the Fargate task's network details dropped. No row was dropped.
- **Teardown.** The unchanged `teardown.sh` completed at 2026-10-04T08:57:28Z, exit 0. Independent native listings found no live spike compute, image versions, package stores, security group, custom logs, roles or budget; direct gets for image versions 1.0 and 2.0 returned `ResourceNotFoundException`. At 09:01:20Z the ECS cluster was `INACTIVE`, its task definition was still `DELETE_IN_PROGRESS`, and the tag index retained those two entries. Full task-definition/tag disappearance remains to be checked; no broader deletion was attempted. The session logged out successfully. The [failed-attempt evidence](../spikes/hosted-chromium/results/2026-10-04-failed-attempt.json) preserves the receipts and their limitations.

## Current-package rerun, 2026-10-04

The maintainer approved rebuilding from `8ae62222e62e547cb4c2f3ed90acb6e41df4c03d` after importing main's current sandbox launch protections, then separately approved the image build and the one-cold/ten-warm trial sequence. The trial, Dockerfile and invocation behavior stayed unchanged. Image version 2.0 used `microvm-8ae62222e62e.zip` (457,649 bytes; SHA256 `1ba49e95a710994d144a97296a58a28377cb1d9d885f79e2219e49e6af8b3756`). Both its builds were `SUCCESSFUL` and the version was `ACTIVE`; the resources, hooks, CPU, logging, egress, environment and capabilities matched version 1.0. The accepted base-version input was `1`; saved version metadata reports `1.0`. LAB_NOTES records the rejected input and separately approved correction.

`spikes/hosted-chromium/scripts/invoke.sh microvm cold` at that HEAD began at 08:51:15Z and exited 28 at 08:53:21Z: its single trial POST timed out after 120.007 seconds with zero received bytes. It emitted no trial row. The stop condition prevented all ten warm attempts and any retry. The service reported version 2.0 `TERMINATED`, from 08:51:18.468Z to 08:52:23.726Z (65.258 seconds), with `maximumDurationInSeconds=900` and reason `MicroVM exceeded maximum lifetime.` The recorded idle policy was 60 seconds, zero suspended duration, auto-resume false. Idle termination is a hypothesis, not an established explanation of the stalled request or the service's reason. Two reads of the custom log group returned zero runtime events, and no matching default runtime log group existed. The clocks bracketed the attempt at +0.000187 ± 0.075675 seconds and −0.001894 ± 0.070562 seconds (`sntp time.aws.com`).

The [separate failure artifact](../spikes/hosted-chromium/results/2026-10-04-failed-attempt.json) is an evidence object, not a trial `Report`, and records the commands, source SHA, timestamps, terminal state, cleanup and raw-receipt hashes. It supplies no sandbox, isolation or ready-time measurement. The October 1 rows and figures below remain historical; hosted validation of the current launcher is still open. No successful rows were fabricated or appended to the old dataset.

Version 2.0's build metadata spans 06:40:58.108Z–06:43:38.914Z (160.806 seconds). Its memory snapshots were 600,137,728 bytes on Graviton 4 and 584,867,840 bytes on Graviton 3; each build had 31,657,984 disk-snapshot bytes and 1,683,935,232 code-install bytes (`list-microvm-image-builds` and `get-microvm-image-build`, 2026-10-04, same HEAD). These inputs and the package's S3 size were preserved before deletion. The approved image estimate was $0.023–$0.125 and the planned eleven-trial estimate $0.034–$0.117; neither is a bill or the cost of this single failed attempt. Billed evidence remains required, including any snapshot write caused by termination/suspension. Collection for the original image stays no earlier than October 9; version 2.0's creation plus eight days is October 12 UTC, with posting completeness checked then. `SESSION-3` cost fields remain unfinished.

## Amendment 2026-10-04: bounded diagnostics before another hosted attempt

**Context and alternatives.** The current-package attempt returned no Report, and its service reason and timing do not identify the failure. Keeping the invocation unchanged would preserve the failed experiment's settings but still leave its missing phase evidence. The alternatives were to add bounded instrumentation and adjust the competing idle window, or have the maintainer choose the issue's hosted-wait fallback. The maintainer selected local instrumentation, including the existing test's idle-value change from 60 to 900; no additional AWS attempt was approved by that choice.

**Decision.** The spike emits fixed-schema, flushed stderr records around handler/body, Playwright startup, guarded launch plus sandbox observation, memory sampling, browser close and response writing. Only a closed `Stage` value and monotonic time enter a record. Health/hooks emit none, other errors retain their existing propagation, and the Report fields are unchanged. The spike README owns the phase vocabulary. Browser cleanup still runs if a close-start diagnostic write fails; no new exception is caught. Logging stays outside the existing ready-time interval and memory-sampling loop.

The MicroVM invocation explicitly names its already scoped runtime log group and sets idle duration to 900 seconds, matching the unchanged maximum lifetime. Suspended duration remains zero, auto-resume remains false, and the one trial POST's timeout remains 120 seconds. The termination trap and no-repeat behavior stand. No IAM, base-image, Dockerfile, dependency, runner-launch or sandbox control changes are part of this amendment.

**Consequences and limits.** These markers can distinguish the last observed handler/startup/launch/memory/close boundary; the launch and sandbox observation remain one guarded call. They do not prove the reason for a stall, client receipt or equivalence between local Docker and an AWS snapshot. Explicit runtime logging is proposed to obtain evidence, not proof that the previous log gap is solved ([MicroVM monitoring](https://docs.aws.amazon.com/lambda/latest/dg/microvms-monitoring.html), checked 2026-10-04). The idle change removes a shorter timer as a competing variable, not an established root cause. The local implementation incurs no AWS/model spend. Any next image must have its own reviewed source/artifact hash and exact build approval, followed by a separate hosted-trial approval with a refreshed cost estimate and teardown scope. Current hosted qualification, billed cost and the recorded metadata-cleanup checks remain open.

## Options
1. **Lambda MicroVMs** (GA 2026-06-22): a Firecracker VM with its own kernel per MicroVM, restored from a snapshot of the image.
2. **Fargate, one task per run.**
3. **Lambda functions**, measured for the record: they reuse execution environments, so they can't qualify.
4. **ECS on EC2 with a fresh instance per run**: not measured, the last resort.
5. **No hosted runs** until something qualifies.

The plan the maintainer approved before the trials set a tie-break, in case more than one candidate qualified: the cheapest per 60-second run at 1,000 runs a month; within 20%, the faster cold end-to-end ready time.

## Results
These are the October 1 historical results. Each figure names the derivation under **Evidence** that produced it from the committed rows. "Cold" and "warm" are `invoke.sh`'s labels.

| | MicroVM | Fargate task | Lambda function |
|---|---|---|---|
| Trials | 1 cold, 10 warm | 10 cold | 10 cold, 10 warm |
| Sandbox on (D1) | 11 of 11 | 0 of 10 | 0 of 20 |
| Fresh VM (D2) | Yes: 11 MicroVM IDs, no earlier runs | Yes: 10 task ARNs and 10 boot IDs, no earlier runs | No: every warm trial ran in its cold trial's environment |
| Ready browser in the trial (D4) | Cold 20.15 s. Warm: median 4.70 s, 0.98–24.60 s | None | None |
| Ready browser end to end (D4) | Cold 28.99 s. Warm: median 8.12 s, 4.24–27.34 s | None | None |
| Peak memory, summed PSS (D5) | 394.4–395.5 MB, with the browser and a blank page | 165.7–172.4 MB, no browser | 167.4–172.6 MB, no browser |
| Compute per 60-second run (D6–D8) | $0.0024 cold; $0.0022 warm (median); before burst, snapshot and data-transfer costs | At least $0.0011 | $0.0016 |
| Qualifies | **Yes** | No: no sandbox | No: no sandbox, and reuse |

**MicroVM.**
- *Clock.* All 11 rows are valid: `requested_at ≤ ready_at ≤ answered_at`, allowing for the offset (D3). That window is seconds wide, so figures that mix the MicroVM's clock with AWS's or this machine's (end to end, D8's `started_to_trial`) are only as exact as the clocks agree, which the rows don't measure.
- *Boot IDs.* The 11 MicroVMs, all from image version 1.0, read two boot IDs: one in the first nine, another in the last two (D9). A restore keeps its snapshot's boot ID, as the 2026-09-30 amendment predicted. Version 1.0 has two builds, one for Graviton 3 and one for Graviton 4, each with its own snapshot (`list-microvm-image-builds`, 2026-10-01), which would explain two; the rows don't record which chipset each MicroVM ran on. The first MicroVM on the second boot ID behaved like a cold start: 18.87 s in the trial, and the slowest warm `started_to_trial`, 4.96 s. Every run ID (`uuid4`, drawn from the kernel) was distinct.
- *Start.* `run-microvm` reached `startedAt` in 0.52 s cold and 0.53 s warm (median; 0.50–0.70 s). From `startedAt` to the trial's start took 8.33 s cold and 2.86 s warm (median; 2.13–4.96 s), including `invoke.sh`'s token call and its polling of `/health` (D8).
- *Spread.* Warm launches ranged from 0.98 s to 24.60 s; the slowest was on the first boot ID, so the tail isn't only the second build. Lambda documents the build's `/validate` hook as a way to have it prefetch the parts of the snapshot a request touches ([MicroVM images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html#microvms-images-build-hooks), checked 2026-10-01). The spike didn't configure it.
- *Back to back.* Every warm trial followed another within seconds. Whether Lambda still holds the snapshot ready when runs arrive minutes apart, as 1,000 runs a month would, wasn't measured; the one cold trial is the conservative figure.
- *Capabilities.* The sandbox started with the default OS capabilities, without `additionalOsCapabilities`.
- *Network.* Every MicroVM got Lambda's default egress, `INTERNET_EGRESS`, and the `ALL_INGRESS` connector that `invoke.sh` asked for. The image build had no connector configured, yet its log names `public.ecr.aws`, `ghcr.io`, `docker.io` and `cdn.playwright.dev`, and it installed 53 packages from Amazon Linux's repositories: builds reach the internet.

**Fargate.** In all 10 tasks, `launch` raised `SandboxUnavailableError`: the sandbox couldn't start. The rows keep only its message for the host, so the cause isn't recorded; the 2026-09-29 facts explain it (a task can't add a custom seccomp profile, and `SYS_PTRACE` is the only capability it may add). Median timeline (D7): 11.19 s from `createdAt` to the image pull, a 10.67 s pull, `startedAt` 25.92 s after the request, and 14.78 s from `stoppingAt` to `stoppedAt`. With no ready browser, the cost uses `startedAt` as the work's start (87.93 s billed, median), so it is a lower bound.

**Lambda function.** The same error in all 20 trials. Each of the 10 warm trials listed its cold trial's run ID in `earlier_runs`, with the same log stream and boot ID (D2). Cold `Init Duration`: median 488 ms (429–3,674 ms; the slowest was the first after the deploy). Warm `Duration`: median 1,572 ms. `Max Memory Used` for the whole environment: 331–355 MB (D6).

## Decision
**Hosted runs execute in Lambda MicroVMs, one MicroVM per run**, started from the runner's MicroVM image and terminated when the run ends. It is the only candidate that met both halves of the predicate, so the tie-break didn't apply.

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
- **Ready time.** The PRD's target is a 10-step strict replay in under 30 s on the hosted compute (PRD §5). The cold MicroVM took 28.99 s just to reach a ready browser, and the slowest warm one 27.34 s, so the target is at risk. M6 measures the runner's own ready time, tries the `/validate` prefetch, and then keeps or amends the target.
- **"One image, two locations" can't hold as written.** Lambda builds MicroVM images itself, from a Dockerfile, on its arm64-only base, and the build installs packages and the headless shell from the internet when it runs. Customer CI runs a container image on the customer's runner, often x86-64. M5 decides, with the Action, which base the CI image uses and how the two builds are kept equivalent for strict replay.
- **Quotas.** The spike's account allows 8 GB of ARM_64 MicroVM memory, running or suspended (four 2 GB MicroVMs at once), and one `RunMicrovm` call a second. AWS's defaults are 1,024 GB and five a second (`aws service-quotas list-service-quotas --service-code lambda` and `list-aws-default-service-quotas`, 2026-10-01). M6's concurrency needs increases in whatever account M4 picks (ROADMAP's risk table).
- **Cost.** Compute at the 2 GB, 1 vCPU baseline: $0.0024 per 60-second run cold, $0.0022 warm (median). Both bill from `startedAt` until `collect.sh`'s compute seconds confirm it. MicroVMs have no free tier.
  - *Burst:* SESSION-3.
  - *Snapshot reads:* SESSION-3.
  - *Snapshot storage, with its one-week minimum:* SESSION-3. Reported at 1,000 and at 10,000 runs a month. The size comes from `get-microvm-image-build`, which reports each build's memory and disk snapshot sizes; the 2026-09-30 amendment's "no MicroVM API reports a snapshot's size" was wrong. Version 1.0's memory snapshots are 599,609,344 bytes (Graviton 4) and 584,867,840 bytes (Graviton 3); each build also has a 31,125,504-byte disk snapshot and a 1,673,818,112-byte code install (`get-microvm-image-build`, 2026-10-01).
  - *The zip in S3:* SESSION-3.
  - *Data transfer,* at standard AWS rates: the trial sent almost none. A real run's, such as screenshots sent to model providers, is for M6 to measure.
  - *Total per run:* SESSION-3. This is M6's cost impact, before data transfer.
- **Docs updated:** ARCHITECTURE §1, §4.2, §5, §6, §8 and §11; SECURITY §6; TECH_STACK §4, §5 and §6; ROADMAP M1, M6 and its risk table; PRD §4, §5 and §8; TESTING §1 and §5; AGENTS.md §1; LAB_NOTES' watch list; the spike's README; ADR-0008's status; the ADR index, whose template gains "Superseded in part".

## Evidence
Run from the repository root. `R` holds the rows; `T` holds three helpers: ISO 8601 timestamps with an offset in seconds, a median, and rounding.

```bash
R=spikes/hosted-chromium/results/2026-10-01.jsonl
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
```

**Costs** apply the spike README's formulas, at its us-east-2 list prices, to these medians:
- *Lambda function:* 2 GB × (60 s + `Init Duration`) × $0.0000133334 per GB-second + $0.0000002 per request. That is $0.0016002 warm, and $0.0016132 cold at the median init of 0.488 s (D6).
- *Fargate:* 87.93 s (D7) × $0.0000123611 per second (1 vCPU, 2 GB and a public IPv4 address) = $0.0010869.
- *MicroVM compute:* the billed seconds (D8) × $0.0000350278 per second (1 vCPU and 2 GB). That is $0.0022018 warm (62.86 s) and $0.0023934 cold (68.33 s).
