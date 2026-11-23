"""Fixtures the verified replay's tests share (#256): two badge pages, each one
solid block of colour on a page that renders no text, a spec whose one
expectation is marked `visual: model`, the scripts that check it with
`model_verify`, the verifier's scripted replies, and a PNG's pixel, read with
the standard library. The recorder's verified cases (#339; ADR-0024's #339
amendment, TESTING §4) are here too: each badge page's `aqa replay --mode
verified`, and the rule that promotes its recording. Imported by its path, as
pytest names the runner's test modules (TESTING.md §1, Shared egress
fixtures)."""

import re
import struct
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from unittest.mock import patch

from aqa_core.compiled import CompiledScript
from aqa_core.model_costs import CostRecord
from aqa_core.project import load_config, load_project, load_spec
from aqa_core.spec import Spec
from aqa_runner.chat_client import Reply
from aqa_runner.publication import published_text
from aqa_runner.replay_result import Outcome
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
from packages.runner.tests.heal_fixtures import unallowed

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


@dataclass(frozen=True)
class VerifiedCase:
    """One recording (#339): its cassette, the badge page its run replays, the
    script and config it replays with, and the outcome it must end in."""

    cassette: str
    page: str
    script: CompiledScript
    config: str
    outcome: Outcome

    def spec(self, root: Path) -> Spec:
        """The badge spec under the spec root `root`, with this case's script
        published and its config, as `test_verified_cassette` writes it."""
        path = badge_spec(root, self.script, config=self.config)
        return load_spec(path, load_config(root / "config.yaml"))


CASES: Final = (
    VerifiedCase("verified_pass", "green", CHECKED, QUICK, "passed"),
    VerifiedCase("verified_violated", "red", CHECKED, QUICK, "expectation_violated"),
)


# Why a run isn't one its recording is promoted from, in fixed words, with
# `heal_fixtures.unallowed`.
NOT_ONE_CALL: Final = "it didn't make exactly one verifier call"
UNPARSED: Final = "the verifier's reply didn't parse as its verdict"
MISCITED: Final = (
    "the verifier's reply didn't cite exactly the screenshot, then the badge's ref"
)
NO_VISUAL_FINDING: Final = "it ended expectation_violated with no visual finding"


def verified_disallowed(
    case: VerifiedCase,
    outcome: str,
    document: Mapping[str, object],
    costs: Sequence[CostRecord],
    given: Sequence[str],
) -> str | None:
    """Why a run of `case` that ended `outcome`, whose `replay.json` is
    `document` and whose own cost lines are `costs`, isn't one its recording is
    promoted from, in fixed words, checked in this order, or None. `given` is
    the two references its request gave: the screenshot's path, then the
    badge's ref. Only one verifier call whose reply parsed and cites exactly
    `given`, in that order, which `judged`'s set allows otherwise; then the
    case's outcome, and for the red page a visual finding."""
    if [(cost.role, cost.mode) for cost in costs] != [("verifier", "verified")]:
        return NOT_ONE_CALL
    a1 = _badge(document)
    said = a1.get("verifier")
    if costs[0].status != "ok" or not isinstance(said, Mapping):
        return UNPARSED
    if said.get("evidence_refs") != list(given):
        return MISCITED
    if outcome != case.outcome:
        return unallowed(outcome)
    finding = a1.get("finding")
    if outcome == "expectation_violated" and (
        not isinstance(finding, Mapping) or finding.get("category") != "visual"
    ):
        return NO_VISUAL_FINDING
    return None


def _badge(document: Mapping[str, object]) -> Mapping[str, object]:
    """a1's verdict in `document`, or nothing when it has none."""
    verdicts = document.get("verdicts")
    if not isinstance(verdicts, list):
        return {}
    found = [
        each
        for each in verdicts
        if isinstance(each, Mapping) and each.get("assertion") == "a1"
    ]
    return found[0] if found else {}


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
    verdict: str,
    refs: list[str] | None = None,
    *,
    rationale: str = "the badge is drawn in one colour",
) -> Callable[[Sequence[BaseMessage]], Reply]:
    """A scripted move: the verifier's `verdict`, citing `refs`, or the two
    references its request gives when None, and saying `rationale`."""

    def reply(messages: Sequence[BaseMessage]) -> Reply:
        cited = evidence(messages) if refs is None else refs
        parsed = VerifierVerdict.model_validate(
            {
                "verdict": verdict,
                "evidence_refs": cited,
                "confidence": 0.9,
                "rationale": rationale,
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
