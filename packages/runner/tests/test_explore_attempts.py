"""Attempts and the reset rule (#56; ADR-0024's Reset hook, Path selection
and attempts, and Confirmation replay): explore runs up to the attempt
budget, resets before every attempt and every confirmation, and starts no
new attempt without a reset hook, since every attempt's start URL has
loaded. Real Chromium against the executor's fixture app, whose `/write/…`
paths count each write, with a scripted model."""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.spec import Reset
from aqa_runner import executor, explore
from aqa_runner.egress_proxy import EgressProxy, PhaseTraffic
from aqa_runner.explore import Explored
from aqa_runner.model_router import ModelRouter
from aqa_runner.reset import reset
from playwright.async_api import async_playwright

from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    STATUS,
    Scripted,
    calling,
    planned,
    ref,
    tool,
)
from packages.runner.tests.test_explore_confirm import PASS, PATH, RESET
from packages.runner.tests.test_explore_graph import (
    CLEAN_PHASE,
    configured,
    files,
    shop_spec,
    written,
)

SELECT = tool(
    "select",
    lambda m: {
        "ref": ref(m, "combobox", "Size"),
        "meaning": "the shop's size select",
        "option": "M",
    },
)
ASSERT_M = tool(
    "assert_check",
    lambda m: {"check_id": "a1", "ref": ref(m, "paragraph", "Saved Ada in size M")},
)
# Another path to the same check: fill, choose size M, save, check.
OTHER = (FILL, SELECT, CLICK, ASSERT_M, calling("finish", {"steps": [1, 2, 3]}))
UNRESET = (
    "the spec has no reset hook, and this attempt's steps, its start URL's load "
    "included, may have changed the app, so no new attempt may repeat them "
    "(AGENTS.md §6)"
)
FRESH = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": STATUS,
                "claim": "says the page is fresh",
                "checks": [
                    {
                        "check": "text_in_target",
                        "target_meaning": STATUS,
                        "text": "Fresh",
                    }
                ],
            }
        ],
        "requires": [],
    }
)
ASSERT_FRESH = tool(
    "assert_check", lambda m: {"check_id": "a1", "ref": ref(m, "paragraph", "Fresh")}
)
# A start page that is quiet the first time; and the page it becomes, whose
# load writes and whose status the check doesn't pass.
ONCE = '<p id="status">Fresh</p>'
AGAIN = (
    '<p id="status">Visited</p><script>fetch("/write/load", {method: "POST"});</script>'
)
# What a page sets on each load, as a sign-in would.
SIGNED = '<script>document.cookie = "fake-session=1; path=/";</script>'


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def exploring(
    app: App,
    tmp_path: Path,
    client: Scripted,
    *,
    preconditions: str = RESET,
    config: ProjectConfig | None = None,
) -> Explored:
    """How `explore` ended a run of the shop spec against the app."""
    config = config or ProjectConfig()
    root = configured(tmp_path / "qa", config)
    spec = shop_spec(root, config, "/page/shop", preconditions)

    async def scenario() -> Explored:
        async with async_playwright() as playwright, asyncio.timeout(240):
            return await explore.explore(
                spec,
                config,
                root,
                app.origin,
                router=ModelRouter.from_config(config, client),
                playwright=playwright,
            )

    return asyncio.run(scenario())


def before_resets(
    monkeypatch: pytest.MonkeyPatch,
    before: dict[int, Callable[[], object]],
    hooks: dict[int, Reset] | None = None,
) -> list[str]:
    """Each reset's path as the run sends it. Before reset `n`, counted from
    0, `before[n]` runs, and `hooks[n]` is sent in its place."""
    sent: list[str] = []

    async def resetting(gate: Any, hook: Reset, start: str, **kwargs: Any) -> None:
        before.get(len(sent), lambda: None)()
        hook = (hooks or {}).get(len(sent), hook)
        sent.append(hook.path)
        await reset(gate, hook, start, **kwargs)

    monkeypatch.setattr(explore, "reset", resetting)
    return sent


def wrong_then_right(
    monkeypatch: pytest.MonkeyPatch, wrong: str = PAGES["shop-wrong"]
) -> dict[int, Callable[[], object]]:
    """The first confirmation's reset swaps in `wrong`, the page whose save
    says the wrong name; the second attempt's puts the shop back."""
    shop = PAGES["shop"]
    return {
        1: lambda: monkeypatch.setitem(PAGES, "shop", wrong),
        2: lambda: monkeypatch.setitem(PAGES, "shop", shop),
    }


def saves_after_resets(paths: list[str]) -> bool:
    """Whether every save had a reset since the save before."""
    reset_since = True
    for path in paths:
        if path == "/write/reset":
            reset_since = True
        elif path == "/write/save":
            if not reset_since:
                return False
            reset_since = False
    return True


def phase(part: str) -> dict[str, object]:
    return CLEAN_PHASE | {"part": part}


# The reset hook before every attempt and every confirmation.


def test_the_reset_runs_before_each_attempt_and_each_confirmation(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "shop", PAGES["shop"] + SIGNED)
    swaps = wrong_then_right(monkeypatch, PAGES["shop-wrong"] + SIGNED)
    resets = before_resets(monkeypatch, swaps)
    # How many resets had gone when each phase ended.
    ends: list[int] = []
    end_phase = EgressProxy.end_phase

    async def ending(proxy: EgressProxy) -> PhaseTraffic:
        traffic = await end_phase(proxy)
        ends.append(len(resets))
        return traffic

    monkeypatch.setattr(EgressProxy, "end_phase", ending)
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})
    client = Scripted(planned(), *PATH, *OTHER)

    run = exploring(app, tmp_path, client, config=config)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    document = written(run)
    assert (document["published"], document["reasons"]) == (
        "confirmed",
        ["confirmation a1: failed"],
    )
    assert document["phases"] == [
        phase("attempt-1"),
        phase("confirmation"),
        phase("attempt-2"),
        phase("confirmation-2"),
    ]
    assert resets == ["/write/reset"] * 4
    # Each phase ended, its browser closed and its requests joined, before
    # the next reset went.
    assert ends == [1, 2, 3, 4]
    assert app.paths() == PASS * 4
    assert saves_after_resets(app.paths())
    # Each phase's browser is fresh: no load carries what the last one set.
    loads = [cookie for path, cookie in app.cookies if path == "/page/shop"]
    assert loads == [None] * 4
    assert [name for name in files(run) if name.startswith("turns/")] == [
        f"turns/{n}.json" for n in range(1, 10)
    ]


def test_a_failed_reset_before_the_next_attempt_exits_14_with_no_browser(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = {2: Reset(http="POST /status/500")}
    before_resets(monkeypatch, wrong_then_right(monkeypatch), failing)
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})
    client = Scripted(planned(), *PATH)

    run = exploring(app, tmp_path, client, config=config)

    assert (run.outcome, run.exit_code) == ("reset_unsafe", 14)
    document = written(run)
    assert document["reasons"] == [
        "the reset hook answered 500",
        "a1: never bound",
        "confirmation a1: failed",
    ]
    assert document["phases"] == [
        phase("attempt-1"),
        phase("confirmation"),
        phase("attempt-2"),
    ]
    assert app.paths() == [*PASS * 2, "/status/500"]
    assert len(client.requests) == 5


# No new attempt without a reset hook: the start's load counts.


def test_without_a_reset_hook_a_failed_confirmation_starts_no_new_attempt(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "shop", ONCE)
    replay = executor.replay

    async def newly_writing(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setitem(PAGES, "shop", AGAIN)
        return await replay(*args, **kwargs)

    monkeypatch.setattr(executor, "replay", newly_writing)
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})
    finish = calling("finish", {"steps": [], "changes_nothing": [0]})
    client = Scripted(planned(FRESH), ASSERT_FRESH, finish)

    run = exploring(app, tmp_path, client, preconditions="", config=config)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    document = written(run)
    assert document["reasons"] == [
        f"no new attempt started: {UNRESET}",
        "confirmation a1: failed",
    ]
    assert document["phases"] == [phase("attempt-1"), phase("confirmation")]
    assert app.paths() == ["/page/shop", "/page/shop", "/write/load"]
    assert len(client.requests) == 3


# The attempt budget.


def test_three_failed_confirmations_give_up_with_exit_3(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = PAGES["shop"]
    monkeypatch.setitem(PAGES, "shop", shop)

    def serving(page: str) -> Callable[[], object]:
        return lambda: PAGES.__setitem__("shop", page)

    # Each attempt's reset serves the shop; each confirmation's, the wrong one.
    before_resets(
        monkeypatch,
        {n: serving(PAGES["shop-wrong"] if n % 2 else shop) for n in range(6)},
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})
    client = Scripted(planned(), *PATH * 3)

    run = exploring(app, tmp_path, client, config=config)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    document = written(run)
    assert document["reasons"] == [
        "confirmation a1: failed",
        "confirmation-2 a1: failed",
        "confirmation-3 a1: failed",
    ]
    assert [each["part"] for each in document["phases"]] == [
        "attempt-1",
        "confirmation",
        "attempt-2",
        "confirmation-2",
        "attempt-3",
        "confirmation-3",
    ]
    assert app.paths().count("/write/save") == 6
    assert saves_after_resets(app.paths())


def test_a_next_part_that_cant_be_made_keeps_each_phase_once_and_exits_15(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "qa" / ".aqa" / "runs"
    swap = wrong_then_right(monkeypatch)[1]

    def squatting() -> None:
        swap()
        (next(runs.iterdir()) / "attempt-2").mkdir()

    before_resets(monkeypatch, {1: squatting})
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})
    client = Scripted(planned(), *PATH)

    run = exploring(app, tmp_path, client, config=config)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    document = written(run)
    assert document["phases"] == [phase("attempt-1"), phase("confirmation")]
    assert document["reasons"] == [
        "the run record took no more lines, so the attempt stopped",
        "confirmation a1: failed",
    ]
    assert len(client.requests) == 5
