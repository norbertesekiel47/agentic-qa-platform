"""Heal's vision fallback (#252; ADR-0002's #252 amendment): when a healer's
tree turn finds no element, one screenshot goes to the `vision_fallback` role,
which names a point; our code reads the element there, never clicks it, and
runs it through the tree pick's checks. A control drawn in a canvas can't be
confirmed, so its heal is inconclusive. Real Chromium against #240's drift
pages and the canvas one, a scripted model, through `replays.replay_run`."""

import asyncio
import json
import struct
from base64 import b64decode
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from aqa_runner import heal
from aqa_runner.chat_client import Reply
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
)
from aqa_runner.repairs import Drift
from aqa_runner.screenshots import Screenshot
from aqa_runner.vision_fallback import VISION_INSTRUCTIONS, vision_request
from langchain_core.messages import BaseMessage, HumanMessage

from packages.runner.tests.drift_fixtures import TOGGLE, page
from packages.runner.tests.executor_fixtures import compiled
from packages.runner.tests.explore_graph_fixtures import calling
from packages.runner.tests.heal_fixtures import (
    FOUND_NONE,
    NO_TOOL,
    Healed,
    basis,
    healed,
    turn_files,
    with_budgets,
)
from packages.runner.tests.screenshot_pages import RED_VALUE
from packages.runner.tests.secret_fixtures import BOUND_AT_START, FAKE_VALUE
from packages.runner.tests.test_replay_repairs import secret_spec_at

CANVAS = page("canvas")
# Points in the 1280 by 800 viewport: inside the control the canvas draws,
# on the page's heading, and on the footer's button.
INSIDE = (88, 36)
ON_THE_HEADING = (24, 100)
ON_THE_BUTTON = (48, 154)
SECRET_CONFIG = f"budgets: {{ resolve_seconds: 2 }}\nsecrets: {{ {BOUND_AT_START} }}\n"
CLICKED = ("GET", "/did/canvas-click")
CANARY = "canary-only-the-model-wrote"

A_CANVAS = (
    "vision fallback: the element at the point is a canvas, and no locator names "
    "a point inside one, so the rebind can't be confirmed"
)
OUTSIDE = (
    "vision fallback: the point is outside the viewport, so nothing was read there"
)
FILLED = (
    "vision fallback: a secret was filled, so a screenshot would be a render "
    "without the page's canvas, and none was taken"
)
GOES_BUSY = "() => { setTimeout(() => { for (;;) {} }, 0); }"
BLOCKED = "() => { fetch('http://blocked.example.test/').catch(() => {}); }"


def clicking(x: int, y: int) -> Reply:
    return calling("vision_click", {"x": x, "y": y})


def vision_record(run: Healed, key: str = "1") -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (run.record / f"heals/{key}/vision.json").read_text()
    )
    return loaded


def costs(run: Healed) -> list[dict[str, Any]]:
    lines = (run.record / "costs.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def image_of(message: BaseMessage) -> bytes:
    """The PNG of the one image block `message` holds."""
    [image] = [
        part
        for part in message.content
        if isinstance(part, dict) and part.get("type") == "image"
    ]
    assert image["mime_type"] == "image/png"
    return b64decode(image["base64"])


def drawing(text: str) -> str:
    """The canvas page, whose canvas also draws `text` past its toggle."""
    return CANVAS + (
        "<script>{const pen = document.querySelector('#toolbar canvas')"
        ".getContext('2d'); pen.font = '40px sans-serif'; "
        f"pen.fillText({json.dumps(text)}, 200, 44);}}</script>"
    )


def test_a_vision_point_on_a_canvas_drawn_toggle_is_inconclusive_with_no_proposal(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL, clicking(*INSIDE), html=CANVAS)

    written = run.document
    assert (written["outcome"], written["exit_code"], written["heal_proposal"]) == (
        "inconclusive",
        3,
        None,
    )
    step = run.verdict("step", 1)
    assert step["verdict"] == "inconclusive"
    assert A_CANVAS in step["reason"]
    [repaired] = run.result.repairs
    assert (repaired.script, repaired.problems) == (None, ())
    assert [tools for tools, _ in run.client.offered] == [
        ("rebind",),
        ("vision_click",),
    ]
    assert CLICKED not in run.seen


def test_the_vision_call_is_costed_as_vision_fallbacks_in_heal_mode(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL, clicking(*INSIDE), html=CANVAS)

    assert [(line["role"], line["mode"]) for line in costs(run)] == [
        ("healer", "heal"),
        ("vision_fallback", "heal"),
    ]
    assert vision_record(run)["costs"] == [2]


def test_the_vision_turn_records_the_images_size_and_hash_and_never_the_image(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL, clicking(*INSIDE), html=CANVAS)

    png = image_of(run.asked()[1][-1])
    assert vision_record(run) == {
        "turn": 2,
        "sent": run.requests()[1],
        "image": {"width": 1280, "height": 800, "sha256": sha256(png).hexdigest()},
        "outcome": "ok",
        "call": json.dumps({"name": "vision_click", "args": {"x": 88, "y": 36}}),
        "costs": [2],
    }
    assert list((run.record / "heals/1").glob("*.png")) == []
    assert turn_files(run) == ["1.json"]


def test_the_vision_request_holds_the_target_and_one_png_of_the_viewport(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL, clicking(*INSIDE), html=CANVAS)

    [system, human] = run.asked()[1]
    assert system.content == VISION_INSTRUCTIONS
    png = image_of(human)
    # A PNG's width and height are the first fields of its header.
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (1280, 800)
    assert human.text.startswith("<target>\n")
    target = human.text[len("<target>\n") : human.text.index("\n</target>")]
    assert json.loads(target)["meaning"] == TOGGLE["semantic"]
    shown = human.text[human.text.index("<screenshot>") :]
    assert "1280" in shown
    assert "800" in shown
    assert "<snapshot>" not in human.text
    assert "<results>" not in human.text


def test_a_screenshot_ocr_withholds_ends_the_heal_with_no_vision_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", RED_VALUE)

    run = healed(
        tmp_path,
        NO_TOOL,
        spec_at=secret_spec_at,
        html=drawing(RED_VALUE),
        config=SECRET_CONFIG,
    )

    assert basis(run).said == (
        "vision fallback: the screenshot was withheld: "
        "the image's text spells a bound value"
    )
    assert len(run.requests()) == 1
    assert [line["role"] for line in costs(run)] == ["healer"]
    assert not (run.record / "heals/1/vision.json").exists()


# The password filled, then the toggle clicked, which the canvas page draws.
SIGNED_IN = compiled(
    [
        {
            "seq": 1,
            "action": "fill_secret",
            "target": "password",
            "secret": "TEST_PASSWORD",
            "side_effect": False,
        },
        {"seq": 2, "action": "click", "target": "toggle", "side_effect": False},
    ],
    targets={
        "password": {
            "semantic": "the login form's password field",
            "locators": [
                {"label": "Password"},
                {"css": "input", "scope": {"css": "#login"}},
            ],
        },
        "toggle": TOGGLE,
    },
)
WITH_A_LOGIN = CANVAS.replace(
    "<footer>",
    '<footer><form id="login"><input type="password" aria-label="Password"></form>',
)


def test_no_screenshot_is_taken_once_a_secret_was_filled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", RED_VALUE)
    taken: list[object] = []

    async def counted(session: object) -> Screenshot:
        taken.append(session)
        raise AssertionError("no screenshot is taken after a fill")

    monkeypatch.setattr(heal, "take_screenshot", counted)

    run = healed(
        tmp_path,
        NO_TOOL,
        script=SIGNED_IN,
        spec_at=secret_spec_at,
        html=WITH_A_LOGIN,
        config=SECRET_CONFIG,
    )

    assert [step.outcome for step in run.result.steps][:2] == ["completed", "completed"]
    assert basis(run).said == FILLED
    assert len(run.requests()) == 1
    assert taken == []
    assert not (run.record / "heals/2/vision.json").exists()


@pytest.mark.parametrize(
    "point",
    [
        pytest.param((-1, 10), id="left_of_it"),
        pytest.param((1280, 10), id="right_of_it"),
        pytest.param((10, 800), id="below_it"),
    ],
)
def test_a_point_outside_the_viewport_reads_nothing(
    tmp_path: Path, point: tuple[int, int]
) -> None:
    run = healed(tmp_path, NO_TOOL, clicking(*point), html=CANVAS)

    assert basis(run).said == OUTSIDE
    assert run.document["heal_proposal"] is None
    assert CLICKED not in run.seen


@pytest.mark.parametrize(
    ("unfit", "why"),
    [
        pytest.param(
            calling("vision_click", {"x": 88, "y": 36, "locator": CANARY}),
            "vision fallback: the call to vision_click was not run: its arguments "
            "don't fit the tool",
            id="with_a_locator",
        ),
        pytest.param(
            calling("vision_click", {"x": CANARY, "y": 36}),
            "vision fallback: the call to vision_click was not run: its arguments "
            "don't fit the tool",
            id="x_as_a_string",
        ),
        pytest.param(
            calling("rebind", {"ref": CANARY, "confidence": 1, "rationale": CANARY}),
            "vision fallback: the reply called a tool other than vision_click, so "
            "nothing ran",
            id="calling_rebind",
        ),
    ],
)
def test_a_vision_reply_that_cant_run_ends_the_heal_in_fixed_words(
    tmp_path: Path, unfit: Reply, why: str
) -> None:
    run = healed(tmp_path, NO_TOOL, unfit, html=CANVAS)

    assert basis(run).said == why
    assert CANARY not in json.dumps(run.document)
    assert run.document["heal_proposal"] is None
    assert CLICKED not in run.seen


def test_a_vision_reply_that_names_no_point_found_none(tmp_path: Path) -> None:
    run = healed(tmp_path, NO_TOOL, NO_TOOL, html=CANVAS)

    assert basis(run).said == FOUND_NONE
    assert vision_record(run)["call"] is None
    assert len(run.requests()) == 2


def test_no_vision_turn_is_left_once_the_turns_are_used(tmp_path: Path) -> None:
    run = healed(tmp_path, NO_TOOL, html=CANVAS, config=with_budgets("heal_turns: 1"))

    assert basis(run).said == "the turn budget (1) ran out"
    assert len(run.requests()) == 1
    assert not (run.record / "heals/1/vision.json").exists()


def test_no_vision_turn_is_taken_once_the_run_is_interrupted(tmp_path: Path) -> None:
    held: list[Drift] = []

    async def holding(drift: Drift) -> None:
        held.append(drift)

    async def interrupting(messages: Sequence[BaseMessage]) -> Reply:
        del messages
        await held[0].session.page.evaluate(BLOCKED)
        for _ in range(400):
            if held[0].interrupted():
                break
            await asyncio.sleep(0.05)
        return NO_TOOL

    run = healed(tmp_path, interrupting, html=CANVAS, before_the_heal=holding)

    assert basis(run).said == "the run was interrupted, so the heal stopped"
    assert len(run.requests()) == 1
    assert not (run.record / "heals/1/vision.json").exists()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            PolicyEventError(
                PolicyEvent("frame", "http://elsewhere.test/", "http://elsewhere.test")
            ),
            id="off_the_origins",
        ),
        pytest.param(DocumentChangedError(), id="a_frame_changed"),
    ],
)
def test_a_screenshot_the_page_breaks_ends_the_heal_as_a_look_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def broken(session: object) -> Screenshot:
        del session
        raise error

    monkeypatch.setattr(heal, "take_screenshot", broken)

    run = healed(tmp_path, NO_TOOL, html=CANVAS)

    assert basis(run).said.startswith("the page couldn't be looked at: ")
    assert len(run.requests()) == 1
    assert not (run.record / "heals/1/vision.json").exists()


def test_a_vision_request_that_still_spells_a_value_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)

    def leaking(*args: Any, **kwargs: Any) -> list[BaseMessage]:
        # A gap in the builder's scans: a bound value left in the request.
        return [*vision_request(*args, **kwargs), HumanMessage(content=FAKE_VALUE)]

    monkeypatch.setattr(heal, "vision_request", leaking)

    run = healed(
        tmp_path, NO_TOOL, spec_at=secret_spec_at, html=CANVAS, config=SECRET_CONFIG
    )

    assert basis(run).said == (
        "a request still spelled a bound value after its scans, so it wasn't sent"
    )
    assert len(run.requests()) == 1
    assert not (run.record / "heals/1/vision.json").exists()


def test_a_page_that_stops_answering_before_the_point_is_read_ends_the_heal(
    tmp_path: Path,
) -> None:
    held: list[Drift] = []

    async def holding(drift: Drift) -> None:
        held.append(drift)

    async def going_busy(messages: Sequence[BaseMessage]) -> Reply:
        del messages
        await held[0].session.page.evaluate(GOES_BUSY)
        return clicking(*INSIDE)

    run = healed(tmp_path, NO_TOOL, going_busy, html=CANVAS, before_the_heal=holding)

    assert basis(run).said == (
        "the page couldn't be looked at: the page didn't answer within 3 s"
    )
    assert CLICKED not in run.seen


def test_a_point_on_another_element_goes_through_the_rebind_checks(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL, clicking(*ON_THE_HEADING), html=CANVAS)

    assert basis(run).said == (
        "vision fallback: rebind did not run: a repair rebound this step's target "
        "to an element without its committed role, so the step did not run"
    )
    assert run.result.repairs[0].script is None
    assert len(run.requests()) == 2


def test_a_point_that_passes_the_checks_offers_the_generators_locators(
    tmp_path: Path,
) -> None:
    """The footer's button has the toggle's role, so the checks pass: the offer
    is the generator's, and no model reported anything of it. A point gives no
    snapshot line, so no role or name, and the generator writes what it finds
    alone without them. The replay's own checks then decide (this one fails:
    the details stay hidden)."""
    run = healed(tmp_path, NO_TOOL, clicking(*ON_THE_BUTTON), html=CANVAS)

    [repaired] = run.result.repairs
    assert repaired.problems == ()
    assert repaired.script is not None
    assert [
        locator.model_dump(mode="json", exclude_none=True)
        for locator in repaired.script.targets["toggle"].locators
    ] == [{"css": "button"}]
    assert basis(run).model_reported is None
    assert basis(run).said == "element"
    assert run.document["heal_proposal"] is None
