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
- **`text_of(element)`** is the element's rendered text: its innerText, or nothing when the page doesn't render it. For such an element innerText falls back to its text content, so a success message rendered ahead and hidden would pass `text_in_target` while `text_visible` failed (#46's security review, coordinator's ruling). One evaluation decides both, so no check-then-read race opens between them; it runs in the page's world, the page's word, as all a page renders is. Rendered follows Playwright 1.63's visibility rule as far as one script can: a box of some size and a computed visibility of `visible`, so `hidden`, `display: none` on the element or an ancestor, `visibility: hidden` and a detached element read as nothing; unlike Playwright, `display: contents` has no box. Only HTML elements have rendered text: reading any other, such as SVG's, raises, as Playwright's innerText does, since its text content holds what the page hides (a hidden `<tspan>`, a `<title>`; the security re-review's probe). `aqa_runner.locators.rendered_text`, which the locator generator reads, is unchanged. It is an observation (`text_in_target`), so the page and the element's frame are checked first. The element is held, so it can't read a document that replaced its own.
- **`visible_text()`** is the rendered text of the page's `body`, read the same way (`text_visible`), or empty when the page has none. It checks the page before and after, and counts frame changes from before the first check, as `resolve` does. If any happened, it discards what it read and raises `DocumentChangedError`, even when the page is back on an allowed origin.
- **Known limit, chosen for M1** (coordinator, 2026-10-02): rendered text never enters a frame, so text inside a frame, even one on an allowed origin, never satisfies `text_visible`. Reading allowed frames would need each one checked. Revisit when a spec needs frame text.

### The executor
`aqa_runner.executor.replay(script, setup, *, chromium, proxy, gate)` runs a compiled script once, with no model. `RunSetup(spec, config, start, record)` carries the spec and project config the run is for, its start origin and its record: four inputs that travel together, bundled to keep within Ruff's argument limit.
- **What M1 can't run is refused first.** `visible_unoccluded` with `in_viewport: false`, and a `press` key the session would refuse (not one key after modifiers, ADR-0026's amendment on actions) are a `SpecError` (exit 5), one problem naming each, before the browser opens (coordinator's ruling), so no intent is written for a step that can't be dispatched. *Rejected:* running the rest and leaving those out, which would report a run that never checked what the spec claims.
  - *Since #49:* `fill_secret` steps run (ADR-0026's fill_secret amendment). One naming a secret the spec doesn't reference is refused the same way, and `replay` binds the test secrets before the browser opens: a missing value raises `MissingSecretError` (exit 12).
- **One session, the script's settings:** `script.browser`, never the project's or the spec's (ADR-0025, "Browser settings").
- **Seq 0 is the start URL** (`aqa_core.project.start_url`), recorded and settled as a step. Each compiled `navigate` joins its path to the start origin (`path_on_origin`).
- **Each step:** resolve its target for `action`, write the intent, dispatch through the session, settle, then write the completion with the index of the locator used and how settling ended.
- **Resolution waits within `resolve_seconds`,** looking every 100 ms. Each look is cut off when the budget runs out, since `is_enabled` can wait for good on an element moved into another document (ADR-0025's #52 amendment); the cut keeps that off the run without touching `locators`. A look the page changed under (`DocumentChangedError`) is made again, within the same budget.
- **Waits are bounded** (`BrowserSession.limit_waits`). An action's own wait, for its element to be actionable or an option to appear, ends at `resolve_seconds`. `navigate` and `reload` wait up to `NAVIGATION_SECONDS`, 30 s, Playwright's default made explicit, and hold the session's lock that long.
  - *Options:* one bound, `resolve_seconds`, for both; Playwright's 30 s for both; `resolve_seconds` for actions and 30 s for navigations.
  - *Chosen:* the third (coordinator). `resolve_seconds` is a lookup budget, and a slow first load on the pilot apps' stack shouldn't fail a step. No config field.
  - Playwright doesn't time every wait: a page script that never returns while a field is filled holds `fill`'s evaluation for good (the final review's probe). So the executor stops waiting for an action `MARGIN_SECONDS` (1 s) past its bound, after Playwright's own error, which says what it waited for, would have come. The step fails with its intent unresolved.
- **What stops the run.** Every assertion is still listed, `not_evaluated`, naming the step that stopped the run. The #47 amendments below and in ADR-0026 supersede the interim policy-event and egress-block rules in this bullet and in *Events during evaluation*.
  - A step whose target never resolves: it drifted, nothing was dispatched and no intent written.
  - A look at the page that raises (`PolicyEventError` for a page that left the allowed origins, or Playwright's `Error`): the step `failed`, with no intent, since nothing was dispatched.
  - An infrastructure or policy event recorded by the time a target's lookup ends stops the run before that step is dispatched, so a popup that came while the run waited for a button never lets a click on it through. One recorded later, during the action's own Playwright wait (up to `resolve_seconds`, for an element to be stable or an option to appear), doesn't stop that action, and a popup is recorded only once Playwright has told the session its opener: what such events do is #47's (security re-review's probe, 2026-10-02).
  - A dispatch or a settle that raises Playwright's `Error` or `PolicyEventError`: the step `failed`, and its intent stays unresolved, since whether the action took effect is unknown.
  - An infrastructure event, or any policy event (a popup's included), after a step. What a policy event does to a run is #47's; until then it ends the run errored.
  - An egress block: a request routing refused (`EgressProxy.blocked_attempts`, overflow included) or one the gate refused. #47 owns what an egress block does, expected-blocked hosts included, and replaces this interim rule; until then it ends the run errored, so an egress block never lets a run pass (ADR-0026, Egress blocks). Expected-blocked hosts aren't matched yet, so a spec that declares one errors until #47.
  - A failed step's reason is the policy event's message, which names only an origin, or the first line of Playwright's, with what isn't printable escaped and at most 200 characters: the rest of Playwright's message can hold what the page chose, a value it was given included. In a run whose script fills a test secret, a reason built from Playwright's error keeps only its type and the call it names, never the message (ADR-0026's fill_secret amendment, #49).
- **Every assertion, after the last step.** Each is evaluated once, in order, as the last step's settling left the page: `text_visible` on the page's visible text (`visible_text`), `text_in_target` on its target's rendered text (`text_of`), both through the bounded search (*Bounded text searches*), `url_matches` on the page's URL (`url()`), and `not_visible`. Each is `pass`; `failed`; `binding_unresolved`, with each locator's miss, when no locator gave its target the match its use needs within `resolve_seconds`; or `check_timed_out` (*What a timed-out search reports*). A target is looked for every 100 ms while no locator gives it the match its use needs, within the same budget (a `not_visible` target that is absent is a result at the first look); the page's text (`visible_text`) is read again while the page changes under the read, within the same budget.
  - *`not_visible` is evaluated once* (coordinator, 2026-10-02): a target found visible fails at once, and only drift (no scope yet, or more than one match) is waited out within `resolve_seconds`.
    - *Options:* (1) evaluate once, like every other check; (2) wait up to `resolve_seconds` for the target to go.
    - *Chosen: 1.* Waiting for the element to go would pass a transient one, such as an error toast that shows and then fades: a false pass, which for a QA tool is worse than a false failure. *The known cost:* an element the app removes on its own, a beat after the last step settled (a fade-out that outlives settling's 500 ms of quiet), fails the check when no one would call it a bug.
  - *A look that raises* (a page that left the allowed origins, a crashed page, a css value that isn't CSS, the page's text changing under every read within the budget, or a page that didn't answer) leaves that assertion `not_evaluated` with the reason, bounded as a step's is. Every later one is `not_evaluated` too, naming the assertion whose look raised, and nothing more is looked at. The run is `errored`.
  - *Each look is bounded.* Playwright's reads of a page (`query_selector`, `inner_text`) take no timeout, so the executor gives each assertion's looks `resolve_seconds` and `MARGIN_SECONDS` past them; a page that doesn't answer by then is a look that raised. A target lookup in which no look finished within `resolve_seconds` is a page that didn't answer too, not drift: every target has a locator, so a finished look leaves a miss. Each assertion gets a budget of its own, so a run whose assertions all drift takes that many budgets.
  - *Events during evaluation:* an infrastructure or policy event, or an egress block, that arrives while assertions are evaluated doesn't stop the evaluation, but the run is `errored`.
  - *The page must answer first.* `url()` reads the URL Playwright last saw, with no round trip to the page, and a target's state can be read from a page whose renderer has stopped; a busy-looping page, or a crashed renderer Chromium never reports (as on CI's Ubuntu 24.04), would pass a `url_matches`. So before the assertions the executor makes one bounded look, a read of the page's visible text. If it raises or doesn't answer, every assertion is `not_evaluated` with that reason and the run is `errored` (coordinator's ruling).
  - *Accepted (coordinator, 2026-10-02):* a look that raises during evaluation leaving every remaining assertion `not_evaluated` and the run `errored`, which the plan didn't spell out; and, as a residual, a target that detaches between being found and being read, which now reads as not rendered, so its text check fails rather than drifts.
- **The run's outcome** is `passed`, `failed` or `errored`, DATA_MODEL's `runs.status` vocabulary (`heal_proposed` is M2's). `errored` follows an infrastructure or policy event, a failed step, or an assertion whose look at the page raised; `passed` needs every step completed and every assertion passed; anything else is `failed`. The #47 amendments supersede these outcome rules. The reason is on each step and assertion, a drifted step's included, so M2 reads drift without a run-level value. *Rejected:* more run-level values (`drifted`, `inconclusive`) with a precedence between them.
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
- **The response format's shape, against the API's limits** (structured outputs, "JSON Schema limitations", platform.claude.com/docs/en/build-with-claude/structured-outputs): 11 optional properties and 10 with `anyOf`, under the limits of 24 and 16. One `$defs` entry is only a `$ref` to another, from the `DistinctListOf` alias. The page lists `$ref` and `$defs` as supported and says nothing of a definition that is only a reference, so the live-recording amendment below records the first measured API acceptance.

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

## Amendment (2026-10-03): network, probe and visible_unoccluded checks (#48)

Evaluating the rest of M1's checks (#48) settled what the Decision's "Probes. Reads repeat until the value is stable, within a bound" left open. DATA_MODEL §6 and §7 hold the rules ("Probe reads"); this records why. #48 lands in three pull requests, and each adds its part here.

### Probes
- **A probe is `GET <path>` on the start origin.**
  - *Options:* (1) `GET` and a path, joined to the start origin; (2) also `GET` and an absolute URL on any allowed origin.
  - *Chosen: 1* (coordinator, 2026-10-03). The pilot's probes are paths on the app's origin, and schema version 1 records no navigate to another allowed origin either.
  - A spec that writes anything else fails to load, naming the probe. The path is held to `start_url`'s rules, so no spelling of it reaches another origin; to ASCII, since the runner sends it as written and a request line carries nothing else (`runner_request` refuses it); and to no fragment, which a request never carries, so the probe would read another path than the one written.
  - The runner-side client still checks the URL's whole origin against the run's allowed origins, a second layer.
- **Reads repeat until two in a row select the same value**, `READ_SECONDS` (0.5 s) apart, within `STABLE_SECONDS` (10 s) for the whole read (`aqa_runner.probes.read_stable`).
  - *Options:* (1) two equal reads in a row; (2) equal reads across a quiet period, as settling waits out; (3) some larger number of equal reads.
  - *Chosen: 1*, with reads 0.5 s apart, so a value that held for settling's quiet period is stable. The pilot's counts change once, when a write lands after the step.
  - *The bound* is settling's 10 s, a constant as settling's is: nothing yet needs another value. It covers every request, through one `asyncio.timeout` around the whole read, so a probe that never answers is cancelled at the bound and its connection closed. #42 left that bound to the runner-side client's callers (ADR-0026's #42 amendment).
  - *The same value* means the same canonical JSON (`spec_hash`'s rules), so `1`, `1.0` and `true` are three values.
- **A probe that never holds still is `check_timed_out`** (the maintainer's ruling, 2026-10-03).
  - When the bound passes after two reads or more, none the same as the one before, the read established neither a pass nor a failure, as a text search that runs out of time doesn't. So it gets that outcome, and the run can't pass.
  - That is how the ticket's "one that never settles fails within the bound" is met: the read ends at the bound, and the check can't pass.
  - *Rejected:* `failed`, which M2 would turn into `expectation_violated`, a finding the app may not have.
  - When the bound passes before a second read, the probe didn't answer, which is a probe that can't be read.
- **What a probe answers is read strictly.** The app under test writes it.
  - *Status:* only a 200. A redirect comes back unfollowed (ADR-0026), and any other status carries no value of the probe's.
  - *Body:* UTF-8 JSON whose top level is an object or array, nested at most 256 deep, with no repeated key, no `NaN` or `Infinity` and no fraction or exponent a float can't hold: too large, or so small that a nonzero number reads as 0, which would let a value that changed from 0 read as the same (#48's Codex review). An integer is read exactly, up to Python's limit on digits, past which the body doesn't parse. A number with a fraction or exponent is read as a double, as JSON's interoperability rule expects (RFC 8259 §6), so two that differ only past about 17 significant digits, or only in a subnormal's lost precision, read as the same (#48's security re-review). *Rejected:* refusing every number a double doesn't hold exactly, which would make a probe that writes a long decimal unreadable. A body the connection's close ends, as a TLS stream that loses its `close_notify` does, can be cut short with nothing in the framing to show it (#42's follow-up). An object or array cut before its closing bracket never parses, where a bare `12` cut to `1` would read as a value. A repeated key would leave the value to the parser's choice, as in a compiled script (#46).
  - *The depth cap:* comparing two values serializes them, and `json.dumps` runs out of stack well before `json.loads` does (#48's security review). Measured with Python 3.14.7 on macOS at `d568d15` (`uv run python`, a bisection over nesting depths): `json.loads` reads arrays about 104,500 deep that `json.dumps` can't write, and objects about 61,500 deep; both raise `RecursionError`, `loads` past about 116,000. Where the limit falls depends on the stack, so it differs on Linux. 256 deep, a compiled script's cap (ADR-0024's #46 amendment), is a limit that holds everywhere; the reader walks the value with a stack to enforce it, and keeps a `RecursionError` from `loads` as the same refusal. `json.loads` also reads `1e400` as `inf` and `1e-400` as `0.0`, so the reader refuses both.
  - A response the reader refuses, a JSON path that selects nothing and a probe that didn't answer raise `ProbeError`, whose message is ours and holds nothing of the body: not a repeated key, not a number's text (#48's doubt review).
  - *Residual:* nothing caps a response body's size (ADR-0026's #42 amendment). The bound caps the time to receive it, but not the time `json.loads` then takes, which no timeout can interrupt.
- **Compared as canonical JSON.** One comparison serves the stable read, `probe_equals` and `probe_equals_baseline`, so `2.0`, `true` and `"2"` never pass for `2` (#48's review folded a separate type-and-value check into it: for an integer or a string they agree).

### Probe execution

- **Preflight validates every probe name.** Assertions and baseline definitions, unused definitions included, must name a spec-declared probe. Problems are aggregated before the browser opens. Structural references remain the compiled loader's responsibility.
- **Baselines precede the named step.** Capture uses the actual `seq`, before resolution, intent or dispatch, through `read_stable`. Successful values belong to that replay in memory only, including JSON null. A capture error or unstable value fails that step without an intent or action; every assertion is then `not_evaluated` at its sequence.
- **Interruption is checked after capture.** A browser egress block during a probe wait cannot allow the following action to dispatch. This applies to targeted and untargeted steps and preserves #47's egress precedence and invariant results.
- **Assertions reuse the same canonical comparison.** `probe_equals` reads its own JSON path and compares with its compiled value. `probe_equals_baseline` reads the baseline definition's path and compares with its capture after the final step. An unstable assertion is `check_timed_out`, and later assertions still run. No model is involved.
- **Request failures stop evaluation.** An unreadable, refused or unreachable probe makes the assertion and those after it `not_evaluated`, and the run `errored`. The result reason is respectively `the probe could not be read`, `the probe request was refused by the egress policy` or `the probe request could not reach its allowed origin`. The documented request-URL `ValueError` is converted only at the probe-read boundary. Raw request exception text is never used in those reasons. An unstable baseline uses `the probe value did not stabilize within its read bound`.
- **Cost.** No paid resource or model call. Probes perform the already declared GET reads under their existing stabilization bound.

### Network checks

- **Source.** The existing `settling.Traffic` listener owns the browser's response records. Its `response` handler joins each [response to its request](https://playwright.dev/python/docs/api/class-response#response-request), whose window already accounts for redirects. The separate invariant observers keep their own counts. Runner-side probes produce no browser event.
  - *Options:* use the invariant observers' response counts, or extend the settle windows that already own request metadata.
  - *Chosen:* settle windows. They retain method, full URL and status for each kept response, and provide the per-step records that #50 needs. Each window keeps the first 100 and counts all. `BrowserSession.windows()` includes the initial window before any action.
  - *Ownership:* `Exchange` and everything a returned window holds are plain data. `Window.open` holds integer request identities. `Traffic._open_requests` retains active requests, and `Traffic` keeps each window's kept requests for its network log (ADR-0026's #50 B amendment). Live Playwright objects cannot be reached through the window. A request's printed representation exposes its URL and method, and the page chooses both, so putting a request on `Exchange` would bypass later redaction.
- **Matching.** `url_pattern` is a Python regex over the complete browser-reported URL. The compiled and planned fields share the existing `PythonRegex` validator, which preserves the plan's JSON schema. Method and status class select candidates, then one isolated search child searches every candidate under the existing 2 s bound. Literal paths and path-only regexes were rejected because all format patterns should follow one rule.
- **Overflow.** A kept match decides either check even when another window overflowed. Without a match, overflow raises `WindowsOverflowError` instead of claiming absence. Returning false for `network_seen` or true for `network_none` would claim knowledge of records that were discarded. The error contains only the window index and counts. Replay maps it to `not_evaluated`, stops later assertions and errors the run. A bounded search timeout instead yields `check_timed_out`, with later assertions still evaluated.

### Visible and unoccluded

- **One shared hit test.** Reuse action resolution's center of the first non-empty client rect, through the element's own root, and accept the element or a descendant. A bounding-box center can fall between a wrapped link's pieces, and asking only the document misses shadow-root elements (LAB_NOTES, 2026-10-02).
- **One observation, no scrolling.** The browser session checks the page and element origins and requires a main-frame target, then reads the bounding-box size, viewport containment and shared hit test in one page-world evaluation. Scrolling would change what later assertions see and let an off-screen element pass. A covered element resolves for assertion use and evaluates false.
- **Child frames: refused in M1** (maintainer, 2026-10-03).
  - *Finding:* the independent spec review exposed false passes for a target in an offscreen iframe and one covered by the parent document. Its visible control passed (LAB_NOTES, 2026-10-03).
  - *Options:* (1) map the target's bounds and hit point through every ancestor frame and check viewport containment and occlusion at each level, accounting for clipping, borders, scrolling, transforms and document changes; (2) refuse child-frame observations explicitly.
  - *Chosen: 2.* An own-document observation cannot establish the containing page's display. Main-frame targets match the current compiled locator surface, which has no frame traversal. Full ancestor geometry is a separate design.
  - *Contract:* after existing origin checks, a target outside the session page's main frame raises `UnsupportedVisualFrameError`, with the fixed message `visible_unoccluded does not support elements inside frames`. This includes visible, offscreen, covered, nested and other allowed-origin frames. An off-allowed-origin target still raises its original policy error first.
  - *Consequence:* M1 cannot establish visual claims inside child frames, including unobstructed ones. Refusal never returns false and never scrolls. Main-frame checks, including open shadow roots, keep the shared hit test.
  - *Cost:* no paid resource, model call or extra browser observation; use the owner frame already returned by the origin check.
- **Limits.** This is the page's word, as action resolution's hit test is. It does not detect overlays with `pointer-events: none`, transparency, or pixel differences. M1's executor refuses `in_viewport: false` before opening the browser. It resolves for assertion use, bounds resolution and observation, and disposes the element handle in `finally`. A missing target is `binding_unresolved`, a resolved false observation is `failed`, and an unsupported frame or observation error stops evaluation with `not_evaluated` and an `errored` run.

## Amendment (2026-10-02): invariant observers (#47)

The Decision's Settling installs invariant observers before the first navigation, collecting for the whole attempt, reloads included. #47 builds them in `aqa_runner.invariants`, which the browser session installs on its page before handing it over (`Observers.watch`), so every run, explore's and replay's, has them. DATA_MODEL §6 holds the rules; this records why. The first #47 pull request adds the observers and judging. The second wires their results into the executor.

- **What each observes.** Playwright 1.63's page events: `console` of type `error` for `console_errors`, `pageerror` for `js_exceptions`, `response` with a status from 500 to 599 for `http_5xx` ([page events](https://playwright.dev/python/docs/api/class-page#page-event-console)). Playwright reports the page's frames and its dedicated workers there; a worker's `Log` entries aren't forwarded, so a worker's failed load has no console entry (measured).
  - *The browser's own entries are console errors.* Playwright turns Chromium's `Log.entryAdded` entries into `console` messages with no arguments and the entry's URL as their location (1.63's `_onLogEntryAdded`). So a 4xx or 5xx response, or a failed load, in the page's documents fires `console_errors` beside `http_5xx` or `broken_images`, as the benchmark's conduit-bug-003 records. The four never count the same event, and each has a trigger that fires it alone: a `console.error` call, a throw or an unhandled rejection, a dedicated worker's 5xx, and an image whose 200 response isn't an image (`packages/runner/tests/test_invariants.py`).
- **Broken images: the element's `error` event.**
  - *Options:* (1) network events: an `image` request that fails or is answered 4xx or 5xx; (2) the `<img>` element's own `error` event, reported by a script in the page; (3) both.
  - *Chosen: 2* (coordinator, 2026-10-02). "An image fails to load" includes a 2xx response that isn't an image (an app serving its HTML for a missing image) and an invalid `data:` image, which no network event shows, and only option 2 gives `broken_images` a trigger that fires nothing else. Option 1 would also count CSS backgrounds and cancelled loads.
  - *Where the reporter runs.* Playwright's `expose_binding` runs in the page's own world, and its wrapper calls `globalThis.__playwright__binding__` and its controller's internals when called, which the page's scripts can replace (read in 1.63's `BindingsController`), so a page could stop its reports. The reporter instead runs in an isolated world of its own, through the page's own CDP session ([`new_cdp_session`](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session)): `Runtime.addBinding` with `executionContextName` and `Page.addScriptToEvaluateOnNewDocument` with `worldName`, as Playwright 1.63's `protocol.d.ts` documents them. The page's scripts share the DOM with that world but none of its objects. Without `Page.enable` on the session the script never runs, and without `Runtime.enable` no call arrives (LAB_NOTES, 2026-10-02).
  - *The script* captures the binding at document start, before any of the page's scripts run, and adds the window's first capture listener for `error`, so no listener the page adds runs before it. It reports each trusted event whose target is an `<img>`: the document's `self.origin`, then the first 2048 characters of the image's `currentSrc` (a `data:` URL can run to megabytes), both read in its own world. Opening a document anew (`document.open()`, or a `document.write()` after it loaded) erases every listener on it and its window, ours included, and starts no new world (the security review's probe), so the script also watches the document's children with a `MutationObserver`, which opening doesn't erase, and listens again when the root is replaced. Measured and tested: a page that deletes and replaces Playwright's binding, the controller and a global named like ours, replaces `JSON.stringify`, `Map.prototype.get` and the `currentSrc` getter, and stops the event in a capture listener of its own still has its broken image reported, with its true URL. A made-up `error` event is ignored, the page's world has no binding to call, and a frame or page written anew after it loaded still reports.
  - *Which documents count:* those on one of the run's allowed origins, by the reported origin (a serialized origin is written as an allowed origin is). A subresource host's frame, a `data:` frame and a sandboxed one aren't. The CDP session is the page's, so popups are never seen.
  - *Residual risk.* A page can make up nothing but real broken images: it can only fail its own run. After the script starts it can hide one only by taking the image out of the document before it fails, or by opening its document anew and adding a capture listener of its own that stops the event before the reporter listens again, which it does when the document next gains or loses a child (in the same task when the document had any child, doctype included; a page that first empties the document has until it adds one; the security re-review's probe). Not seen: an `<img>` inside a shadow root, since an `error` event isn't composed and stops at the root; and one in a frame on an allowed origin of another site, which Chromium runs out of the page's process, where the page's CDP session doesn't reach. A frame on another origin of the same site (another port) is seen.
- **Console calls with no arguments** reach no `console` event in Playwright 1.63 (measured, with a `//# sourceURL=` too), so they never count, and the rule that leaves out Chromium's own entries for refused loads (no arguments, a refused URL; ADR-0026's #47 amendment) can't leave out a call of the page's own.
- **What is kept.** Each invariant keeps the first 100 things it saw (`Records`), each cut to 200 characters, and counts them all: console text, error messages and URLs are the page's, so they stay in memory, bounded, until #50 redacts them and #53 reports them. Nothing here writes them to the run record.
- **Judged once the run is over.** After the assertions, inside the browser session, the executor calls `invariant_results(seen, spec.invariants)` once. Every run reports each invariant as `held`, `violated` or `disabled`, errored runs included. A disabled invariant still shows what it saw, except that a script with a `fill_secret` step withholds every invariant's `seen` text while retaining outcomes and totals (ADR-0026's #47 amendment).
  - A run passes only when every step completed, every assertion passed, and every enabled invariant held. Otherwise it fails, unless it errored.
  - An egress block takes precedence over every other outcome and ends the run `errored` with `error_code: egress_blocked`. An infrastructure event, failed step, or assertion whose look raised also ends the run `errored`, with no error code. Expected-blocked hosts and a popup's policy event alone do not error the run (ADR-0026's #47 amendment).
- **Cost:** no model, no AWS resource. One more CDP session per page, with the `Page` and `Runtime` domains enabled; one event handler per page event, and one CDP message per broken image.

## Amendment (2026-10-03): controlling the late-scope fixture (#136)

The executor's late-scope test must distinguish a missing scope from an empty scope within its resolution budget. Its page-load timer ran during settling, before that budget began, which made the one-second case depend on settling time (LAB_NOTES, 2026-10-03).

- **Options:** extend the page-load timer; arm an autonomous timer after the first lookup; or let later lookups release the fixture after a delay measured from the first lookup. A longer page-load timer preserves the measured race. Arming a timer later removes settling from its delay, but keeps an independently advancing producer.
- **Decision:** the existing test wraps `BrowserSession.resolve` locally and records its first lookup's time. Only a later lookup at least 1.5 s after that time dispatches the fixture's one-shot `show-status-area` event. Every lookup calls the real resolver and returns its actual result. The fixture owns the same empty section markup. Neither the event nor the wrapper chooses behavior from the budget or expected verdict.
- **Consequences:** both parameter rows keep their budgets, expected results, misses and eight-second bound. An added assertion requires the first real lookup to report `no scope`, so the ten-second case needs retries to pass. The executor continues to enforce its own deadline and cooperative cancellation. A lookup admitted before its deadline may resume late and dispatch before cancellation runs; this is not an absolute prohibition on late dispatch. The fixture removes the measured page-load-versus-settling race without changing production behavior, dependencies or cost.

## Amendment (2026-10-03): live provider recordings and retained costs (#41 B1)

The earlier handwritten successful cassettes proved request construction and response parsing, but not provider acceptance. B1 replaces `coverage_plan`, `tools`, `structured_output` and `plain_with_effort` with four real Anthropic responses. The five `BY DESIGN` failure cassettes stay unchanged. This establishes the four generic requests; the five pilot plans and their REVIEW.md obligations remain B2's separate acceptance work.

- **Options:** re-record through the pytest fixture, which can promote a response before later assertions fail; or use a small test-only named-case recorder that retains evidence and costs before acceptance and promotion.
- **Decision:** use `packages.runner.tests.record_cassettes`, reusing the router, price map, `cost_record`, existing request examples, `make_plan` and VCR sanitizer. Each attempt gets a unique ignored directory. The recorder saves returned costs and reconciles observed SDK responses, including any retry response with usage. Unavailable usage stays explicitly unpriced. It never edits an answer to make it pass. Promotion requires a usable single response and request ID; unsuccessful or multi-response attempts remain private. TESTING §4 owns the command and artifact details.
- **Consequences:** recording is deliberate paid work; replay stays offline. This changes no production call, retry, model, price or schema behavior. The source SHA and request hashes connect a capture to its code and wire request. Costs from local fake-provider tests are fixture values, not live spending.

### Measured provider evidence

Source SHA `207061428232f3c34e4fd0cd8d3ad8ec00062301`, using the pinned Anthropic 1.9.0, langchain-anthropic 1.7.4 and VCR.py 8.3.0. From that worktree root, with the provider key sourced from its authorized ignored file, the four command arguments were:

```bash
capture_root="$PWD/.scratch/ticket-41/session6/live"
uv run python -m packages.runner.tests.record_cassettes coverage_plan "$capture_root/coverage_plan-001"
uv run python -m packages.runner.tests.record_cassettes tools "$capture_root/tools-001"
uv run python -m packages.runner.tests.record_cassettes structured_output "$capture_root/structured_output-001"
uv run python -m packages.runner.tests.record_cassettes plain_with_effort "$capture_root/plain_with_effort-001"
```

Each command was run separately, with its receipt and replay checked before the next call. All four used the configured `claude-sonnet-5-5`, had HTTP 200 and one accepted response, and needed no retry. No failed or unpriced live attempt occurred. The receipts' CostRecords used pinned map `ae6b762190658e695c77b0ba63b6c65913b86ac0`; all cached-input counts were zero.

| Case | Input tokens | Output tokens | CostRecord USD | Provider request ID |
|---|---:|---:|---:|---|
| `coverage_plan` | 3711 | 204 | 0.009462 | `req_011Cfgkgiautn768DeQnBRED` |
| `tools` | 388 | 47 | 0.001246 | `req_011CfgkkuphjhdeqXVwGUe9W` |
| `structured_output` | 271 | 59 | 0.001132 | `req_011CfgkqJ1KMjukpjFJsk3oP` |
| `plain_with_effort` | 16 | 6 | 0.000092 | `req_011CfgkuTzwtUBficMR6ghHm` |

The exact Decimal sum is **$0.011932**. Request bodies are unchanged from the preceding cassettes. The coverage-plan request includes the reference-only `$defs` entry discussed above; the API accepted it, returned `end_turn`, and produced a parsed plan with no spec misfits. This proves acceptance of this request at this source SHA, not all schemas or all future provider versions. Replay tests compare parsed replies with the saved wire responses and require no provider key.


## Amendment (2026-10-04): recorder sanitation and exceptional exits (#41 B1)

The correction applies to the test-only named-case recorder. The four historical live captures and their receipts remain unchanged; this amendment requires no paid re-recording. The reproduced root causes are recorded in [LAB_NOTES.md](../LAB_NOTES.md), dated 2026-10-04.

- **Options:** scan only serialized bytes or inspect the bounded representations the adapter consumes; add SDK exception types to a catch list or finalize the entire attempt; own VCR's internal patch machinery or defer its persistence through the public persister API; preserve arbitrary exception diagnostics or expose fixed categories.
- **Decision:** inspect raw text, decoded wire JSON, assembled adapter text and the installed structured parser's output. Retain usage before richer conversion. Give only this recorder's VCR instance a persister that holds the pending cassette in memory, then flush through its existing filesystem persister after VCR releases transport patches. Finalization attempts a priced receipt on failed SDK calls, warnings and interrupts and after cassette/hash write failure. Matched router records are reused once; ambiguous associations remain failures and do not discard other usable observations. Warning escalation is local and restored. Fixed safe errors suppress display of the original chain. The first interrupt during the call, capture finalization or promotion retains its kind despite later evidence failures, with SystemExit always mapped to integer 1. The separate `first_interrupt` discriminator leaves `primary_failure` unset after a successful call. Incomplete phases record the interrupt before subsequent evidence writes can mask it; completed phases do not inspect the caller’s active exception.
- **Consequences:** the recorder may reject a response on a dependency warning or uninspectable representation. No production retry, SDK, model, price, gate or fallback setting changes. A complete accepted receipt establishes safe capture evidence; successful library replacement is a separate requirement for the command's zero exit. Failed attempts remain private. Filesystem failure and process termination can prevent durable accounting, so a missing receipt is never a zero-cost claim. The original exception may remain as Python's internal context, but the recorder does not print or serialize it. TESTING §4 owns the operational behavior and tests.

## Amendment (2026-10-04): finite pilot-plan oracles (#41 B2a)

The Conduit REVIEW table requires checks that establish each expectation. A check's type alone cannot prove its target or condition has the reviewed meaning. B2a adds a test-only oracle before B2b records the pilot responses.

- **Options:** match keywords in free-text meanings, ask another model to judge them, or compare complete reviewed phrases and patterns. Keywords can accept a negated target, the wrong duplicate element or the wrong reload order. Another model would add paid, nondeterministic judgment to offline replay.
- **Decision:** use the expectation-indexed rows in `packages/runner/tests/pilot_plan_oracles.py`. Each row records its required check multiset and forbidden proxy descriptions. Compare exact check types, complete literals, target meanings, probe names and strict values. Extra checks fail even when establishing checks are present. Required conditions also use complete reviewed phrases.
- Target and condition comparison normalizes only whitespace and case. The header's corresponding individual link and the whole header nav are explicit alternatives for its text checks. Its visual checks still require separate links. Unknown phrases fail for inspection; substring matches never grant acceptance.
- The favorite condition requires reloading the article after favoriting and before checking the button and count. A missing, contradictory or unreviewed condition fails. This proves the recorded plan's requirement; enforcing condition links during execution remains separate work.
- Literal checks retain the core schema's normalization and case behavior. Regex alternatives for text do not pass merely because they match sample strings. URL patterns must be one of the exact reviewed patterns, and tests call the real bounded URL helper with complete absolute URLs.
- Root uses `^https?://[^/?#]+/$`. Publishing uses `(?i)^https?://[^/?#]+/article/benchmarks-we-trust-`, preserving title case independence and ignoring the generated suffix. REVIEW's path examples are interpreted against the runtime's complete-URL search, without preprocessing the URL.
- Completeness independently parses REVIEW headings and numbered rows, pins their text fingerprints and compares the current parsed spec expectations with pinned fingerprints and row counts. Added, edited, omitted or duplicate source rows require deliberate review. Positive plans are handwritten independently of the predicate data. Every listed proxy has replacement and addition controls.
- Numbered REVIEW rows allow horizontal whitespace around the index, leading indentation, and optional outer pipes.
  Fingerprints hash each original source line unchanged, so formatting changes also require review.
- **Consequences:** a future provider response with equivalent but unlisted wording remains a failure until its complete phrase receives explicit row-by-row review. Relaxing a meaning, scope, check or condition needs a separate design decision. Finite URL examples support the selected patterns; they do not establish equivalence of arbitrary regexes.
- B2a changes no production request, prompt, schema, model or retry behavior. It performs no paid call and leaves historical provider evidence intact. It does not claim that pilot recordings or the remaining #41 acceptance criteria are complete.

## Amendment (2026-10-05): recorded pilot plans and the prompt's three rounds (#41 B2b)

B2b records the five pilot plans live and replays them offline against the B2a oracle (above). The final cassettes are the round-3 recordings at source commit f9f17b0 (4844fe7 when recorded, before the rebase onto main), made with the named recorder (TESTING.md section 4). Every B2b call used model `claude-sonnet-5-5`, the pinned price map and no cached input; the `coverage_plan` rows are the generic cassette's re-records after each prompt change:

| Round | Case | Request ID | Tokens in / out | CostRecord (USD) |
|---|---|---|---|---|
| 1 | plan_read-article | `req_011CfiUB8A4qGchFN3JKxw5J` | 3874 / 710 | 0.014848 |
| 1 | coverage_plan-002 | `req_011CfiUjU7G4DE5DfvQkcCQi` | 3798 / 207 | 0.009666 |
| 1 | plan_read-article-002 | `req_011CfiUmfMz68f721NMkdYsz` | 3961 / 721 | 0.015132 |
| 1 | plan_favorite-article-001 | `req_011CfiXn6TwhNqX7AN92fXiM` | 3834 / 229 | 0.009958 |
| 1 | plan_publish-article-001 | `req_011CfiY73RezGmMUBaj4o2WF` | 4018 / 521 | 0.013246 |
| 1 | plan_post-comment-001 | `req_011CfkGaRd3ByrRGf8GPc357` | 3947 / 418 | 0.012074 |
| 1 | plan_login-001 | `req_011CfkGdJJarNgJXYc6RzMqn` | 3918 / 685 | 0.014686 |
| 2 | coverage_plan-003 | `req_011CfkJneHchfsjBrqzeGaND` | 3956 / 191 | 0.009822 |
| 2 | plan_read-article-r2 | `req_011CfkJpWny4Mq9quxVmUdjx` | 4119 / 717 | 0.015408 |
| 2 | plan_favorite-article-r2 | `req_011CfkJqaguRvqN5EaGsoafi` | 3992 / 777 | 0.015754 |
| 2 | plan_publish-article-r2 | `req_011CfkJrTd28dxu4Yz1hHBLc` | 4176 / 520 | 0.013552 |
| 2 | plan_post-comment-r2 | `req_011CfkJt5c15LvGXo23qbFHN` | 4105 / 384 | 0.012050 |
| 2 | plan_login-r2 | `req_011CfkJtp7dMKR7eYAezaUye` | 4076 / 677 | 0.014922 |
| 3 | coverage_plan-004 | `req_011CfkKff7PeiR9AVLXFjGLR` | 3994 / 201 | 0.009998 |
| 3 | plan_read-article-r3 | `req_011CfkKhDTc4QDEEMoJa1wtK` | 4157 / 746 | 0.015774 |
| 3 | plan_favorite-article-r3 | `req_011CfkKi2W4xiJVx8LFT3zGK` | 4030 / 237 | 0.010430 |
| 3 | plan_publish-article-r3 | `req_011CfkKiUkdgrXF34nsdG7bQ` | 4214 / 532 | 0.013748 |
| 3 | plan_post-comment-r3 | `req_011CfkKj57bwkTVvXPpFwg2c` | 4143 / 392 | 0.012206 |
| 3 | plan_login-r3 | `req_011CfkKjegDWR1WQ7zWM8rsP` | 4114 / 700 | 0.015228 |

B2b's calls cost USD 0.248502 over three rounds; with B1's four captures (USD 0.011932) the live total is USD 0.260434. Only the round-3 recordings are kept as cassettes; the earlier attempts stay in the ignored `.scratch` evidence.

- **Why the prompt changed.** The first recordings named elements the oracle could not accept: an ambiguous copy of a repeated element, a pattern that left out part of the claim, labels inside target meanings, a claim about a page's address with no `url_matches`, a collection claim turned into one item. Three rounds of general rules in `INSTRUCTIONS` (no pilot or REVIEW text; the examples are generic) addressed these: name the instance of a repeated element by where it sits and keep every part of a claim in a pattern (round 1); keep the asserted text out of a target meaning, check a shown page's address, name a collection whole (round 2); target one item for its own property, and add `url_matches` to a claim that a particular page is shown (round 3). The rules are not asserted by wording: a changed prompt fails the cassette replays until the generic `coverage_plan` cassette is re-recorded. Each round re-recorded the generic cassette and the five pilots, and the maintainer ruled the third the last paid round.
- **Reviewed oracle keys.** The oracle's rows accept a second complete check set where an independent review found it means REVIEW's row and admits none of its rejected proxies: login/0 (two more root patterns, `^https?://[^/]+/?(?:[?#].*)?$` and `^https?://[^/]+/?(#/?)?$`), login/1 and post-comment/2 (`text_in_target` on the feed tabs or the comment list, scoped, where REVIEW names `text_visible`), and read-article/6 (three patterns scoped to the signed-out comment prompt: R6-B's `(or|and)` pattern, the pattern with nothing required between the links, and `(?i)sign in.*sign up.*comment`; none checks "to add comments"). The written reviews are the comment above `ROWS`.
- **Reviewed aliases.** A recorded phrase maps to its oracle role when it names the same element, carries no label and selects nothing REVIEW rejects (`ALIASES`); the favorite plan's recorded reload wording is an alias of the reload condition. Rows whose element renders twice (REVIEW.md:62: the author meta, the favorite button, the byline) take a page-level meaning: which copy it resolves to is #53's binding, to the banner copy, and until then a change in only one copy is caught only if #53 binds the banner.
- **Open rows (criterion 4 stays open).** 19 of the 23 expectations pass. read-article/0 (an extra `url_matches`), read-article/5 (an extra weaker author check), publish-article/0 (`url_matches` matches any article URL) and post-comment/1 (the signed-in reader named in the meaning) do not, and are written up as #161, which first pins each to the checks its recording holds (`test_each_open_row_replays_exactly_its_recorded_checks`).
- **Consequences:** the prompt's request changed three times, and each change needs the generic cassette re-recorded. The pilot cassettes pair with the source SHA above. No retry, model or schema behavior changed.
