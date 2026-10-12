"""Which locator resolved each assertion's target (#51 B5; ADR-0024's #51 B5
amendment): a check whose target resolved, then passed, failed or timed out,
carries that locator's index; an unresolved, absent, untargeted or
unevaluated one carries none. The scripts are written by hand, and the
tests launch real Chromium through `replay`."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

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


def check(id_: str, kind: str, **fields: object) -> dict[str, object]:
    return {"id": id_, "expect_index": 0, "check": kind, **fields}


def target(*locators: dict[str, object]) -> dict[str, object]:
    return {"semantic": "a target", "locators": list(locators)}


def test_each_check_records_the_locator_that_resolved_its_target(
    app: App, tmp_path: Path
) -> None:
    area = {"css": "#status-area"}
    targets = SHOP_TARGETS | {
        # The first locator finds nothing, so the second resolves it.
        "moved": target({"css": "#status-text"}, {"testid": "status"}),
        "shown": target(
            {"css": "#missing", "scope": area}, {"css": "#status", "scope": area}
        ),
        "gone": target({"css": "#nothing"}),
    }
    script = compiled(
        [],
        targets=targets,
        assertions=[
            check("a1", "text_in_target", target="status", text="Not saved"),
            check("a2", "text_in_target", target="moved", text="Not saved"),
            check("a3", "text_in_target", target="moved", text="Saved Ada"),
            check("a4", "not_visible", target="error"),
            check("a5", "not_visible", target="shown"),
            check("a6", "text_visible", text="Not saved"),
            check("a7", "url_matches", pattern="/page/shop"),
            check("a8", "text_in_target", target="gone", text="anything"),
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
    script = compiled(
        [],
        targets={"line": target({"css": "p.none"}, {"css": "p"})},
        assertions=[
            # Each backtracks for exponential time on forty a's and a b.
            check("a1", "text_in_target", target="line", pattern="(a+)+$"),
            check("a2", "text_visible", pattern="(a+)+$"),
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
    visual = target({"css": "#gone"}, {"css": "#visual"})
    shows = check(
        "a1",
        "visible_unoccluded",
        target="visual",
        min_size_px=[44, 24],
        in_viewport=True,
    )
    script = compiled([], targets={"visual": visual}, assertions=[shows])
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
    script = compiled(
        [],
        targets={"moved": target({"css": "#x"}, {"css": "#status"})},
        assertions=[
            check("a1", "text_in_target", target="moved", text="Not saved"),
            check("a2", "url_matches", pattern="/"),
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
