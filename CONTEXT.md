# Agentic QA Platform

An agent explores a plain-English spec once, compiles it into a deterministic script, replays that script with no LLM calls, and heals on UI drift by telling UI that merely moved apart from expectations that no longer hold. This file fixes the words; formats, schemas and rules live in the docs that own them (AGENTS.md §2).

## Language

### Specs

**Spec**:
A Markdown file in the customer's repo that states one user goal in structured plain English: preconditions, optional step hints, expectations and invariants. The repo copy is canonical.
_Avoid_: test case, scenario

**Project config**:
The committed file of project-wide settings next to a project's specs: model roles, browser settings, egress hosts and test-secret bindings. The directory that holds it is the project's **spec root**, under which every spec ID is unique.

**Expectation**:
One observable claim in a spec's `expect` list about what must be true once the goal is done. Only a human edit to the spec can change an expectation.
_Avoid_: expect item, clause

**Subject**:
What an expectation is about, such as "jake's comment" or "the Pay button". It becomes a target's meaning when the spec is explored, and replays don't re-check it.

**Claim**:
Everything an expectation says about its subject: its text, state, position, destination or count. The expectation's assertions must establish all of it.

**Invariant**:
A condition every run checks regardless of the spec's expectations: no console errors, no uncaught exceptions, no HTTP 5xx responses and no broken images. Each is a separate invariant, so an uncaught exception is not also a console error. A spec inherits all of them and can disable individual ones.

**Probe**:
A read-only endpoint a spec declares so it can check app state the UI can't show, such as "no order was created".

**Reset hook**:
An optional endpoint a spec declares to put the app in the spec's starting state. Every attempt starts by calling it, the first included, so a spec that declares one can run repeatedly.

**Start origin**:
The origin a run starts at: the invocation's `--url`, or the project config's base URL when the invocation gives none. Never the spec's.

**Start URL**:
Where a run's first navigation goes: its start origin followed by the spec's `start_url` path.
_Avoid_: "start URL" for the spec's `start_url`, which is only a path

**Allowed origins**:
The origins a run may navigate to and act on: the run's start origin plus any the spec lists. Test secrets can be bound only to allowed origins.

**Subresource host**:
A host the project lets pages load resources from, such as a CDN or font host, without making it an allowed origin: the agent can't navigate there, and no test secret can be bound to it.
_Avoid_: calling it an allowed origin

**Expected-blocked host**:
A host the project declares that pages may try to reach but the browser must never load from, such as an analytics host. Its egress blocks are expected, so their direct symptoms don't count against invariants.

### Compiled scripts

**Compiled script**:
The committed, deterministic result of a successful exploration of one spec: its targets, ordered steps and assertions.
_Avoid_: compiled spec, test script, Playwright script

**Target**:
A named element of the app under test, defined by its meaning together with its binding. A meaning says what the element is for and where it sits ("the payment step's submit button"), never its current label; it is fixed once compiled, and only the binding can be healed.
_Avoid_: element, selector. Don't use "target" for the app or URL being tested; say "app under test".

**Locator**:
One way of finding a target on the page, such as by role and accessible name or by test ID. A target has several, tried in order until one gives the unique match its use needs: actionable for a step, present on the page for an assertion.
_Avoid_: selector

**Element ref**:
A short-lived handle to one element in one accessibility snapshot, such as `e12`, that the agent's tools act on. A browser session never gives the same ref twice, and refuses a ref from an older snapshot. Compiling turns each ref on the explored path into a target.
_Avoid_: using "ref" for a locator or a target

**Binding**:
The link between a target's meaning and the locators that currently find it. A binding either resolves or doesn't; heals repair bindings, never meanings.

**Assertion**:
A compiled check that establishes all or part of an expectation's claim, never a weaker proxy.
_Avoid_: using "expectation" for the compiled check

**Coverage plan**:
The first thing exploring a spec produces, from the spec alone and before the browser opens: each expectation's subject and the checks that would establish its claim, or why no check can, plus any condition the goal requires, such as checking after a reload. It stays fixed for the whole explore run, and an expectation it can't cover fails compilation by name.
_Avoid_: test plan

**Replay-safe step**:
A step whose `side_effect` flag is false: it can run again without changing app state, such as navigation, reading or an idempotent fill. Continuation may re-execute it to rebuild the page.

**Side-effect step**:
A step whose `side_effect` flag is true: it changes app state and can't safely run twice, such as a submit, purchase or delete. Within an attempt it is never re-executed automatically, a new attempt repeats it only after the spec's reset hook succeeds (the one exception is a single confirmation replay the person running explore allows with `--confirm-repeat` when the spec has no reset hook), and a heal may re-bind its target but can't add, remove or otherwise change it.

### Runs

**Run**:
One execution of one spec version in one mode (explore, strict or verified), whether in CI, on a hosted runner or locally.

**Explore**:
The mode for a spec that has no compiled script yet: the agent drives the browser until every expectation's planned checks pass, then compiles the path it took and proves it with a confirmation replay.

**Confirmation replay**:
The strict replay that ends every explore run: in a fresh browser, after the spec's reset hook if it declares one, the newly compiled script must pass every assertion and every invariant the spec keeps enabled before it is written. A script whose path has side-effect steps but whose spec has no reset hook is written without one, marked unconfirmed, unless the person running explore allows the repeat.
_Avoid_: verification run, since "verified" names a replay mode

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
The record of an action a runner writes before dispatching it (under the current lease, when the run has one) and completes afterwards. An unresolved step intent on a side-effect step means its outcome is unknown.
_Avoid_: bare "intent", since the healer can never observe author intent (see `drift_consistent`)

**Lease**:
The dispatcher's grant that lets one runner invocation record progress for a run. Every continuation gets a new lease, and writes under a superseded lease are rejected.
_Avoid_: lock

**Continuation**:
Resuming a hosted run that hit its time limit, in a fresh invocation with a new lease: browser storage is restored and replay-safe steps are re-executed up to the last completed step.
_Avoid_: retry

**Non-resumable**:
The state of a run that can't safely continue because a side-effect step's outcome is unknown. The run ends as errored, with evidence.

**Run record**:
The directory a local run writes what it leaves behind to, such as the coverage plan and the cost record of every model response: under its spec root's `.aqa/runs/`, which git ignores.

**Attempt**:
One pass through a run from step 1, in a fresh browser and after the spec's reset hook if it declares one. The report keeps every attempt.

### Healing and verdicts

**Drift**:
A binding that doesn't resolve: within the wait budget, no locator gives the unique match its use needs, for a step or for an assertion. Drift sends the run to heal and says nothing yet about whether the app is broken.

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
The change a heal makes to a compiled script. It may touch only target locators and non-side-effect steps, never assertions, target meanings, side-effect steps, `side_effect` flags, browser settings, the coverage plan or invariants.

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
A run on our runners, started from the dashboard. It may navigate to and act on verified domains only; which subresource hosts it may load from is decided at M6 (#29).

**Runner**:
The container that executes one run: the run graph plus a browser. It holds no database or cloud credentials and does all its I/O through the API.

**Browser session**:
The fresh browser that one attempt or replay of a run uses. It is launched through the sandbox check with an empty environment, the run's browser settings and a profile of its own, sends its pages' HTTP(S) and WebSocket requests through routing and the run's egress proxy, refuses service workers, and its accessibility snapshots give the element refs the agent acts on.
_Avoid_: "session" alone where a MicroVM's session could be meant

**Sandbox check**:
The proof, before a run loads any page, that the browser's renderer is confined by Chromium's sandbox, made by comparing a renderer process with the browser process. It is the first half of the fresh-VM predicate that hosted compute must meet.
_Avoid_: startup check, sandbox probe

**Run token**:
The short-lived credential a runner uses for one run under one lease. Only a hosted-execution token, issued for a hosted run, may also read that run's test secrets and provider key.

**Verified domain**:
A hostname the organization has proven it controls. Hosted runs may navigate to and act on verified domains only.

**Egress proxy**:
The browser's only way out: an in-runner forward proxy that passes only allowed origins and subresource hosts, resolves each name itself and pins its first answer that passes the IP policy for the whole run, and refuses link-local and metadata addresses, and non-public ones outside the start origin and declared private origins.

**Egress block**:
A request from the run's browser to a host that is neither an allowed origin nor a subresource host, which the run refuses. Unless the host is expected-blocked, an egress block keeps the run from passing without making it a finding.
_Avoid_: network error

**Runner-side request**:
A request the runner sends itself, not the browser: the reset hook's or a probe's. It goes to an allowed origin only, under the run's DNS pins and IP policy, carries no cookies and never follows a redirect.

**Policy event**:
A document from an origin the run doesn't allow, which the browser session found where it was about to observe or act, or in a popup, or a URL off the allowed origins that `navigate` refused before anything was requested. A document can be on a subresource host, on Chromium's error page after an egress block, or anywhere else. The session observes and acts on nothing there and records the event; only navigating back to an allowed origin, or restarting, moves on. A frame from another origin inside an allowed page is left out of observations without one.
_Avoid_: egress block (that is a request the proxy refused)

**Test secret**:
A named test credential, bound to allowed origins and a field, that a spec references by name. The browser fills it only into that field on those origins, and our tools never give its value to a model.

**Provider key**:
An organization's own LLM provider key, stored encrypted and decrypted only in memory during a hosted run.
_Avoid_: API key (that is the project credential for the CLI outside GitHub Actions)

**Model role**:
The job a model does in a run: navigator, verifier, healer or vision fallback. Each role declares the capabilities its model must have.

**Model router**:
The runner's one way to call a model: it picks the model for a role, calls it through its provider's client, returns a cost record for every response, and calls the role's fallback when a model refuses. A run that makes no model call builds no client.

**Cassette**:
A recorded exchange with a model provider that tests replay, matched by a hash of the prompt, so a changed prompt fails the test until someone re-records it with a key.

**Cost record**:
What one model call leaves behind: its role, mode, model, token counts, latency and cost, the price map version in force and the rates it applied (the map's or the config's), and its status (DATA_MODEL §2). A billed response is always recorded, whatever became of it.

**Price map**:
The vendored copy of LiteLLM's model price and capability file, pinned to an upstream commit and its sha256. It is checked whenever it loads and refreshed only by its script, in a reviewed pull request.
_Avoid_: the `litellm` package, which we never install

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
