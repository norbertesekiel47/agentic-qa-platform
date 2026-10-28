"""The OCR check a screenshot passes before a `Screenshot` holds it (#250,
ADR-0035): RapidOCR reads the image's text, and the run's scan looks for a
bound value in it. The engine is RapidOCR's, behind this one module."""

from functools import cache
from itertools import batched
from pathlib import Path

import rapidocr
from rapidocr.main import RapidOCR, RapidOCRError

from aqa_runner.redaction import Redactor

# RapidOCR's models, bundled in its wheel. Named by path, a model is never
# downloaded, and a missing file fails the check as its model loads.
MODELS = Path(rapidocr.__file__).parent / "models"
DETECT = "PP-OCRv6_det_small.onnx"
CLASSIFY = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
RECOGNISE = "PP-OCRv6_rec_small.onnx"

# The most text an image may hold for OCR to read it, as the recogniser's
# input length: each detected line's width over its height, at least the
# recogniser's shortest, summed in its batches, each line as wide as its
# batch's widest. Reading takes about 0.7-1.4 ms a unit on the developer's
# Mac, and Conduit's pages measure 132-280. Provisional: to be set on CI's
# runner (ADR-0035).
TEXT_CAP = 500

# RapidOCR 3.10.0's recogniser (its config.yaml, `Rec`): each line is read
# 48 px high and at least 320 wide, six lines a batch.
_SHORTEST = 320 / 48
_BATCH = 6


class TooMuchTextError(Exception):
    """An image with more text than OCR reads in its bound, found before
    any is read."""


@cache
def _engine(models: Path) -> RapidOCR:
    """One engine a process, of the models in `models`. Its stages change
    nothing it holds once its models are loaded, under its own locks, so one
    runs in any thread."""
    return RapidOCR(
        params={
            "Det.model_path": str(models / DETECT),
            "Cls.model_path": str(models / CLASSIFY),
            "Rec.model_path": str(models / RECOGNISE),
            "Global.log_level": "critical",
        }
    )


def spells_a_value(png: bytes, redactor: Redactor) -> bool:
    """Whether the text OCR reads in `png` holds a spelling `redactor`'s
    scan finds. `TooMuchTextError` when its detected text is over
    `TEXT_CAP`."""
    engine = _engine(MODELS)
    image, record = engine.preprocess_img(engine.load_img(png))
    try:
        crops, _ = engine.detect_and_crop(image, record)
    except RapidOCRError:
        # Its only error there: no text was detected.
        return False
    widths = sorted(max(_SHORTEST, crop.shape[1] / crop.shape[0]) for crop in crops)
    batches = batched(widths, _BATCH, strict=False)
    if sum(len(batch) * batch[-1] for batch in batches) > TEXT_CAP:
        raise TooMuchTextError
    rotated, _ = engine.cls_and_rotate(crops)
    text = "\n".join(engine.recognize_txt(rotated).txts or ())
    # Also with every space taken out: a value shown in groups, or read in
    # pieces, and OCR adds spaces of its own.
    return redactor.finds(text) or redactor.finds("".join(text.split()))
