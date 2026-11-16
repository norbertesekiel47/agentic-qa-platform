"""How `pixel_diff` compares its target with a committed baseline (DATA_MODEL
§7, Pixel diff; ADR-0025's #324 amendment), with no browser: the crop
rectangle Playwright's own element screenshots would cut from the target's
box, and whether it lies wholly inside the viewport; the page's PNG read at
the viewport's size and cut to that rectangle; the baseline's size, read
from its PNG header before any pixel is decoded, then its pixels; and how
many pixels differ, compared with `max_diff_ratio` exactly. A refusal is
never a pass and never a failure: it carries one of the fixed reasons in
`REASONS`."""

import io
import math
import struct
from fractions import Fraction
from typing import Final, Literal, NamedTuple, TypedDict

from PIL import Image, ImageChops

type Refusal = Literal[
    "frame",
    "no_box",
    "viewport",
    "withheld",
    "moved",
    "page",
    "missing",
    "unread",
    "pinned",
    "undecodable",
    "size",
]

# Each case the check can't compare, and its reason, which every run presents
# the same way, withheld or not: P1 to P11 in DATA_MODEL §7, in the order
# they are met.
REASONS: dict[Refusal, str] = {
    "frame": "pixel_diff does not support elements inside frames",
    "no_box": "pixel_diff's target has no box to compare",
    "viewport": "pixel_diff compares only a target wholly inside the viewport",
    "withheld": "pixel_diff's screenshot was withheld",
    "moved": "pixel_diff's target moved while the page was captured",
    "page": "pixel_diff could not read the page's screenshot at the viewport's size",
    "missing": "pixel_diff's baseline file is missing",
    "unread": "pixel_diff could not read its baseline file",
    "pinned": "pixel_diff's baseline file isn't the one baseline_sha256 pins",
    "undecodable": "pixel_diff could not decode its baseline as a PNG",
    "size": "pixel_diff's baseline isn't the size of its target's crop",
}


class PixelDiffRefusedError(RuntimeError):
    """A target the check can't compare with its baseline, and which case in
    `REASONS` it is."""

    def __init__(self, case: Refusal) -> None:
        super().__init__(REASONS[case])
        self.case: Refusal = case


class Box(TypedDict):
    """An element's box in CSS pixels, relative to the viewport, as
    Playwright's `ElementHandle.bounding_box` gives it."""

    x: float
    y: float
    width: float
    height: float


class Rectangle(NamedTuple):
    """A crop, in whole CSS pixels: its left and top edges, and the right and
    bottom edges it stops before, as Pillow's `crop` takes them."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def size(self) -> tuple[int, int]:
        return self.right - self.left, self.bottom - self.top


# Playwright 1.63's `enclosingIntRect` absorbs this much float noise at each
# edge of a box (`helper.enclosingIntRect` in its driver's `coreBundle.js`).
TOLERANCE: Final = 1e-3


def enclosing(box: Box | None) -> Rectangle | None:
    """The whole-pixel rectangle that encloses `box`, as `enclosingIntRect`
    rounds an element's box for Playwright's own element screenshots; None
    for no box, or a rectangle with no width or height."""
    if box is None:
        return None
    rectangle = Rectangle(
        math.floor(box["x"] + TOLERANCE),
        math.floor(box["y"] + TOLERANCE),
        math.ceil(box["x"] + box["width"] - TOLERANCE),
        math.ceil(box["y"] + box["height"] - TOLERANCE),
    )
    width, height = rectangle.size
    return rectangle if width > 0 and height > 0 else None


def crop_rectangle(box: Box | None, viewport: tuple[int, int]) -> Rectangle:
    """The crop rectangle of a target whose box is `box` (`enclosing`), once
    it has an area (P2) and lies wholly inside `viewport`, from 0 to its
    width and height (P3): a partly visible target is refused, not compared
    in part."""
    rectangle = enclosing(box)
    if rectangle is None:
        raise PixelDiffRefusedError("no_box")
    width, height = viewport
    left, top, right, bottom = rectangle
    if left < 0 or top < 0 or right > width or bottom > height:
        raise PixelDiffRefusedError("viewport")
    return rectangle


def page_crop(
    png: bytes, viewport: tuple[int, int], rectangle: Rectangle
) -> Image.Image:
    """The pixels at `rectangle` of the page's screenshot `png`, as 8-bit
    RGBA, once it decodes as a PNG of exactly `viewport`'s size in CSS
    pixels (P6)."""
    page = _decoded(png, "page")
    if page.size != viewport:
        raise PixelDiffRefusedError("page")
    return page.convert("RGBA").crop(rectangle)


def cropped_png(png: bytes, rectangle: Rectangle) -> bytes:
    """The PNG of the 8-bit RGBA pixels at `rectangle` of the PNG `png`: the
    crop a run saves, never reaching past `png`'s own pixels (ValueError)."""
    image = Image.open(io.BytesIO(png), formats=["PNG"]).convert("RGBA")
    width, height = image.size
    left, top, right, bottom = rectangle
    if left < 0 or top < 0 or right > width or bottom > height:
        raise ValueError(f"{rectangle} reaches outside a {width} by {height} image")
    out = io.BytesIO()
    image.crop(rectangle).save(out, format="PNG")
    return out.getvalue()


# A PNG's signature, then its first chunk's length and type: IHDR, always
# 13 bytes, whose first eight are the width and height, big-endian.
_HEADER: Final = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"


def png_size(data: bytes) -> tuple[int, int]:
    """The width and height `data`'s PNG header gives, read before any pixel
    is: P10 when it doesn't start with a PNG's signature and IHDR."""
    if not data.startswith(_HEADER) or len(data) < len(_HEADER) + 8:
        raise PixelDiffRefusedError("undecodable")
    width, height = struct.unpack_from(">II", data, len(_HEADER))
    return width, height


def baseline_pixels(data: bytes, size: tuple[int, int]) -> Image.Image:
    """The baseline `data` as 8-bit RGBA pixels, for a crop of `size`: P10
    when its PNG header can't be read (`png_size`), P11 when the header's
    size isn't `size`, so a baseline of any other size is never decoded;
    then P10 for anything Pillow raises decoding it, or a mode other than
    RGB or RGBA."""
    if png_size(data) != size:
        raise PixelDiffRefusedError("size")
    baseline = _decoded(data, "undecodable")
    # Pillow reads the size from the same IHDR, so only a PNG it reads
    # differently from its header, such as one with two, has another.
    if baseline.mode not in ("RGB", "RGBA") or baseline.size != size:
        raise PixelDiffRefusedError("undecodable")
    return baseline.convert("RGBA")


def _decoded(data: bytes, refusal: Refusal) -> Image.Image:
    """`data` decoded as a PNG, every pixel read, or `refusal` for anything
    Pillow raises: its `SyntaxError` for a broken file,
    `DecompressionBombError` and `OSError` among them."""
    try:
        image = Image.open(io.BytesIO(data), formats=["PNG"])
        image.load()
    except Exception as error:
        raise PixelDiffRefusedError(refusal) from error
    return image


def differing(crop: Image.Image, baseline: Image.Image, threshold: int) -> int:
    """How many pixels of two RGBA images of one size differ: those whose
    largest channel difference, alpha's included, is over `threshold`."""
    red, green, blue, alpha = ImageChops.difference(crop, baseline).split()
    largest = ImageChops.lighter(
        ImageChops.lighter(red, green), ImageChops.lighter(blue, alpha)
    )
    return sum(largest.histogram()[threshold + 1 :])


def matches(
    crop: Image.Image,
    baseline: Image.Image,
    *,
    pixel_threshold: int,
    max_diff_ratio: float,
) -> bool:
    """Whether the share of `crop`'s pixels that differ from `baseline`'s
    (`differing`) is at most `max_diff_ratio`, compared as exact fractions,
    with no rounding."""
    width, height = crop.size
    count = differing(crop, baseline, pixel_threshold)
    return Fraction(count, width * height) <= Fraction(max_diff_ratio)
