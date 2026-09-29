# Agentic QA Platform

An agent explores a plain-English spec once, compiles it into a deterministic script, replays that script with no LLM calls, and heals on UI drift by telling UI that merely moved apart from expectations that no longer hold. This file fixes the words; formats, schemas and rules live in the docs that own them (AGENTS.md §2).

## Language

### Specs

**Spec**:
A Markdown file in the customer's repo that states one user goal in structured plain English: preconditions, optional step hints, expectations and invariants. The repo copy is canonical.
_Avoid_: test case, scenario

**Expectation**:
One observable claim in a spec's `expect` list about what must be true once the goal is done. Only a human edit to the spec can change an expectation.
_Avoid_: expect item, clause

**Invariant**:
A condition every run checks regardless of the spec's expectations: no console errors, no uncaught exceptions, no HTTP 5xx responses and no broken images. Each is a separate invariant, so an uncaught exception is not also a console error. A spec inherits all of them and can disable individual ones.

**Probe**:
A read-only endpoint a spec declares so it can check app state the UI can't show, such as "no order was created".

**Reset hook**:
An optional endpoint a spec declares to return the app to its starting state, so a non-resumable run can start again as a new attempt.

**Allowed origins**:
The origins a run's browser may reach: the spec's start origin plus any it lists.

### Compiled scripts

**Compiled script**:
The committed, deterministic result of a successful exploration of one spec: its targets, ordered steps and assertions.
_Avoid_: compiled spec, test script, Playwright script

**Target**:
A named element of the app under test, defined by its meaning ("Pay button on the payment step") together with its binding. The meaning is fixed once compiled; only the binding can be healed.
_Avoid_: element, selector. Don't use "target" for the app or URL being tested; say "app under test".

**Locator**:
One way of finding a target on the page, such as by role and accessible name or by test ID. A target has several, tried in order until one gives a unique, actionable match.
_Avoid_: selector

**Binding**:
The link between a target's meaning and the locators that currently find it. A binding either resolves or doesn't; heals repair bindings, never meanings.

**Assertion**:
The compiled check that establishes an expectation. What an expectation is about becomes a target's meaning; everything it claims about that target needs at least one assertion that actually establishes it, never a weaker proxy.
_Avoid_: using "expectation" for the compiled check

**Replay-safe step**:
A step whose `side_effect` flag is false: it can run again without changing app state, such as navigation, reading or an idempotent fill. Continuation may re-execute it to rebuild the page.

**Side-effect step**:
A step whose `side_effect` flag is true: it changes app state and can't safely run twice, such as a submit, purchase or delete. It is never re-executed automatically, and a heal may re-bind its target but can't add, remove or otherwise change it.

### Runs

**Run**:
One execution of one spec version in one mode (explore, strict or verified), whether in CI, on a hosted runner or locally.

**Explore**:
The mode for a spec that has no compiled script yet: the agent drives the browser until every expectation is verified, then compiles the path it took.

**Replay**:
Executing a compiled script, in strict or verified mode.
_Avoid_: "replay" for looking back at a finished run (that's the run viewer) or for re-sending events or requests

**Strict replay**:
The default replay mode, used in CI. It makes zero LLM calls, so every assertion must be deterministic.

**Verified replay**:
An opt-in replay mode that adds model-assisted visual checks for expectations that ask for them. Its cost and results are always reported apart from strict replays.

**Run viewer**:
The step-by-step view of any finished run, explore runs included, with its screenshots, evidence and verdicts. It is a dashboard screen, and `aqa report` opens the same view locally.
_Avoid_: replay, replay viewer, run replay

**Step intent**:
The record of an action a runner writes before dispatching it, under the current lease, and completes afterwards. An unresolved step intent on a side-effect step means its outcome is unknown.
_Avoid_: bare "intent", since the healer can never observe author intent (see `drift_consistent`)

**Lease**:
The dispatcher's grant that lets one runner invocation record progress for a run. Every continuation gets a new lease, and writes under a superseded lease are rejected.
_Avoid_: lock

**Continuation**:
Resuming a hosted run that hit its time limit, in a fresh invocation with a new lease: browser storage is restored and replay-safe steps are re-executed up to the last completed step.
_Avoid_: retry

**Non-resumable**:
The state of a run that can't safely continue because a side-effect step's outcome is unknown. The run ends as errored, with evidence.

**Attempt**:
One pass through a run from step 1. A reset hook starts a new attempt with a fresh browser, and the report keeps every attempt.

### Healing and verdicts

**Drift**:
A binding that doesn't resolve: no locator gives a unique, actionable match within the wait budget, for a step or for an assertion. Drift sends the run to heal and says nothing yet about whether the app is broken.

**Heal**:
The part of a run that handles drift: it re-observes the page, repairs bindings and non-side-effect steps, and classifies the outcome as a verdict.
_Avoid_: auto-fix

**Verdict**:
The classified outcome of a step: `pass`, `drift_consistent`, `expectation_violated` or `inconclusive`. Every verdict cites evidence.

**`drift_consistent`**:
The verdict that the UI changed, and that after repairing bindings and non-side-effect steps every assertion evaluates and passes and every invariant holds. It produces a heal proposal and claims nothing about why the UI changed.
_Avoid_: intentional change, expected change

**`expectation_violated`**:
The verdict that an assertion whose target resolved evaluated false, or an invariant failed, even after the best repair. It produces a finding.

**`inconclusive`**:
The verdict when neither `drift_consistent` nor `expectation_violated` can be established within the budget. It goes to a human.

**Heal patch**:
The change a heal makes to a compiled script. It may touch only target locators and non-side-effect steps, never assertions, target meanings, side-effect steps, `side_effect` flags or invariants.

**Heal proposal**:
A heal patch awaiting a human decision, tied to the PR head commit and compiled-script version it was computed against. No heal patch is committed without a recorded human acceptance.
_Avoid_: patch proposal, suggested fix

**Stale**:
The state of a heal proposal whose PR head or compiled script has changed since it was computed. A stale proposal can't be accepted; a fresh run replaces it.

**Accept heal**:
The human action that commits a heal proposal to the PR branch, after checking the person's write access and that the proposal isn't stale.

**Finding**:
The report of an `expectation_violated` verdict: category, title, repro steps and evidence. Triage decides whether it is a real bug.
_Avoid_: bug report, issue

**Evidence**:
What a verdict cites: before and after screenshots, an accessibility-snapshot diff, network and console logs, and the model's rationale.

**Triage label**:
A person's judgment on a verdict (correct, should have been `expectation_violated`, should have been `drift_consistent`, or not a bug). Triage labels are the ground truth for evaluating the healer.

### Execution and trust

**CI run**:
A run on the customer's own CI, using the customer's LLM key. It can never retrieve test secrets or provider keys stored with us.

**Hosted run**:
A run on our runners, started from the dashboard. Its browser may only reach verified domains.

**Runner**:
The container that executes one run: the run graph plus a browser. It holds no database or cloud credentials and does all its I/O through the API.

**Run token**:
The short-lived credential a runner uses for one run under one lease. Only a hosted-execution token, issued for a hosted run, may also read that run's test secrets and provider key.

**Verified domain**:
A hostname the organization has proven it controls. Hosted runs may only reach verified domains.

**Test secret**:
A named test credential, bound to allowed origins and a field, that a spec references by name. The browser fills it only into that field on those origins, and our tools never give its value to a model.

**Provider key**:
An organization's own LLM provider key, stored encrypted and decrypted only in memory during a hosted run.
_Avoid_: API key (that is the project credential for the CLI outside GitHub Actions)

**Model role**:
The job a model does in a run: navigator, verifier, healer or vision fallback. Each role declares the capabilities its model must have.

### Benchmark

**Case**:
One benchmark item: a planted bug or a benign change in one app, switched on by its flag.
_Avoid_: test case

**Planted bug**:
A deliberate defect in a benchmark app, in one of six categories, whose ground truth is `expectation_violated` in at least one spec.

**Benign change**:
A deliberate UI change in a benchmark app, such as a moved, restyled or relabeled element, under which every expectation still holds. Its ground truth is `drift_consistent` in each spec whose bindings it breaks.
_Avoid_: intentional change

**Flag**:
The opaque ID that switches one case on. It never names the case, because the app's flag list reaches the browser.

**Ground truth**:
The verdict a case should produce in each spec it is scored on. A spec in which the case only blocks a step isn't scored, because its outcome depends on what healing may do. Ground truth lives only in the benchmark manifest and never travels in traces or reaches the app under test.

**Split**:
The partition a case belongs to: dev, for development and tuning, or test, frozen before tuning and run only for release numbers.

**Family**:
Cases on the same code path. A whole family sits in one split, so near-duplicates can't leak test cases into tuning.

**Burned**:
The state of a test split whose results have influenced a design decision. The next release needs a freshly written test split.

**Pilot**:
The first, development-only slice of the benchmark: a few cases on one app, used to validate the compiler and the heal contract before the full benchmark. Pilot cases belong to the dev split.

**Flaky**:
A replay that fails and then passes on retry. It is recorded as flaky, never as passed.
