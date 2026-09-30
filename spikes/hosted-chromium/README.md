# Hosted-compute spike

M1's decision gate (ADR-0008 amendment, 2026-09-29): which hosted compute runs Chromium with its sandbox on **and** gives every run a fresh VM. Three candidates run the same program, the **trial**: a Lambda MicroVM, a Fargate task and a Lambda function. #37 builds the trial and its packages; #38 runs them on AWS and records the outcome in an ADR. Why the trial measures as it does is in the ADR-0008 amendment of 2026-09-30.

"Trial" is the spike's word for one run of this program on one candidate. It isn't a probe: in CONTEXT.md, a probe is a spec's read-only endpoint.

| Path | What |
|---|---|
| `src/aqa_hosted_chromium_spike/trial.py` | The trial, and `trial_on_this_host()`, which every entry point calls |
| `src/aqa_hosted_chromium_spike/__main__.py` | The Fargate task's command: one trial, printed as a line of JSON |
| `src/aqa_hosted_chromium_spike/lambda_function.py` | The Lambda function's handler: one trial per invocation |
| `src/aqa_hosted_chromium_spike/microvm.py` | The MicroVM's HTTP server on port 8080: answers Lambda's lifecycle hooks, and runs one trial per `POST /trial` |
| `Dockerfile` | One target per candidate: `fargate`, `lambda` and `microvm` (the last stage, which Lambda builds) |
| `package.sh` | Builds a candidate's package locally, from HEAD |
| `tests/` | The trial's tests, run by the test gate (AGENTS.md §4) |

## The trial's report

Each trial launches Chromium through `aqa_runner.sandbox.launch`, which runs the sandbox check (ADR-0026), then closes the browser and reports one JSON object:

| Field | Meaning |
|---|---|
| `sandbox` | `{"on": true}` when the sandbox check proved the sandbox. Otherwise `{"on": false, "error": …}`, with `launch`'s reason and host fix. Every other error stops the trial |
| `ready_seconds` | From asking Playwright to launch until the sandbox check passed; `null` when it didn't |
| `peak_memory_bytes` | The largest sample of the summed PSS of the trial's process and all its descendants, sampled every 50 ms from before the launch until the browser closed. A lower bound for a real run: the only page is the sandbox check's blank one |
| `run_id` | This trial's random ID |
| `earlier_runs` | The run IDs of trials this environment ran before, from a marker file in the temporary directory (`/tmp`) |
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
