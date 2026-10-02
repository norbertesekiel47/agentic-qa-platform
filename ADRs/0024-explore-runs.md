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
  1. *A thread with a timeout.* `re` holds the GIL while it matches, so the event loop's thread would stall as well. Measured with Python 3.14.7 on macOS at `0690045`: while `re.search(r"(a+)+$", "a" * 24 + "b")` ran in a `threading.Thread` for 0.78 s, a `while t.is_alive(): time.sleep(0.01)` loop in the main thread ran once (`uv run python -c` with those lines). Nor can a thread be stopped, so it would keep its core after the deadline.
  2. *The `regex` package's `timeout`.* It can be interrupted, but it is another engine than the `re.search` the format names, with its own syntax and behaviour, and a new dependency.
  3. *A child process per search, killed at the deadline.*
- **Chosen: 3** (`aqa_runner.text_search`).
  - The child runs fixed code in isolated mode (`-I`) with an empty environment, so it never holds the runner's keys or test secrets. The search's kind, needle and text reach it on stdin as ASCII-only JSON, so no locale changes them and a lone surrogate survives.
  - Matching is `aqa_core.text`'s, as in the format. A URL is searched as it is.
  - The deadline is 2 s per search, starting the process included. A timeout or a cancellation kills the child, so no search outlives its check.
  - *Cost:* one process start per text check, a median of 25.4 ms (51.9 ms at most) over 50 `url_matches` calls on macOS at `0690045` (`uv run python -c` timing each call with `time.perf_counter`). No model and no network are involved.
- **What a timed-out search reports.**
  - *Options:*
    1. a fourth assertion outcome, `check_timed_out`;
    2. `failed`, with the reason;
    3. the run ends `errored`.
  - *Chosen: 1.* A timed-out search established neither a pass nor a failure. Option 2 would let M2 file a finding the page never showed, and option 3 would leave the other assertions unevaluated, though every assertion is evaluated so the report is complete.
  - *Rule:* a run with any timed-out check can't pass, and M2 maps the outcome to `inconclusive`, never to `expectation_violated`.

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
- **The spec's text as validated, without `tags`.**
  - *Options:* (1) the frontmatter's YAML, as written; (2) the frontmatter as validated, without `tags`, as JSON.
  - *Chosen: 2.* It is what `spec_hash` covers (DATA_MODEL §7), so the request changes when a compiled script would go stale, and not otherwise. Expectations come out as objects in order, with nothing left to count, and comments and tags stay out. The Markdown body never enters, as DATA_MODEL §6 says the agent never reads it, and neither do the spec's path, the start origin, the project config or the environment. A test explores one spec from two spec roots and start origins against one cassette.
  - *Only what the spec sets* is written (`exclude_unset`), so a field the spec format gains later, with a default, leaves every existing request and cassette as it was.
- **The instructions are static** and name no pilot spec, so REVIEW.md's establishing checks are what PR B's recordings must arrive at, not what the prompt says. They state DATA_MODEL §7's rules: no weaker proxy, a probe for a count, a reload or a probe for persistence, meanings without labels, `text` before `pattern`, nothing the spec can't tell.
- **The plan models' docstrings are part of the request.** langchain-anthropic sends the response format through anthropic's `transform_schema`, which keeps each model's docstring as its `description`. Editing one changes the request like editing the instructions.

### The call
`make_plan(router, spec)` makes one `ModelRouter.call` on the navigator role, in `explore` mode, with `CoveragePlan` as the response format and no tools, so the request has no tool choice at all. It returns the plan the model wrote if it parsed, the misfits if it doesn't fit, and the routed call with every response's cost record.
- **Bounds on every Anthropic request** (#40's follow-ups). The adapter now states `max_tokens` 4096 and a 120 s timeout. Left to langchain-anthropic 1.7.4, the fallback model `claude-opus-5-5` asked for 128,000 output tokens, up to $2.56 at its $20 per million, and no request ever timed out (LAB_NOTES, 2026-10-02). 4096 keeps the role's own requests as they were. 120 s covers 4096 tokens at 40 a second, and the SDK's two retries stay. Both are constants, not config: nothing yet needs another value.
- **The response format's shape, against the API's limits** (structured outputs, "JSON Schema limitations", platform.claude.com/docs/en/build-with-claude/structured-outputs): 11 optional properties and 10 with `anyOf`, under the limits of 24 and 16. One `$defs` entry is only a `$ref` to another, from the `DistinctListOf` alias. The page lists `$ref` and `$defs` as supported and says nothing of a definition that is only a reference, so PR B's first recording is what proves the API takes it.
