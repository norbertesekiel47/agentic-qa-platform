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

- **When it runs.** `aqa_runner.sandbox.launch` asks for `chromium_sandbox=True`, then opens a blank page in a context of its own, because a renderer exists only once a page does. It checks every renderer alive at that moment and closes the context before it returns the browser. Later renderers start with the same sandbox (on Linux, from the same zygote) and aren't checked again.
- **Process IDs** come from CDP's `SystemInfo.getProcessInfo`, an experimental domain that lists the browser's and each renderer's PID as the host sees them. That holds only for a browser launched on this host, never one connected to, which is why `launch` runs the check. The alternative, walking the process tree from Playwright's driver, has to work around the zygote (a renderer's parent) and can't tell two browsers apart.
- **Linux: strict.**
  - The renderer must be in its own user, pid and net namespaces.
  - It must carry more seccomp filters than the browser (`Seccomp_filters` in `/proc/<pid>/status`, Linux 5.9 and later).
  - The seccomp mode can't serve. In a container whose seccomp profile lets the sandbox start, such as Playwright's, every process, the browser included, is already in mode 2 (measured: 1 filter on the browser, 2 on the renderer).
  - Chromium's setuid sandbox, which Playwright's builds don't ship, would fail this check, and a kernel too old to report the count stops it with an error of its own. Neither ever passes.
- **macOS.** The check calls libSystem's `sandbox_check(pid, NULL, 0)`, which Apple exports but doesn't document.
  - The renderer must be sandboxed and the browser not. The browser's "no" also shows the call can answer no.
  - It also answers "sandboxed" for a PID with no running process, a zombie or one already reaped. So the process must still be running once it has answered (`proc_pidpath` finds it), or the check stops with an error.
  - If Apple changes the call, the comparison fails and the launch is refused. It can't pass.
- **Other operating systems** have no sandbox check, so their launches are refused.
- **Failures.** Both cases raise `SandboxUnavailableError`, with exit code 10 (API.md §7): a launch whose sandbox can't start (Chromium's `No usable sandbox!`, passed through in Playwright's error), and a launch whose sandbox the check can't prove.
  - The message names the host fix. On Linux: the AppArmor sysctl for throwaway hosts, Chromium's AppArmor profile for machines you keep, and Playwright's seccomp profile in containers.
  - An error while reading the processes (a kernel without the count, a renderer that exits mid-check) propagates as it is. The browser is closed first, so nothing passes.
  - The CLI maps the error to its exit code once a command launches a browser (#53).
- **Residual risk.** The check trusts a PID for the moments between CDP's list and its reads. A renderer that exits and whose PID another process takes in that window could be read as that process, on either OS. PIDs are allocated in sequence, so that takes a host forking tens of thousands of processes within microseconds, and the blank page gives page content no way to end the renderer. Comparing macOS's per-process unique ID before and after `sandbox_check` would close it.
- **Nothing skips the check.** `launch` takes no option and reads no setting or environment variable. The check measures the renderer itself, so every way of turning the sandbox off fails it, for every browser launched through `launch`. Every run's browser must be: #36's session launches through it. Nothing yet stops other code from calling Playwright's launch directly. The negative controls in `packages/runner/tests/test_sandbox.py` launch without the sandbox, the only place allowed to.
- **CI.** The `python` job installs Playwright's headless shell, logs `kernel.apparmor_restrict_unprivileged_userns`, sets it to 0, and runs the browser tests within `pytest --cov`. A runner without the setting fails the job.

## Amendment (2026-09-30): a browser running as root (#37)

Chromium won't start its sandbox in a browser that runs as root, a container's default user. Its zygote logs "Running as root without" followed by the switch that turns the sandbox off, and Playwright raises the same `TargetClosedError` it raises for `No usable sandbox!`. `launch` now recognizes that line too and raises `SandboxUnavailableError` (exit code 10), whose message says to run the runner as a non-root user. Before this, the error passed through as Playwright's own.

## Amendment (2026-10-01): the browser session (#36)

Every browser a run uses comes from a **browser session** (CONTEXT.md): one for each attempt or replay, opened by `aqa_runner.browser_session.open_browser_session`, for explore and replay alike. It launches through `launch`, so the sandbox check runs for every session. #44's document-origin checks will live in it too.

- **The browser's environment is empty.** `launch` passes Chromium `env={}`; Playwright's default is the runner's own environment ([`env`](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-option-env)). The `Chromium` protocol takes `env` for this, and `launch` still takes no option. No Chromium process holds the runner's provider keys, cloud credentials or `AQA_SECRET_*` values, and no proxy variable of the host's can steer the browser. The session's tests read each Chromium process's environment (`/proc/<pid>/environ` on Linux, `ps -E` on macOS, with a positive control for the reader), in CI on Linux and locally on macOS. The hosted runner image isn't covered yet: the spike's image has no fonts (LAB_NOTES, 2026-10-01), and its `HOME=/tmp` no longer reaches the browser.
  - *Residual risk:* Playwright's driver (a Node process) and the runner itself keep the runner's environment. They run as the same user as the browser, so an exploit that escapes the sandbox can read them; the sandbox, and on hosted runs the VM, are the defences there. The empty environment stops the accidental paths: inherited variables, crash reports and child processes.
- **Fresh state.** Each session launches its own browser. Playwright gives each launch a new, randomly named profile directory (`playwright_chromiumdev_profile-*` in the temp directory) and deletes it when the browser closes, so the session makes none of its own. The page runs in a new context, never a persistent one, which refuses downloads (`accept_downloads=False`; Playwright accepts them by default, [`accept_downloads`](https://playwright.dev/python/docs/api/class-browser#browser-new-context-option-accept-downloads)). Blocking service workers belongs to #43.
- **Browser settings.** `aqa_core.browser.BrowserSettings` holds the five settings, with ADR-0025's pins as its defaults. The session applies them to its context, and the caller may pass its own. #39 merges the project's and the spec's overrides into one and validates it, and #45 and #46 record and replay it.
- **Element refs.** The snapshot is Playwright's AI mode ([`page.aria_snapshot(mode="ai")`](https://playwright.dev/python/docs/api/class-page#page-aria-snapshot), added in 1.59), which gives refs to elements that are visible and receive pointer events. Playwright's built-in `aria-ref=` selector engine resolves them against each frame's latest snapshot; it isn't in Playwright's public docs, so the session's tests pin its behavior on 1.63.
  - *Why not Playwright's refs:* they can name a different element in a later snapshot, and otherwise an old one names nothing and the action waits out its timeout (LAB_NOTES, 2026-10-01). Page text can also write `[ref=e2]` wherever Playwright renders text.
  - *Decision:*
    - The session renumbers each element's own ref, the last token of its line's key (page text can't end a key that way), to `e<n>`, a number it never gives again. Every other `[ref=` in the snapshot, which only page text writes, reads `(ref=`; a plain replacement keeps the cost linear in whatever text a page holds.
    - A snapshot retires the earlier refs before it calls Playwright, which may store a snapshot the session never sees. Snapshots and lookups take turns, so no snapshot replaces Playwright's while a ref is being resolved against it.
    - `locate(ref)` resolves only the current snapshot's refs and returns the element itself, which never becomes another element. A ref from an older snapshot, one never given and one whose element, document or frame has gone each raise `RefError`, and no other string reaches a selector.
    - Only the session takes accessibility snapshots of its page, since any other snapshot changes what Playwright's refs resolve to. Keeping the page private to the session is for #44 and #53, which add its observations and actions.
  - *Rejected:* refs prefixed with their snapshot's number (`s3e12`), which need no map but lengthen every ref and expose frame structure, and Playwright's refs unchanged, which can't refuse a stale ref.
  - *Not decided here:* which frame each ref came from, which #44's checks on cross-origin frames need.

## Amendment (2026-10-01): the start URL (#89)

A run's first navigation goes to its **start URL** (CONTEXT.md): the start origin followed by the spec's `start_url` as written. `aqa_core.project.start_url(spec, start)` builds it; the executor's first navigation (#46), explore's and the confirmation replay's (#53) call it rather than building it.

- **Joined as text,** never percent-decoded and never resolved. The start origin has no path, and `start_url` opens with one `/` that no `/` or `\` follows, so a URL parser ends the host at that `/`. Since `start_url` also has no empty, `.` or `..` segment (`%2e` is a dot; DATA_MODEL §6), the path the parser reads, dot segments resolved, doesn't start with `//`. `tests/test_start_url.py` checks both with Chromium's own `URL` parser, against DNS, IPv4 and IPv6 start origins: for `/`, `/%2f%2fevil.test`, `/..;/x` and `//` in a query or a fragment, and for every `start_url` the spec parser accepts of up to 4 characters after the `/` from an alphabet of separators, dots, percent escapes and letters, plus the backslash and whitespace it must keep refusing. Controls show it catches a URL on another origin and a path starting `//`.
- **Why:** some accepted values are safe only as text. A browser keeps `%2f` encoded, but decoded, `/%2f%2fevil.test` is `///evil.test`. Joined to the start origin, that keeps the origin but gives the path `///evil.test`, which a parser resolving the path again, Chromium included, reads as the host `evil.test`. `/..;/x` is no dot segment to a browser, and `//` in a query or fragment names no host.
- *Rejected:* resolving the path, with Python's `urljoin` or against a Playwright context's [`base_url`](https://playwright.dev/python/docs/api/class-browser#browser-new-context-option-base-url), which `page.goto` applies with the `URL()` constructor (`constructURLBasedOnBaseURL` in 1.63's driver). On accepted values both give the same URL, but safety would then rest on a second parser agreeing with the browser's, not on `start_url`'s rules alone. Callers pass the absolute start URL, never the path; `page.goto` re-serializes it with Node's `URL()`, which keeps an absolute URL's origin.
- **Compiled `navigate` paths** (DATA_MODEL §7) are joined to the start origin by the same rule: their single leading `/`, then the path as written, never decoded. One that breaks `start_url`'s segment rule can read as a path starting `//`: `/..//evil.test` reads as `//evil.test`. #45 validates them and #46 joins them.

## Amendment (2026-10-01): what the sandbox check observed (#81)

The spike's trial reported only whether the check proved the sandbox, so #38's outcome ADR could cite a boolean but not the measurements behind it. The check now reports what it read alongside its reasons.

- **What it reads.** `SandboxObservations` holds the browser process and each renderer the check compared with it. On Linux each is a `LinuxProcess`: its PID, its `user`, `pid` and `net` namespace links, and its `Seccomp_filters` count. On macOS each is a `MacProcess`: its PID, and whether `sandbox_check` reported it sandboxed. Nothing new is read.
- **The same reads as the reasons.** `check_processes` returns the observations with its reasons. It reads each process once and compares exactly those reads, so an observation can't vouch for a renderer the check refuses: the report never comes from a second read or a subset. An OS with no sandbox check reads nothing (`None`).
- **How the observations leave the check.**
  - `launch_with_observations(chromium)` runs `launch`'s steps and also returns the observations. A check that read nothing has proved nothing, so a browser it returns always comes with them. `launch` delegates to it and still returns only the browser, so its callers and the existing tests are unchanged.
  - A refusal carries them in `SandboxUnavailableError.observed`. That is `None` when the check read nothing: the sandbox couldn't start, so no browser ran, or the OS has no sandbox check.
  - Neither function takes an option or reads a setting, so nothing skips the check.
  - *Rejected:* `launch` returning a pair, which would change every caller; and reading the processes again after `launch`, which would observe other renderers than the ones judged, and finds nothing once a refused browser is closed.
- **The spike's trial** launches through `launch_with_observations`, where ADR-0008's 2026-09-30 amendment names `launch`. It reports the observations on success and on refusal in a new top-level key, `sandbox_observed` (spikes/hosted-chromium/README.md). Every existing key keeps its value, so trials recorded before this change stay comparable.
  - *Rejected:* nesting the observations under `sandbox`, which would change that key's value.
- **What the check decides is unchanged.** `packages/runner/tests/test_sandbox.py`'s existing tests pass unedited, its negative controls included.

## Amendment (2026-10-01): only `launch` starts Chromium (#77)

This supersedes the 2026-09-30 amendment's (#35) "Nothing yet stops other code from calling Playwright's launch directly."

- **The gate.** `tests/test_chromium_launches.py` runs within `uv run pytest --cov`. It parses every module in `packages/*/src` and refuses each call of, or reference to, a method that launches or connects to Chromium outside `packages/runner/src/aqa_runner/sandbox.py`. Its message gives the file and line, and names `aqa_runner.sandbox.launch` as the fix. A reference counts as well as a call, so `functools.partial(playwright.chromium.launch)` and a bound method passed on are refused too.
- **The sandbox module** is the only module in `packages/*/src` left alone, and only for `launch`'s own call: the test also requires `chromium.launch` to be the module's only Chromium start, so a second launch or a connection added there fails.
- **What counts.** Playwright's `BrowserType` starts or attaches to a browser through four methods: `launch`, `launch_persistent_context`, `connect` and `connect_over_cdp` ([`BrowserType`](https://playwright.dev/python/docs/api/class-browsertype), 1.63).
  - Only `BrowserType` has the last two, so they count on any object.
  - `launch` and `connect` are common names (`sqlite3.connect`, a socket's `connect`, `aqa_runner.sandbox.launch` itself). They count only on an object spelled as Chromium's `BrowserType`: an attribute `chromium` (`playwright.chromium`), a name `chromium`, or `playwright["chromium"]`.
- **Residual risk.** The test reads spelling, not types, so review has to catch what it can't see:
  - a `BrowserType` under another name or attribute (`browser_type = playwright.chromium`, then `browser_type.launch()`; `self._chromium.launch()`);
  - one reached through `getattr`;
  - an unbound call through the class (`BrowserType.launch(playwright.chromium)`).

  A type-aware check through mypy's build API would see them too. It was rejected because it ties a test to mypy's internals and runs a full type check inside pytest.
- **Scope.**
  - The package tests aren't scanned. The negative controls in `packages/runner/tests/test_sandbox.py` launch without the sandbox, and the tests' doubles wrap Playwright's launch.
  - `spikes/` and `bench/` aren't scanned either. The spike's trial launches through `launch`. `bench/harness/toggle_checks.py` launches Chromium itself in the checks image, with an empty environment; ADR-0023's 2026-10-01 amendment (#77) records why.
  - Firefox and WebKit launches aren't covered, because nothing here launches them.
- **A test, not a policy_guard rule.** A guard rule would refuse each edit in Claude Code, but the guard matches lines, and every guard change needs the maintainer's approval. CI's pytest binds every agent and person, and an AST walk matches calls and references as such (#77's triage).
