# ADR-0033: M1 saves no screenshots; bound-field masking moves to M2

- Status: Accepted
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
