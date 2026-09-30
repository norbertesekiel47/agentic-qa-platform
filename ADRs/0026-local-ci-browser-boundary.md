# ADR-0026: The browser boundary for local and CI runs: sandbox, egress and test secrets

- Status: Accepted
- Date: 2026-09-29

## Context
M1 runs the first model-using agent against real pages, locally and in our own CI, before any hosted runner exists. SECURITY §5–§7 describe the controls mostly from the hosted side, and several of them don't fit M1:
- **Sandbox.** GitHub's ubuntu-24.04 runners restrict unprivileged user namespaces through AppArmor (`kernel.apparmor_restrict_unprivileged_userns=1`), so Playwright's Chromium can't start its sandbox there. ARCHITECTURE §5 said CI runs "attempt the sandbox and warn", and policy_guard refuses the code for that unless an ADR is cited.
- **Loopback and private addresses.** SECURITY §7 refused them in every mode, but local runs target `http://127.0.0.1:4100` and CI previews may be private.
- **Secrets move into M1.** Four of the five pilot specs sign in with `TEST_PASSWORD`, and redaction applies in every model-using mode (AGENTS.md §6). So `fill_secret`, its binding and redaction move from M2 into M1. Local runs had no source for secret values or bindings.
- **Third-party hosts.** Apps load CDNs, fonts and analytics. A refused request logs a console error and can leave an image broken, and the invariants would blame both on the app (DATA_MODEL §6).
- **Codex's review** of the M1 decisions (gpt-6-astra, 2026-09-29) found four gaps in the first draft:
  - a spec could widen a secret's destinations by changing its start URL;
  - a click, redirect or popup could turn a resource-only host into a document the agent acts on (Playwright's routes don't see redirect hops; LAB_NOTES, 2026-09-29);
  - hiding the symptoms of blocked requests could hide real defects;
  - allowlisted hosts could resolve to internal services.

## Options
- **Sandbox:** (1) a hard error everywhere in M1; (2) warn and continue in CI.
- **Sandbox in our CI:** (1) set the AppArmor sysctl on the GitHub-hosted runner; (2) run browser tests in a container with Playwright's seccomp profile.
- **Private addresses:** (1) refused everywhere; (2) allowed everywhere outside hosted runs; (3) allowed only for the invocation's target and for private origins the project declares.
- **Secret bindings:** (1) in the project config, with values from prefixed environment variables; (2) in each spec; (3) bare environment variable names.
- **Blocked requests:** (1) never count their symptoms; (2) count everything; (3) a blocked request keeps the run from passing, except for declared expected-blocked hosts.
- **Reporting an egress block:** (1) its own exit code (6) and run state (`errored`, `egress_blocked`); (2) reuse the infrastructure codes (10+); (3) explore reuses the spec-error code (5), and replays reuse 10+.
- **Saved evidence:** (1) never save request or response bodies, HAR files or Playwright traces; (2) ban them in M1 only, until a scrubber covers them.

## Decision

### Sandbox
- **Always on.** Chromium launches with `chromium_sandbox=True`.
- **Hard error in M1.** Everywhere in M1, a launch that can't sandbox is an error, with a message that says how to fix the host.
- **Startup check.** The check proves the sandbox is on:
  - it compares a renderer process with the browser process: namespaces and seccomp mode, allowing for the zygote;
  - it includes negative controls;
  - macOS uses its own method.
- **Our CI** records the runner's AppArmor state, then sets `kernel.apparmor_restrict_unprivileged_userns=0` before the browser tests. This is Chromium's documented fix, and the runner is discarded after the job. Browser tests move into the runner image once it exists.
- **Customer CI.** Whether customer CI may ever run unsandboxed is decided with the Action at M5 (#26).

### The browser's environment
The browser launches with an explicit, minimal environment: no provider keys, no `AQA_SECRET_*` values and no cloud credentials. A test secret reaches the browser only through `fill_secret`.

### Two tiers of hosts
- **Allowed origins** are the start origin plus the origins the spec lists. The agent may navigate and act only there, and a secret can be bound only to them.
- **Subresource hosts** are declared in the project config. Pages may load resources from them.

The egress proxy lets traffic through to both tiers. Before every observation and every action, the tools check the origin of the top-level page and of the target's frame. A document from any other origin is a policy event:
- the agent can't observe it or act on it;
- the agent may only navigate back to an allowed origin, or restart.

Popups are recorded and closed. The redirect hop itself isn't blocked, because its host is already on the egress allowlist. The tiers limit what the agent may do, not where traffic may go.

### Start origin
The start origin comes only from the invocation (`aqa explore --url`) or the project config (`base_url`, DATA_MODEL §9), never from the spec. `start_url` must be a path.

### IP policy by location
- **Every mode:**
  - the proxy passes only allowed origins and subresource hosts;
  - link-local and cloud-metadata addresses are always refused;
  - IP forms are normalized, including IPv4-mapped IPv6;
  - each hostname's first validated DNS answer is pinned for the whole run, so it can't rebind mid-run.
- **Hosted runs** may reach public addresses only.
- **Local and CI runs** may reach loopback and private addresses only for the invocation's target origin and for private origins the project config declares. Every other host must resolve to a public address.
- **Same rules for every request.** They cover every redirect hop and the runner's own probe and reset requests. Those requests go to allowed origins only and carry none of the browser's cookies.
- **Other transports.** Chromium launches so that no UDP traffic bypasses the proxy (WebRTC). The egress tests cover WebSockets, QUIC, IPv6 and DNS prefetch, observed at the packet level.

### Test secrets
- **Bindings live in the project config**, and a spec may reference only declared secrets. Each binding names:
  - the origins the secret may be filled on: `start` means the invocation's start origin, and any other must be an allowed origin;
  - the field it may go into: `password` means an `<input type="password">`; otherwise a role and accessible name.
- **Destinations.** A secret's destinations are the intersection of its binding and the run's allowed origins.
- **Values** come from `AQA_SECRET_<NAME>` environment variables, locally and in CI. Hosted runs keep the `test_secrets` API.
- **CI's trusted revision.** Whether a CI run trusts the base branch's config or the pull request's is decided at M5 (#27).

### Egress blocks
- **Default.** A request to a host that is neither an allowed origin nor a subresource host is refused and recorded. The egress block keeps the run from passing: it is a policy outcome, neither a pass nor a finding.
- **Reporting (option 1).** The run ends `errored` with `error_code: egress_blocked` and no verdict, the run record names the refused host, and the CLI exits 6 (API.md §7). CI can tell our sandbox stopping a run apart from an app bug (exit 1) and a broken environment (10+).
- **Expected-blocked hosts.** If the project config lists the host as expected-blocked, the block's direct symptoms don't count against invariants: its console error and a broken image, matched by the failed request, never by console text. Indirect effects, such as app code that fails because a script didn't load, still count.
- **The proxy never invents a response** the page could count as the app's. On an upstream failure it drops the connection or fails the tunnel, and records an infrastructure event. An unreachable start origin is an infrastructure error (exit 10+), not a finding.

### Evidence (option 1)
- Text observations are redacted before any model sees them (SECURITY §5).
- Every secret-bearing field is masked in every screenshot.
- **Never saved, in any milestone:** request or response bodies, HAR files and Playwright traces. Request bodies are where secrets travel (a sign-in POST carries the password), and reliably scrubbing a trace's page snapshots and bodies isn't worth a debugging convenience.
- Network evidence is metadata only: method, redacted URL, status, timing and size (`network_log`, DATA_MODEL §2).
- Text redaction runs over everything that is saved.
- OCR checks for secrets that a page reflects into pixels arrive with hybrid perception in M2, before any artifact can leave the machine. Uploads start in M4.

## Consequences
- **Explore runs behind the proxy from its first commit.** The egress, origin-check and injection tests block merges from M1 on (TESTING §1).
- **The CI workflow gains a sysctl step.** Like any gate change, it needs the maintainer's approval under the guard.
- **An undeclared third-party host stops a confirmation replay**, with a message that names the host to declare. Conduit makes no third-party requests.
- **Local screenshots can still show a secret that a page reflects** until M2's OCR checks. That stays within SECURITY §5's best-effort class, and M1 uploads nothing.
- **Later milestones decide:**
  - the Action's sandbox (#26);
  - the CI run's trusted config revision (#27);
  - the hosted-run policy for subresource hosts (#29).
- **Docs updated:** SECURITY §5–§7 and ARCHITECTURE §5 are amended to match.

## Amendment (2026-09-30): how the sandbox check works (#35)

The Decision's "startup check" is called the **sandbox check** (CONTEXT.md), so it can't be confused with startup hygiene (SECURITY §6).

- **When it runs.** `aqa_runner.sandbox.launch` asks for `chromium_sandbox=True`, then opens a blank page in a context of its own, because a renderer exists only once a page does. It checks every renderer alive at that moment and closes the context before it returns the browser. Later renderers come from the same zygote with the same sandbox and aren't checked again.
- **Process IDs** come from CDP's `SystemInfo.getProcessInfo`, an experimental domain that lists the browser's and each renderer's PID as the host sees them. The alternative, walking the process tree from Playwright's driver, has to work around the zygote (a renderer's parent) and can't tell two browsers apart.
- **Linux: strict.**
  - The renderer must be in its own user, pid and net namespaces.
  - It must carry more seccomp filters than the browser (`Seccomp_filters` in `/proc/<pid>/status`, Linux 5.9 and later).
  - The seccomp mode can't serve: under Docker's default profile every process, the browser included, is in mode 2.
  - Chromium's setuid sandbox, which Playwright's builds don't ship, would fail this check, and a kernel too old to report the count stops it with an error of its own. Neither ever passes.
- **macOS.** The check calls libSystem's `sandbox_check(pid, NULL, 0)`, which Apple exports but doesn't document.
  - The renderer must be sandboxed and the browser not. The browser's "no" also shows the call can answer no.
  - If Apple changes the call, the comparison fails and the launch is refused. It can't pass.
- **Other operating systems** have no sandbox check, so their launches are refused.
- **Failures.** Both cases raise `SandboxUnavailableError`, with exit code 10 (API.md §7): a launch whose sandbox can't start (Chromium's `No usable sandbox!`, passed through in Playwright's error), and a launch whose sandbox the check can't prove.
  - The message names the host fix. On Linux: the AppArmor sysctl for throwaway hosts, Chromium's AppArmor profile for machines you keep, and Playwright's seccomp profile in containers.
  - An error while reading the processes (a kernel without the count, a renderer that exits mid-check) propagates as it is. The browser is closed first, so nothing passes.
  - The CLI maps the error to its exit code once a command launches a browser (#53).
- **Nothing skips the check.** `launch` takes no option and reads no setting or environment variable. The check measures the renderer itself, so every way of turning the sandbox off fails it. The negative controls in `packages/runner/tests/test_sandbox.py` launch without the sandbox, the only place allowed to.
- **CI.** The `python` job installs Playwright's headless shell, logs `kernel.apparmor_restrict_unprivileged_userns`, sets it to 0, and runs the browser tests within `pytest --cov`. A runner without the setting fails the job.
