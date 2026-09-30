# Hosted-compute spike

M1's decision gate (ADR-0008 amendment, 2026-09-29): which hosted compute runs Chromium with its sandbox on **and** gives every run a fresh VM. Three candidates run the same program, the **trial**: a Lambda MicroVM, a Fargate task and a Lambda function. #37 builds the trial and its packages; #38 runs them on AWS and records the outcome in an ADR. How the trial measures is in the ADR-0008 amendment of 2026-09-30.

"Trial" is the spike's word for one run of this program on one candidate. It isn't a probe: in CONTEXT.md, a probe is a spec's read-only endpoint.

| Path | What |
|---|---|
| `src/aqa_hosted_chromium_spike/trial.py` | The trial, and `measure()`, which every entry point calls |
| `src/aqa_hosted_chromium_spike/__main__.py` | The Fargate task's command: one trial, printed as a line of JSON |
| `src/aqa_hosted_chromium_spike/lambda_function.py` | The Lambda function's handler: one trial per invocation |
| `src/aqa_hosted_chromium_spike/microvm.py` | The MicroVM's HTTP server: answers Lambda's `/ready` and `/run` hooks, and runs one trial per `POST /trial` |
| `Dockerfile` | One target per candidate: `fargate`, `lambda` and `microvm` (the last stage, which Lambda builds) |
| `package.sh` | Builds a candidate's package locally, from HEAD |
| `tests/` | The trial's tests, run by the test gate (AGENTS.md §4) |

## The trial's report

Each trial launches Chromium through `aqa_runner.sandbox.launch`, which runs the sandbox check (ADR-0026), then closes the browser and reports one JSON object:

| Field | Meaning |
|---|---|
| `sandbox` | `{"on": true}` when the sandbox check proved the sandbox. Otherwise `{"on": false, "error": …}`, with the check's reason and the host fix. A sandbox that can't start is reported, never raised; any other error stops the trial |
| `ready_seconds` | From asking Playwright to launch until the sandbox check passed; `null` when it didn't |
| `peak_memory_bytes` | The largest sample of the summed PSS of the trial's process and all its descendants (Playwright's driver and every Chromium process), taken every 50 ms from before the launch until the browser closed. PSS counts a page that processes share once. The trial's only page is the sandbox check's blank page, so this is a lower bound for a real run |
| `run_id` | This trial's random ID |
| `earlier_runs` | The run IDs of trials this environment ran before, from a marker file in the temporary directory (`/tmp`). An environment used again shows them |
| `boot_id` | `/proc/sys/kernel/random/boot_id` |

**Reading the fresh-VM evidence.** A run had a fresh VM only if `earlier_runs` is empty *and* the platform's own ID for it (Lambda's log stream, the Fargate task's ARN, the MicroVM's ID) is new. `boot_id` separates VMs on Fargate and Lambda. It proves nothing on MicroVMs: every MicroVM restored from one snapshot reads the same boot ID ([Firecracker, random-for-clones](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/random-for-clones.md)).

The trial runs on Linux only, where the candidates run. Elsewhere, `measure()` refuses.

## The packages

All three build from one base: `public.ecr.aws/lambda/microvms:al2023-minimal`, the container base Lambda publishes for MicroVMs (Amazon Linux 2023, arm64 only), pinned by digest.

- **Fargate** (`fargate`): the image's command runs one trial and prints its report.
- **Lambda function** (`lambda`): the Lambda Python base image has no package manager to add Chromium's libraries with, so the function runs this image through awslambdaric, the runtime interface client (4.1.0, locked in `uv.lock`).
- **MicroVM** (`microvm`): Lambda builds the image itself from a zip holding the Dockerfile and its context ([MicroVM images](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html)).
  - The build snapshots the server with every running process once `/ready` answers, and each MicroVM restores that snapshot.
  - The server therefore launches Chromium only for a `POST /trial`, after the restore. Otherwise one browser, its memory and random state included, would be cloned into every MicroVM.

Every image:
- runs the trial as the non-root user `trial`, since Chromium won't sandbox a root browser;
- gets Python 3.14 from uv;
- installs `uv.lock`'s versions;
- fails its build if the headless shell lacks a library.

The images are about 1.1 GB, mostly the headless shell, the workspace's dependencies and Chromium's libraries.

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
