"""How `contrast_min` reads its target (DATA_MODEL §7, Contrast; ADR-0025's
#254 amendment). `READ_STYLES`, a page script, reads the computed styles of
each text node that counts and the hit test at its point; `lowest_ratio`
checks the shape of what it read, refuses the first case styles can't
settle, and works out WCAG 2.2's contrast ratio of each node in Python, so
the arithmetic needs no browser. A refusal is never a pass and never a
guess: it carries one of the fixed reasons in `REASONS`."""

import math
import re
from typing import Literal, NamedTuple, TypedDict

from pydantic import ConfigDict, TypeAdapter, ValidationError, with_config

type Refusal = Literal[
    "frame",
    "shadow",
    "no_text",
    "viewport",
    "hit_test",
    "effects",
    "background",
    "colour",
    "no_opaque",
    "unread",
]

# Each case styles can't settle, and its reason, which every run presents
# the same way, withheld or not: R1 to R10 in DATA_MODEL §7.
REASONS: dict[Refusal, str] = {
    "frame": "contrast_min does not support elements inside frames",
    "shadow": "contrast_min does not support text in or slotted into a shadow tree",
    "no_text": "contrast_min found no rendered text in its target",
    "viewport": "contrast_min reads only text inside the viewport",
    "hit_test": (
        "contrast_min's hit test at its text found another element, or not the "
        "text's own"
    ),
    "effects": (
        "contrast_min does not support opacity, filters, masks or blend modes on "
        "its text or behind it"
    ),
    "background": (
        "contrast_min does not support a background image, gradient, generated "
        "background or clipped background behind its text"
    ),
    "colour": "contrast_min reads only colours that compute to rgb() or rgba()",
    "no_opaque": "contrast_min found no opaque background behind its text",
    "unread": "contrast_min could not read its target's styles",
}


class ContrastRefusedError(RuntimeError):
    """A target whose contrast its styles can't establish, and which case
    in `REASONS` it is."""

    def __init__(self, case: Refusal) -> None:
        super().__init__(REASONS[case])
        self.case: Refusal = case


# What `lowest_ratio` needs of a target, read in one evaluation so the page
# can't change between the reads: whether the target is in a shadow tree,
# the viewport's size, and each text node that counts, in document order (a
# walk over the light DOM, entering no shadow root and no frame: text with a
# non-space character, whose parent is an HTML element the page shows and
# whose range has a box). For each: whether it, its parent or an ancestor is
# slotted; the centre of its first non-empty box; whether the hit test there
# finds its parent first and only the parent's ancestors after it; its
# parent's text colour; whether the parent or an ancestor has an effect a
# colour can't account for; and, from the parent out to the root element,
# each background colour and whether that element paints more behind the
# text than its colour. It runs in the page's world, so it is the page's
# word, as the hit test of `visible_unoccluded` is; what it reads decides
# nothing here.
READ_STYLES = r"""(element) => {
    const owner = element.ownerDocument;
    const answer = { shadow: false, width: innerWidth, height: innerHeight, nodes: [] };
    if (element.getRootNode() !== owner) return { ...answer, shadow: true };
    const generated = (at, pseudo) => {
        const style = getComputedStyle(at, pseudo);
        return style.content !== "none" && style.content !== "normal" && (
            style.backgroundColor !== "rgba(0, 0, 0, 0)" || style.backgroundImage !== "none"
        );
    };
    const read = new Map();
    const styled = (at) => {
        if (!read.has(at)) {
            const style = getComputedStyle(at);
            const clips = ["background-clip", "-webkit-background-clip"].map(
                (name) => style.getPropertyValue(name).split(", ")
            );
            read.set(at, {
                effects: style.opacity !== "1" || style.filter !== "none" ||
                    style.backdropFilter !== "none" ||
                    style.getPropertyValue("mask-image") !== "none" ||
                    style.mixBlendMode !== "normal",
                layer: {
                    colour: style.backgroundColor,
                    image: style.backgroundImage !== "none" ||
                        clips.some((clip) => clip.includes("text")) ||
                        generated(at, "::before") || generated(at, "::after"),
                },
            });
        }
        return read.get(at);
    };
    const walker = owner.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
        const parent = node.parentNode;
        if (!/\S/.test(node.data) || !(parent instanceof HTMLElement)) continue;
        const style = getComputedStyle(parent);
        if (style.visibility !== "visible") continue;
        const range = owner.createRange();
        range.selectNodeContents(node);
        const piece = [...range.getClientRects()].find(
            (rect) => rect.width > 0 && rect.height > 0
        );
        if (piece === undefined) continue;
        const x = piece.left + piece.width / 2;
        const y = piece.top + piece.height / 2;
        const up = [];
        for (let at = parent; at !== null; at = at.parentElement) up.push(at);
        const [first, ...rest] = owner.elementsFromPoint(x, y);
        answer.nodes.push({
            slotted: node.assignedSlot !== null || up.some((at) => at.assignedSlot !== null),
            x,
            y,
            hit: first === parent && rest.every((hit) => up.includes(hit)),
            colour: style.getPropertyValue("-webkit-text-fill-color"),
            effects: up.some((at) => styled(at).effects),
            layers: up.map((at) => styled(at).layer),
        });
    }
    return answer;
}"""

# The page script's answer, in exactly this shape: any other is unread.
_SHAPE = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


@with_config(_SHAPE)
class _Layer(TypedDict):
    colour: str
    image: bool


@with_config(_SHAPE)
class _Node(TypedDict):
    slotted: bool
    x: float
    y: float
    hit: bool
    colour: str
    effects: bool
    layers: list[_Layer]


@with_config(_SHAPE)
class _Answer(TypedDict):
    shadow: bool
    width: float
    height: float
    nodes: list[_Node]


_ANSWER = TypeAdapter(_Answer)


class _Colour(NamedTuple):
    """sRGB channels from 0 to 255, and an alpha from 0 to 1."""

    red: float
    green: float
    blue: float
    alpha: float


# Chromium's serialization of a colour given in legacy syntax: integer
# channels, and an alpha only when it isn't 1.
_RGB = re.compile(r"rgb\(([0-9]{1,3}), ([0-9]{1,3}), ([0-9]{1,3})\)")
_RGBA = re.compile(
    r"rgba\(([0-9]{1,3}), ([0-9]{1,3}), ([0-9]{1,3}), ([0-9]+(?:\.[0-9]+)?)\)"
)


def lowest_ratio(answer: object) -> float:
    """The lowest WCAG contrast ratio over the text that `answer`, the page
    script's, describes; or `ContrastRefusedError` with the first case met:
    the target's own (in a shadow tree, then no text), then each node's, in
    document order. A refusal wins over any ratio."""
    try:
        read = _ANSWER.validate_python(answer)
    except ValidationError:
        # Its message quotes the answer, which the page wrote.
        raise ContrastRefusedError("unread") from None
    if read["shadow"]:
        raise ContrastRefusedError("shadow")
    if not read["nodes"]:
        raise ContrastRefusedError("no_text")
    return min(_ratio_of(node, read["width"], read["height"]) for node in read["nodes"])


def _ratio_of(node: _Node, width: float, height: float) -> float:
    """One node's ratio: its text colour, composited over its background,
    against that background."""
    _require_plain(node, width, height)
    reached = _reached(node["layers"])
    if any(layer["image"] for layer in reached):
        raise ContrastRefusedError("background")
    text = _colour(node["colour"])
    behind = [_colour(layer["colour"]) for layer in reached]
    if not behind or behind[-1].alpha < 1:
        raise ContrastRefusedError("no_opaque")
    background = behind[-1]
    for layer in reversed(behind[:-1]):
        background = _over(layer, background)
    return _ratio(_over(text, background), background)


def _require_plain(node: _Node, width: float, height: float) -> None:
    """Refuse a node that is slotted, outside the viewport, not the hit
    test's, or under an effect; in that order."""
    if node["slotted"]:
        raise ContrastRefusedError("shadow")
    if not (0 <= node["x"] < width and 0 <= node["y"] < height):
        raise ContrastRefusedError("viewport")
    if not node["hit"]:
        raise ContrastRefusedError("hit_test")
    if node["effects"]:
        raise ContrastRefusedError("effects")


def _reached(layers: list[_Layer]) -> list[_Layer]:
    """The layers the background walk reaches, from the text's parent out:
    up to the first whose colour is opaque, or one it can't read, whose
    opacity it can't know; all of them when none is opaque."""
    for index, layer in enumerate(layers):
        colour = _parsed(layer["colour"])
        if colour is None or colour.alpha == 1:
            return layers[: index + 1]
    return layers


def _colour(text: str) -> _Colour:
    if (colour := _parsed(text)) is None:
        raise ContrastRefusedError("colour")
    return colour


def _parsed(text: str) -> _Colour | None:
    """`text` as `rgb(R, G, B)` or `rgba(R, G, B, A)` give it, or None."""
    found = _RGB.fullmatch(text) or _RGBA.fullmatch(text)
    if found is None:
        return None
    red, green, blue = (int(channel) for channel in found.group(1, 2, 3))
    alpha = 1.0 if found.re is _RGB else float(found[4])
    if max(red, green, blue) > 255 or alpha > 1:
        return None
    return _Colour(red, green, blue, alpha)


def _over(top: _Colour, below: _Colour) -> _Colour:
    """`top` composited source-over onto the opaque `below`, per channel in
    non-linear sRGB, as browsers composite."""

    def mixed(upper: float, lower: float) -> float:
        return top.alpha * upper + (1 - top.alpha) * lower

    return _Colour(
        mixed(top.red, below.red),
        mixed(top.green, below.green),
        mixed(top.blue, below.blue),
        1.0,
    )


def _ratio(one: _Colour, other: _Colour) -> float:
    """WCAG 2.2's contrast ratio of two opaque colours."""
    lighter, darker = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(colour: _Colour) -> float:
    """WCAG 2.2's relative luminance of an opaque colour."""

    def linear(channel: float) -> float:
        value = channel / 255
        if value <= 0.04045:
            return value / 12.92
        return math.pow((value + 0.055) / 1.055, 2.4)

    return (
        0.2126 * linear(colour.red)
        + 0.7152 * linear(colour.green)
        + 0.0722 * linear(colour.blue)
    )
