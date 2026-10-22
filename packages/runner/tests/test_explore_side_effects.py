"""Where explore infers the flags (ADR-0025's #54 amendment; ADR-0024): once
the attempt's browser has closed and its phase has ended, and again once a
passing confirmation's has, which can only raise one and, with no reset
hook before it, warns when it did; and what the start URL's load, step 0,
decides for a spec with no reset hook: it is safe only when the model lists
0 in `changes_nothing` and its run showed positive evidence. Real Chromium
against the executor's fixture app, with a scripted model."""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner import explore
from aqa_runner.egress_proxy import EgressProxy, PhaseTraffic
from aqa_runner.explore import Explored
from aqa_runner.model_router import ModelRouter
from aqa_runner.navigator import Finish
from aqa_runner.navigator_tools import Attempt, Finished
from playwright.async_api import async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.explore_graph_fixtures import Scripted, calling, ref, tool
from packages.runner.tests.test_explore_confirm import published
from packages.runner.tests.test_explore_graph import (
    configured,
    files,
    shop_spec,
    written,
)
from packages.runner.tests.test_explore_preflight import planned
from packages.runner.tests.test_navigator_side_effects import LATER, ON_A_PAGE, WROTE

# What the run says when a confirmation with no reset hook before it saw a
# possible side effect that the attempt didn't.
REPEATED = (
    "warning: the confirmation passed, but with no reset hook before it, it saw "
    "a possible side effect that the attempt didn't: repeating the path, or "
    "loading its start URL, may have changed the app (AGENTS.md §6). Declare a "
    "reset hook, and explore it again"
)
RESET = "  reset: { http: POST /write/reset }\n"
TO_FORM = calling("navigate", {"path": "/page/form"})
CHECK = calling("assert_check", {"check_id": "a1", "ref": None})
# Probe reads that write only once it has been read before, as the
# confirmation's replay reads it.
AGAIN = """fetch('/probe/visits').then((r) => r.json())
    .then((b) => b.count > 0 && fetch('/write/again', {method: 'POST'}))"""
# A start page whose load writes; a button that writes only when clicked a
# second time; and a start page whose load does only the second time.
PAGES_HERE = {
    "writes": """<a href="/page/form">Form</a><script>
        fetch("/write/load", {method: "POST"});
    </script>""",
    "again": f"""<button onclick="{AGAIN}">Look</button>""",
    "again-start": f"""<a href="/page/form">Form</a><script>{AGAIN}</script>""",
}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    for name, page in PAGES_HERE.items():
        monkeypatch.setitem(PAGES, name, page)
    with serving_app() as served:
        served.probes["visits"] = []
        yield served


def explored(
    app: App, tmp_path: Path, page: str, *moves: Any, preconditions: str = ""
) -> Explored:
    config = ProjectConfig()
    root = configured(tmp_path / "qa", config)
    spec = shop_spec(root, config, page, preconditions)
    client = Scripted(planned(ON_A_PAGE), *moves)

    async def scenario() -> Explored:
        async with async_playwright() as playwright, asyncio.timeout(120):
            return await explore.explore(
                spec,
                config,
                root,
                app.origin,
                router=ModelRouter.from_config(config, client),
                playwright=playwright,
            )

    return asyncio.run(scenario())


def flags(tmp_path: Path) -> list[tuple[bool, str | None]]:
    return [(s.side_effect, s.side_effect_basis) for s in published(tmp_path).steps]


def agreed(steps: list[int], *, start: bool = True) -> Any:
    """`finish` keeping `steps`, each of which, and the start's load when
    `start`, the model says changes nothing."""
    listed = [0, *steps] if start else steps
    return calling("finish", {"steps": steps, "changes_nothing": listed})


def moved(path: list[int]) -> list[Any]:
    return [TO_FORM, CHECK, agreed(path)] if path else [CHECK, agreed(path)]


@pytest.mark.parametrize("path", [[], [1]], ids=["empty", "one-step"])
def test_a_path_with_no_side_effect_and_a_quiet_start_confirms_with_no_reset_hook(
    app: App, tmp_path: Path, path: list[int]
) -> None:
    run = explored(app, tmp_path, "/page/start", *moved(path))

    assert (run.outcome, written(run)["published"]) == ("compiled", "confirmed")
    assert written(run)["reasons"] == []
    assert flags(tmp_path) == [(False, None)] * len(path)
    assert "confirmation/steps.jsonl" in files(run)
    assert "/write/reset" not in app.paths()


@pytest.mark.parametrize("path", [[], [1]], ids=["empty", "one-step"])
def test_a_quiet_start_finish_didnt_list_keeps_the_path_unconfirmed_with_no_reset_hook(
    app: App, tmp_path: Path, path: list[int]
) -> None:
    moves = [TO_FORM] if path else []

    run = explored(
        app, tmp_path, "/page/start", *moves, CHECK, agreed(path, start=False)
    )

    assert (run.outcome, written(run)["published"]) == ("compiled", "unconfirmed")
    assert flags(tmp_path) == [(False, None)] * len(path)
    assert "confirmation/steps.jsonl" not in files(run)
    assert app.paths().count("/page/start") == 1


@pytest.mark.parametrize("path", [[], [1]], ids=["empty", "one-step"])
def test_a_start_url_whose_load_writes_keeps_the_path_unconfirmed_with_no_reset_hook(
    app: App, tmp_path: Path, path: list[int]
) -> None:
    run = explored(app, tmp_path, "/page/writes", *moved(path))

    assert (run.outcome, written(run)["published"]) == ("compiled", "unconfirmed")
    # The step after the start's write is no side-effect step of its own.
    assert flags(tmp_path) == [(False, None)] * len(path)
    assert "confirmation/steps.jsonl" not in files(run)
    assert app.paths().count("/write/load") == 1


def test_a_write_after_finish_compiled_raises_the_published_flag(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finish = Attempt.finish

    async def writing_after(attempt: Attempt, call: Finish, by: Any) -> Finished:
        done = await finish(attempt, call, by)
        await attempt.session.page.evaluate("fetch('/write/after', {method: 'POST'})")
        await until(lambda: ("POST", "/write/after") in app.seen)
        return done

    monkeypatch.setattr(Attempt, "finish", writing_after)

    run = explored(app, tmp_path, "/page/start", *moved([1]), preconditions=RESET)

    assert (run.outcome, written(run)["published"]) == ("compiled", "confirmed")
    assert flags(tmp_path) == [(True, WROTE)]
    # The reset hook ran before the confirmation repeated it: no warning.
    assert written(run)["reasons"] == []


def test_a_write_the_confirmation_sees_raises_the_flag_and_the_run_warns(
    app: App, tmp_path: Path
) -> None:
    look = tool("click", lambda m: {"ref": ref(m, "button", "Look"), "meaning": "look"})

    run = explored(app, tmp_path, "/page/again", look, CHECK, agreed([1]))

    assert (run.outcome, written(run)["published"]) == ("compiled", "confirmed")
    assert flags(tmp_path) == [(True, WROTE)]
    assert written(run)["reasons"] == [REPEATED]
    assert app.paths().count("/write/again") == 1
    assert run.script is not None
    assert [step.side_effect for step in run.script.steps] == [True]


def test_each_kept_step_gets_its_own_confirmation_run_past_a_detour(
    app: App, tmp_path: Path
) -> None:
    # Step 1 is a detour finish leaves out, so the kept steps 2, 3 and 4
    # replay as 1, 2 and 3; only the look writes, and only when replayed.
    look = tool("click", lambda m: {"ref": ref(m, "button", "Look"), "meaning": "look"})
    to_look = calling("navigate", {"path": "/page/again"})
    moves = [TO_FORM, to_look, look, TO_FORM, CHECK, agreed([2, 3, 4])]

    run = explored(app, tmp_path, "/page/start", *moves)

    assert (run.outcome, written(run)["published"]) == ("compiled", "confirmed")
    assert flags(tmp_path) == [(True, LATER), (True, WROTE), (False, None)]
    assert written(run)["reasons"] == [REPEATED]


def test_a_start_whose_load_writes_only_on_the_confirmation_warns(
    app: App, tmp_path: Path
) -> None:
    run = explored(app, tmp_path, "/page/again-start", CHECK, agreed([]))

    assert (run.outcome, written(run)["published"]) == ("compiled", "confirmed")
    assert written(run)["reasons"] == [REPEATED]
    assert app.paths().count("/write/again") == 1


@pytest.mark.parametrize(
    ("preconditions", "published"),
    [("", "unconfirmed"), (RESET, "confirmed")],
    ids=["no-reset-hook", "reset-hook"],
)
def test_a_write_the_proxy_reads_as_the_phase_ends_raises_the_published_flag(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preconditions: str,
    published: str,
) -> None:
    # After the browser has closed, while ending the phase joins every
    # connection, the proxy may still read a request the browser sent.
    told: list[Callable[[bytes], None]] = []
    watch, end = EgressProxy.watch, EgressProxy.end_phase

    def telling(proxy: EgressProxy, on_write: Callable[[bytes], None]) -> None:
        told.append(on_write)
        watch(proxy, on_write)

    async def ending(proxy: EgressProxy) -> PhaseTraffic:
        for on_write in told:
            on_write(b"POST")
        told.clear()
        return await end(proxy)

    monkeypatch.setattr(EgressProxy, "watch", telling)
    monkeypatch.setattr(EgressProxy, "end_phase", ending)

    run = explored(
        app, tmp_path, "/page/start", *moved([1]), preconditions=preconditions
    )

    # With no reset hook, the attempt's flags alone decide: never replayed.
    assert (run.outcome, written(run)["published"]) == ("compiled", published)
    assert flags(tmp_path) == [(True, WROTE)]
