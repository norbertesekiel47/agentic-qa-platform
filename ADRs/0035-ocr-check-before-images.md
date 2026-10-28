# ADR-0035: An OCR check before any image is saved, sent to a model or leaves the machine

- Status: Accepted (the engine is open with the maintainer: RapidOCR is applied, Tesseract is the alternative)
- Date: 2026-10-28

## Context
- **Two triggers disagreed.** ADR-0033 and ROADMAP's M2 line ask for an OCR check before any image is saved, reaches a model or leaves the machine. SECURITY §5 said OCR runs "once any secret has been filled". ADR-0034 hands #250 a value the app's server shows before the first fill, when the capture is still the live page's, so a check that starts at the first fill misses it.
- **What a capture can't scrub.** ADR-0034's render removes a bound value from the page's text, attributes and fields, but not from pixels: a value the page draws into a `data:` or same-origin image, or, before the first fill, into live text or a canvas. Only something that reads the image can see it.
- **One place makes images.** `aqa_runner.screenshots.take_screenshot` is the only function that makes a `Screenshot`, and `RunRecord.write_screenshot` writes only a `Screenshot` (ADR-0034). No run path saves or sends one yet; #251 saves them, and #252 and #256 send them to a model.
- **A new dependency needs an ADR** (AGENTS.md §5), and its pins must be at least 3 days old (Renovate's floor, ADR-0031).

## Options
The engines were measured on the developer's Mac (Apple M4 Pro, Python 3.14; scripts in `.scratch/ticket-250/ocrbench/`). The probe set: 12 Conduit pilot pages as controls, and 18 plants of a fake bound value drawn into a canvas and shown as a `data:` PNG on Conduit's home page (10 to 24 px; sans, serif and mono; grey on white, light on dark, rotated 8°), at 1440 and 390 px wide.

| | RapidOCR 3.10.0 on onnxruntime | Tesseract 5.5.3 through tesserocr |
|---|---|---|
| Licence | Apache-2.0 (rapidocr and its PP-OCR models), MIT (onnxruntime) | MIT (tesserocr), Apache-2.0 (Tesseract and its trained data) |
| Install, macOS and ubuntu-latest | `uv sync`, wheels only, no system package | `uv sync`: the wheel bundles libtesseract and Leptonica, but not `eng.traineddata`, which would be vendored (4.1 MB fast, 15 MB best) and pinned by sha256 |
| Size installed | about 273 MB: OpenCV 119 MB, onnxruntime 75 MB, models 31 MB, numpy and the rest | about 8.6 MB, Pillow and the trained data |
| Plants read exactly | 18 of 18 | 4 of 18; 11 to 12 at 3x upscale; 15 with a one-edit fuzzy match; the rotated plant missed in every mode |
| False positives, 12 controls | 0 | 0 |
| Time an image, Mac | 0.2-0.66 s | 0.05-0.15 s, 0.15-0.56 s at 3x |
| Dense text, Mac | a 1440x900 page of 10 px text: 9.6 s; 1920x1080: 15.7 s | not measured |
| Deadline | none in its API | `Recognize(timeout=ms)` |

1. **RapidOCR.** It reads screen text exactly, so the run's existing scan is enough and no fuzzy matcher has to be written. It costs install size and time, and has no deadline of its own.
2. **Tesseract.** Small and fast with a deadline, but it needs upscaling and a fuzzy matcher, and still misses plants RapidOCR reads.
3. **Rejected without measuring:** EasyOCR, docTR and PaddleOCR, which need torch or paddle (gigabytes); Apple Vision, macOS only; a cloud OCR service or a model, which sends the image off the machine.

## Decision
**Option 1, RapidOCR** (the coordinator, 2026-10-28). The maintainer holds the engine open: RapidOCR's 273 MB against Tesseract's recall. The engine is behind `aqa_runner.ocr`, so a change of engine touches one module and its pins.

- **One trigger rule.** In a run that binds a test secret, every image `take_screenshot` captures, live or rendered, from the first capture, is OCR-checked before a `Screenshot` exists. Since only a `Screenshot` can be saved or, later, sent, every image a run saves, sends to a model or uploads has passed the check. A run that binds nothing has nothing to look for and runs no OCR.
- **The check** (`aqa_runner.ocr.spells_a_value`). RapidOCR detects the image's text lines, classifies their direction and reads them. The run's scan (`Redactor.finds`) then looks at the lines joined by newlines, and at the same text with every space taken out, since OCR adds spaces and a page can show a value in groups. So the scan's spellings (raw in any case, percent and base64) match when OCR reads them exactly.
- **Withheld (`ScreenshotWithheldError`, nothing written).** The reason is fixed text that quotes nothing:
  - "the image's text spells a bound value";
  - "the image has more text than OCR reads in its bound": the detected text is longer than `TEXT_CAP`, found before any line is read;
  - "the image's OCR took over 5.0 s": the check took longer than `OCR_SECONDS`.
  - That reason is the drop's record. No run path saves evidence until #251, which records it, as it records the capture's own reasons (ADR-0034).
- **The bound.** The check runs in a thread under `OCR_SECONDS` (5 s), so it holds up neither the event loop nor the egress proxy. A thread can't be stopped, and recognition time grows with the text, so `TEXT_CAP` is checked after detection and before recognition. It counts what the recogniser reads: each detected line's width over its height, at least 320/48, the lines sorted and read six a batch, each as wide as its batch's widest. On the Mac, reading takes 0.73-1.35 ms a unit and detection 0.08-0.23 s; Conduit's pages measure 132-280 units, and a 1440x900 page of 10 px text 9,016. `TEXT_CAP` is 500 for now, set on the Mac. It is to be set on CI's runner, the slowest host, as `ARCHIVE_CHARS` was (LAB_NOTES, 2026-10-28): through a throwaway draft PR whose test fails with its timings, so that an image at the cap is detected and read in half of `OCR_SECONDS`.
- **Fail closed.** An error of the engine's, such as a model that won't load or an image it can't decode, propagates out of `take_screenshot`, so no `Screenshot` exists. The engine is built once a process, from the three model files bundled in RapidOCR's wheel, named by path (`aqa_runner.ocr.MODELS`). RapidOCR downloads a model only when none is named, so none is ever downloaded, and a missing file raises `FileNotFoundError` as the engine loads.
- **No other image.** `tests/test_image_exits.py` parses `packages/*/src` and refuses any image capture outside `take_screenshot`'s two capture functions: Playwright's `screenshot` and `pdf`, a context's `record_video_dir`, tracing's `start` and `start_chunk`, and DevTools' screenshot, archive, screencast, PDF and frame methods, as far as the code's spelling shows. With the `Screenshot` only `take_screenshot` makes, that proves no code path saves or sends an image that hasn't passed the check.

## Consequences
- **What holds.** In a run that binds a test secret, a screenshot whose text OCR reads as a bound value, in any spelling the scan finds, is withheld before it can be saved or sent. That covers a value drawn into an image the render carries, and a value the live page shows before the first fill.
- **Residuals, best-effort under SECURITY §5:**
  - text OCR doesn't read: too small, low contrast, distorted or CAPTCHA-like; non-Latin scripts;
  - homoglyphs, such as Cyrillic а for a, or 0 for O;
  - a value split across distant lines, images or captures, or shown in part; there is no partial rule like ADR-0034's `SHARED_RUN`, since a 4-character rule would drop every login page whose label shares "pass";
  - spellings the scan doesn't know: reversed, ROT13, double encoding;
  - steganography.
  - A vision model reads more than OCR does, so for #252 and #256 the check is a backstop.
- **A page can force withholding** by showing more text than `TEXT_CAP`. A withheld screenshot is safe; #251 records the reason.
- **Seams.** #251 calls `take_screenshot`, then `RunRecord.write_screenshot`, and records the reason when it catches `ScreenshotWithheldError`; it adds no check of its own. #252 and #256 must send a `Screenshot`, and the gate refuses any other capture. #272 changes only the render, which is still captured before the check.
- **Amends ADR-0033 and ADR-0034:** the OCR check is in place, inside `take_screenshot`, not between it and `write_screenshot`.
- **Cost.** About 273 MB more in every install, CI's included. On the Mac a check takes 0.2-0.66 s an image, and its first call about 0.05 s more, as the models load. #171's hostile suite now OCRs every capture, since its runs bind a secret. No paid resource or model call.
