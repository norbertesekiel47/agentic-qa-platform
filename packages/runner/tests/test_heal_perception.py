"""Heal's perception setting (#315; ADR-0002's #315 amendment): `tree_only`
offers the healer's `rebind` only, `vision_only` no tree turn and one vision
turn per drift, through the same guards as `hybrid`'s, and `hybrid` a vision
turn once a tree turn finds no element. Real Chromium against #240's drift
pages and the canvas one, a scripted model, through `replays.replay_run`."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from aqa_core.config import Perception
from aqa_runner import heal
from aqa_runner.chat_client import Reply
from aqa_runner.heal import Healing
from aqa_runner.screenshots import Screenshot, take_screenshot
from aqa_runner.vision_fallback import VISION_INSTRUCTIONS, VISION_ONLY_INSTRUCTIONS
from langchain_core.messages import BaseMessage

from packages.runner.tests import heal_fixtures
from packages.runner.tests.heal_fixtures import (
    BUDGETS,
    FOUND_NONE,
    MOVED,
    NO_TOOL,
    basis,
    healed,
    turn_files,
)
from packages.runner.tests.screenshot_pages import RED_VALUE
from packages.runner.tests.test_heal_vision import (
    A_CANVAS,
    CANVAS,
    CLICKED,
    FILLED,
    INSIDE,
    ON_THE_MOVED_TOGGLE,
    SECRET_CONFIG,
    SIGNED_IN,
    WITH_A_LOGIN,
    clicking,
    costs,
    vision_record,
)
from packages.runner.tests.test_replay_repairs import secret_spec_at


def perceiving(perception: Perception, config: str = BUDGETS) -> str:
    return f"{config}heal: {{ perception: {perception} }}\n"


@pytest.mark.parametrize(
    ("perception", "offered", "lines"),
    [
        pytest.param("tree_only", [("rebind",)], [("healer", "heal")], id="tree_only"),
        pytest.param(
            "vision_only",
            [("vision_click",)],
            [("vision_fallback", "heal")],
            id="vision_only",
        ),
        pytest.param(
            "hybrid",
            [("rebind",), ("vision_click",)],
            [("healer", "heal"), ("vision_fallback", "heal")],
            id="hybrid",
        ),
    ],
)
def test_each_perception_offers_its_tools_and_costs_its_calls(
    tmp_path: Path,
    perception: Perception,
    offered: list[tuple[str, ...]],
    lines: list[tuple[str, str]],
) -> None:
    """#252's third criterion: the tools each setting offers, on a drift where
    no call finds anything."""
    replies = [NO_TOOL] * len(offered)

    run = healed(tmp_path, *replies, html=CANVAS, config=perceiving(perception))

    assert [tools for tools, _ in run.client.offered] == offered
    assert [(line["role"], line["mode"]) for line in costs(run)] == lines
    assert basis(run).said == FOUND_NONE


def test_a_vision_only_pick_on_the_moved_toggle_is_a_heal_proposal(
    tmp_path: Path,
) -> None:
    run = healed(
        tmp_path,
        clicking(*ON_THE_MOVED_TOGGLE),
        html=MOVED,
        config=perceiving("vision_only"),
    )

    written = run.document
    assert (written["outcome"], written["exit_code"]) == ("drift_consistent", 7)
    assert written["heal_proposal"]["patch"] == [
        {
            "op": "replace",
            "path": "/targets/toggle/locators",
            "value": [{"css": "button", "scope": {"css": "#order"}}],
        }
    ]
    assert [tools for tools, _ in run.client.offered] == [("vision_click",)]
    # No tree turn, so the vision turn is the drift's first, and the folder
    # holds the first look's evidence beside it.
    assert turn_files(run) == []
    record = vision_record(run)
    assert (record["turn"], record["costs"]) == (1, [1])
    assert sorted(path.name for path in (run.record / "heals/1").iterdir()) == [
        "a11y_snapshot.yaml",
        "console_log.json",
        "network_log.json",
        "screenshot.png",
        "vision.json",
    ]


def test_a_vision_only_point_on_a_canvas_drawn_toggle_is_inconclusive(
    tmp_path: Path,
) -> None:
    run = healed(
        tmp_path, clicking(*INSIDE), html=CANVAS, config=perceiving("vision_only")
    )

    written = run.document
    assert (written["outcome"], written["exit_code"], written["heal_proposal"]) == (
        "inconclusive",
        3,
        None,
    )
    assert A_CANVAS in run.verdict("step", 1)["reason"]
    assert CLICKED not in run.seen
    assert [tools for tools, _ in run.client.offered] == [("vision_click",)]


def test_a_vision_only_heal_after_a_fill_gives_up_with_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fill's guard stops the vision turn, and the heal never falls back
    to a tree turn."""
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", RED_VALUE)
    taken: list[object] = []

    async def counted(session: object) -> Screenshot:
        taken.append(session)
        raise AssertionError("no screenshot is taken after a fill")

    monkeypatch.setattr(heal, "take_screenshot", counted)

    run = healed(
        tmp_path,
        script=SIGNED_IN,
        spec_at=secret_spec_at,
        html=WITH_A_LOGIN,
        config=perceiving("vision_only", SECRET_CONFIG),
    )

    assert basis(run).said == FILLED
    assert taken == []
    assert run.client.requests == []
    costed = run.record / "costs.jsonl"
    assert not costed.exists() or costed.read_text() == ""
    assert not (run.record / "heals/2/vision.json").exists()


def test_a_vision_only_request_says_no_tree_turn_ran(tmp_path: Path) -> None:
    """`VISION_INSTRUCTIONS` tells the model the tree showed nothing, which no
    `vision_only` heal looked at; its own prompt is that one without the claim,
    and `hybrid` still sends `VISION_INSTRUCTIONS`."""
    alone = healed(
        tmp_path / "alone", NO_TOOL, html=CANVAS, config=perceiving("vision_only")
    )
    after = healed(tmp_path / "after", NO_TOOL, NO_TOOL, html=CANVAS)

    [[system, _]] = alone.client.requests
    assert system.content == VISION_ONLY_INSTRUCTIONS
    [_, [system, _]] = after.client.requests
    assert system.content == VISION_INSTRUCTIONS
    assert "accessibility tree" not in VISION_ONLY_INSTRUCTIONS
    # The claim removed, and nothing else changed.
    claim = ", and the page's accessibility tree showed no element for it"
    assert VISION_INSTRUCTIONS.replace(claim, "") == VISION_ONLY_INSTRUCTIONS


def test_a_perception_outside_the_three_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value `heal.perception` never loads, which only a defect could hand a
    `Healing`, raises before any call or capture, so it never acts as `hybrid`
    and sends the page's pixels."""
    sent: list[Sequence[BaseMessage]] = []
    taken: list[object] = []

    def asked(messages: Sequence[BaseMessage]) -> Reply:
        sent.append(messages)
        return NO_TOOL

    async def counted(session: Any) -> Screenshot:
        taken.append(session)
        return await take_screenshot(session)

    def unknown(*args: Any) -> Healing:
        router, spending, turns, seconds, _ = args
        return Healing(
            router, spending, turns, seconds, cast(Perception, "pixels_please")
        )

    monkeypatch.setattr(heal, "take_screenshot", counted)
    monkeypatch.setattr(heal_fixtures, "Healing", unknown)

    with pytest.raises(AssertionError, match="pixels_please"):
        healed(tmp_path, asked, asked, html=CANVAS)

    assert (sent, taken) == ([], [])
