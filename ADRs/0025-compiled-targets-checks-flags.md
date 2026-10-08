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
- **A target whose text is checked is found by no name.**
  - *Options:*
    - compare the name with the coverage plan's prose claim;
    - drop a name the assertion's own check would accept;
    - give a text check's target no name at all.
  - *Chosen:* the third.
    - The prose has no fixed form, so whether it "mentions" a name is guesswork.
    - The second was built first and fell to #52's reviews. A name and the rendered text can share words without either accepting the other: `aria-label="Save"` on a button that reads "Save changes", text styled upper case for a case-sensitive pattern, or a count hidden from the name. A change to the text then changes the name too, and a locator by that name misses the element exactly when the check should fail, as under conduit-bug-003, where the button keeps "Favorite Article". That turns the failure into drift.
  - An assertion that checks no text, such as `visible_unoccluded` on the header's links, keeps a control's name.
  - With nothing else to tell the element apart, a text check's target gets no locator, and compiling fails by name.
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
- **A target used at several points is split, never thinned.**
  - *Options:*
    1. keep one target, and drop the locators that miss at a later use;
    2. a later use joins the current target only if every one of its locators holds there, and otherwise starts a target generated there;
    3. as the second, but a use may join any earlier target of the meaning.
  - *Chosen:* the second.
    - The first keeps a target whole by weakening it: a locator that missed at one use would be gone at every use, and "Locator grammar" requires each one at every use.
    - The third would save a target when a state flips back, but which target a use gets would depend on the whole history. The second compares a use with the target before it, and nothing else.
  - *Holds* means two things. The locator is a kind the use allows: an assertion's target has no label or placeholder, and a text check's has no name. And, alone, it resolves for the use to the element used, or, at a negative check, finds its scope with nothing visible in it. So the favorite toggle splits: the click finds it by a name that favoriting changes, and the check after reloading reads its text.
  - `TargetUses` holds one meaning and whether any of its checks reads text (`checks_text`, from the coverage plan's checks, #41), so every use gets the same rule. Its errors name the meaning, which `generate_for_action` and `generate_for_assertion` are never given.
  - A class that flips with state now splits a target. A Bootstrap `btn-outline-primary` locator holds at the first click and misses at the second, so the second click starts a target (the known limit below).
- **A negative check's target is generated where the element was seen.**
  - The element is gone at the check, so the caller shows it to `TargetUses` while it is on screen (`see_for_negative_check`), and the target is made at the check (`add_negative_check`).
  - It uses an assertion's kinds, and every locator is scoped, as the loader requires (above, "a negative check's target must be scoped").
  - *Which scope:*
    - *Options:* the nearest scope that is unique where the element was seen; a list of landmarks; or the nearest that is unique where it was seen and also holds at the check.
    - *Chosen:* the third. On the pilot, `ul.nav` is the nearest unique ancestor of the header's links on the login page. The home page's feed tabs are a `ul.nav` too, so under the first option login's "no Sign in link" check had no scope. A list of landmarks would be per app.
    - So each locator is kept under every scope that finds the element where it was seen, nearest first. At the check, of each kind, the first whose scope is on the page with nothing visible in it is kept: on the pilot, `ul.navbar-nav`.
  - *An element still shown:*
    - *Options:*
      1. keep the locators that miss it and drop those that find it;
      2. let the first locator of each kind whose scope is on the page decide;
      3. look at every locator seen, and fail if any finds something visible.
    - *Chosen:* the third. #52's security reviews defeated the first two.
      - Under the first, a page that changes the attribute one kind uses, such as its test ID, keeps the element on screen with a target that misses it, so the check passes on every replay.
      - Under the second, a page that renames a button and its class keeps it on screen, while the bare `button` its toolbar holds, a later locator of the same kind, still finds it.
    - A visible match counts whether there is one or several, because several hide which one is the element.
    - The price is that a broad locator, under a far scope, may find another element like it, and then the check fails by name too.
    - A look the page breaks, as by navigating, establishes nothing, so it fails the check as well.
  - A check whose scope is gone, as on an error page, or whose element is still shown, gets no target, so compiling fails by name. So does a check of an element never seen, or one whose latest sighting failed. Every negative check, one that joins a target included, is judged against the latest sighting.
- **Known limits, for the callers (#53, #46):**
  - A class that flips with state, such as Bootstrap's `btn-outline-primary` and `btn-primary`, can't be told from a stable one. A target used before and after such a flip is checked at both uses, so the flip splits it (above).
  - A page can copy a filled secret into a name, a test ID or a placeholder, and a locator built on it would carry the secret. Before a script is written, its caller must drop, never redact, any locator that reveals a secret value.

## Amendment (2026-10-03): `probe_equals` and JSON paths (#48)

The Decision's "only the checks with fields" left `probe_equals` refused until #48. DATA_MODEL §7 holds the rules; this records why.

- **`probe_equals` takes `probe`, `json_path` and `value`.**
  - *Options:* (1) the planned check's `probe` and `value`, compared with the whole body; (2) those and a `json_path`, as `probe_baselines` has; (3) a path per probe, in a table beside `probe_baselines`.
  - *Chosen: 2.* A probe answers JSON, such as the pilot's `{"count": 2}`, and a claim is about one value in it. The baseline check already names its value by `json_path`, so both checks read a probe the same way. Compiling adds the path, as it adds a target's locators: the plan names the probe and the value from the spec alone, and only exploring sees the response.
  - *`value`* is an integer or a non-empty string, as the planned check's is (#41). `true` and `2.0` aren't `2`.
- **A JSON path is a small grammar, not JSONPath.**
  - *Options:* (1) RFC 9535 JSONPath, through a library; (2) `$`, then `.name` or `[index]` steps only.
  - *Chosen: 2.* A path selects one value. Wildcards, filters and slices select sets, and a quoted key's escapes are a second syntax to get right. It needs no dependency, and `aqa_core.compiled.json_path_steps` reads it. An index has at most nine digits: no array is longer, and Python's `int()` refuses a string of more than 4,300.
  - `probe_baselines.<name>.json_path`, a `NonEmpty` until now, takes the same grammar. The example's `$.count` already fits it.
  - A key with characters other than ASCII letters, digits, `_` and `-` can't be named yet. A probe that needs one adds a quoted form.
- **Additive:** a committed script stays valid, and `schema_version` stays 1.


## Amendment (2026-10-08): subject-contract schema and replay admission (#53 P7a)

The reviewed #53 v6 rulings MR and MR6 make location a config decision rather than a page-derived guess. Each reviewed subject is keyed by spec ID and zero-based expectation index. The decision's schema and grammar live in DATA_MODEL §7 and §9.

- **Every row requires a region and a part.** A region with a childless-element check alone can accept a flattened control or an unrelated childless decoy. A region without a part can accept a container holding the expected text. The required, value-independent part names the intended element; `leaf` defaults to false and adds childlessness when enforced. Region, PartSelector and Contract are shared core types, avoiding a config/compiled import cycle.
- **Rows are a list with distinct subject keys.** Config parsing validates strict fields, required part, the restricted grammars and duplicate `(spec, expect)` keys. Project loading also checks the named spec and index. A row cannot silently shift onto another subject or become a partial contract.
- **Provenance includes the contracts.** Every compiled script requires `compiled_by.subject_contracts`, the canonical hash of its spec's normalized `{expect, region, part, leaf}` rows sorted by index. Other specs' rows do not change it, and no rows has the hash of `[]`. A changed contract requires exploration again, as a changed spec does.
- **Replay checks agreement before accepting steps.** It refuses a different fingerprint, a listed expectation without its governing target contract, and every target sharing that target's semantic meaning without that contract. A listed expectation with no target is refused too. Refusal is a spec error before secret binding, browser launch or an intent. Correctly fingerprinted no-row scripts remain admissible.
- **Contracts become loadable with enforcement (D46).** P7a's target format still forbids unknown fields, including `contract`, so all scripts of listed specs fail closed. P7b adds Target.contract with region-rooted resolution, the held-region postcondition and the reviewed Conduit rows in one change. P7b precedes the compiler, and P7a's public replay refusal tests remain true afterwards.
  - Alternatives were a temporary blanket refusal removed in P7b, which would require changing an earlier boundary test, or one atomic schema/enforcement PR beyond the approved production-size bar. The persistent agreement rule gives each intermediate revision an enforceable format.
- **Consequences.** Handwritten fixture scripts carry the no-row fingerprint. Benchmark builders compute it from their own loaded temporary projects. Existing scripts missing the required field are invalid rather than silently upgraded. This adds no dependency, model call, paid resource or tenant surface. Target enforcement and Conduit rows remain P7b work.

## Amendment (2026-10-08): trusted subject engine and offered-handle verdict (#53 P7b-I)

The reviewed MR/MR6 contract is a location rule from config. P7b-I supplies its trusted engine and direct boundary; public generation, compiled target fields, admission/resolution integration and the five Conduit rows belong together to P7b-II. Until that integration lands, P7a continues to refuse every script of a listed spec. The engine alone does not make a contract loadable or enforce it in public replay.

- **The offered handle stays the offered handle.** `binding_verdict` returns Bind or a fixed Refused reason, never a replacement element. A listed use must be the region's one matching part, childless when leaf is required. The verdict does not consult copy detection for listed uses and does not choose by asserted text.
- **Authority stays in the utility world.** `register_identity_engine` registers both `aqa-identity` and `aqa-binding` before browser contexts open. Missing binding registration is a programming error. A random token holds the original region in an isolated closure. No DOM attribute carries that authority. The region must remain connected, in the same document and the region selector's unique original match. That document is the engine's own frame document, the one acquisition searched: Playwright sends a handle's queries to the frame that created it, so a region or offered element moved into another document, before or after the hold, is drift. Containment, exact part identity and leaf checks are native reads there. Separate diagnostics only refine a refusal; they cannot admit an element after the trusted predicate refused it.
- **Page nodes are read through prototype natives.** Every read of a page node goes through a prototype getter or method called on the node, because a form's named controls override the element's own properties even in the utility world (a `<fieldset name="ownerDocument" form=…>` makes the form's `ownerDocument` the fieldset). Each native is resolved on the engine's first utility-world query, which is as safe as resolving it at load because the page can't reach that world's globals. That holds while the engine is queried only in the utility world, so P7b-II queries it only through `ElementHandle.query_selector_all` (as `_query` does), `Locator.count` or `Locator.element_handles`, on a selector whose every engine is a content script. Other routes can run the engine's main-world copy, whose natives are the page's; measured ones include `evaluate_all`, `all_text_contents`, `all_inner_texts`, `wait_for_function` and a chain through a non-content-script engine, and by Playwright 1.63's source `eval_on_selector_all` and a selector `dispatch_event` do too. Load was ruled out because Playwright also evaluates the engine source in the page's main world: measured, a page whose `Object.getOwnPropertyDescriptor` throws made a load-time lookup break main-world calls such as `dispatch_event` (`test_a_page_poisoning_its_own_globals_keeps_main_world_actions`; LAB_NOTES).
- **Every attempted hold is cleaned up once.** A hold that failed or whose acknowledgement was cancelled counts: one drop attempt follows, then one handle-disposal attempt, even when the drop failed or was cancelled; nothing is retried or shielded. Successful cleanup retires the token on normal exit, body failure and cancellation, and the body's exception propagates unchanged. A failed drop is never taken as retirement, so a navigation inside a held region surfaces as the drop's Error. A cancellation or other non-Exception BaseException is always the exception raised, so `asyncio.run`, `asyncio.timeout` and `TaskGroup` still see it: one that ended the body is raised from the cleanup failure, and a cancelled drop keeps the body's error as its cause. Otherwise the latest cleanup failure is raised from the one before it (disposal from drop, drop from the body's error).
- **An empty locator alone proves nothing.** After a zero lookup, the direct negative boundary checks the original held region's identity and uniqueness together with all part visibility in one synchronous utility-world query. Region replacement, class movement, duplication or disconnection returns false, never an absence proof. The guarantee covers the light DOM and open shadow roots. Playwright's CSS pierces open shadow roots and native reads don't, so a held region that is or contains an open shadow host fails the read: drift or a refusal, never an absence or a part match. A region that itself lies in a shadow tree fails it too, because the original region must be the document's light-DOM match. User-agent shadow roots don't count. Region uniqueness counts matches through every open shadow tree, so a duplicate there is drift too. Absence covers what the engine and Playwright's CSS both reach, not rendered pixels: a part rendered inside a closed shadow root or a user-agent tree such as an SVG `<use>` instance is invisible to the engine and to every Playwright locator, and one inside a nested browsing context (iframe, frame, object, embed) is invisible to the engine and to every locator a compiled script can express, since page locators never enter a frame; either can still read as absent. That is a documented residual; trusted CDP detection of closed roots is an M2+ follow-up. Enumeration followed by later visibility or identity reads is rejected. P7b-II applies this same boundary to ordinary resolution and generation; P11 later activates every-locator confirmation.
- **Visibility follows pinned Playwright 1.63.** Display-none, hidden visibility and zero boxes are invisible. Display-contents observes its element or text children. Opacity zero and offscreen boxes retain Playwright's visible classification. This adds no occlusion or viewport policy. Direct controls compare the isolated predicate to Playwright on fixed fixture pages, and neither page-world overrides nor a form's named controls can change its answer.
- **Detection is a bounded refusal trigger for unlisted uses.** It walks the nearest sixteen ancestors below body. A branch tag path through a custom element reaches copies in sibling branches with the same tag and a different set of stable classes. Same-style repeats are not copies. Stable classes use the generator's existing identifier/state/generated-prefix/hash rules. Beyond sixteen ancestors, sixty-four children or eight copies it refuses too_many. Detected copies refuse unlisted_copies; missed layouts bind only the offered handle. No custom component or a different depth can miss copies; a reviewed row is the remedy. Direct capture tests retain the fifteen article-meta pairs and zero groups on login, home and editor.
- **The disposable App is test infrastructure.** It serves saved captures, explicit planted markup modes and template-inferred published-author markup, with reset, held-write barriers, a successful local WebSocket write and counters. Published markup is not a measured capture. Server cleanup joins its connections. This implements no P8 phase/reset/teardown behavior; listener-retirement and the standalone post-browser-teardown infrastructure recheck remain separate work.

The separate benchmark adapter's registration and actual script/config/fingerprint compatibility remain a pre-P7b-II-merge prerequisite under v6 §16. This unit imports no held adapter repair. It adds no dependency, model call, paid resource or tenant surface.

## Amendment (2026-10-08): target contracts in resolution, generation and admission (#53 P7b-II)

P7b-II makes the reviewed contract loadable together with its enforcement (D46). A compiled target gains `contract: {region, part, leaf} | null`, the five Conduit rows land in the pilot's config, and every public path that binds a subject runs the P7b-I engine on it. DATA_MODEL §7 and §9 hold the rules.

- **The contract lives on the target, and resolution applies it.** Locators stay relative. A locator repeating the region as its outermost scope was rejected: a hand edit or a generator bug could leave one locator unscoped, and only a validator would stop it. A field that resolution always applies can't be partly applied.
- **The handle is judged, not the selector.** Resolution holds the region's one element for the look and runs each locator under the region selector's match. A `:scope` sibling selector or an escaping scope can still find an element outside the region. So every scope's element must be inside the held region, and the found element must pass the held postcondition as the last read, after actionability for an action. A region relabelled, duplicated or replaced between holding and retrieving is drift. The executor dispatches an action only after the look has returned and the hold is released, so no action of ours navigates inside a held region.
- **Absence is one read.** After every locator's zero result, the engine's single utility-world read decides: the held region is still the selector's only match and none of its parts shows. Ordinary negatives and generation's looks share this boundary, and P11's every-locator negatives will reuse it. A missing, repeated, replaced, relabelled, removed or adopted region is drift, never absence.
- **Errors are classified by evidence, never by message.** A navigation inside a held region breaks its release with Playwright's error; `BrowserSession.resolve` raises `DocumentChangedError` when its frame-change count moved, and the executor looks again. A region another document adopted makes Playwright refuse every utility-world query on it with its own error. Resolution therefore keeps its own main-world handle on the held region, and after an error it reads whether the region left the page's document. True is drift; anything else raises. A page that lies to that read only chooses between drift and the error: it can make an adoption raise, or a broken script drift, but never a binding or an absence.
- **Every use runs the verdict on the offered element.** `TargetUses` takes the meaning's contract, judges every add and sighting with `binding_verdict` first, and raises `BindingRefusedError` with the fixed reason. A refusal starts no target and keeps no sighting. Every candidate, look and target carries the contract, and a scope counts as unique inside the region. A meaning with no contract keeps P7b-I's rule: refused where copies are detected, otherwise bound as offered. A negative check's locators stay scoped, because the loader refuses an unscoped `not_visible` target; the region is an extra gate on top of the scope.
- **Admission agrees with the rows.** `contract_problems` admits a governed target only with exactly its row's contract. It refuses a contract that no row gives its target, because a forged region holding none of the subject would otherwise let a negative check prove absence while the subject shows. Heals may not change a contract (DATA_MODEL §7). P7a's boundary tests keep their literal messages.
- **Rows.** read-article 1 and 2, publish-article 3, favorite-article 0 and 1, all in `div.banner`, with the parts and leaf flags v6 measured; publish-article 3 rests on the shared article template, not a capture. No compiled Conduit script exists, so nothing migrates. Scripts #51 writes for a listed spec must carry the rows' contracts and the fingerprint, and #51 B3b's admission repeats `contract_problems` before its reset.
- **Consequences.** P7b-II must merge after the engine fix for shadow trees and the frame document. This adds no dependency, model call, paid resource or tenant surface. Each contracted look costs a few utility-world reads plus one main-world handle; the cost is unmeasured.
