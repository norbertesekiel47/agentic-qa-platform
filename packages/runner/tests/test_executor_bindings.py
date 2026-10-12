"""Which locator resolved each assertion's target (#51 B5; ADR-0024's #51 B5
amendment): a check whose target resolved, then passed, failed or timed out,
carries that locator's index; an unresolved, absent, untargeted or
unevaluated one carries none. The scripts are written by hand, and the
tests launch real Chromium through `replay`."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import Target
from aqa_core.config import ProjectConfig
from aqa_runner import text_search
from aqa_runner.browser_session import BrowserSession
from aqa_runner.executor import AssertionOutcome
from aqa_runner.executor import AssertionResult as R
from aqa_runner.locators import Absent, Resolved, Unresolved, Use
from playwright.async_api import Error

from packages.runner.tests.executor_fixtures import (
    SHOP_TARGETS,
    App,
    a_spec,
    compiled,
    run,
    serving_app,
)

ONE_SECOND = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def in_target(id_: str, target: str, **match: str) -> dict[str, Any]:
    """A `text_in_target` check of `target`, by `text` or `pattern`."""
    return {
        "id": id_,
        "expect_index": 0,
        "check": "text_in_target",
        "target": target,
        **match,
    }


def test_each_check_records_the_locator_that_resolved_its_target(
    app: App, tmp_path: Path
) -> None:
    scoped = {"scope": {"css": "#status-area"}}
    targets = SHOP_TARGETS | {
        # The first locator finds nothing, so the second resolves it.
        "moved": {
            "semantic": "the save status, moved",
            "locators": [{"css": "#status-text"}, {"testid": "status"}],
        },
        "shown": {
            "semantic": "the save status, shown",
            "locators": [{"css": "#missing"} | scoped, {"css": "#status"} | scoped],
        },
        "gone": {"semantic": "nothing on the page", "locators": [{"css": "#nothing"}]},
    }
    script = compiled(
        [],
        targets=targets,
        assertions=[
            in_target("a1", "status", text="Not saved"),
            in_target("a2", "moved", text="Not saved"),
            in_target("a3", "moved", text="Saved Ada"),
            {"id": "a4", "expect_index": 0, "check": "not_visible", "target": "error"},
            {"id": "a5", "expect_index": 0, "check": "not_visible", "target": "shown"},
            {
                "id": "a6",
                "expect_index": 0,
                "check": "text_visible",
                "text": "Not saved",
            },
            {
                "id": "a7",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": "/page/shop",
            },
            in_target("a8", "gone", text="anything"),
        ],
    )
    spec = a_spec(tmp_path, ONE_SECOND, start_url="/page/shop")

    result = run(app, tmp_path, script, config=ONE_SECOND, spec=spec).result

    assert result.assertions == (
        R("a1", "pass", locator_index=0),
        R("a2", "pass", locator_index=1),
        R("a3", "failed", locator_index=1),
        R("a4", "pass"),
        R("a5", "failed", locator_index=1),
        R("a6", "pass"),
        R("a7", "pass"),
        R("a8", "binding_unresolved", ("no match",)),
    )
    assert result.outcome == "failed"


def test_a_search_that_times_out_after_resolution_keeps_its_locator(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than the 2 s rule, which test_text_search measures.
    monkeypatch.setattr(text_search, "SEARCH_SECONDS", 1)
    line = {
        "semantic": "the line of a's",
        "locators": [{"css": "p.none"}, {"css": "p"}],
    }
    script = compiled(
        [],
        targets={"line": line},
        assertions=[
            # Each backtracks for exponential time on forty a's and a b.
            in_target("a1", "line", pattern="(a+)+$"),
            {
                "id": "a2",
                "expect_index": 0,
                "check": "text_visible",
                "pattern": "(a+)+$",
            },
        ],
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/catastrophic")

    result = run(app, tmp_path, script, spec=spec).result

    assert result.assertions == (
        R("a1", "check_timed_out", locator_index=1),
        R("a2", "check_timed_out"),
    )


@pytest.mark.parametrize(
    ("state", "outcome"), [("visible", "pass"), ("covered", "failed")]
)
def test_a_visual_check_keeps_the_locator_that_resolved_it(
    app: App, tmp_path: Path, state: str, outcome: AssertionOutcome
) -> None:
    visual = {
        "semantic": "the save link",
        "locators": [{"css": "#gone"}, {"css": "#visual"}],
    }
    check = {
        "id": "a1",
        "expect_index": 0,
        "check": "visible_unoccluded",
        "target": "visual",
        "min_size_px": [44, 24],
        "in_viewport": True,
    }
    script = compiled([], targets={"visual": visual}, assertions=[check])
    spec = a_spec(tmp_path, ONE_SECOND, start_url=f"/page/visual?state={state}")

    result = run(app, tmp_path, script, config=ONE_SECOND, spec=spec).result

    assert result.assertions == (R("a1", outcome, locator_index=1),)


async def _never(*_: object) -> str:
    await asyncio.Event().wait()
    return ""


async def _crashed(*_: object) -> str:
    raise Error("Page.evaluate: Target crashed")


@pytest.mark.parametrize(
    ("text_of", "error"),
    [
        (_crashed, "Error: Page.evaluate: Target crashed"),
        (_never, "the page didn't answer within 2 s"),
    ],
    ids=["raises", "never answers"],
)
def test_a_read_that_fails_after_resolution_keeps_no_locator(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text_of: Callable[..., Awaitable[str]],
    error: str,
) -> None:
    resolve = BrowserSession.resolve
    resolved: list[int] = []

    async def resolving(
        session: BrowserSession, target: Target, use: Use
    ) -> Resolved | Absent | Unresolved:
        found = await resolve(session, target, use)
        if isinstance(found, Resolved):
            resolved.append(found.locator_index)
        return found

    monkeypatch.setattr(BrowserSession, "resolve", resolving)
    monkeypatch.setattr(BrowserSession, "text_of", text_of)
    moved = {
        "semantic": "the save status",
        "locators": [{"css": "#x"}, {"css": "#status"}],
    }
    script = compiled(
        [],
        targets={"moved": moved},
        assertions=[
            in_target("a1", "moved", text="Not saved"),
            {"id": "a2", "expect_index": 0, "check": "url_matches", "pattern": "/"},
        ],
    )
    spec = a_spec(tmp_path, ONE_SECOND, start_url="/page/shop")

    result = run(app, tmp_path, script, config=ONE_SECOND, spec=spec).result

    # The second locator found the target before its read failed, and the
    # outcome makes no claim about the target, so it names no locator.
    assert resolved == [1]
    assert result.assertions == (
        R("a1", "not_evaluated", error=error),
        R(
            "a2",
            "not_evaluated",
            error="not evaluated after a1's look at the page raised",
        ),
    )
    assert result.outcome == "errored"
