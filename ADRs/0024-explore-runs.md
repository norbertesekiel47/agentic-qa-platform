# ADR-0024: Explore runs: coverage plan, stateless navigator and confirmation replay

- Status: Accepted
- Date: 2026-09-29

## Context
M1 builds `aqa explore` (ROADMAP). DATA_MODEL §7 says what exploring must produce: a compiled script in which every expectation has an establishing check. It didn't say how a run gets there, or what a successful compile proves. The M0 pilot sets the bar:
- five Conduit specs whose compiled checks must match `bench/apps/conduit/qa/REVIEW.md`;
- two benign cases that heal only if targets are compiled for meaning (#20);
- absolute counts ("exactly 2 comments", "jake now has 6") that hold only from the seeded state, which exploring itself changes.

More constraints:
- **Coverage is the hard judgment.** Every claim needs a check that establishes it, never a weaker proxy (ADR-0003 addendum).
- **No automatic repeats.** A side-effect step that already ran must never be repeated automatically (AGENTS.md §6).
- **Context limits.** Accessibility snapshots run 5–20K tokens each, and Claude Haiku 4.5 has a 200K context window.
- **Model API behavior (2026-09).** Claude Sonnet 5.5 and Opus 5.5 reject a forced tool choice. For accounts created on or after 2026-08-31, they also reject requests whose earlier turns were edited.
- **Untrusted pages.** Everything the agent observes comes from the app under test (SECURITY §4).

The decisions came out of a design review on 2026-09-29, followed by an adversarial review by Codex (gpt-6-astra). This ADR includes the findings accepted from that review.

## Options
- **Coverage:** (1) a coverage plan written first, from the spec alone; (2) the agent picks checks as it goes, and the compiler counts passing assertions.
- **Navigator memory:** (1) stateless steps, each a fresh request built from graph state; (2) one growing conversation.
- **What a compile proves:** (1) the JSON validates, and every expectation had a passing assertion while exploring; (2) that plus a confirmation replay with no model calls.
- **What gets compiled:** (1) every action of the final attempt; (2) the steps the agent names as the path; (3) a clean second pass.
- **Reset hook:** (1) before every attempt, the first included; (2) only to restart a non-resumable run (ADR-0006 amendment); (3) two hooks.
- **Bugs found while exploring:** (1) explore never reports `expectation_violated`; (2) it does, when a bound check reads false on every attempt.

## Decision

### Reset hook: before every attempt, the first included, in every mode
- A spec that declares a reset hook can run repeatedly.
- `preconditions.seed` is removed. Nothing ever defined it, and the hook's own parameters cover it.
- **Repeats wait for a successful reset.** A new attempt or the confirmation replay would repeat side-effect steps the run has already dispatched, so it starts only after the hook succeeds.
- **A post-reset attempt is fresh.** It runs every step from step 1, sign-ins included.
- **Dispatch record.** M1 records every step's dispatch locally, an intent before and a completion after, so a repeat knows which side-effect steps it would redo. Lease fencing arrives once runs upload or run hosted.

### Coverage plan first
Before the browser opens, one structured call (using the navigator role's model) turns the spec into a coverage plan. For each expectation, the plan records:
- its **subject**, which becomes a target meaning;
- its **claim**, as check types with a literal or a pattern;
- or `unsupported`, with a reason.

The plan also records conditions that the goal or the expectations require, such as "checked after a reload". Step hints stay optional.

- **Unsupported expectations fail early.** The compile fails and names the expectation, without any browsing.
- **The plan is frozen.** It is hashed and fixed for the whole explore run. Attempts may change bindings and paths, never the plan. Replanning is a separate, recorded operation that uses the spec alone.
- **The plan never sees page content.** An injected page can't change what "done" means.

### Stateless navigator steps
Each navigator turn is a fresh request built from graph state:
- the static system prompt;
- the spec and the coverage plan (a cacheable prefix);
- a compact log of actions and results;
- the model's short notes from its previous turn, delimited as data;
- the current accessibility snapshot.

The conversation never grows and is never edited. In M1 the navigator reads the snapshot only. Screenshots are kept as evidence but aren't sent to a model until hybrid perception arrives in M2. Tool choice is never forced (ADR-0007 amendment, 2026-09-29).

### Path selection and attempts
- **The agent names its path.** When the planned checks pass, the agent's `finish` call names the steps of the current attempt that form the path, dropping detours.
- **Required conditions are enforced.** The compiler rejects a path that drops a condition the plan requires, such as the reload in `favorite-article`.
- **At most the attempt budget per explore run** (default 3; see Budgets). A restart follows the reset rule above. Without a reset hook, the agent can't restart once it has dispatched a side-effect step.

### Confirmation replay
- **What it is.** The selected path is compiled, then run as a strict replay with no model calls, in a fresh browser, after the reset hook.
- **Pass condition.** Every assertion and every invariant the spec keeps enabled must pass before the file is written. Invariants count only here, never during the agent's detours.
- **A failed confirmation** is kept in the record. Its evidence goes back to the agent as feedback for the next attempt.
- **Flaky specs.** If a confirmation fails and a later one passes with the identical compiled script, the spec is flaky and explore writes nothing (TESTING §6).
- **No reset hook.** If the path has side-effect steps and the spec has no reset hook, the script is written unconfirmed, and explore exits 0 with a warning. The person running explore can pass `--confirm-repeat` instead. That authorizes one confirmation that repeats the side effects without a reset.
- **Model-assisted checks.** In M1, a spec with a `visual: model` expectation is a `spec_error`: a model-assisted check can't pass a strict confirmation. M2 defines how such specs compile and confirm before verified mode ships.

### Settling
- **Step readiness.** After each action, the run waits until that action's requests have finished and the DOM is stable, within 10 s. It doesn't use Playwright's `networkidle`, which can fire before a client-side write (ADR-0023 amendment).
- **Probes.** Reads repeat until the value is stable, within a bound.
- **Invariant observers** are installed before the first navigation and collect for the whole attempt, reloads included.
- **Before a reset**, the previous browser is stopped and its outstanding requests settle.

### Outcomes

| Outcome | Exit code | When |
|---|---|---|
| `compiled` | 0 | The confirmation replay passed, or the script was written unconfirmed |
| `spec_error` | 5 | The spec is invalid, or the plan can't cover an expectation |
| `egress_blocked` | 6 | In any phase, navigation or confirmation, the page requested a host that is neither an allowed origin, a subresource host nor expected-blocked (ADR-0026). The run ends at once, `errored` with no verdict or finding, and its record lists the refused hosts. Declare each one as a subresource or expected-blocked host, or investigate the page |
| `gave_up` | 3 | Attempts or budget ran out, the model refused with no fallback model, or the confirmation was flaky. The reasons name the planned checks that never passed and what each saw |
| infrastructure error | 10+ | See API.md §7 |

- **Explore never reports `expectation_violated`.** A bound check that read false becomes a `gave_up` reason, because explore can't tell its own mistakes from app bugs.
- **`--plan-only`** writes the coverage plan and stops.
- **No accidental overwrite.** Explore won't overwrite an up-to-date compiled script without `--force`.

### Budgets
Per explore run:
- 3 attempts;
- 40 actions per attempt;
- $3 of model spend;
- 15 minutes;
- 10 s for a target to resolve.

Exceeding any of them is `gave_up`. Each can be overridden in the project config (DATA_MODEL §9) or by a flag.

## Consequences
- **M1 absorbs work from M2:**
  - a minimal strict executor (locator resolution, bounded waits, assertion evaluation);
  - the invariant observers;
  - a local record of side-effect dispatches.

  M2 adds verdicts, heal, lease-fenced step intents and verified mode.
- **Every compiled script has passed once with no model**, except unconfirmed ones, which say so.
- **No parallel runs against a shared reset.** Specs that reset one shared app can't run in parallel against it. The benchmark already runs one case per stack (ADR-0022), and customers get the same note.
- **Cassettes stay stable.** A navigator prompt depends only on graph state, and the plan depends only on spec text (TESTING §4).
- **Injection surface.** The plan is immune by construction. Binding, navigation and secrets aren't, so M1 ships explore-focused injection fixtures (TESTING §1, SECURITY §4).
- **API.md §7 gains exit code 5.**
- **Refines earlier ADRs.** ADR-0006's reset rule (ARCHITECTURE §3.3) now applies to every attempt. ADR-0003's "compile the path it took" becomes "select and confirm the path".
