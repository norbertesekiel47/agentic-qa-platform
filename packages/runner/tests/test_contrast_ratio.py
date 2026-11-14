"""`contrast_min`'s arithmetic on what its page script read (DATA_MODEL §7,
Contrast; ADR-0025's #254 amendment): WCAG 2.2's contrast ratio of each
counted text node's colour against the background composited from its
ancestors, the lowest deciding, and the first case styles can't settle
refused by its fixed reason. No browser: each answer is written here as the
page script gives one. Every expected value is a literal."""

from typing import Any

import pytest
from aqa_runner.contrast import ContrastRefusedError, Refusal, lowest_ratio

WHITE = "rgb(255, 255, 255)"
BLACK = "rgb(0, 0, 0)"
CLEAR = "rgba(0, 0, 0, 0)"


def node(colour: str = BLACK, *layers: str, **changed: object) -> dict[str, Any]:
    """A counted text node, as the page script answers for one: its text
    colour `colour`, and `layers`, the background colours from its parent
    out to the root element (white on the parent when none are given), at a
    point inside the viewport that hits its own parent."""
    return {
        "slotted": False,
        "x": 10.5,
        "y": 17,
        "hit": True,
        "colour": colour,
        "effects": False,
        "layers": [{"colour": layer, "image": False} for layer in layers or (WHITE,)],
    } | changed


def answer(*nodes: dict[str, Any], **changed: object) -> dict[str, Any]:
    """The page script's answer for a target whose counted text is `nodes`,
    in a 1280 by 720 viewport."""
    return {
        "shadow": False,
        "width": 1280,
        "height": 720,
        "nodes": list(nodes),
    } | changed


def refused(read: object) -> Refusal:
    with pytest.raises(ContrastRefusedError) as raised:
        lowest_ratio(read)
    return raised.value.case


@pytest.mark.parametrize(
    ("text", "background", "ratio"),
    [
        (BLACK, WHITE, 21),
        (WHITE, BLACK, 21),
        (WHITE, WHITE, 1),
        ("rgb(118, 50, 200)", "rgb(118, 50, 200)", 1),
    ],
)
def test_black_on_white_is_21_and_any_colour_on_itself_is_1(
    text: str, background: str, ratio: float
) -> None:
    assert lowest_ratio(answer(node(text, background))) == ratio


def test_767676_on_white_meets_4_5_and_777777_falls_short() -> None:
    assert lowest_ratio(answer(node("rgb(118, 118, 118)"))) >= 4.5
    assert lowest_ratio(answer(node("rgb(119, 119, 119)"))) < 4.5


@pytest.mark.parametrize(
    ("text", "layers", "ratio"),
    [
        # Half white over black is grey 127.5.
        (BLACK, ["rgba(255, 255, 255, 0.5)", BLACK], 5.280822809644651),
        # Clear layers change nothing on the way out.
        (BLACK, [CLEAR, "rgba(255, 255, 255, 0.5)", CLEAR, BLACK], 5.280822809644651),
        # Outermost first: blue over white, then red over that, is
        # (191.25, 63.75, 127.5); innermost first would give 3.39.
        (
            BLACK,
            ["rgba(255, 0, 0, 0.5)", "rgba(0, 0, 255, 0.5)", WHITE],
            4.258568619535895,
        ),
        # Half black text on white is grey 127.5 on white.
        ("rgba(0, 0, 0, 0.5)", [WHITE], 3.976653024912438),
    ],
    ids=[
        "translucent background",
        "clear layers",
        "outermost first",
        "translucent text",
    ],
)
def test_translucent_colours_are_composited_over_what_is_behind_them(
    text: str, layers: list[str], ratio: float
) -> None:
    assert lowest_ratio(answer(node(text, *layers))) == pytest.approx(ratio, abs=1e-12)


def test_the_lowest_ratio_over_the_counted_text_decides() -> None:
    read = answer(node(BLACK), node("rgb(204, 204, 204)"), node(BLACK))

    assert lowest_ratio(read) == pytest.approx(1.6059285649300714, abs=1e-12)


@pytest.mark.parametrize(
    "colour",
    [
        "oklch(0.6 0.1 200)",
        "lab(50 20 30)",
        "color(srgb 0.2 0.4 0.6)",
        "rgb(256, 0, 0)",
        "rgba(0, 0, 0, 1.5)",
        "rgb(0,0,0)",
        "rgb(٣, 0, 0)",
        "",
    ],
)
@pytest.mark.parametrize("where", ["text", "background"])
def test_a_colour_that_isnt_rgb_or_rgba_is_refused(colour: str, where: str) -> None:
    read = node(colour) if where == "text" else node(BLACK, colour)

    assert refused(answer(read)) == "colour"


@pytest.mark.parametrize(
    "read",
    [
        None,
        [],
        "answer",
        {"width": 1280, "height": 720, "nodes": []},
        answer(node(), extra=True),
        answer(node(), width="1280"),
        answer(node(), height=float("nan")),
        answer(node(x="10")),
        answer(node(hit=1)),
        answer({key: value for key, value in node().items() if key != "colour"}),
        answer(node(layers={"colour": WHITE, "image": False})),
        answer(node(layers=[{"colour": WHITE}])),
        answer(nodes={}),
    ],
    ids=[
        "none",
        "a list",
        "a string",
        "no shadow",
        "an unknown key",
        "a string width",
        "a NaN height",
        "a string point",
        "a number for a boolean",
        "no text colour",
        "layers that aren't a list",
        "a layer without image",
        "nodes that aren't a list",
    ],
)
def test_an_answer_of_any_other_shape_is_refused(read: object) -> None:
    assert refused(read) == "unread"


@pytest.mark.parametrize(
    ("read", "case"),
    [
        (answer(node(), shadow=True), "shadow"),
        (answer(), "no_text"),
        (answer(node(slotted=True)), "shadow"),
        (answer(node(x=-0.5)), "viewport"),
        (answer(node(x=1280)), "viewport"),
        (answer(node(y=720)), "viewport"),
        (answer(node(hit=False)), "hit_test"),
        (answer(node(effects=True)), "effects"),
        (
            answer(node(layers=[{"colour": WHITE, "image": True}])),
            "background",
        ),
        (answer(node(BLACK, CLEAR, "rgba(255, 255, 255, 0.5)")), "no_opaque"),
    ],
    ids=[
        "in a shadow tree",
        "no text",
        "slotted",
        "left of the viewport",
        "at its right edge",
        "at its bottom edge",
        "another element hit",
        "effects",
        "an image behind",
        "nothing opaque",
    ],
)
def test_each_case_styles_cant_settle_is_refused(
    read: dict[str, Any], case: Refusal
) -> None:
    assert refused(read) == case


@pytest.mark.parametrize(
    ("read", "case"),
    [
        # The target's own cases first, then each node's in document order.
        (answer(node(hit=False), shadow=True), "shadow"),
        (answer(node("lab(50 20 30)"), node(hit=False)), "colour"),
        (answer(node(hit=False), node("lab(50 20 30)")), "hit_test"),
        # A refusal wins over a ratio met, or fallen short, before it.
        (answer(node(), node("rgb(204, 204, 204)"), node(effects=True)), "effects"),
        # Within a node, in the table's order.
        (answer(node(slotted=True, x=-1)), "shadow"),
        (answer(node(x=-1, hit=False)), "viewport"),
        (answer(node(hit=False, effects=True)), "hit_test"),
        (
            answer(node(effects=True, layers=[{"colour": WHITE, "image": True}])),
            "effects",
        ),
        (
            answer(node("lab(50 20 30)", layers=[{"colour": CLEAR, "image": True}])),
            "background",
        ),
        (answer(node("lab(50 20 30)", CLEAR)), "colour"),
        # The walk stops at the first opaque layer: what is past it is unread.
        (
            answer(
                node(
                    layers=[
                        {"colour": WHITE, "image": False},
                        {"colour": "lab(50 20 30)", "image": True},
                    ]
                )
            ),
            None,
        ),
        # and at a colour it can't read, whose opacity it can't know.
        (
            answer(
                node(
                    layers=[
                        {"colour": "lab(50 20 30)", "image": False},
                        {"colour": WHITE, "image": True},
                    ]
                )
            ),
            "colour",
        ),
    ],
)
def test_the_first_case_met_is_the_one_refused(
    read: dict[str, Any], case: Refusal | None
) -> None:
    if case is None:
        assert lowest_ratio(read) == 21
    else:
        assert refused(read) == case


@pytest.mark.parametrize(
    ("case", "text"),
    [
        ("frame", "contrast_min does not support elements inside frames"),
        (
            "shadow",
            "contrast_min does not support text in or slotted into a shadow tree",
        ),
        ("no_text", "contrast_min found no rendered text in its target"),
        ("viewport", "contrast_min reads only text inside the viewport"),
        (
            "hit_test",
            (
                "contrast_min's hit test at its text found another element, or not "
                "the text's own"
            ),
        ),
        (
            "effects",
            (
                "contrast_min does not support opacity, filters, masks or blend "
                "modes on its text or behind it"
            ),
        ),
        (
            "background",
            (
                "contrast_min does not support a background image, gradient, "
                "generated background or clipped background behind its text"
            ),
        ),
        ("colour", "contrast_min reads only colours that compute to rgb() or rgba()"),
        ("no_opaque", "contrast_min found no opaque background behind its text"),
        ("unread", "contrast_min could not read its target's styles"),
    ],
)
def test_each_refusal_says_its_fixed_reason(case: Refusal, text: str) -> None:
    assert str(ContrastRefusedError(case)) == text
