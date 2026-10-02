# ADR-0025: Compiled scripts: meanings, locators, checks and step flags

- Status: Accepted
- Date: 2026-09-29

## Context
DATA_MODEL §7 fixed the compiled format's shape: targets with meanings and locators, steps with a required `side_effect` flag, and assertions per expectation. It didn't say how the compiler fills that shape in. The M0 pilot and a probe of Playwright 1.63 (LAB_NOTES, 2026-09-29) exposed the gaps:
- **Unnamed elements.** Most things the pilot's assertions check have no accessible name, test ID or id: the favorites count (`<span class="counter">` inside the favorite button), the article's date, its body and its tag list. Conduit has no test IDs at all, and `app-article-meta` renders twice (banner and footer).
- **Circular locators.** Locating an assertion's target by the text that assertion checks turns a real bug into drift. conduit-bug-001 (a wrong article date) would go to heal instead of failing in strict replay with no model calls.
- **Playwright's `Locator.normalize()`** bakes icon-font glyphs into names (`"\uf218\xa0New Article"`) and picks fragile selectors, such as a bare `form`.
- **Case.** Chromium's rendered text applies CSS `text-transform`, but accessible names don't. A benign restyle to uppercase would break a case-sensitive text check.
- **Substrings.** "Favorite Article" is inside "Unfavorite Article", and "1" is inside "10".
- **Dates.** Rendered dates depend on the browser's time zone. Conduit's seeded dates show correctly only between UTC−10 and UTC+13 (#21).
- **`side_effect`.** A wrong `false` lets a continuation or a heal repeat a purchase (ADR-0006 amendment). "The explorer infers the flag" didn't say how.
- **Benign cases (#20).** A heal may rebind a target but never change its meaning or an assertion, so meanings and patterns must describe the claim, not today's markup.

The decisions came out of the 2026-09-29 design review, followed by an adversarial review by Codex (gpt-6-astra). This ADR includes the findings accepted from that review.

## Options
- **Locators:**
  1. Playwright's `normalize()` as-is.
  2. Our own grammar, with no structural fallback for action targets, so every relabel drifts.
  3. Our own grammar, with validated fallbacks and a record of which locator resolved.
- **Text checks:**
  1. Regex only, as documented.
  2. Exact-case literals only.
  3. A case-insensitive, whole-word literal `text`, plus an explicit regex `pattern`.
- **`side_effect`:**
  1. The model's judgment.
  2. Network evidence or the model, with `false` only on positive evidence.
  3. Network evidence as a default that the model may lower with a reason.
- **Browser settings:**
  1. Pinned for every run and recorded in the compiled script.
  2. Pinned for benchmark runs only.
  3. Pinned, but replay reads the current config.

## Decision

### Meanings
A target's meaning says what the element is for and where it sits, never its current label. Examples: "the comment form's submit button", "the favorites count in the article banner". If an expectation claims a label, the label goes in an assertion.

### Locator grammar (option 3)
- **Kinds:**
  - role plus accessible name: private-use glyphs are stripped, whitespace and non-breaking spaces collapsed, then the name is matched exactly;
  - label;
  - placeholder;
  - test ID;
  - a stable id;
  - a constrained structural locator, built from stable class names, attributes and custom-element tags such as `app-favorite-button`.

  Never positional (`nth-child`), never generated class names, and never text-only locators for containers.
- **Scope.** Any locator may be scoped under a stable ancestor, such as `.banner` or `app-article-meta`. Uniqueness is checked inside the scope.
- **Action targets** (elements the agent clicks or types into) list role plus name first, then label, placeholder, test ID and stable id, then a structural locator.
- **Assertion targets are never located by what their claim says.** They use a test ID, a stable id, a role (with a name only if the claim doesn't mention it), or a structural locator.
- **Every locator is validated.** It must be unique and resolve to the element the agent used, both at compile time and on the confirmation replay (ADR-0024).
- **Targets used more than once.** A target used at several points must resolve with each of its locators at every use; otherwise it is split into separate targets. The favorite toggle is the pilot's example: it is clicked, then checked after its state changes.
- **The executor records which locator resolved each target.** M2 decides the rest before M3's numbers (#25):
  - what a fallback match means for verdicts;
  - how benign cases are scored when a fallback absorbs them;
  - whether a side-effect step may act through a fallback.

### Resolution per use
- **Action target:** needs a unique, actionable match.
- **Assertion target:** needs a unique match attached to the page. It doesn't have to receive pointer events, so `visible_unoccluded` can evaluate a covered element to false.
- **Negative check** (`not_visible`): resolves its scope. Zero matches inside the scope is a result, not drift.

### Text checks (option 3)
- **`text`** is a literal. It is matched case-insensitively, at word boundaries, against the element's normalized rendered text (whitespace collapsed, private-use glyphs stripped).
- **`pattern`** is a Python regex (`re.search`, flags explicit), for claims that need one.

The compiler prefers `text`. A claim that depends on case uses a `pattern`.

### `side_effect` (option 2)
- **`false` needs positive evidence.** All of these must hold:
  - the step's settle window ended in idle, not a timeout;
  - no write request (anything other than GET, HEAD or OPTIONS) went to any host;
  - no WebSocket message was sent;
  - the model agrees that the step changes nothing.
- **Late writes count.** A write that arrives before the next action counts against the step.
- **Anything else is `true`**, and `side_effect_basis` records why.
- **Only a person lowers the flag.** They edit the compiled script, and the pull request shows the edit. ARCHITECTURE §3.3's "humans can override in the spec" is withdrawn, because the spec has no such field.

### Browser settings (option 1)
- **Pinned for every run:** time zone UTC, locale en-US, viewport 1280×800, device scale factor 1 and a light color scheme.
- **Overrides:** per project (DATA_MODEL §9) and per spec (`browser:`).
- **Recorded.** The compiled script stores the settings it was explored under, and replay uses those.

This closes #21 for every run, not only the benchmark.

### Format additions (DATA_MODEL §7)
`schema_version` stays 1, because nothing has been compiled yet; the ADR-0006 amendment set the same precedent. New fields:
- **`browser`:** the pinned settings.
- **`coverage`:** the frozen coverage plan, giving each expectation's subject, claim, assertion IDs and required conditions.
- **`side_effect_basis`:** why a step's flag is `true`.
- **A `reload` action.**
- **`confirmed`:** whether the confirmation replay passed.
- **Scoped and structural locators.**

Two rules change:
- **`spec_hash`** covers the parsed frontmatter except `tags`. A mismatch makes the script stale.
- **Location.** Compiled scripts live at `<spec root>/.compiled/<spec id>.json`.

The heal-patch validator also forbids changes to `browser` and `coverage`.

## Consequences
- **Bugs fail without a heal.** conduit-bug-001 fails the article's date check in strict replay, because the date's target is found by structure, not by its text.
- **Benign relabels may not drift.** A validated fallback can absorb conduit-benign-001, so the manifest's `drift_consistent` depends on how M2 scores fallback matches (#25).
- **Sign-in clicks become side-effect steps**, because they POST. Heals get more conservative, and M6's continuation rebuild must handle skipped side-effect steps (#28).
- **Compiled-script diffs explain themselves.** Each assertion traces to a claim, and each side-effect flag to its basis.
- **The compiler owns locator generation**, so its rules are tested on the five real pilot pages (TESTING §2), not synthetic ones.

## Amendment (2026-10-02): reading a compiled script (schema version 1)

Building the strict reader (#45) and its reviews raised choices this ADR left open. DATA_MODEL §7, "Reading a compiled script", holds the rules; this records why.

- **Normalizing also strips soft hyphens and zero-width spaces.** Playwright 1.63 drops U+00AD and U+200B from accessible names, and rendered text keeps them, so `Pay&shy;ment` would otherwise miss a `text` of "Payment". A `name` or `text` is written already normalized. One that isn't could never match, so it is a format error, not drift later.
- **A locator names exactly one kind.**
  - *Options:* six optional fields with a rule, or one class per kind (`ByRole`, `ByLabel`, `ByPlaceholder`, `ByTestId`, `ByCss`) chosen by the kind the JSON names.
  - *Chosen:* one class per kind. `name` then exists only beside `role`, and the executor and the compiler dispatch on the class with nothing left optional. The JSON is the same either way.
- **A `css` value has no `>>`.**
  - *Options:* refuse it, escape it, or parse CSS to allow it inside quoted attribute values.
  - *Chosen:* refuse it. Playwright chains selectors at `>>` even after `css=`. On 1.63 a value chained into XPath and into an engine that enters frames. Escaping would hide the author's mistake, and an attribute value can write `\>\>`. A value must also leave no quote or escape open by Playwright's count, which includes quotes inside CSS comments: an open quote swallowed the separator Playwright puts before a scoped locator, and the inner locator chained into a frame (LAB_NOTES, 2026-10-02). Resolution must send the value as `css=<value>` (#45), so no other engine is reachable. Playwright's own pseudo-classes, such as `:has-text()`, would still work inside it, so the format refuses those too (amendment below, "generating locators", #52).
- **`side_effect_basis` goes only with a true flag.**
  - *Options:* refuse it on a false flag, or allow it.
  - *Chosen:* refuse it. A person who lowers a flag removes its basis in the same edit, so the pull request shows both.
- **A navigate stores a path on the start origin,** held to `start_url`'s rules, and the executor joins it as the start URL is joined (#89). Schema version 1 records no navigate to another allowed origin, because nothing needs one yet. One that does adds absolute URLs with the allowed-origin check.
- **Only the checks with fields are in schema version 1:** those §7 gives fields to, plus `not_visible` and `network_seen`. The rest are refused by name until their fields are defined: `probe_equals` in #48; `pixel_diff`, `contrast_min` and `model_verify` in M2. Adding a check is additive, so no committed script breaks and `schema_version` stays 1.
- **`browser` records every setting.** A missing one is an error, never the pinned default, because replay uses what the script records.
- **The loader, not the format, checks the parts against each other:** names that must exist or be unique, and repeated JSON keys. A repeated key matters because pydantic keeps the last one, so a hidden `"side_effect": false` would win (#46).

## Amendment (2026-10-02): resolving a target per use

Building resolution (#45) settled three choices that "Resolution per use" left open. DATA_MODEL §7 holds the rules.

- **A negative check's absence must be unanimous.**
  - *Options:*
    - the first locator whose scope resolves decides, so its zero matches is absence;
    - absence only when no locator finds the element.
  - *Chosen:* the second.
    - Under the first, a button relabeled from "Delete" to "Remove" finds nothing by role and name. A `not_visible` check would then pass in strict replay while the structural fallback still sees the button: a weaker check than the claim.
    - Under the second, any unique match resolves the element. Several matches are drift. The element is absent only when no locator finds one, none finds several, and at least one finds its scope empty.
    - An unscoped locator's scope is the page.
    - A negative check counts only visible matches, and its role locators see past the accessibility tree. A visible button under `aria-hidden` is seen rather than read as absent. A hidden element with the target's old name, such as a closed dialog's "Delete", can't resolve first and hide the visible, relabeled one a fallback finds. *Options:* count every match and let the executor judge visibility, which a hidden decoy defeats; or count visible matches only. *Chosen:* the second.
- **Actionable is one look at the page.**
  - *Options:*
    - visible, enabled, and a hit test finds the element: at the center of its first box, through its own root, and again after an instant scroll into view if the first test misses;
    - Playwright's trial click with a timeout, which also waits for stability.
  - *Chosen:* the first.
    - Resolution stays a single judgement, and the executor's loop owns waiting within `resolve_seconds`. A trial click would wait inside every locator's turn, so the waits would add up across fallbacks.
    - The rule is the same for every targeted step, `fill` and `select` included. That is stricter than Playwright's own fill, which skips the hit test, because a person can't fill a field under an overlay either.
    - An element in an open shadow root is tested through its own root, so it counts. Page locators never enter a frame, so a frame's element is no match. A native control hidden under its own label, as some styled checkboxes are, fails the hit test: drift, never a wrong action.
    - The hit test runs in the page's own world, so it is the page's word, not a control. A page can make it pass or fail, but not choose which element it judges. An error from it, such as an element removed between two checks, is drift; only a closed page raises.
- **`pattern` searches the normalized rendered text,** the same text `text` matches, so a pattern spans line items without `(?s)`. `url_matches` searches the URL as it is. The search runs in the runner on text the page controls, and Python's `re` can't be interrupted, so the executor bounds it (#46).
- **`text` is whole words only where its edges are word characters,** so "(3)" is found in "Cart(3)" while "1" isn't found in "10". Text written without spaces between words, such as Japanese, needs a `pattern`, and so does a number that starts with a sign or a decimal point, such as "-1", which would be found in "10-1".
- **Role names are matched by Playwright's own role engine.**
  - The name is matched with an anchored pattern built from the normalized name. The pattern lets private-use glyphs sit anywhere and lets a space be any run of whitespace and glyphs.
  - Each character other than an ASCII letter or digit is written `\uXXXX`, which Python and JavaScript read alike, so no character of a name becomes selector syntax. An astral character stays literal.
  - Playwright passes the pattern to JavaScript without the `u` flag, so an astral private-use glyph is matched as a surrogate pair (LAB_NOTES, 2026-10-02).
- **A scope is one element.**
  - *Options:* search inside every element the scope matches, or require the scope to match exactly one.
  - *Chosen:* exactly one. The scope is there to pick out one place, such as the banner rather than the footer. A scope that matches twice can't tell them apart, so its locator misses with "no scope".

## Amendment (2026-10-02): where the time-zone check gets its zone list (#90)

"Browser settings" pins a time zone and lets a project or a spec override it. #39 checked an override by name against `zoneinfo.available_timezones()` less `localtime`, a list the host supplies. Measured 2026-10-01 (LAB_NOTES): 598 names on macOS, and 498 on Ubuntu 24.04 with its own `tzdata` package, which lists `localtime` and leaves out 101 backward-compatible aliases such as `Asia/Calcutta`. Chromium accepts the aliases, so `timezone: Asia/Calcutta` would pass on a Mac and fail in CI, before the browser started: safe, but the answer depended on the machine.

- **Options:**
  1. *Keep the host's list.* No new dependency, and the answer stays host-dependent.
  2. *Depend on the `tzdata` package* (CPython's own IANA data, which the [zoneinfo docs](https://docs.python.org/3/library/zoneinfo.html#data-sources) recommend declaring a dependency on) and validate against its list only.
  3. *Accept canonical names only.* It needs a list of canonical names, so it needs option 2's data or a vendored list, and it refuses names Chromium accepts.
- **Chosen: option 2.** `tzdata` is pinned in `aqa-core` and in `uv.lock`, so one version of the list is the answer everywhere, and Renovate moves it (TECH_STACK §1). PyPI's `2026.4` is the release the IANA calls 2026d. Measured 2026-10-02 with `uv run --no-project --with tzdata==2026.4 python`, reading `importlib.resources.files("tzdata").joinpath("zones")`: 598 names, the same set as the maintainer's Mac gives from `zoneinfo.available_timezones()` (the issue's triage comment), with `Asia/Calcutta`, `America/Buenos_Aires` and `US/Pacific` in it and `localtime` out.
- **The list is the package's `zones` file, read directly (`aqa_core.browser.time_zones`),** not `zoneinfo.available_timezones()`, which adds the host's zone files (LAB_NOTES, 2026-10-02). A missing package is an error, not a fallback to the host's list.
- **Every accepted zone opens in Chromium.** `uv run pytest packages/runner/tests/test_time_zones.py` passes at `f36f74f`, on macOS with Playwright 1.63.0's Chromium 153.0.8010.12 and tzdata 2026.4, for all 598 names, `Factory` and the aliases included. The test sets each zone on one page with CDP's `Emulation.setTimezoneOverride`, clearing the override first because of a Chromium quirk (LAB_NOTES, 2026-10-02). The full list took 0.71 to 2.15 s over four runs, the first the slowest (`--durations=2`, same command and SHA, on 2026-10-02 with other test suites running). A context per zone, as `open_browser_session` opens one, took 18.7 to 23.2 s: one `launch`, then `new_context(timezone_id=zone)`, `new_page()` and `close()` per zone, timed with `time.perf_counter` at `44049ef`; that script isn't kept.
- **The shortcut is held to what a run does.** A second test sends every 30th zone, plus `UTC`, `Asia/Calcutta` and `Factory` (23 names today), and three refused names (`Mars/Phobos`, `utc`, `localtime`) through both the override and `new_context(timezone_id=…)`, and requires the same refusals and the same page reports.
- **Consequences:**
  - *A tzdata bump runs the whole-list test.* Renovate's weekly pull request (ADR-0031, where a minor bump joins the grouped one) turns red if the new list has a zone this Chromium refuses, which no one would otherwise notice until a spec named it.
  - *A bump can drop a name.* A spec or compiled script that names a removed zone then fails to load, as an unknown zone, and the pull request's tests don't read anyone's specs.
  - *Aliases are accepted and passed to Chromium as written.* A page may report the zone under another name (`Asia/Kolkata` as `Asia/Calcutta`, LAB_NOTES, 2026-10-01), so compare what a page renders, never the name.
  - *Nothing in the packages reads the host's zone list any more.*

## Amendment (2026-10-02): generating locators (#52)

Building the compiler's locator generation (#52) settled choices that "Locator grammar" and "Resolution per use" left open. DATA_MODEL §7 holds the rules, and the loader's rule for negative checks joins it with the loader (#46); this records why.

- **The five pilot pages.** "The five real pilot pages" in Consequences are the pages the five pilot specs visit, on the clean Conduit app, whose cases are all on the dev split (TESTING §5). One of the five is seen in two states:
  - the login page, signed out, with its form filled in;
  - the home page, signed in;
  - an article page as a signed-in reader, before and after favoriting it and reloading;
  - an article page as a signed-out visitor;
  - the editor, filled in.
- **A css value refuses Playwright's own pseudo-classes.**
  - *Options:*
    - leave them to the compiler, which doesn't write them;
    - refuse the names as written;
    - refuse them as Playwright reads them.
  - *Chosen:* the third.
    - Playwright 1.63 trims each selector part (`css=<value>`) with JavaScript's `trim()`, which reaches the value's end, then reads the value with its own CSS tokenizer, which decodes escapes and drops comments. Its parser then lowercases a pseudo-class name before checking it against its list. So `:HAS-TEXT(`, `:has\2d text(` and `:/**/visible` all reach its own engines, and the browser can parse none of them (LAB_NOTES, 2026-10-02).
    - Under the first option, a person's edit or a heal could locate by text, position or layout, which the grammar forbids. The second misses those spellings.
    - The format reads a value as that tokenizer does, so a name inside a quoted attribute value, a comment or an escaped colon is no pseudo-class.
    - A value is written trimmed. CSS reads a no-break space and 18 other characters `trim()` removes as part of a name, so a trailing one hid `:visible` from the format's reading but not from Playwright's (found by #52's security review). *Options:* model the trim, or refuse what it would remove. *Chosen:* refuse, at both ends so the rule is simple, from an explicit list, because Python's own whitespace differs from JavaScript's. An invisible character at the end of a reviewed edit is worth refusing anyway. A test computes the list from Chromium's `trim()`.
  - The list is Playwright's `customCSSNames` less the five standard names it also parses: `not`, `is`, `where`, `has` and `scope`.
    - One test reads the installed driver's list and checks it against the names the browser itself can't parse. A Playwright upgrade that adds a name fails until the format refuses it too.
    - Another test checks that each tricky value the format accepts matches as many elements under Playwright as under the browser's own `querySelectorAll`.
- **Standard positional pseudo-classes stay readable.** `:nth-child()` and its kin are CSS, so the format reads them, and a person may write one in a reviewed edit. The grammar forbids them, so the generator (#52) must not write one. M2's heal-patch validator should refuse a heal that writes one, because a heal is the model's edit, not a person's.
- **A negative check's target must be scoped.**
  - *Options:*
    1. In resolution: an unscoped locator's zero matches never establishes absence, so the check is drift.
    2. At load: the loader refuses a `not_visible` whose target has an unscoped locator.
    3. No rule: an unscoped locator's scope is the page.
  - *Chosen:* the second.
    - Under the third, an unscoped negative check finds its target absent on any page with no match, an error page included, so it passes when the app is broken.
    - An unscoped negative check is a structural error in the script, found when the script is read. That makes it a spec error (exit 5), as the loader's other checks are.
    - The first would make it drift, which M2 routes to heal as though the UI had moved.
  - The loader's check is #46's, and the generator (#52) must scope every negative-check target it writes. Resolution is unchanged, because a loaded script never gives it an unscoped negative check.
- **The generator takes an element, not only a ref.**
  - The snapshot gives a ref to what it shows as a node, and folds an inline element's text into its parent's. The favorites count, an inline span inside the favorite button, has no ref.
  - Whether an element gets one depends on its styling. The banner date has a ref with Conduit's CSS, which makes it a block, and had none in a probe without it (LAB_NOTES, 2026-10-02).
  - So `generate` takes the element a use put the target to, with the role and name its ref's line gives when it has one. How the agent points at an element with no ref is the navigator's (#53).
- **Only a control's name locates it.**
  - *Options:* name any role the snapshot names, or only controls.
  - *Chosen:* controls. A heading, paragraph or list item takes its name from its text, so locating one by name is a text locator for a container, which the grammar forbids.
- **What counts as stable.**
  - *Options:* an allowlist of known-good class names, or a rule that refuses what tools generate.
  - *Chosen:* the rule. An allowlist would be per app.
  - A token is one plain CSS identifier, so it needs no escaping and can't carry `>>`, a quote, a colon or a trimmed character. It has no generated prefix and no hash-like part, and it is no state class.
  - An id with any other character is left out, never escaped.
  - Attributes are `name` and `type` only. Other attributes are often text (`title`) or per item (`href`).
- **The scope is the nearest ancestor that makes the locator unique.**
  - *Options:* the nearest such ancestor, a list of landmarks, or nested scopes.
  - *Chosen:* the nearest, one level deep.
    - On the pilot it is the banner (`div.banner`) for everything the article page renders twice.
    - `html` and `body` never count, because every page has them.
- **Every locator is resolved before it is kept.**
  - The element's facts (tag, id, classes, attributes, labels and ancestors) are read in the page's own world, so they are the page's word. They are checked strictly, with bounded sizes.
  - Each candidate is resolved alone, for the use, on the live page, and kept only if it finds that element.
  - *Which element it found is judged outside the page's world.*
    - *Options:*
      1. compare the two elements in the page's own world;
      2. mark the element with a random attribute and read the mark back through Playwright's utility world;
      3. hold the element in a selector engine of our own that runs in the utility world.
    - *Chosen:* the third. #52's reviews defeated the first two.
      - Playwright's evaluation in the page's own world passes arguments through iterators the page can rewrite, and a page made two elements compare equal.
      - A mark is DOM state, which the page can move onto a decoy. Its listeners run when Playwright marks the target of `get_attribute`, and its hit test runs in the page's world. Waiting for an attribute could also hang for good on an element moved into another document.
    - The engine (`aqa-identity`, registered with `content_script=True`) keeps the used element in a closure in the utility world, and answers one query: is this the element it holds? It reads no DOM state and fires no event, and a query on an element that has left the document fails at once.
    - Engines reach only browser contexts created after them, so whoever opens the session calls `register_identity_engine` first. Generating without it is a programming error, which says so.
  - A page can make the generator build fewer or odder locators, but never one that finds another element (`test_the_page_cannot_fake_which_element_a_locator_found`, `test_a_page_that_tampers_with_the_trial_gets_no_locator_for_another_element`).
- **The work per element is bounded.**
  - Only the nearest 16 ancestors and the first 16 stable classes of a node are tried, and each scope is counted on the page once.
  - One element costs at most 400 tries, each a resolution or a count. Tries that run out keep the locators already found.
  - A page whose scripts busy-loop can still stall one call. Only a bound on the whole explore run's time closes that (ADR-0024, `minutes`).
  - Resolution's `is_enabled` waits on an element moved into another document, as `get_attribute` did. That is the executor's path (#46), outside the generator.
- **Known limits, for the callers (#53, #46):**
  - A class that flips with state, such as Bootstrap's `btn-outline-primary` and `btn-primary`, can't be told from a stable one. A target used before and after such a flip is checked at both uses (#52's third pull request).
  - A page can copy a filled secret into a name, a test ID or a placeholder, and a locator built on it would carry the secret. Before a script is written, its caller must drop, never redact, any locator that reveals a secret value.
