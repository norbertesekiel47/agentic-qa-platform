# ADR-0033: M1 saves no screenshots; bound-field masking moves to M2

- Status: Accepted (amended 2026-10-27: the capture and the record's binary writer exist, ADR-0034; 2026-10-28: the OCR check, ADR-0035; 2026-11-14: the paths that save screenshots, #251)
- Date: 2026-10-08

## Context
ADR-0026's Evidence decision saves a screenshot after each step and masks every secret-bearing field in it, and #50's second criterion asks for that: "Every saved screenshot masks the secret-bearing field." Three facts shape what M1 can deliver:
- **Playwright's mask is an overlay the page owns.** In a measured probe, a page hid it with one CSS rule and removed it with a MutationObserver. Both times the saved PNG showed the bound field's fake value in full, while the field's identity, box and frame stayed the same before and after the capture (LAB_NOTES, 2026-10-07). No check made before or after the capture can see an overlay the page hid during it.
- **Most pilot runs bind a secret.** Four of the five Conduit pilot specs bind `TEST_PASSWORD` (`bench/apps/conduit/qa/`: login, favorite-article, post-comment and publish-article; read-article doesn't).
- **M1 has other evidence and no model that looks at pixels.** Each kept step already saves a scanned accessibility snapshot and metadata-only console and network logs (ADR-0026's #50 B amendment), and M1's navigator reads the accessibility tree only (ADR-0024).

## Options
1. **Masked screenshots:** Playwright's `mask`, with the field's identity, box and frame checked before and after the capture. Both measured attacks pass every one of those checks.
2. **Screenshots only where nothing is bound:** capture only in a run that binds no secret and shows one main document, and withhold every other. That saves none for four of the five pilot specs, and still adds a capture entry, a binary writer and lifecycle tests (planned at 315 to 385 changed lines) for an image of the simplest pages, which can still show sensitive content the run never bound.
3. **No screenshots in M1:** bound-field masking moves to M2, beside the OCR checks.

## Decision
Option 3 (the maintainer, 2026-10-08). M1 has no screenshot capture and no binary writer; a step's evidence is its scanned snapshot and logs. #50's second criterion moves to #171.

## Consequences
- **Supersedes, for M1 only,** ADR-0026's "Every secret-bearing field is masked in every screenshot" and its consequence that local screenshots can show a reflected secret. The rest of ADR-0026 stands.
- **No M1 file holds pixels,** so ADR-0026's pixel-reflection residual has nothing to apply to until M2.
- **M2's masking has to be established where the capture happens,** not by an overlay the page can reach: a mechanism that sets the final geometry and the mask in one trusted capture step, or keeps the page's rendering frozen through it. It has to defeat both measured attacks and hold for a field that moves away and back during the capture, closed shadow roots, re-rendering, frames, scrolling, fractional geometry and device scale, under sandboxed Chromium, with no path that saves an unmasked image. Checks before and after the capture, retries, and masking a rectangle afterwards are not proof.
- **The run record's contents are pinned:** `test_evidence.py`'s `test_the_run_record_holds_only_the_steps_and_each_steps_evidence` lists every file a hostile replay leaves, so a step that saved anything else would fail it.
- No dependency, paid resource or model call.

## Amendment (2026-10-27): the capture exists, and no run path saves one yet (#171)
- **The mechanism is ADR-0034's.** Once `fill_secret` has handed the page a value in the browser session, a screenshot is a render of the page's scrubbed snapshot, captured with no script and no network; before that, the live page's. It meets this ADR's bar: the masking is in the capture, not in anything the page can reach. The hostile pages of both reviews of #171, M1's two attacks among them, show no bound value in 8 captures each.
- **"No binary writer" no longer holds.** `RunRecord.write_screenshot` writes a `Screenshot`, which only `aqa_runner.screenshots.take_screenshot` makes. Still, no run path calls either until #251, after #250's OCR check, so the run record's pinned contents (`test_evidence.py`) stand.

## Amendment (2026-10-28): the OCR check is in place (#250)
- In a run that binds a test secret, `take_screenshot` OCR-checks every image it captures, live or rendered, from the first capture, before a `Screenshot` exists, and withholds one whose text spells a bound value (ADR-0035). A run that binds nothing runs no OCR. No run path saves a screenshot until #251, so `test_evidence.py`'s pinned contents still stand.

## Amendment (2026-11-14): the paths that save screenshots (#251)
- **Exactly two paths save an image**, each through `aqa_runner.evidence.capture_evidence`, which holds the one call of `take_screenshot` and of `RunRecord.write_screenshot` in the packages' source:
  - `evidence/<seq>/screenshot.png`, for a step that drifted or failed, in `aqa replay`, `aqa run` and `aqa heal <spec>`, which all replay through `strict_replay`;
  - `heals/<key>/screenshot.png`, at a heal's first look. Its later looks save none.
- **Nothing else saves one.** A completed step saves none, and neither do explore (its attempts and its confirmation replay) or the bench pilot. No image reaches a model: a heal's requests are as before, and sending one is #252's and #256's.
- **Decisions** (the maintainer may escalate the fourth, a product call):
  1. The file is `screenshot.png` in both folders, a name that leaves the follow-up's before/after choice open.
  2. Explore and the bench pilot are left out by an opt-in flag that only `strict_replay` sets (`executor.RunSetup.screenshots`, off by default), so a new caller can't save page pixels by accident.
  3. `replay.json` stays schema 1. Each verdict's `evidence.screenshot`, always null before, describes the step its `evidence.path` cites: `{path, withheld}`, or null when that step asked for none; in `aqa run`, each `heal` object gains `screenshot` in the same shape, for the heal's first look (DATA_MODEL §7).
  4. An OCR engine error withholds the capture instead of propagating (ADR-0035's #251 amendment).
  5. A page off the run's allowed origins, or a frame change during the capture, withholds it with a fixed reason and records no policy event, as evidence's own look records none (`take_screenshot(session, record_refusal=False)`).
  6. A write the disk or a link refuses still raises, as every evidence write does: the disk's is a `record_error` stop in replay, and the heal's record-error stop in a heal.
- **A withheld capture** writes nothing and changes no step outcome, verdict, reason or exit code. Its fixed reason, which never quotes the page, is kept only in `replay.json`, scanned with the run's scan as every reason is. A drifted step whose heal gave up saves two images, the heal's look and the kept step's, which can show different moments.
- **A capture on a page that never answers again** is withheld within `CAPTURE_SECONDS`: `screenshots._devtools` detaches only once its calls have answered, so a failed step on a stalled page can't hold the run (LAB_NOTES, 2026-11-14).
- **The run record's pinned contents:** `test_evidence.py`'s `test_the_run_record_holds_only_the_steps_and_each_steps_evidence` now asks for screenshots in its hostile replay, as `strict_replay` does, and lists exactly one new file, `evidence/3/screenshot.png`, for its drifted step.
- **Amends ADR-0024's #243 "Observation before screenshots":** a heal's first look keeps a masked screenshot as evidence, and a verdict cites it, while the healer still reads only the accessibility tree.
- **Left to follow-ups:** the "before" screenshot, with what "before" and "after" mean for a drifted step, and the report's Screenshot tab and its swipe.
