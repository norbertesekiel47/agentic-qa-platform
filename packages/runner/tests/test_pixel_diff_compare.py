"""`pixel_diff`'s arithmetic (DATA_MODEL §7, Pixel diff; ADR-0025's #324
amendment): the crop rectangle Playwright's `enclosingIntRect` rounds a box
to, wholly inside the viewport; the page's PNG read at the viewport's size;
the baseline's size read from its PNG header before any pixel is decoded,
then its pixels; and how many pixels differ, compared exactly. No browser:
every image is made here with Pillow, and every expected value is a
literal."""

import io
import struct
import zlib
from collections.abc import Callable

import pytest
from aqa_runner import pixel_diff
from aqa_runner.pixel_diff import (
    Box,
    PixelDiffRefusedError,
    Rectangle,
    Refusal,
    baseline_pixels,
    crop_rectangle,
    cropped_png,
    differing,
    enclosing,
    matches,
    page_crop,
    png_size,
)
from PIL import Image

VIEWPORT = (100, 50)


def box(x: float, y: float, width: float, height: float) -> Box:
    return {"x": x, "y": y, "width": width, "height": height}


def png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def refused(call: Callable[..., object], *args: object) -> Refusal:
    with pytest.raises(PixelDiffRefusedError) as raised:
        call(*args)
    return raised.value.case


@pytest.mark.parametrize(
    ("given", "rectangle"),
    [
        (box(10, 20, 30, 15), Rectangle(10, 20, 40, 35)),
        (box(10.5, 20.25, 30, 15), Rectangle(10, 20, 41, 36)),
        (box(9.9995, 19.9995, 30.001, 15.001), Rectangle(10, 20, 40, 35)),
        (box(10.0005, 20.0005, 29.999, 14.999), Rectangle(10, 20, 40, 35)),
        (box(9.998, 19.998, 30.004, 15.004), Rectangle(9, 19, 41, 36)),
    ],
    ids=[
        "integer edges",
        "fractional edges",
        "0.0005 px outside each edge",
        "0.0005 px inside each edge",
        "past the 0.001 px tolerance",
    ],
)
def test_the_crop_rectangle_rounds_a_box_as_enclosing_int_rect_does(
    given: Box, rectangle: Rectangle
) -> None:
    assert enclosing(given) == rectangle
    assert crop_rectangle(given, VIEWPORT) == rectangle


@pytest.mark.parametrize(
    "given",
    [None, box(10, 20, 0, 15), box(10, 20, 30, 0), box(10, 20, 0.0005, 15)],
    ids=["no box", "no width", "no height", "a width that rounds away"],
)
def test_no_box_or_a_rectangle_with_no_area_is_refused(given: Box | None) -> None:
    assert enclosing(given) is None
    assert refused(crop_rectangle, given, VIEWPORT) == "no_box"


@pytest.mark.parametrize(
    ("given", "inside"),
    [
        (box(0, 0, 100, 50), True),
        (box(-0.5, 10, 20, 10), False),
        (box(10, -0.5, 20, 10), False),
        (box(80, 10, 20.5, 10), False),
        (box(10, 40, 20, 10.5), False),
        (box(-0.0005, -0.0005, 100.001, 50.001), True),
    ],
    ids=[
        "the whole viewport",
        "past the left edge",
        "past the top edge",
        "past the right edge",
        "past the bottom edge",
        "within the tolerance of every edge",
    ],
)
def test_a_rectangle_not_wholly_inside_the_viewport_is_refused(
    given: Box, inside: bool
) -> None:
    if inside:
        assert crop_rectangle(given, VIEWPORT) == enclosing(given)
    else:
        assert refused(crop_rectangle, given, VIEWPORT) == "viewport"


def page(colour: tuple[int, ...] = (255, 255, 255, 255), mode: str = "RGBA") -> bytes:
    return png(Image.new(mode, VIEWPORT, colour))


def test_the_page_crop_is_the_pages_pixels_at_the_rectangle() -> None:
    image = Image.new("RGBA", VIEWPORT, (255, 255, 255, 255))
    image.paste((200, 0, 0, 255), (10, 20, 40, 35))

    crop = page_crop(png(image), VIEWPORT, Rectangle(10, 20, 40, 35))

    assert Rectangle(10, 20, 40, 35).size == (30, 15)
    assert crop.mode == "RGBA"
    assert crop.size == (30, 15)
    assert crop.getcolors() == [(450, (200, 0, 0, 255))]


def test_an_rgb_page_is_cropped_as_rgba() -> None:
    crop = page_crop(page((1, 2, 3), "RGB"), VIEWPORT, Rectangle(0, 0, 2, 1))

    assert crop.getcolors() == [(2, (1, 2, 3, 255))]


@pytest.mark.parametrize(
    "data",
    [
        png(Image.new("RGBA", (100, 51))),
        png(Image.new("RGBA", (99, 50))),
        b"not a png",
        page()[:60],
    ],
    ids=["one row too many", "one column too few", "not a PNG", "truncated"],
)
def test_a_page_screenshot_not_a_png_of_the_viewports_size_is_refused(
    data: bytes,
) -> None:
    assert refused(page_crop, data, VIEWPORT, Rectangle(0, 0, 1, 1)) == "page"


def test_the_saved_crop_is_a_png_of_the_pages_pixels_at_the_rectangle() -> None:
    image = Image.new("RGB", VIEWPORT, (255, 255, 255))
    image.paste((0, 0, 200), (5, 5, 8, 7))

    saved = Image.open(io.BytesIO(cropped_png(png(image), Rectangle(5, 5, 8, 7))))

    assert (saved.format, saved.mode, saved.size) == ("PNG", "RGBA", (3, 2))
    assert saved.getcolors() == [(6, (0, 0, 200, 255))]


def test_a_saved_crop_never_reaches_past_its_page() -> None:
    with pytest.raises(ValueError, match="outside"):
        cropped_png(page(), Rectangle(90, 40, 101, 50))


def solid(
    colour: tuple[int, int, int, int], size: tuple[int, int] = (3, 1)
) -> Image.Image:
    return Image.new("RGBA", size, colour)


def test_identical_images_differ_in_no_pixel() -> None:
    assert differing(solid((9, 8, 7, 255)), solid((9, 8, 7, 255)), 0) == 0


@pytest.mark.parametrize(
    ("changed", "threshold", "count"),
    [
        ((110, 100, 100, 255), 10, 0),
        ((111, 100, 100, 255), 10, 1),
        ((100, 89, 100, 255), 10, 1),
        ((100, 100, 100, 244), 10, 1),
        ((100, 100, 100, 245), 10, 0),
    ],
    ids=[
        "a difference equal to the threshold",
        "one over it",
        "one over it, downward",
        "alpha alone, over it",
        "alpha alone, equal to it",
    ],
)
def test_a_pixel_differs_when_its_largest_channel_difference_is_over_the_threshold(
    changed: tuple[int, int, int, int], threshold: int, count: int
) -> None:
    crop = solid((100, 100, 100, 255))
    baseline = crop.copy()
    baseline.putpixel((1, 0), changed)

    assert differing(crop, baseline, threshold) == count


def test_black_and_white_differ_at_the_largest_threshold() -> None:
    assert differing(solid((0, 0, 0, 255)), solid((255, 0, 0, 255)), 254) == 3


def test_an_rgb_baseline_equals_an_rgba_crop_of_the_same_colours() -> None:
    crop = solid((12, 34, 56, 255))
    baseline = baseline_pixels(png(Image.new("RGB", (3, 1), (12, 34, 56))), (3, 1))

    assert baseline.mode == "RGBA"
    assert differing(crop, baseline, 0) == 0


@pytest.mark.parametrize(
    ("ratio", "held"),
    [(0.3333333333333333, False), (0.3333333333333334, True), (0, False)],
)
def test_one_differing_pixel_of_three_is_compared_with_the_ratio_exactly(
    ratio: float, held: bool
) -> None:
    crop = solid((0, 0, 0, 255))
    baseline = crop.copy()
    baseline.putpixel((2, 0), (255, 255, 255, 255))

    assert matches(crop, baseline, pixel_threshold=0, max_diff_ratio=ratio) is held


def test_identical_images_match_at_a_ratio_of_0() -> None:
    crop = solid((0, 0, 0, 255))

    assert matches(crop, crop.copy(), pixel_threshold=0, max_diff_ratio=0) is True


def test_a_pngs_size_is_read_from_its_header() -> None:
    assert png_size(png(Image.new("RGBA", (321, 7)))) == (321, 7)


def header(width: int, height: int) -> bytes:
    """A PNG's signature and IHDR chunk, for an 8-bit RGBA image of this size,
    with no pixel after them."""
    body = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + body
        + struct.pack(">I", zlib.crc32(body))
    )


def truncated() -> bytes:
    """A 64 by 64 PNG of noise, its pixel data cut short after its header."""
    noise = Image.frombytes("RGBA", (64, 64), bytes(range(256)) * 64)
    whole = png(noise)
    return whole[: len(whole) // 2]


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (b"not a png at all, though long enough to hold a header", (3, 1)),
        (header(3, 1)[:20], (3, 1)),
        (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IDAT" + bytes(17), (3, 1)),
        (truncated(), (64, 64)),
        (png(Image.new("L", (3, 1))), (3, 1)),
        (header(3, 1), (3, 1)),
    ],
    ids=[
        "no PNG signature",
        "a header cut short",
        "no IHDR first",
        "truncated pixels",
        "an L-mode PNG",
        "a header and no pixels",
    ],
)
def test_a_baseline_that_doesnt_decode_as_an_rgb_or_rgba_png_is_refused(
    data: bytes, size: tuple[int, int]
) -> None:
    assert refused(baseline_pixels, data, size) == "undecodable"


def test_a_baseline_whose_header_claims_another_size_is_refused_before_any_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*_: object, **__: object) -> None:
        raise AssertionError("the baseline was decoded")

    monkeypatch.setattr(Image, "open", never)

    assert refused(baseline_pixels, header(100_000, 100_000), (3, 1)) == "size"


@pytest.mark.parametrize("size", [(4, 1), (3, 2)])
def test_a_baseline_one_pixel_off_its_crops_size_is_refused(
    size: tuple[int, int],
) -> None:
    assert refused(baseline_pixels, png(solid((0, 0, 0, 255))), size) == "size"


def test_each_refusal_has_its_fixed_reason() -> None:
    assert {case: str(PixelDiffRefusedError(case)) for case in pixel_diff.REASONS} == {
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
