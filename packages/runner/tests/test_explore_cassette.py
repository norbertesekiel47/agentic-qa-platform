"""The fixture explore replayed from its live recording (ADR-0024's #53 P21b
amendment): `aqa explore` in process with the real adapter, in real Chromium
against the executor's fixture app, and the cassette's requests as the file
holds them. No test here reaches a provider."""

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
import typer.testing
from aqa_cli.main import app as aqa
from aqa_core.project import load_compiled
from aqa_runner import navigator

from packages.runner.tests.conftest import CASSETTES, Recording
from packages.runner.tests.executor_fixtures import serving_app
from packages.runner.tests.record_cassettes import EXPLORE, FIXTURE_CONFIG, fixture
from packages.runner.tests.test_record_explore_cassette import bodies

pytestmark = pytest.mark.usefixtures("reset_tracing")

Cassette = Callable[..., AbstractContextManager[Recording]]

# Read from the accepted live capture (ADR-0024's #53 P21b receipts), and
# empty until it is recorded: each plan call's roles, the navigator's turns,
# the run's cost as its line gives it, and the compiled assertions and steps.
PLAN_ROLES: list[list[str]] = []
TURNS = 0
COST = ""
ASSERTIONS: list[dict[str, Any]] = []
STEPS: list[dict[str, Any]] = []
# The navigator's tools, as every turn offers them.
TOOLS = [
    "navigate",
    "reload",
    "click",
    "fill",
    "fill_secret",
    "select",
    "press",
    "describe_parts",
    "assert_check",
    "note_for_absence",
    "finish",
    "restart",
]


def recorded() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The cassette's request bodies in the order they were sent: the plan
    calls', then the navigator's."""
    sent = bodies(CASSETTES / f"{EXPLORE}.yaml")
    return sent[: len(PLAN_ROLES)], sent[len(PLAN_ROLES) :]


def test_the_fixture_explore_passes_from_its_recorded_cassette(
    cassette: Cassette, tmp_path: Path
) -> None:
    root = tmp_path / "qa"
    spec = fixture(root)

    with serving_app() as app, cassette(EXPLORE, replay_only=True) as recording:
        result = typer.testing.CliRunner().invoke(
            aqa, ["explore", str(spec.path), "--url", app.origin]
        )

    assert result.exit_code == 0, result.output
    target = root / ".compiled" / "shop.json"
    assert re.fullmatch(
        rf"compiled shop: \d+\.\ds, \${re.escape(COST)}, {re.escape(str(target))}\n",
        result.stdout,
    )
    assert result.stderr == ""
    script = load_compiled(target, FIXTURE_CONFIG)
    assert script.confirmed is True
    assert [
        each.model_dump(mode="json", exclude_none=True) for each in script.assertions
    ] == ASSERTIONS
    # The flags come from the run's network evidence, which
    # test_explore_side_effects.py owns.
    assert [
        step.model_dump(mode="json", exclude={"side_effect", "side_effect_basis"})
        for step in script.steps
    ] == STEPS
    assert len(recording.sent) == len(PLAN_ROLES) + TURNS
    [record] = (root / ".aqa" / "runs").iterdir()
    confirmation = json.loads((record / "explore.json").read_text())["confirmation"]
    # The confirmation replays with every locator (ADR-0025's #53 P11
    # amendment).
    assert (confirmation["outcome"], confirmation["error_code"]) == ("passed", None)


def test_no_navigator_request_forces_a_tool_choice() -> None:
    plans, turns = recorded()
    choices = [None] * len(PLAN_ROLES) + [{"type": "auto"}] * TURNS

    assert [body.get("tool_choice") for body in plans + turns] == choices
    assert [body.get("tools") for body in plans] == [None] * len(PLAN_ROLES)
    names = [[tool["name"] for tool in body["tools"]] for body in turns]
    assert names == [TOOLS] * TURNS
    strict = [{tool["strict"] for tool in body["tools"]} for body in turns]
    assert strict == [{True}] * TURNS


def test_every_navigator_request_holds_the_instructions_and_two_blocks() -> None:
    plans, turns = recorded()

    assert [[message["role"] for message in body["messages"]] for body in plans] == (
        PLAN_ROLES
    )
    assert [body["system"] for body in turns] == [navigator.INSTRUCTIONS] * TURNS
    # One user message: the spec and plan, then the turn's log and page; no
    # earlier request's messages.
    assert [
        [
            (
                message["role"],
                [block["text"].split("\n", 1)[0] for block in message["content"]],
            )
            for message in body["messages"]
        ]
        for body in turns
    ] == [[("user", ["<spec>", "<log>"])]] * TURNS
