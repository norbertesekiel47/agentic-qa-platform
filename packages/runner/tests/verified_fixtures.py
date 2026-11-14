"""Fixtures the verified replay's tests share (#256): two badge pages, each one
solid block of colour on a page that renders no text, a spec whose one
expectation is marked `visual: model`, the scripts that check it with
`model_verify`, the verifier's scripted replies, and a PNG's pixel, read with
the standard library. Imported by its path, as pytest names the runner's test
modules (TESTING.md §1, Shared egress fixtures)."""

import re
import struct
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from unittest.mock import patch

from aqa_core.compiled import CompiledScript
from aqa_core.project import load_project
from aqa_runner.chat_client import Reply
from aqa_runner.publication import published_text
from aqa_runner.verifier import VerifierVerdict
from langchain_core.messages import AIMessage, BaseMessage

from packages.runner.tests.executor_fixtures import (
    PAGES,
    App,
    by_role,
    compiled,
    serving_app,
)
from packages.runner.tests.explore_graph_fixtures import USAGE

VERIFIED = Path(__file__).resolve().parents[3] / "tests/fixtures/pages/verified"
# The colour each page draws its badge in, and the badge's centre in CSS
# pixels, at the pinned viewport and a device scale factor of 1.
GREEN: Final = (0, 160, 0)
RED: Final = (200, 0, 0)
CENTRE: Final = (160, 120)
# What the spec expects of the badge, and the badge's accessible name, a word
# the expectation doesn't use.
EXPECTATION: Final = "The status badge is green"
NAME: Final = "Signal"
BADGE = {
    "semantic": "the order's status badge",
    "locators": [by_role("img", NAME), {"css": "div[role=img]"}],
}
MODEL_VERIFY = {"expect_index": 0, "check": "model_verify", "target": "badge"}
# The badge checked by the verifier once, and twice.
CHECKED = compiled(
    [], targets={"badge": BADGE}, assertions=[{"id": "a1", **MODEL_VERIFY}]
)
TWICE = compiled(
    [],
    targets={"badge": BADGE},
    assertions=[{"id": "a1", **MODEL_VERIFY}, {"id": "a2", **MODEL_VERIFY}],
)
# A badge no locator finds: the page's is an img, not a button.
DRIFTED = compiled(
    [],
    targets={
        "badge": {**BADGE, "locators": [by_role("button", NAME), {"css": "button"}]}
    },
    assertions=[{"id": "a1", **MODEL_VERIFY}],
)
QUICK = "budgets: { resolve_seconds: 2 }\n"


def page(name: str) -> str:
    """The badge page `name`: `green` or `red`."""
    return (VERIFIED / f"{name}.html").read_text()


@contextmanager
def serving(html: str) -> Iterator[App]:
    """The fixture app, serving `html` at `/page/badge` until the block ends."""
    with patch.dict(PAGES, {"badge": html}), serving_app() as app:
        yield app


def badge_spec(
    root: Path,
    script: CompiledScript = CHECKED,
    *,
    expectation: str = f"{{ text: {EXPECTATION}, visual: model }}",
    config: str = QUICK,
    preconditions: str = "",
) -> Path:
    """The badge spec under the spec root `root`, its one expectation
    `expectation`, with `script` compiled from it and committed as published.
    The spec's path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(config)
    path = root / "badge.spec.md"
    path.write_text(
        "---\nid: badge\ngoal: A customer sees their order's status.\n"
        f"preconditions:\n  start_url: '/page/badge'\n{preconditions}"
        f"expect:\n  - {expectation}\n---\n"
    )
    spec = load_project(root).specs["badge"]
    current = {"spec_id": "badge", "spec_hash": spec.spec_hash}
    (root / ".compiled").mkdir(exist_ok=True)
    (root / ".compiled/badge.json").write_text(
        published_text(script.model_copy(update=current))
    )
    return path


def evidence(messages: Sequence[BaseMessage]) -> list[str]:
    """The two references a verifier request's `<evidence>` block gives: the
    screenshot's path, then the element's ref."""
    found = re.search(
        r"<evidence>\nscreenshot: (\S+)\nelement: (e[0-9]+)\n</evidence>",
        messages[-1].text,
    )
    assert found is not None, messages[-1].text
    return [found[1], found[2]]


def replying(
    verdict: str, refs: list[str] | None = None
) -> Callable[[Sequence[BaseMessage]], Reply]:
    """A scripted move: the verifier's `verdict`, citing `refs`, or the two
    references its request gives when None."""

    def reply(messages: Sequence[BaseMessage]) -> Reply:
        cited = evidence(messages) if refs is None else refs
        parsed = VerifierVerdict.model_validate(
            {
                "verdict": verdict,
                "evidence_refs": cited,
                "confidence": 0.9,
                "rationale": "the badge is drawn in one colour",
            }
        )
        return Reply(
            message=AIMessage(content=parsed.model_dump_json()),
            usage=USAGE,
            refused=False,
            complete=True,
            parsed=parsed,
        )

    return reply


def pixel(png: bytes, x: int, y: int) -> tuple[int, int, int]:
    """The red, green and blue of the pixel at (`x`, `y`) of `png`, an 8-bit
    RGB or RGBA image with no interlacing, as Chromium writes one, unfiltered
    as the PNG specification says (https://www.w3.org/TR/png-3/#9Filters)."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    chunks: dict[bytes, bytes] = {}
    at = 8
    while at < len(png):
        (length,) = struct.unpack(">I", png[at : at + 4])
        kind = png[at + 4 : at + 8]
        chunks[kind] = chunks.get(kind, b"") + png[at + 8 : at + 8 + length]
        at += 12 + length
    width, _, depth, colour, _, _, interlace = struct.unpack(
        ">IIBBBBB", chunks[b"IHDR"]
    )
    assert (depth, interlace, colour in (2, 6)) == (8, 0, True)
    size = 3 if colour == 2 else 4
    stride = width * size
    data = zlib.decompress(chunks[b"IDAT"])
    previous = bytearray(stride)
    for row in range(y + 1):
        start = row * (stride + 1)
        kind, line = data[start], bytearray(data[start + 1 : start + 1 + stride])
        for i in range(stride):
            left = line[i - size] if i >= size else 0
            up = previous[i]
            corner = previous[i - size] if i >= size else 0
            line[i] = (line[i] + _predicted(kind, left, up, corner)) & 0xFF
        previous = line
    red, green, blue = previous[x * size : x * size + 3]
    return red, green, blue


def _predicted(kind: int, left: int, up: int, corner: int) -> int:
    if kind == 0:
        return 0
    if kind == 1:
        return left
    if kind == 2:
        return up
    if kind == 3:
        return (left + up) // 2
    estimate = left + up - corner
    near = min(
        (abs(estimate - left), 0, left),
        (abs(estimate - up), 1, up),
        (abs(estimate - corner), 2, corner),
    )
    return near[2]
