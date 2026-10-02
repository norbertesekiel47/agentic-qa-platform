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
  - *Chosen:* refuse it. Playwright chains selectors at `>>` even after `css=`. On 1.63 a value chained into XPath and into an engine that enters frames. Escaping would hide the author's mistake, and an attribute value can write `\>\>`. Resolution must send the value as `css=<value>` (#45), so no other engine is reachable.
- **`side_effect_basis` goes only with a true flag.**
  - *Options:* refuse it on a false flag, or allow it.
  - *Chosen:* refuse it. A person who lowers a flag removes its basis in the same edit, so the pull request shows both.
- **A navigate stores a path on the start origin,** held to `start_url`'s rules, and the executor joins it as the start URL is joined (#89). Schema version 1 records no navigate to another allowed origin, because nothing needs one yet. One that does adds absolute URLs with the allowed-origin check.
- **Only the checks with fields are in schema version 1:** those §7 gives fields to, plus `not_visible` and `network_seen`. The rest are refused by name until their fields are defined: `probe_equals` in #48; `pixel_diff`, `contrast_min` and `model_verify` in M2. Adding a check is additive, so no committed script breaks and `schema_version` stays 1.
- **`browser` records every setting.** A missing one is an error, never the pinned default, because replay uses what the script records.
- **The loader, not the format, checks the parts against each other:** names that must exist or be unique, and repeated JSON keys. A repeated key matters because pydantic keeps the last one, so a hidden `"side_effect": false` would win (#46).

## Amendment (2026-10-02): resolving a target per use

Building resolution (#45) settled three choices that "Resolution per use" left open. DATA_MODEL §7 holds the rules.

- **A negative check's absence must be unanimous (Q1).**
  - *Options:*
    - A: the first locator whose scope resolves decides, so its zero matches is absence;
    - B: absence only when no locator finds the element.
  - *Chosen:* B.
    - Under A, a button relabeled from "Delete" to "Remove" finds nothing by role and name. A `not_visible` check would then pass in strict replay while the structural fallback still sees the button: a weaker check than the claim.
    - Under B, any unique match resolves the element. Several matches are drift. The element is absent only when no locator finds one, none finds several, and at least one finds its scope empty.
- **Actionable is one look at the page (Q2).**
  - *Options:*
    - J: visible, enabled, and a hit test at the element's center finds it, after scrolling it into view if needed;
    - P: Playwright's trial click with a timeout, which also waits for stability.
  - *Chosen:* J.
    - Resolution stays a single judgement, and the executor's loop owns waiting within `resolve_seconds`. A trial click would wait inside every locator's turn, so the waits would add up across fallbacks.
    - The rule is the same for every targeted step, `fill` and `select` included. That is stricter than Playwright's own fill, which skips the hit test, because a person can't fill a field under an overlay either.
    - An element in a shadow root or a frame fails the document's hit test, which is drift, never a wrong action.
- **`pattern` searches the normalized rendered text (Q3),** the same text `text` matches, so a pattern spans line items without `(?s)`. `url_matches` searches the URL as it is.
- **Role names are matched by Playwright's own role engine.**
  - The name is matched with an anchored pattern built from the normalized name. The pattern lets private-use glyphs sit anywhere and lets a space be any run of whitespace and glyphs.
  - Each non-alphanumeric character is written `\uXXXX`, which Python and JavaScript read alike, so no character of a name becomes selector syntax.
  - Only the Basic Multilingual Plane's private-use range is allowed. Playwright passes the pattern to JavaScript without the `u` flag, so a name holding an astral glyph doesn't match, which is drift.
