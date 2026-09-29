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
