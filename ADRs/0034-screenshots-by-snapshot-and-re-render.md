# ADR-0034: Once a secret is filled, a screenshot is a render of a scrubbed snapshot

- Status: Accepted
- Date: 2026-10-27

## Context
- **#171 moves #50's masking criterion to M2** (ADR-0033). A saved screenshot must not show a bound field's value, even on a page that adapts to the capture. ADR-0033 sets the bar: the masking happens where the capture happens, not in anything the page can reach, and checks before and after the capture, retries, and masking a rectangle afterwards are not proof.
- **The page holds the value.** It is on one of the secret's destinations, so a page that wants the value on screen can always draw it itself. That is reflection: best-effort under SECURITY §5, and #250's OCR check. What a capture can promise is narrower: nothing a field holds, and no text or attribute the DOM carries, shows a bound value.
- **Most pilot runs fill a secret.** Four of the five Conduit pilot specs bind `TEST_PASSWORD` (ADR-0033), so withholding every screenshot once a secret is filled would leave the pilot almost none.

## Options
1. **Playwright's `mask`.** A page hid its overlay with CSS and removed it with a MutationObserver (M1; LAB_NOTES 2026-10-07; ADR-0033).
2. **A browser-drawn mask or a frozen page.** Measured by #171's first plan (`.scratch/ticket-171/plan-v1-value-removal.md`, Chromium 153): `Overlay.highlightNode` isn't in `Page.captureScreenshot` (3,333 red pixels before and after); `Page.setWebLifecycleState` frozen leaves rAF and interval movers moving on a visible page (12 of 12 captures moved); `Page.captureScreenshot` times out under `Debugger.pause`; `Emulation.setScriptExecutionDisabled` drops a pending timeout, an image's `onload` and a MessagePort message, so it changes the app under test; `Animation.setPlaybackRate(0)` misses an animation on a page-made `DocumentTimeline`.
3. **Value removal.** Empty every text field from an isolated world for the capture and restore it after. It passed 0 leaks in 8 on pages that ignore the mask, but three reviewers defeated it with pages that adapt (reviews/171-interrogate.md):
   - a type flip to `submit`, `button` or `reset` copies the value into the button's label: 8 leaks in 8;
   - a write-back and clear inside one frame passes the post-check: 10 in 10;
   - a clone or swap that stays out of the document at both checks and shows for one frame: 10 in 10 and 9 in 10;
   - an element-scoped or an ancestor frame's view transition escapes the transition check: 3 in 3;
   - a hidden contenteditable escapes detection by computed style: 10 in 10;
   - the restore is unbounded under a hung renderer (3.01 s and 8.01 s against a 0.5 s budget), and after a route change it can type the value into a different field.
4. **Snapshot and re-render.** Scrub a serialized snapshot of the page with the run's scan, render it with no script and no network, and capture that.
5. **No screenshot once a secret is filled.** Safe, and it leaves four of five pilot specs with almost none.

## Decision
**Option 4: the prototype's variant B, with its residuals 1 to 3 closed** (the coordinator, 2026-10-27; reviews/171-prototype.md). `aqa_runner.screenshots.take_screenshot(session)` does this:

- **The predicate is per browser session.** Until `fill_secret` hands the page a test secret's value, the screenshot is the live page's, from one DevTools `Page.captureScreenshot`. The session flags the value as handed over just before the fill runs, so a fill that fails partway counts too. A run that binds nothing always gets the live page. An explore attempt's fresh browser starts on the live path until its own first fill. A value the app's server shows before that is reflection, and #250's.
- **From then on, the snapshot.** Under the session's lock and origin checks, DevTools takes `Page.getLayoutMetrics` for the scroll, `Page.captureSnapshot` for the MHTML archive, and `DOMSnapshot.captureSnapshot` for each field's value and each node's computed `-webkit-user-modify`. None of the three runs anything in the page's worlds.
- **Every archive part is scrubbed.**
  - Each part is decoded, text by its charset. Every span the run's scan finds is cut out, first in the raw text, then in the text with HTML's character references decoded, where a span takes every reference it touches.
  - Text is served to the render as UTF-8 of exactly the scanned text, whatever its encoding was, so the renderer reads nothing the scan didn't.
  - A binary part is served as it is.
- **Field values are scrubbed and dropped.** Values come from the DOM snapshot in its flat-tree order: shadow roots' content, slotted nodes, slot fallback, and each frame's document at its element. A value is dropped (made empty) when the scan finds a bound value in it, or when it shares `SHARED_RUN` = 4 consecutive characters with one, compared casefolded with gaps removed, or all of a shorter one. The render drops a field value it shows that the snapshot didn't hold by the same rule, before its capture: a `value` attribute, or a field in a frame whose document the snapshot leaves out.
- **Editing hosts are emptied.** Every element with a `contenteditable` attribute and every editing host is emptied in the render.
- **Why N is 4.** A test secret has at least 4 bytes besides gaps (SECURITY §5, #53 ZW), so any larger N would let a whole short secret, or a 4-character prefix a `maxlength` left, show. A smaller N drops more ordinary values that share a common trigram with a password. The tests check both sides: 3 shared characters are kept, 4 are dropped.
- **The scrub runs in a thread,** inside the render's own `CAPTURE_SECONDS`, so a large archive holds up neither the event loop nor the egress proxy on it. An archive over `ARCHIVE_CHARS` (400,000 characters) is withheld before it: a page of text with an archive that size took 2.4 s to scrub and render on #171's host under other suites' load, half the bound, so a slower host still finishes.
- **The render.** It runs in a new context of the session's browser, with JavaScript off and service workers blocked, at the session's viewport, scale, colour scheme and locale. The context inherits the launch's fail-closed proxy, and every request is answered from the archive or aborted.
  - Before the render loads the page: frames' `cid:` references are rewritten to the page's own origin, so every frame is same-origin, and `<template shadowmode>` becomes `shadowrootmode="open"`.
  - One evaluation walks every document's flat tree. Its fields (element and an input's `type`), frames and elements with a `contenteditable` attribute must match, in order, what the snapshot held before the scrub, so a cut that took markup is caught. The snapshot side leaves out what Blink's archive leaves out: `<noscript>`, hidden inputs, elements with a `hidden` attribute and frames in `<head>`, each with all it holds. The walk gives each field its typed value (a button's label and a file input are left as they are) and each checkbox and radio button its checked state, hides a frame it can't reach (sandboxed onto an opaque origin), empties editable elements and scrolls to the live position.
  - The field values and text the render then shows are scanned again, which catches a value the render's parser joins (a NUL it drops).
  - Playwright then captures the render in CSS pixels, as the live capture is (`animations="disabled"`, `caret="hide"`, `scale="css"`), and the context closes.
- **Withheld (`ScreenshotWithheldError`, nothing written).** The capture's reason never quotes the page. It is withheld when:
  - a part is in an encoding Python can't decode;
  - a binary part spells a bound value;
  - a spelling is left in any served text after the scrub;
  - the archive is over `ARCHIVE_CHARS`, isn't 7-bit MIME or names no page;
  - the page's checks and snapshot, or then the scrub and render, take over `CAPTURE_SECONDS` (5 s);
  - any DevTools or Playwright call fails, or DevTools replies in a shape the capture doesn't read;
  - any node the page draws is editable with no `contenteditable` around it (designMode, or editing turned on by an adopted style sheet or a shadow-root `<style>`, which a render can't reproduce);
  - the render's fields, frames and editable elements don't match the snapshot's;
  - the render's field values or text spell a bound value.
  - A page off the allowed origins raises `PolicyEventError`, and a frame change during the snapshot raises `DocumentChangedError`, as every observation does.
- **A `Screenshot` can't be forged.** Only `take_screenshot` makes a `Screenshot`: its constructor refuses without the module's key. `RunRecord.write_screenshot(name, shot)` writes it once, only to a record whose scan is the session's. A record therefore never holds an image no capture scrubbed.

### Measured
The prototype (reviews/171-prototype.md; Chromium 153.0.8010.12 headless shell, Playwright 1.63, macOS, sandbox on, 8 captures a case, a leak being more than 30 red pixels):

| Set | Variant B leaks | Note |
|---|---|---|
| Reviewers' 15 adaptive attacks | 0 of 8 each | 5 controls failed: values weren't restored in frames or shadow roots |
| Owner's 13 tricks, DPR 1 and 2 | 0 of 8, except a `maxlength` partial fill, 8 of 8 | closed here by N = 4 |
| Contenteditable split by an empty `<b>` or a comment | 8 of 8 | closed here by emptying editing hosts |
| A value with `&<"` after a type flip | withheld 8 of 8 | closed here by the decoded pass |
| `@media (scripting: none)` content | shown 8 of 8 in the render, 0 live | a residual |

| What MHTML carries | |
|---|---|
| Carried | text, attributes, document `<style>` (script-added rules too, as their own `cid:` parts), images, same-origin and cross-site frames, open and closed shadow roots (inline styles only), the main scroll |
| Not carried | live field values, script-ticked checkboxes, 2D canvas (10,000 red pixels to 0), WebGL, adoptedStyleSheets, shadow-root `<style>`, font files, inner scroll, `<noscript>`, hidden inputs, elements with a `hidden` attribute, frames in `<head>` |

| Conduit fidelity, B (share of pixels that differ / differ strongly) | home | article | login | settings | editor |
|---|---|---|---|---|---|
| 1440 px | .030 / .021 | .025 / .018 | .011 / .008 | .017 / .012 | .013 / .009 |
| 390 px | .101 / .073 | .106 / .076 | .048 / .034 | .055 / .043 | .055 / .037 |

Fallback fonts replace Source Sans, and the icon font shows boxes. The live page saw none of 31 event types, no mutation, no observer callback, and no change to focus, selection or value across 6 snapshots and 6 captures. A snapshot took 2-4 ms (median) on a 46- and a 15,005-node page, and waited out a busy main thread (1,397 ms on a page blocking 1.5 s). A variant B capture of Conduit took 505-555 ms, against 14-22 ms live.

This build, at `e27cc6c1`, same browser and host:

- **The suite.** `test_a_saved_screenshot_shows_no_bound_value_whatever_the_page_does` runs 43 cases: 39 pages, with 4 of them also at scale 2. They cover every reviewer attack above, the prototype's attacks on a snapshot and its scrub, and the owner's tricks. Each case saves 8 captures with the bound value filled, and every saved PNG holds 0 red pixels.
- **Controls.** Each case first runs a control on the same page with an unbound value. For a field, the control fills another secret so that the render runs. For an editable element, nothing is filled, since a render empties every editable element. The controls show 1,812 to 20,462 red pixels.
- **Withholds.** `blind-clone` was withheld once in 8, because its fields changed between the snapshot's two calls. No other case was withheld.
- **Against an unscrubbed render.** With the scrub, the value drop and the emptying of editable elements switched off, 41 of the 43 cases fail on red pixels. The other two, a decoy swap and a hidden contenteditable, keep the field off the live page too.
- **Capture time.** On these small pages a capture took 67 to 95 ms at scale 1 and 104 to 107 ms at scale 2 (median of 8, from a measurement script in the ticket's notes).

After the review fixes (#171's Phase 5), the suite runs 55 cases: 51 pages, 4 of them also at scale 2. The new pages are a sandboxed frame, frames whose document the snapshot leaves out holding a `maxlength` prefix, what the archive leaves out (a CSRF input, a frame in `<head>`, `<noscript>`, a `hidden` element the page's style shows), entity- and percent-encoded spellings, a textarea and a slotted field. Every saved PNG holds 0 red pixels and all 400 pixels of a green marker each page carries, so a blank render can't pass. `blind-clone` and `blind-type-flip` are withheld 1 to 3 times in 8 and must save at least one capture; no other case is withheld.

## Consequences
- **What is guaranteed.** In a session where a secret has been filled, a saved screenshot shows no text, attribute or field value in which the run's scan finds a bound value, raw or with HTML's references decoded. It shows no field value sharing 4 characters with one, and no editable element's content. A screenshot that can't meet that is withheld, and nothing is saved.
- **Residuals, named and not fixed:**
  - a value the page draws into an image (a `data:` or same-origin image), a font or anything else pixel-made: #250's OCR;
  - content the page shows only without script (`@media (scripting: none)`), and content a style the archive leaves out had hidden: the render shows it;
  - the render has no canvas, web fonts, adoptedStyleSheets, shadow-root `<style>`, inner scroll, or select's chosen option (restoring it would have cost about 10 more lines);
  - the render lacks what the archive leaves out that a page can still show: an element with a `hidden` attribute that the page's style shows; and it hides a frame on an opaque origin.
- **Canvas pixels are absent from every secret-filled screenshot.** This limits #252's visual checks and `pixel_diff` (#254) in those runs: a baseline taken live won't match a render.
- **A filled field renders empty**, and so does every editable element in a secret-filled session. A screenshot shows a filled password field as unfilled. Heal reads the accessibility tree for that.
- **A page can force withholding**: by changing its fields between the two snapshot calls, keeping its main thread busy, growing its archive past `ARCHIVE_CHARS`, or turning on designMode. A withheld screenshot is safe. #251 records the reason.
- **Seams for later tickets:**
  - #251 calls `take_screenshot(session)`, then `record.write_screenshot(f"evidence/{seq}/<name>.png", shot)`, and records the reason when it catches `ScreenshotWithheldError`.
  - #250 checks `shot.png` between the two, and before any model input.
  - No run path saves a screenshot until #251.
- **Amends ADR-0033**: the capture and the record's binary writer exist.
- **Cost.** A secret-filled capture took about 70-110 ms on the test pages and 0.5 s on Conduit in the prototype, against 14-22 ms live. There is no dependency, paid resource or model call.
