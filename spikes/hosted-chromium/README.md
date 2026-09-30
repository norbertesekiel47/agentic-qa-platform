# Hosted-compute spike

M1's decision gate (ADR-0008 amendment, 2026-09-29): which hosted compute runs Chromium with its sandbox on **and** gives every run a fresh VM. Three candidates run the same program, the **trial**: a Lambda MicroVM, a Fargate task and a Lambda function. #37 builds the trial and its packages; #38 runs them on AWS and records the outcome in an ADR. Why the trial measures as it does is in the ADR-0008 amendment of 2026-09-30.

"Trial" is the spike's word for one run of this program on one candidate. It isn't a probe: in CONTEXT.md, a probe is a spec's read-only endpoint.

| Path | What |
|---|---|
| `src/aqa_hosted_chromium_spike/trial.py` | The trial, and `trial_on_this_host()`, which every entry point calls |
| `src/aqa_hosted_chromium_spike/__main__.py` | The Fargate task's command: one trial, printed as a line of JSON |
| `src/aqa_hosted_chromium_spike/lambda_function.py` | The Lambda function's handler: one trial per invocation, answered with the execution environment's log stream |
| `src/aqa_hosted_chromium_spike/microvm.py` | The MicroVM's HTTP server on port 8080: answers Lambda's lifecycle hooks and `GET /health`, and runs one trial per `POST /trial` |
| `Dockerfile` | One target per candidate: `fargate`, `lambda` and `microvm` (the last stage, which Lambda builds) |
| `package.sh` | Builds a candidate's package locally, from HEAD |
| `scripts/` | The AWS CLI scripts #38 runs: budget alarm, deploy, invoke, collect, teardown |
| `tests/` | The trial's tests and the scripts' dry run, run by the test gate (AGENTS.md §4) |

## The trial's report

Each trial launches Chromium through `aqa_runner.sandbox.launch`, which runs the sandbox check (ADR-0026). It times the launch with nothing else running, then samples its memory for a second with a blank page open, closes the browser and reports one JSON object:

| Field | Meaning |
|---|---|
| `sandbox` | `{"on": true}` when the sandbox check proved the sandbox. Otherwise `{"on": false, "error": …}`, with `launch`'s reason and host fix. Every other error stops the trial |
| `ready_seconds` | From asking Playwright to launch until the sandbox check passed; `null` when it didn't |
| `ready_at` | When the sandbox check passed, in seconds since the epoch by the host's clock; `null` when it didn't |
| `peak_memory_bytes` | The largest sample of the summed PSS of the trial's process and all its descendants, sampled every 50 ms for 1 s after the launch, with a blank page open in the browser. A lower bound for a real run, whose pages hold content. Without a browser, the trial's own |
| `run_id` | This trial's random ID |
| `earlier_runs` | The run IDs of trials this environment ran before, in the order they ran, from a marker file in the temporary directory (`/tmp`) |
| `boot_id` | `/proc/sys/kernel/random/boot_id` |

A run had a fresh VM only if `earlier_runs` is empty and the platform's own ID for the run is new. A boot ID can't separate MicroVMs restored from one snapshot (ADR-0008 amendment, 2026-09-30).

The trial runs on Linux only, where the candidates run. Elsewhere, `trial_on_this_host()` refuses.

## The packages

All three targets build on the container base Lambda publishes for MicroVMs, `public.ecr.aws/lambda/microvms:al2023-minimal` (Amazon Linux 2023, arm64), pinned by digest. The Lambda function runs its image through awslambdaric, and Lambda builds the MicroVM's image itself from a zip of the Dockerfile and its context ([MicroVM images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html)).

Every image:
- runs the trial as the non-root user `trial`, since Chromium won't sandbox a root browser;
- gets Python 3.14 from uv;
- installs `uv.lock`'s versions;
- fails its build if the headless shell or a library it loads lacks a system library.

## Commands

Run them from the repository root. They need Docker.

```bash
spikes/hosted-chromium/package.sh fargate     # image aqa-spike-trial:fargate
spikes/hosted-chromium/package.sh lambda      # image aqa-spike-trial:lambda
spikes/hosted-chromium/package.sh microvm     # image aqa-spike-trial:microvm, and build/microvm-<commit>.zip

# One trial in a local container, sandboxed with Playwright's seccomp profile:
docker run --rm --platform linux/arm64 \
  --security-opt seccomp=bench/harness/chromium-seccomp.json aqa-spike-trial:fargate
# Under Docker's default seccomp profile, the sandbox can't start and the report says so:
docker run --rm --platform linux/arm64 aqa-spike-trial:fargate
```

`package.sh` builds from HEAD and refuses a working tree with uncommitted changes to the files it packages, so an image always traces to a commit. It labels the image with that commit (`org.opencontainers.image.revision`) and names the MicroVM zip after it.

## Scripts

AWS CLI v2 only, in us-east-1 (Terraform arrives in M4). They need a CLI that has the `aws lambda-microvms` commands (AWS doesn't state the first version that does), credentials in the environment or a profile, and Docker, jq, curl and python3. Nothing in them holds a secret: the ECR password reaches `docker login` on stdin, and a MicroVM's auth token reaches curl in a header file.

```bash
spikes/hosted-chromium/scripts/budget.sh <email>        # first: the budget alarm, $10 a month, emails at 50% and 100%
spikes/hosted-chromium/package.sh <candidate>           # the local package deploy.sh uploads
spikes/hosted-chromium/scripts/deploy.sh <candidate>    # refuses to run without the budget alarm
spikes/hosted-chromium/scripts/invoke.sh <candidate> cold|warm >> results.jsonl   # one trial, one line of JSON
spikes/hosted-chromium/scripts/collect.sh <first day> <day after the last>        # billed usage by type, from Cost Explorer
spikes/hosted-chromium/scripts/teardown.sh              # deletes everything, checks, then deletes the budget alarm
```

- **Names and tags.** Every resource has a fixed name from `scripts/common.sh`, starting `aqa-spike-hosted-chromium` (IAM roles also sit under the path `/aqa-spike/`, log groups under `/aqa-spike/hosted-chromium/`). Every resource that takes tags carries `aqa-spike=hosted-chromium`, the Fargate tasks too (from their task definition); MicroVMs aren't tagged. Teardown lists resources by those exact names, not by prefix or tag (the tag index lags a delete), so it deletes nothing else. Each candidate's resources are its own, and `deploy.sh` refuses a candidate that is already deployed: its first change creates the candidate's log group, which marks it.
- **Budget first.** `deploy.sh` and `invoke.sh` refuse to run until the budget alarm exists. The alarm covers the whole account, and AWS updates its spend up to three times a day, so the scripts also cap how long anything runs:
  - Lambda: a 120-second timeout;
  - Fargate: the container's command kills the trial after 300 seconds, and `invoke.sh` stops the task if anything fails first;
  - MicroVM: `invoke.sh` terminates it after its trial, even when the trial fails, and it can't live past 15 minutes.
- **Least privilege.**
  - Roles can write their candidate's logs and, for the Fargate task and the MicroVM's build, read their own image or zip.
  - The Fargate task has no task role, so its container holds no AWS credentials.
  - Its security group has no inbound rule.
  - The trial's browser inherits the Lambda function's role, which can therefore do nothing but log.
- **Outbound traffic.** Every candidate keeps its platform's default outbound internet access: a Lambda function and a MicroVM have it by default ([MicroVM networking](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html)), and the Fargate task through its public IP. The trial loads only a blank page and needs none. Blocking a MicroVM's egress takes a VPC egress connector, which the spike doesn't create; hosted runs control egress with the runner's proxy (ADR-0026).
- **What they create:**
  - the budget alarm;
  - per candidate: a log group and one or two IAM roles;
  - the Lambda function: its ECR repository and the function;
  - Fargate: its ECR repository, a cluster, a task definition and a security group in the default VPC;
  - the MicroVM: an S3 bucket for the zip, a MicroVM image, and the MicroVMs `invoke.sh` runs.

  AWS may add the ECS service-linked role with the first cluster; it costs nothing and stays.
- **Teardown** lists what exists under those names, deletes it, and lists again for up to five minutes, since some deletes finish later. Only once nothing else is left does it delete the budget alarm and check that too; otherwise it fails, names what is left, and keeps the alarm. `tests/test_scripts.py` runs every script against fake `aws`, `docker`, `curl` and `sleep` commands that answer a listing only when it names the spike's resources exactly. Its dry run fails when a script creates something that `teardown.sh` doesn't delete, or when a create isn't classified in the test's table.
- **#38 needs to know.**
  - Lambda's MicroVM builder runs the Dockerfile itself, so it must reach the internet to pull the base image, dnf packages, Python, wheels and the headless shell. The docs don't say which network a build gets.
  - Lambda MicroVM images keep their storage for at least a week, and billed usage keeps posting for about a day after the teardown, after the budget alarm is gone.
  - Record each MicroVM's `egressNetworkConnectors` (in `platform.microvm`) with the results.
  - `invoke.sh`'s lines carry the account ID, role ARNs and network details. The repository is public, so redact them before committing results.

## Measurement plan

What #38 records for each candidate, with the command and the commit that produced every number (AGENTS.md rule 8). `invoke.sh` prints each trial as one JSON line: the trial's report (`trial`), `requested_at` and `answered_at` by this machine's clock, and the platform's record (`platform`). The package's commit is in its image tag or zip name.

**Runs.** Each candidate runs with 2 GB: the Fargate task and the MicroVM with 1 vCPU, and the Lambda function with about 1.16, since its CPU follows its memory (1,769 MB is one vCPU).
- Lambda, 2,048 MB: at least five cold and five warm trials. `invoke.sh lambda cold` changes the function's configuration first, which makes Lambda start a new execution environment. `warm` invokes again, which may reuse it.
- Fargate, 1 vCPU and 2 GB: at least five cold trials. Every task starts cold, and a warm start doesn't exist.
- MicroVM, a 2 GB baseline: one cold trial per image build (the first MicroVM after `deploy.sh`), then at least five warm ones. Every MicroVM is a new VM restored from the image's snapshot. The second and later MicroVMs may read a snapshot that Lambda has already cached.

**Time to a ready browser.**
- *In the trial:* `trial.ready_seconds`.
- *End to end,* from the request to a ready browser: `trial.ready_at − requested_at`. This spans two clocks, this machine's and the candidate's, so it is only as exact as their sync; #38 records this machine's offset (for example `sntp time.aws.com` on macOS) with its results. It also includes the client's own path, which differs:
  - Lambda: one `invoke` call;
  - Fargate: one `run-task` call;
  - MicroVM: `run-microvm`, an auth token, and `GET /health` once a second until Lambda routes traffic to the MicroVM.

  So the MicroVM's end-to-end time is an upper bound, by the token call plus up to a second of polling.
- *Valid rows:* the candidate's clock must agree with this machine's, so a row counts only if `requested_at ≤ trial.ready_at ≤ answered_at`, allowing for the recorded offset. #38 drops and reports any row that fails, since a MicroVM's clock right after a snapshot restore is an assumption the docs don't state.
- *Lambda's own breakdown* comes from the REPORT line in `platform.report`: `Init Duration` (cold only) and `Duration`.
- *Fargate's* comes from the task's timestamps in `platform.task`: `createdAt → pullStartedAt → pullStoppedAt → startedAt`.
- *The MicroVM's:* `platform.microvm.startedAt`.

**Peak memory.** `trial.peak_memory_bytes`. For Lambda, also `Max Memory Used` in the REPORT line, which counts the whole execution environment.

**Fresh VM.**
- `trial.earlier_runs` must be empty, and the platform's ID must be new: the log stream in `platform.log_stream` (one per Lambda execution environment), the task ARN in `platform.task`, the MicroVM ID in `platform.microvm`.
- A warm Lambda trial whose `earlier_runs` lists the cold one records the reuse that rules Lambda functions out.

**Cost per 60-second run.** A run is 60 seconds of work, the browser's launch included, plus the time each platform bills before the work can start, both measured above. Prices are us-east-1 list prices, checked 2026-09-30: [Lambda](https://aws.amazon.com/lambda/pricing/), [Fargate](https://aws.amazon.com/fargate/pricing/), [public IPv4](https://aws.amazon.com/vpc/pricing/) and [ECR](https://aws.amazon.com/ecr/pricing/).

- **Lambda function** (arm64, 2 GB). Container-image functions bill their Init phase.

  cost = 2 GB × (60 + init) s × $0.0000133334 per GB-s + $0.0000002 per request

  Here `init` is `Init Duration` on a cold start and 0 on a warm one.
- **Fargate task** (ARM, 1 vCPU, 2 GB). Billed per second from the start of the image pull until the task stops, with a one-minute minimum. The task also holds a public IPv4 address for its whole life.

  s = (the trial's start − pullStartedAt) + 60 + (stoppedAt − stoppingAt), where the trial's start = trial.ready_at − trial.ready_seconds

  cost = s × (1 × $0.0000089944 + 2 × $0.0000009889 + $0.005 / 3,600)

  Ephemeral storage is within the 20 GB included.
- **MicroVM** (a 2 GB, 1 vCPU baseline, and up to 4 × that in bursts). Compute runs while the MicroVM runs. Snapshot storage and reads come from `collect.sh`, because no MicroVM API reports a snapshot's size.

  s = (the trial's start − startedAt) + 60, where the trial's start = trial.ready_at − trial.ready_seconds

  compute = s × (1 × $0.0000276944 + 2 × $0.0000036667) + burst

  - *When billing starts:* Lambda bills the baseline "while your MicroVM is running", so from `startedAt`. #38 checks that against the compute seconds `collect.sh` reports.
  - *Burst* is billed only for use above the baseline. The trial's peak memory sits below 2 GB, so any burst is CPU during the launch; #38 reads its cost from `collect.sh`.
  - *Snapshot reads:* each MicroVM reads its snapshot on start. The per-run cost is `R × $0.00155`, where R is the GB that `collect.sh` bills as snapshot reads, divided by the number of MicroVMs run.
  - *Snapshot storage:* $0.08 per GB-month for the image's snapshot, with a one-week minimum per image version. Per run, it is the monthly storage cost divided by the runs in a month. #38 reports it at 1,000 and at 10,000 runs a month. The snapshot's size, in GB, is the snapshot-storage GB-hours `collect.sh` shows for one whole UTC day inside the image's life, divided by 24. That holds however the one-week minimum is billed, which AWS doesn't describe. #38 runs `collect.sh` at least eight days after the image's creation, so the minimum has posted, and records how it shows up.
  - *Snapshot writes* ($0.0038 per GB): `invoke.sh` terminates each MicroVM before its one-minute idle window can suspend it, and suspending writes a snapshot. Whether the image build's own snapshot bills as a write is for `collect.sh` to show.
- **Every candidate** also stores its package: ECR at $0.10 per GB-month for the Lambda function's and the Fargate task's image, and S3 Standard ([S3](https://aws.amazon.com/s3/pricing/)) for the MicroVM's zip. Per run: the package's GB × the monthly price ÷ the runs in a month.

