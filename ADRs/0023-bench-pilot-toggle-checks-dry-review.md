# ADR-0023: Benchmark pilot: browser toggle checks and a written dry compile review

- Status: Accepted
- Date: 2026-09-28

## Context
M0 ends when "pilot cases toggle reliably; specs pass a dry compile-rules review; apps run via `docker compose`" (ROADMAP). Two of these need a mechanism.
- **Toggle checks.** ADR-0022's self-test proves that both tiers receive a flag. It doesn't prove that a case's planted change appears when its flag is on and disappears when it is off. Most pilot changes live in the Angular frontend, so only a real browser can observe them.
- **Dry review.** The compiler arrives in M1. Until then, "passes the compile rules" (DATA_MODEL §7: every `expect` item maps to at least one check that establishes it, with no weaker proxy) can only be judged by reading the specs.

## Options
**Toggle checks:**
1. **Python checks in `bench/harness/`, run in the official Playwright Python image** at the version TECH_STACK pins (1.63.0), on the app's Compose network. The code is policed like the rest of the harness. CI must install the `playwright` package (types only, no browsers) so `mypy --strict` can check it.
2. **TypeScript Playwright Test files next to the app,** run in the Node Playwright image. No CI change, but the harness logic would sit in `bench/apps/` (unpoliced, ADR-0021) and in a second language.
3. **A manual checklist.** No code, but weak evidence for "reliably".

**Dry review:**
1. **A written table per app:** each `expect` clause, the check(s) that establish it, the probe if any, and the weaker proxy it must not compile to.
2. **A machine-checked compile plan** (JSON per spec, validated by a script). Stronger, but it is a throwaway proto-compiler written just before the real one.

## Decision
**Toggle checks: option 1.**
- `bench/harness/toggle_checks.py` runs inside `mcr.microsoft.com/playwright/python:v1.63.0-noble`, which reaches the app at its internal origin (`http://frontend`). It holds one check per case. A check resets the app through `/test-api/reset`, then drives the page and returns what it observed.
- **A check must recognise both states exactly:** the clean value and the planted value. Anything else is an error, never "absent".
- `bench/harness/toggle.py` (host side, standard library) runs cycles. In each cycle, the clean app must make every case's check report *absent*, and each case's flag must make its own check report *present*. It fails when a manifest case has no check.
- **CI:** the `guardrails` job installs `playwright==1.63.0` next to ruff and mypy, so the checks are type-checked. They don't run in CI, because they need the Compose stack.

**Dry review: option 1.** `bench/apps/<app>/qa/REVIEW.md` maps every `expect` clause to its establishing check(s) from DATA_MODEL §7. At M1, `aqa explore` must compile every pilot spec (ROADMAP), and its compiled assertions are compared with the table.

## Consequences
- **Toggle evidence is local** (it needs Docker). A pull request that adds or changes cases carries the output of a toggle run and the SHA it ran at.
- **Toggle checks are not the benchmark's oracle.** They only prove that a flag switches its planted change. Ground truth stays in the manifest, and the system under test never runs these checks.
- **Pins move together:** the Playwright image, CI's `playwright` package, and TECH_STACK.
- **Pilot flows change state** (comments, favorites, articles), so every check starts from a reset.
- **The review table is superseded at M1** by compiled output. Any disagreement between the two is an M1 finding about either the spec or the compiler.

## Amendment (2026-09-28): the checks image and Chromium's sandbox
- **Playwright's Python image `v1.63.0-noble` ships the browsers but not the `playwright` package.** `bench/harness/checks.Dockerfile` builds on it, pinned by digest, and installs `playwright==1.63.0` from `checks-requirements.txt`, which is hash-locked (`--require-hashes`).
- **The checks run with Chromium's sandbox on** (AGENTS.md §6). As root or under Docker's default seccomp profile, a sandboxed launch fails ("Chromium sandboxing failed!"). So the container runs as `pwuser` with Playwright's seccomp profile for that release, vendored as `bench/harness/chromium-seccomp.json` (Apache-2.0, `microsoft/playwright` at `v1.63.0`, `utils/docker/seccomp_profile.json`, sha256 `cc3e61ca…1cc7849`). It is Docker's default profile plus the namespace syscalls the sandbox needs, which is narrower than `--cap-add=SYS_ADMIN`. `chromium_sandbox=True` makes a launch that can't sandbox fail instead of silently falling back.
- **Upgrading Playwright** means moving the image tag and digest, the lock, and the seccomp profile together.

## Amendment (2026-09-29): every check under every flag, and fresh images
A review found two ways `toggle.py` could pass on bad evidence:
- **Cross-case contamination.** A case's flag was checked only against its own check, and the other checks ran only with every flag off, so a flag that also switched another case's change passed. Now every check runs on the clean app and under each case's flag. Under a flag, that case's check must report planted and every other check clean. A cycle runs n + n² checks for n cases: 56 for the 7 pilot cases.
- **Stale images.** `toggle.py` now builds the app's images from the working tree before its first switch (`docker compose build`, quick when nothing changed), so its evidence can't come from images of older code.

Also, the `conduit-bug-004` check now reads the article count until it stops changing (`bench/harness/polling.py`). It used to wait for Playwright's "networkidle", which has already fired by the time of a client-side navigation.

## Amendment (2026-09-29): Playwright comes from the lockfile in CI

CI's `python` job installs the workspace from `uv.lock` (ADR-0029), which includes Playwright 1.63.0 for `toggle_checks.py`'s types; `guardrails` no longer pip-installs it. As before, no browsers are downloaded and the checks don't run in CI.

## Amendment (2026-10-01): toggle.py runs under uv

`toggle.py` imports the manifest validator, which now reads specs with `aqa_core`'s parser (ADR-0030), so it is no longer standard library only and runs as `uv run python bench/harness/toggle.py` (bench/README.md). `toggle_checks.py` is unchanged: it imports neither and still runs in the checks image.

## Amendment (2026-10-01): the checks' browser skips the sandbox check and gets an empty environment (#77)

A test now refuses a Chromium launch in `packages/*/src` outside `aqa_runner.sandbox.launch`, which runs the sandbox check (ADR-0026's 2026-10-01 amendment, #77). `toggle_checks.py` still launches Chromium itself.

- **Why not through `launch`.** Installing `aqa_runner` in the checks image doesn't fit. Measured at `a631995`:
  - The image's Python is 3.12.3 (`python3 --version` in `mcr.microsoft.com/playwright/python:v1.63.0-noble`), while `aqa-runner` and `aqa-core` require Python 3.14 or later (`requires-python` in their `pyproject.toml`).
  - `aqa-runner` brings 52 third-party packages (`uv export --package aqa-runner --no-dev --no-hashes --frozen`), the model SDKs, LangChain and LangGraph among them. All of them would join the hash-locked `checks-requirements.txt`, which holds 4.
  - `launch` is async, and the checks use Playwright's sync API throughout.
- **Why the exemption is acceptable.** The checks aren't the system under test, and no run executes them. They run in a throwaway container as `pwuser` under Playwright's seccomp profile, and `chromium_sandbox=True` makes a launch that can't sandbox fail (the 2026-09-28 amendment). What remains: no sandbox check proves that the sandbox is on there.
- **An empty environment.** The launch passes `env={}`, as `launch` does (ADR-0026's 2026-10-01 amendment, #36). `BENCH_FIXTURE_PASSWORD` and the container's other variables stay out of the browser, while the checks' Python still reads the password to sign in through the API.
- **Partly enforced.** The test scans `packages/*/src` only. policy_guard refuses `chromium_sandbox=False` here as everywhere. But dropping the keyword, which leaves Playwright's default of no sandbox, or dropping `env={}`, is left to review.

## Amendment (2026-10-04): in-memory pilot rebinding (#51)

The compiled-pilot harness needs to prove that a human-written binding change restores a benign case while preserving its assertions. `bench/harness/pilot_rebinding.py` supplies that boundary; the acceptance command and real replay proof follow separately.

- **Patch format.** A JSON object contains `base_hash` and `operations`. The base is `canonical_hash(original.model_dump(mode="json"))`, covering the complete parsed compiled model, defaults and metadata included. Every operation has exactly `op: "replace"`, `path`, and `value`. Its decoded JSON Pointer must be `/targets/<existing-id>/locators`; `value` is a locator list. Unknown fields, repeated JSON keys, stale hashes, duplicate replacements, invalid locators and every other operation or path are errors. An empty operations list explicitly requests a no-op.
- **Choice.** Apply the patch to a new model in memory, then compare all fields outside the locator lists with the original. The alternative was to write a temporary compiled file and load it. In-memory application leaves the committed artifact and its acceptance history untouched, needs no cleanup, and keeps both versions available for later replay evidence. It does not record human acceptance or persist a heal.
- **Shared validation.** `aqa_core.project.parse_compiled(text, config, source=path)` owns the loader's existing strict JSON and cross-field checks without file I/O. `load_compiled` delegates to it. Rebinding uses it too, so every locator of a `not_visible` target must remain scoped, as ADR-0025 already requires. `source` only labels diagnostics. The alternative was a harness-specific scope check; that would duplicate a loader rule and could diverge as the loader evolves. Pydantic model validation alone does not enforce these cross-field checks.
- **Consequences.** The patch caller supplies the loaded project config for the existing declared-secret checks. The boundary neither reads a key nor invokes a model or browser. Tests cover exact paths and escapes, isolated input refusals, unscoped primary and fallback locators on a shared positive/negative target, positive-only unscoped replacements, immutable originals and explicit no-ops. This does not prove replay success or semantic equivalence of arbitrary human-selected locators; the later acceptance command must replay and score the candidate.

## Amendment (2026-10-04): semantic pilot results (#51)

The pilot acceptance harness compares replay behavior across attempts and against explicit manifest rows. `bench/harness/pilot_results.py` supplies pure observation and scoring; the command and real replay evidence follow separately.

- **Observation.** `observe(script, result, invariants=spec.invariants)` accepts a strictly loaded compiled script and its spec's existing settings. Frozen records retain run outcome/error code; ordered step identities, outcomes, settlement, locator indexes, misses and error presence; canonical assertion identities/outcomes/misses/stopped-at/error presence; and invariant identities/outcomes/counts. Unique failed expectation indexes and violated invariant names are separate sets. Raw errors, invariant text, traffic, policy details, URLs, timestamps and run IDs are excluded, and no mutable window is retained.
- **Completeness.** Every compiled assertion and all four invariants, disabled ones included, must appear exactly once. Step identities must form the compiled prefix, including navigation seq 0. Malformed identities or negative counts raise bounded errors. A proper prefix stays observable but is ineligible. Invariant outcomes must agree with the actual enabled settings and counts; disabled observations never count as violations.
- **Eligibility.** Infrastructure errors, egress blocks, run errors/error codes, failed or drifted steps, step/assertion error presence, unresolved or unevaluated assertions and incomplete results cannot score. Every step must settle idle: a completed step with settlement timeout is still ineligible, as is an assertion timeout. These benchmark acceptance rules do not change executor outcomes.
- **Choice and consequences.** Compare immutable semantic values rather than complete `RunResult` objects, whose fresh run IDs and mutable windows differ between equivalent attempts. Equality alone cannot establish a clean replay: clean acceptance additionally requires eligibility, a passed run and empty failure sets. The later acceptance command must enforce that rule over three actual strict replays. This pure module neither launches a browser nor constructs a model, and proves no benchmark result by itself.

- **Exact scoring.** Only `compare(observation, expected)` receives a validated manifest row. It requires the same spec ID, eligibility, the appropriate passed/failed run outcome, and equality of the expectation-index and invariant-name sets independently. Shared assertions contribute their expectation index once. `expectation_violated` requires a nonempty failure set; `drift_consistent` requires both sets empty. Subset matching and converting errors into expected failures were rejected because either could hide an extra failure or missing proof.
- **Unscored pairs.** No omitted manifest entry becomes an implicit empty expectation. Clean replay validation and explicitly unscored reporting belong to the acceptance command; neither requires fabricating an `Expected` row. Manifest answers never enter `observe` or the replay interface. Assertion locator provenance is not fabricated here and remains later work.

## Amendment (2026-10-04): pilot input admission (#51)

The acceptance command replays several pilots under every case flag, so a bad input found halfway would leave the app switched and reset for nothing. `bench/harness/pilot_inputs.py` admits every selected input before any of that starts; the command, reset and replay follow separately.

- **Interface.** `load_pilots(qa_root, compiled_dir, selected_ids)` returns a frozen `PilotInput` per selected spec: the spec, the project config, the strictly loaded compiled script, the start origin, which is the config's `base_url` since the harness takes no `--url`, and an optional `ResetRequest`. `validate_pilot(spec, config, script, *, source)` applies the same admission to a script already in memory, such as a rebound candidate. No manifest row, case, flag, expected outcome or secret value enters.
- **Selection.** No selection means every spec, by ID; an explicit selection keeps its order. Duplicate and unknown IDs, and a project with no specs, are refused before any path is built from an ID. A selected script is `<compiled_dir>/<id>.json` and must resolve inside `compiled_dir`; a missing script for a spec not selected is no error, but every spec file and the config under `qa_root` must load, selected or not.
- **Current spec and coverage.** The script must name the spec and carry the hash the parser computes for it now. Coverage lists every expectation once, in order, each row with exactly the assertions that name it, and some step satisfies every required condition. The check lives in the harness. The alternative, the generic loader checking it for every caller, waits for its own approved change. Coverage is structural: that an assertion establishes its expectation's prose is the read-article proof's to show.
- **What the executor runs.** The conditions `aqa_runner.executor` checks before it opens anything are repeated with public helpers rather than its private function: a press is one key (`one_key`), a `fill_secret` names a secret the selected spec references (`secret_references`), every probe a check or a baseline names is declared by the spec, and `visible_unoccluded` has `in_viewport: true`. All nine checks and seven actions are otherwise admitted. Replay uses the script's recorded browser settings, so no rule compares them with the current overrides.
- **Reset.** A reset hook is `POST` and a path held to start_url's rules, ASCII and without a fragment, joined to the start origin as text, never decoded. Any step with a side effect requires one, since the command repeats every script; there is no `--confirm-repeat` exception here.
- **Secrets.** `bound_secrets(spec, start)` runs for each selected spec, so a missing or short value, a binding the run doesn't allow, or protocol logging is refused up front. The values are read to check them and never kept: the replay must bind them again just before its browser starts. A secret the config declares but the spec doesn't reference neither permits a `fill_secret` nor needs a value.
- **Errors.** A refusal is a ValueError reading `<path>: invalid selection`, `invalid compiled input` or `invalid pilot input`, raised only after the underlying error is dropped, so neither its text nor the error itself (on `__context__`) travels with the refusal: loader and reset messages can repeat spec or compiled text, and a secret value that fails to encode is quoted by its error. The cost is that the message doesn't say which rule failed.
- **Limits.** Admission builds nothing, switches no flag, sends no reset and starts no browser or replay; its tests trap those entry points and any socket or subprocess use. It doesn't prove the command admits everything before its first change of state, which the command's own tests must show.
