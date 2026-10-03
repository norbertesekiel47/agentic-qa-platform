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

## Amendment (2026-10-02): the minimal strict executor (#46)

Building the executor (#46) settled choices the Consequences above left open. DATA_MODEL §7 holds the rules ("Checked by the loader", *Text parameters*, *Replay outcomes*); this records why. #46 lands in three pull requests, and each adds its part here.

### Reading a compiled script
`aqa_core.project.load_compiled` reads the file: JSON that repeats no key, the same text validated in JSON mode, then the parts checked against each other and against the project config's secrets.
- **An invalid compiled script is a spec error.**
  - *Options:* `SpecError` and exit 5, or an error type and exit code of its own.
  - *Chosen:* the first. The script is committed beside its spec and reviewed with it, and its problems are reported as a spec's are: each with its file and key, all at once. API.md §7's exit 5 now names the compiled script.
- **Repeated keys are found by walking the parsed JSON with a stack.** Python 3.14's `json` reads arrays nested 100,000 deep, so recursion would run out first, and a path copied at every level made a 100,000-deep file take 10 s (LAB_NOTES, 2026-10-02). The walk links each value to its parent's place, and refuses objects and arrays nested more than 256 deep, beyond any script and beyond pydantic's own limit of about 200.
- **A `not_visible` check's target scopes every locator.** Unscoped, the target is absent from any page that lacks a match, a wrong page or an app's 404 included, so the check would pass whatever the page. That is a broken script, so the loader refuses it, rather than resolution reporting drift. The locator generator (#52) scopes such targets. A scope every page has, such as `html` or `body`, defeats the rule, and no list of such selectors could be complete (`body > *`), so choosing a scope particular to the page stays the generator's job.

### Bounded text searches
A `pattern` comes from the script and the text from the page, and Python's `re` can backtrack for exponential time: `(a+)+$` against forty `a`s and a `b`. A `text` literal's search is a regex too, built with lookarounds, whose cost grows with the text.
- **Options:**
  1. *A thread with a timeout.* `re` holds the GIL while it matches, so the event loop's thread would stall as well. Measured with Python 3.14.7 on macOS at `199754d`: while `re.search(r"(a+)+$", "a" * 24 + "b")` ran in a `threading.Thread` for 0.78 s, a `while t.is_alive(): time.sleep(0.01)` loop in the main thread ran once (`uv run python -c` with those lines). Nor can a thread be stopped, so it would keep its core after the deadline.
  2. *The `regex` package's `timeout`.* It can be interrupted, but it is another engine than the `re.search` the format names, with its own syntax and behaviour, and a new dependency.
  3. *A child process per search, killed at the deadline.*
- **Chosen: 3** (`aqa_runner.text_search`).
  - The child runs fixed code in isolated mode (`-I`) with an empty environment, so it never holds the runner's keys or test secrets. The search's kind, needle and text reach it on stdin as ASCII-only JSON, so no locale changes them and a lone surrogate survives.
  - Matching is `aqa_core.text`'s, as in the format. A URL is searched as it is.
  - The deadline is 2 s per search, starting the process included. A timeout or a cancellation kills the child, so no search outlives its check.
  - *Cost:* one process start per text check, a median of 25.4 ms (51.9 ms at most) over 50 `url_matches` calls on macOS at `199754d` (`uv run python -c` timing each call with `time.perf_counter`). No model and no network are involved.
- **What a timed-out search reports.**
  - *Options:*
    1. a fourth assertion outcome, `check_timed_out`;
    2. `failed`, with the reason;
    3. the run ends `errored`.
  - *Chosen: 1.* A timed-out search established neither a pass nor a failure. Option 2 would let M2 file a finding the page never showed, and option 3 would leave the other assertions unevaluated, though every assertion is evaluated so the report is complete.
  - *Rule:* a run with any timed-out check can't pass, and M2 maps the outcome to `inconclusive`, never to `expectation_violated`.

### Settling
The Decision's step readiness, as the browser session builds it (`aqa_runner.settling`, `BrowserSession.settle`).
- **Options:**
  1. *Playwright's `networkidle`* (no connection for 500 ms, on the whole page). The Decision rules it out: it can fire before a client-side write. A request any earlier action left open, such as an app's long poll, would also keep it from ever firing.
  2. *A count of the page's open requests.* The second flaw again.
  3. *A settle window per action, and a quiet page.*
- **Chosen: 3.**
  - *The settle window.* Each action (`navigate`, `reload`, `click`, `fill`, `select`, `press`) returns the window of the requests it starts (CONTEXT.md). A request joins the window that is latest when Playwright reports it ([`request`](https://playwright.dev/python/docs/api/class-page#page-event-request)). A redirect's next hop joins the window of the request it continues ([`redirected_from`](https://playwright.dev/python/docs/api/class-request#request-redirected-from)), so a slow chain never leaks into the next action's window. A window opens once the action's checks pass, so a refused action opens none, and what the page sends next stays in the previous action's window. It keeps the method and URL of its first 100 requests and counts all, as the session's other records do, so a write that arrives after settling but before the next action counts against the step (ADR-0025).
  - *Idle.* `settle(window)` looks every 100 ms. It returns `idle` once the window has no open request and, for the last 500 ms, no request in it has started or ended and the page's document had no mutation. The quiet period covers requests too, so the moment between a redirect hop's end and the next hop's start, or between a fetch and one chained after it, isn't idle. Otherwise it returns `timeout` at 10 s, even while a look is under way. Every step costs at least 0.5 s.
- **What doesn't hold a window.**
  - *WebSockets.* Playwright reports no request for one, only the page's `websocket` event (measured on 1.63, and pinned by `test_an_open_websocket_doesnt_hold_settling`).
  - *Event streams.* An EventSource's request stays open for as long as the page listens (measured). It is kept in the window but never holds it open, as a WebSocket doesn't: both are streams the page keeps open, and an EventSource is a GET, so it hides no write. What either's messages do to the page shows as DOM changes. Chosen by the coordinator (2026-10-02) over letting it hold the window, which would settle every step that opens a live stream as a timeout.
  - Any other request that stays open, such as a long-poll fetch, holds the window, and the step settles as a timeout.
- **How a quiet page is seen.** Each look takes the session's lock and checks the page as every observation does: a page off the allowed origins raises `PolicyEventError` and records the event. It then runs a fixed script, a `MutationObserver` on the whole document (subtree, children, attributes and text), installed at the first look, which counts as a change, as a new document's first look does.
  - The script runs in the page's world, so its answer is the page's word, as the locators' hit test is. A page can look busy or quiet, and tells the run nothing else.
  - A look that Playwright fails while the page is open counts as a change: a navigation replaced the document, or the page's own scripts broke the observer. Whether the page changed can't be told, so a page that breaks the observer settles only as a timeout. A closed or crashed page raises (the page's [`crash`](https://playwright.dev/python/docs/api/class-page#page-event-crash) event), so a dead page never passes for a slow one. Where Chromium never reports a crash, as on CI's Ubuntu 24.04 host (Playwright skips its own crash tests there), the session can't know of one, but its looks at the dead page fail or never answer, so settling returns `timeout` after 10 s, never `idle` (LAB_NOTES, 2026-10-02).
  - Settling catches nothing else. `PolicyEventError` and `DocumentChangedError` reach the caller, and an action's own error, such as a navigation another one interrupted, is raised by the action before any settling.
- **Consequences.**
  - A page that changes its DOM for good, such as a clock, settles every step as a timeout, after 10 s.
  - Only the page's own document is watched. A change inside a shadow root or a frame isn't seen, though a frame's navigation is a request in the window.
  - Playwright's default action timeout (30 s) still holds the session's lock while an action waits (ADR-0026's amendment on actions). The executor sets its own from its budget, in #46's third pull request.

### Reading text
- **`text_of(element)`** is the element's rendered text: its innerText, as `aqa_runner.locators.rendered_text` reads it, in Playwright's utility world. It is an observation (`text_in_target`), so the page and the element's frame are checked first. The element is held, so it can't read a document that replaced its own.
- **`visible_text()`** is the rendered text of the page's `body`, read the same way (`text_visible`), or empty when the page has none. It checks the page before and after, and counts frame changes from before the first check, as `resolve` does. If any happened, it discards what it read and raises `DocumentChangedError`, even when the page is back on an allowed origin.
- **Known limit, chosen for M1** (coordinator, 2026-10-02): rendered text never enters a frame, so text inside a frame, even one on an allowed origin, never satisfies `text_visible`. Reading allowed frames would need each one checked. Revisit when a spec needs frame text.

### The executor
`aqa_runner.executor.replay(script, setup, *, chromium, proxy, gate)` runs a compiled script once, with no model. `RunSetup(spec, config, start, record)` carries the spec and project config the run is for, its start origin and its record: four inputs that travel together, bundled to keep within Ruff's argument limit.
- **What M1 can't run is refused first.** A `fill_secret` step (#49), the `network_none`, `network_seen`, `probe_equals_baseline` and `visible_unoccluded` checks (#48), and a `press` key the session would refuse (not one key after modifiers, ADR-0026's amendment on actions) are a `SpecError` (exit 5), one problem naming each, before the browser opens (coordinator's ruling), so no intent is written for a step that can't be dispatched. *Rejected:* running the rest and leaving those out, which would report a run that never checked what the spec claims.
- **One session, the script's settings:** `script.browser`, never the project's or the spec's (ADR-0025, "Browser settings").
- **Seq 0 is the start URL** (`aqa_core.project.start_url`), recorded and settled as a step. Each compiled `navigate` joins its path to the start origin (`path_on_origin`).
- **Each step:** resolve its target for `action`, write the intent, dispatch through the session, settle, then write the completion with the index of the locator used and how settling ended.
- **Resolution waits within `resolve_seconds`,** looking every 100 ms. Each look is cut off when the budget runs out, since `is_enabled` can wait for good on an element moved into another document (ADR-0025's #52 amendment); the cut keeps that off the run without touching `locators`. A look the page changed under (`DocumentChangedError`) is made again, within the same budget.
- **Waits are bounded** (`BrowserSession.limit_waits`). An action's own wait, for its element to be actionable or an option to appear, ends at `resolve_seconds`. `navigate` and `reload` wait up to `NAVIGATION_SECONDS`, 30 s, Playwright's default made explicit, and hold the session's lock that long.
  - *Options:* one bound, `resolve_seconds`, for both; Playwright's 30 s for both; `resolve_seconds` for actions and 30 s for navigations.
  - *Chosen:* the third (coordinator). `resolve_seconds` is a lookup budget, and a slow first load on the pilot apps' stack shouldn't fail a step. No config field.
  - Playwright doesn't time every wait: a page script that never returns while a field is filled holds `fill`'s evaluation for good (the final review's probe). So the executor stops waiting for an action `MARGIN_SECONDS` (1 s) past its bound, after Playwright's own error, which says what it waited for, would have come. The step fails with its intent unresolved.
- **What stops the run.** Every assertion is still listed, `not_evaluated`, naming the step that stopped the run.
  - A step whose target never resolves: it drifted, nothing was dispatched and no intent written.
  - A look at the page that raises (`PolicyEventError` for a page that left the allowed origins, or Playwright's `Error`): the step `failed`, with no intent, since nothing was dispatched.
  - An infrastructure or policy event recorded by the time a target's lookup ends stops the run before that step is dispatched, so a popup that came while the run waited for a button never lets a click on it through. One recorded later, during the action's own Playwright wait (up to `resolve_seconds`, for an element to be stable or an option to appear), doesn't stop that action, and a popup is recorded only once Playwright has told the session its opener: what such events do is #47's (security re-review's probe, 2026-10-02).
  - A dispatch or a settle that raises Playwright's `Error` or `PolicyEventError`: the step `failed`, and its intent stays unresolved, since whether the action took effect is unknown.
  - An infrastructure event, or any policy event (a popup's included), after a step. What a policy event does to a run is #47's; until then it ends the run errored.
  - A failed step's reason is the policy event's message, which names only an origin, or the first line of Playwright's, with what isn't printable escaped and at most 200 characters: the rest of Playwright's message can hold what the page chose, a value it was given included.
- **Every assertion, after the last step.** Each is evaluated once, in order, as the last step's settling left the page: `text_visible` on the page's visible text (`visible_text`), `text_in_target` on its target's rendered text (`text_of`), both through the bounded search (*Bounded text searches*), `url_matches` on the page's URL (`url()`), and `not_visible`. Each is `pass`; `failed`; `binding_unresolved`, with each locator's miss, when no locator gave its target the match its use needs within `resolve_seconds`; or `check_timed_out` (*What a timed-out search reports*). A target is looked for as a step's is, every 100 ms; the page's text is read again while the page changes under the read, within the same budget.
  - *`not_visible` is evaluated once* (coordinator, 2026-10-02): a target found visible fails at once, and only drift (no scope yet, or more than one match) is waited out within `resolve_seconds`.
    - *Options:* (1) evaluate once, like every other check; (2) wait up to `resolve_seconds` for the target to go.
    - *Chosen: 1.* Waiting for the element to go would pass a transient one, such as an error toast that shows and then fades: a false pass, which for a QA tool is worse than a false failure. *The known cost:* an element the app removes on its own, a beat after the last step settled (a fade-out that outlives settling's 500 ms of quiet), fails the check when no one would call it a bug.
  - *A look that raises* (a page that left the allowed origins, a crashed page, or text that kept changing under every read within the budget) leaves that assertion `not_evaluated` with the reason, bounded as a step's is, and the rest `not_evaluated` too: the page can't be observed. The run is `errored`.
- **The run's outcome** is `passed`, `failed` or `errored`, DATA_MODEL's `runs.status` vocabulary (`heal_proposed` is M2's). `errored` follows an infrastructure or policy event, a failed step, or an assertion whose look at the page raised; `passed` needs every step completed and every assertion passed; anything else is `failed`. The reason is on each step and assertion, a drifted step's included, so M2 reads drift without a run-level value. *Rejected:* more run-level values (`drifted`, `inconclusive`) with a precedence between them.
- **The run record.** `RunRecord.step_intent` and `step_completed` each append one JSON line to the run's `steps.jsonl` and force it to disk (`os.fsync`, and the directory's when the file is new) before returning, so an action is dispatched only after its intent is on disk. The first line also forces every directory from the run's up to the spec root, so a new run's directory reaches the disk too. `os.fsync` is the OS's flush: on macOS it leaves the drive's own cache, which only `F_FULLFSYNC` empties. The JSON is ASCII, so no character of a value, such as U+2028, ends a line for a reader. An intent with no completion after it is unresolved (DATA_MODEL §7, "Local run record").
- **Cost:** none. No model, and no AWS resource; every step costs at least settling's 0.5 s.

## Amendment (2026-10-02): the coverage plan (#41)

Building `aqa explore --plan-only` (#41) settled how the plan is shaped and hashed. DATA_MODEL §7 ("Coverage plan first") holds the format; this records why. #41 lands in three pull requests, and each adds its part here.

### The plan's shape
`aqa_core.coverage_plan.CoveragePlan` is both what the model is asked to write and the frozen plan.
- **One check model, not one per type.**
  - *Options:* (1) a tagged union with a model per check type, as the compiled script has; (2) one `PlannedCheck` whose `check` is an enum of M1's nine types, with a validator holding each type to its own fields.
  - *Chosen: 2.* The model writes the plan through Anthropic's structured output, and anthropic 1.9.0's `transform_schema`, which langchain-anthropic 1.7.4 applies to the response format, turns a single-value `Literal` into a bare string with a hint in its description (`{'type': 'string', 'title': 'Check', 'description': '{const: text_visible}'}`, measured at `1a46c0a`), so the API wouldn't hold a union's tag. An enum of several values stays an enum. The validator gives each type the fields its compiled assertion takes, less what compiling adds.
- **A meaning per check.**
  - *Options:* (1) each expectation's `subject` names the one target its checks read; (2) each check that reads an element names it, in `target_meaning`.
  - *Chosen: 2.* One expectation can claim something of several elements. `login`'s expectation 4 says three header links are visible and not covered, which takes three targets, one per link (`bench/apps/conduit/qa/REVIEW.md`). The meaning is the expectation's subject when the check reads the subject itself. The locator generator (#52) takes `target_meaning` as the assertion target's meaning.
- **Planned values follow the compiled script's rules.** A `text` must be normalized and a `pattern` must compile as a Python regex, through the same types `aqa_core.compiled` uses. A planned check then always fits the assertion it compiles to, and a plan that couldn't compile fails before the browser opens.
- **What can't be covered says what it needs.** An `unsupported` expectation gives a reason, and `needs` names `pixel_diff`, `contrast_min` or `model_verify` when an M2 check would establish it. `aqa_core.coverage_plan.uncovered` then names the cause in each expectation's line, not only the expectation.
- **A check type with no compiled fields yet.** The plan may name `probe_equals`, whose assertion gets fields in #48. A plan says what establishes a claim, and compiling it is #53's, so `--plan-only` accepts it. A full explore can't compile it until #48, and must say so before the browser opens.

### The hash
- **`plan_hash` uses `spec_hash`'s rules:** `sha256:` and the sha256 of the canonical JSON (sorted keys, no whitespace, ASCII escapes) of the plan, leaving out every field whose value is null, such as the fields a check's type doesn't take. `aqa_core.spec.canonical_hash` computes both, so the two hashes can't drift apart.
- **It covers the plan alone.** Subjects, claims, checks, unsupported entries and conditions count; the spec's hash doesn't. A compiled script records `spec_hash` beside it.

### Fitting the spec
- **Checked after the answer parses**, by `aqa_core.coverage_plan.misfits`: one entry per expectation, in the spec's order, and only probes the spec declares.
  - *Options:* (1) a response schema built for each spec, whose `expect_index` and `probe` are enums of that spec's values; (2) one fixed schema, and this check after parsing.
  - *Chosen: 2.* For a spec that declares no probe, option 1 would need a schema without probe checks, since an empty enum accepts no value: two shapes instead of one. A misfit is reported like an answer that doesn't parse.

### The request
`aqa_runner.coverage_plan.plan_request(spec)` is the static instructions as the system prompt, then the spec's frontmatter as one JSON user message.
- **The spec's text as validated, without `tags`, the account or the reset hook.**
  - *Options:* (1) the frontmatter's YAML, as written; (2) the frontmatter as validated, without `tags`, as JSON.
  - *Chosen: 2.* Everything in it comes from what `spec_hash` covers (DATA_MODEL §7), so a change to the request means a change to the hash, and a compiled script is redone. The hash can change while the request doesn't, as when an expectation written as a string is rewritten as `{ text }`. Expectations come out as objects in order, with nothing left to count, and comments and tags stay out.
  - *Also left out: the account and the reset hook* (review, 2026-10-02). A plan needs neither, and the format lets a spec write a password, or a token in the hook's URL, as a literal, which would then reach the model (AGENTS.md §6). A `{ secret: NAME }` reference would carry only the name, but leaving the whole account out needs no rule about which form is safe.
  - *URLs lose their query and fragment* (security re-review, 2026-10-02). The start URL and each probe's endpoint go as their path, such as `GET /test-api/comments/count`: the path says what a page or a probe is, and a query can carry a token. A probe keeps its name, which is what a planned check names.
  - *What stays as written:* `goal`, `steps` and `expect` are the author's prose and reach the model whole, so a spec writes a secret only as `{ secret: NAME }`, in the account, never in prose. The Markdown body never enters, as DATA_MODEL §6 says the agent never reads it, and neither do the spec's path, the start origin, the project config or the environment. A test explores one spec from two spec roots and start origins against one cassette.
  - *Only what the spec sets* is written (`exclude_unset`), so a field the spec format gains later, with a default, leaves every existing request and cassette as it was.
- **The instructions are static** and name no pilot spec, so REVIEW.md's establishing checks are what PR B's recordings must arrive at, not what the prompt says. They state DATA_MODEL §7's rules: no weaker proxy, a probe for a count, a reload or a probe for persistence, meanings without labels, `text` before `pattern`, nothing the spec can't tell.
- **The plan models' docstrings are part of the request.** langchain-anthropic sends the response format through anthropic's `transform_schema`, which keeps each model's docstring as its `description`. Editing one changes the request like editing the instructions.

### The call
`make_plan(router, spec)` makes one `ModelRouter.call` on the navigator role, in `explore` mode, with `CoveragePlan` as the response format and no tools, so the request has no tool choice at all. It returns the plan the model wrote if it parsed, the misfits if it doesn't fit, and the routed call with every response's cost record.
- **Bounds on every Anthropic request** (#40's follow-ups). The adapter now states `max_tokens` 4096 and a 120 s timeout for each try. Left to langchain-anthropic 1.7.4, the fallback model `claude-opus-5-5` asked for 128,000 output tokens, up to $2.56 at its $20 per million, and no request ever timed out (LAB_NOTES, 2026-10-02). 4096 keeps the role's own requests as they were. 120 s covers 4096 tokens at 40 a second, and the SDK's two retries stay, so a provider that never answers holds a call for about 6 minutes. Both are constants, not config: nothing yet needs another value. A plan longer than the bound is cut off with the stop reason `max_tokens`, so the adapter leaves it unparsed and the router records it `invalid` (#40): never a shorter plan. PR B's recordings measure how far the pilot's plans are from it.
- **The response format's shape, against the API's limits** (structured outputs, "JSON Schema limitations", platform.claude.com/docs/en/build-with-claude/structured-outputs): 11 optional properties and 10 with `anyOf`, under the limits of 24 and 16. One `$defs` entry is only a `$ref` to another, from the `DistinctListOf` alias. The page lists `$ref` and `$defs` as supported and says nothing of a definition that is only a reference, so PR B's first recording is what proves the API takes it.

### The command
`aqa explore <spec> --plan-only` (`aqa_cli.explore`) resolves everything before any model call, makes the plan call, and writes the result to the run record. API.md §7 has the synopsis and the exit codes.
- **`--plan-only` is required until exploring is built** (#53). Without it the command is refused as a usage error, which Typer exits with 2. API.md §7 gives 2 to pending heal proposals, so a usage error of any `aqa` command already shares that code; mapping usage errors to a code of their own is left for later.
- **The spec root** is the nearest directory, from the spec's own up, that holds `config.yaml` (DATA_MODEL §9).
- **The whole project is read** (`load_project`), so a spec id used twice is caught before anything is written (DATA_MODEL §6). The cost: a problem in any spec of the project stops the command, though the problem names its own file.
- **Everything that can't work is a spec error, exit 5, before any model call.** A spec or the project config that doesn't parse, a path that isn't a spec of the project, no start origin, a `--url` that isn't an origin, and no `ANTHROPIC_API_KEY`. `load_project` already reports a config's problems with the specs' as one `SpecError`, and `start_origin` raises one, so one code covers them all. API.md §7's exit 5 now names the project config and a missing setting.
- **After the call:**
  - an expectation the plan can't cover exits 5, one line each, with what it needs;
  - a refusal, an answer that didn't parse or was cut off at the output bound (said so, from `stop_reason`), and a plan that doesn't fit its spec exit 3, ADR-0024's `gave_up`: explore produced no usable plan;
  - no response exits 11, a new infrastructure code beside the sandbox's 10. langchain-anthropic raises the SDK's own errors, subclassed, so `anthropic.APIError` covers them; the adapter names it `ProviderError`, so the command needn't import the SDK. A fallback that gets no response after a billed refusal (`ModelCallError` caused by the SDK's error) exits 11 too; one that fails any other way is raised as it is, a fault of ours, never "no response". Either way the refusal's cost record is written first; for the second the record's outcome is `error`.
  - What a billed run cost is printed with its outcome, and every outcome after the call names the run record.
- **The run record** (`aqa_runner.run_record.RunRecord`) is `<spec root>/.aqa/runs/<run_id>/`, `run_id` a UUIDv7. `.aqa/` holds a `.gitignore` of `*`, written if missing and never overwritten, so the user's repository needn't list it. `plan.json` holds the run and spec ids, `spec_hash`, the outcome and its reasons, the plan and its `plan_hash` (null without a plan), and the cost record of every response, so a billed response is always kept. A `.aqa` or `.aqa/runs` that is a link is refused, a spec error before the call (security reviews, 2026-10-02): a repository could commit one pointing outside the spec root. A document is made before its file is opened, so one that can't be written leaves no half-written file. A spec root that can't be written, on a read-only checkout say, ends the run with Python's own error before the call, for now: its exit code is an open question. The record is made before the call, so a spec root that can't hold one stops the run before anything is billed, and every run that reaches the call writes `plan.json`, including one that got no response, which has no cost record. #46 and #53 add their documents to the same record.
- **What the model wrote is printed with control characters escaped:** C0 and C1 controls, line and paragraph separators, and bidi controls. A reason with a newline or an ANSI escape can't break the one-line-per-cause output or recolour the terminal.
- **No second tracing guard.** The command builds the router, whose constructor switches ambient tracing off, before any LangChain call (#40's follow-up). A test pins that tracing is off when the model is called.
