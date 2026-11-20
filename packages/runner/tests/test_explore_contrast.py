"""Exploring a planned `contrast_min` end to end (#327; ADR-0024's #327
amendment): the navigator binds it through `assert_check`, `finish` compiles
it to `ContrastMin` with the plan's ratio, and the strict confirmation replay
confirms it with no model call. Real Chromium against the contrast fixture
pages, served at `/page/order`, with a scripted model."""

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import load_compiled, load_spec
from aqa_runner import explore
from aqa_runner.explore import Explored
from aqa_runner.model_router import ModelRouter
from langchain_core.messages import BaseMessage
from playwright.async_api import async_playwright

from packages.runner.tests.drift_fixtures import serving
from packages.runner.tests.explore_graph_fixtures import (
    Scripted,
    calling,
    planned,
    ref,
    tool,
)
from packages.runner.tests.test_explore_confirm import RESET
from packages.runner.tests.test_explore_graph import configured, written

CONTRAST = Path(__file__).resolve().parents[3] / "tests/fixtures/pages/contrast"
NOTICE = "the order's shipping notice"
# The ratio the expectation states, as the plan must write it (`misfits`).
LEGIBLE = "The order's shipping notice has a text contrast of at least 4.5 to 1"
PLAN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": NOTICE,
                "claim": "its text has a contrast of at least 4.5 to 1",
                "checks": [
                    {
                        "check": "contrast_min",
                        "target_meaning": NOTICE,
                        "min_ratio": 4.5,
                    }
                ],
            }
        ],
        "requires": [],
    }
)
ASSERT = tool(
    "assert_check",
    lambda m: {
        "check_id": "a1",
        "ref": ref(m, "paragraph", "Order 42 ships on Monday"),
    },
)
FINISH = calling("finish", {"steps": []})
# One attempt of three actions, so a run whose check never passes ends.
CONFIG = ProjectConfig.model_validate(
    {"budgets": {"attempts": 1, "actions_per_attempt": 3, "resolve_seconds": 1}}
)


def explored(
    name: str, tmp_path: Path, client: Scripted, confirming: list[int]
) -> Explored:
    """How `explore` ended a run of the order spec, with its reset hook, on
    the contrast page `name`, with `client` as its model. `confirming` gets
    the model's call count as each confirmation starts and as it ends."""
    root = configured(tmp_path / "qa", CONFIG)
    path = root / "order.spec.md"
    path.write_text(
        "---\nid: order\ngoal: A customer reads when their order ships.\n"
        f"preconditions:\n  start_url: '/page/order'\n{RESET}"
        f"expect:\n  - {LEGIBLE}\n---\n"
    )
    spec = load_spec(path, CONFIG)
    replay_run = explore.replay_run

    async def counted(*args: Any, **kwargs: Any) -> Any:
        confirming.append(len(client.requests))
        try:
            return await replay_run(*args, **kwargs)
        finally:
            confirming.append(len(client.requests))

    async def scenario() -> Explored:
        with serving((CONTRAST / f"{name}.html").read_text()) as app:
            async with async_playwright() as playwright, asyncio.timeout(120):
                router = ModelRouter.from_config(CONFIG, client)
                return await explore.explore(
                    spec, CONFIG, root, app.origin, explore.Drivers(router, playwright)
                )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(explore, "replay_run", counted)
        return asyncio.run(scenario())


def compiled(tmp_path: Path) -> Path:
    return tmp_path / "qa" / ".compiled" / "order.json"


def results(request: Sequence[BaseMessage]) -> str:
    """What the turn before `request` got from its tool."""
    found = re.search(r"<results>\n(.*?)\n</results>", request[2].text, re.DOTALL)
    assert found is not None
    return found[1]


def test_a_notice_that_meets_its_ratio_is_explored_compiled_and_confirmed(
    tmp_path: Path,
) -> None:
    client = Scripted(planned(PLAN), ASSERT, FINISH)
    confirming: list[int] = []

    run = explored("meets", tmp_path, client, confirming)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert written(run)["published"] == "confirmed"
    script = load_compiled(compiled(tmp_path), CONFIG)
    assert script.confirmed is True
    [assertion] = script.assertions
    assert assertion.model_dump(mode="json") == {
        "id": "a1",
        "expect_index": 0,
        "check": "contrast_min",
        "target": "t1",
        "min_ratio": 4.5,
    }
    assert script.targets["t1"].semantic == NOTICE
    # The plan, the binding and finish; the confirmation called no model.
    assert len(client.requests) == 3
    assert confirming == [3, 3]


@pytest.mark.parametrize(
    ("page", "why"),
    [
        # 4.48 to 1: the check fails on the page at finish.
        ("falls-short", "failed"),
        # A gradient behind the text, which contrast_min refuses to read.
        (
            "refused",
            (
                "not_evaluated: contrast_min does not support a background image, "
                "gradient, generated background or clipped background behind its "
                "text"
            ),
        ),
    ],
)
def test_a_notice_whose_contrast_is_not_established_publishes_nothing(
    tmp_path: Path, page: str, why: str
) -> None:
    client = Scripted(planned(PLAN), ASSERT, FINISH, FINISH)
    confirming: list[int] = []

    run = explored(page, tmp_path, client, confirming)

    assert results(client.requests[3]) == (
        f"finish compiled nothing: on this page a1 is {why}"
    )
    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["published"] is None
    assert written(run)["reasons"] == [
        "the attempt took all 3 of its turns",
        f"a1: {why}",
    ]
    assert not compiled(tmp_path).exists()
    assert confirming == []
