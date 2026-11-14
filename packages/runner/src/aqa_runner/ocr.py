"""The OCR check a screenshot passes before a `Screenshot` holds it (#250,
ADR-0035): RapidOCR reads the image's text, and the run's scan looks for a
bound value in it. The engine is RapidOCR's, behind this one module."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from itertools import batched
from pathlib import Path

from aqa_runner.redaction import Redactor


@contextmanager
def _environment_kept() -> Iterator[None]:
    """The process's environment as it was before the block, put back once
    the block ends, however it ends."""
    kept = os.environ.copy()
    try:
        yield
    finally:
        for name in os.environ.keys() - kept.keys():
            del os.environ[name]
        os.environ.update(kept)


# OpenCV's loader, which RapidOCR imports, writes into the process's
# environment for the processes it starts: outside Windows it prepends its
# own directory and an empty entry, the working directory, to
# LD_LIBRARY_PATH (opencv-python's cv2/__init__.py). Every process `aqa`
# starts would inherit that, Playwright's driver among them, so the
# environment is put back as it was.
with _environment_kept():
    import rapidocr
    from rapidocr.main import RapidOCR, RapidOCRError

# RapidOCR's models, bundled in its wheel. Named by path, a model is never
# downloaded, and a missing file fails the check as its model loads.
MODELS = Path(rapidocr.__file__).parent / "models"
DETECT = "PP-OCRv6_det_small.onnx"
CLASSIFY = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
RECOGNISE = "PP-OCRv6_rec_small.onnx"

# The most text an image may hold for OCR to read it, as the recogniser's
# input length: each detected line's width over its height, at least the
# recogniser's shortest, summed in its batches, each line as wide as its
# batch's widest. Set on CI's runner, the slowest host, so that detecting
# and reading an image at the cap takes half of `OCR_SECONDS` there:
# floor((2.5 s - 0.346 s, its slowest detection) / 2.19 ms, its slowest
# unit) (ADR-0035; Actions run 40747977277). The other half covers
# classification, the first check's model load and a slower runner.
# Conduit's pages measure 132-280.
TEXT_CAP = 983


class TooMuchTextError(Exception):
    """An image with more text than OCR reads in its bound, found before
    any is read."""


@cache
def _engine(models: Path) -> RapidOCR:
    """One engine a process, of the models in `models`, which it loads on
    first use under its own locks, so it is made in one thread and used in
    another. Checks use it one at a time (`aqa_runner.screenshots`)."""
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
    # As the engine's recogniser reads them, by its config: each line `high`
    # px high and at least `wide` wide, `rec_batch_num` lines a batch.
    _, high, wide = engine.cfg.Rec.rec_img_shape
    widths = sorted(max(wide / high, crop.shape[1] / crop.shape[0]) for crop in crops)
    batches = batched(widths, engine.cfg.Rec.rec_batch_num, strict=False)
    if sum(len(batch) * batch[-1] for batch in batches) > TEXT_CAP:
        raise TooMuchTextError
    rotated, _ = engine.cls_and_rotate(crops)
    text = "\n".join(engine.recognize_txt(rotated).txts or ())
    # Also with every space taken out, of the text and then of the values
    # too: a value shown in groups, or read in pieces, and OCR adds spaces
    # of its own.
    unspaced = "".join(text.split())
    return (
        redactor.finds(text)
        or redactor.finds(unspaced)
        or redactor.spaceless.finds(unspaced)
    )
