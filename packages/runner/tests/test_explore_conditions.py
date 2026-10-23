"""Keeping the coverage plan's required conditions in the path explore
selects (#55; ADR-0024's "Path selection and attempts"): `finish` names the
step that meets each condition, a path that drops the reload, names
another step for it, or takes a step after it, is refused as a tool error
the attempt survives, and the published script carries `satisfies` on the
reload. Real Chromium against a page whose favorite button changes at the
click, before the app has kept anything, as Conduit's does; the favorite is
kept, as a cookie the page sets, only once the app accepted the write. A
scripted model."""

import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import RequiredCondition
from aqa_core.coverage_plan import CoveragePlan
from langchain_core.messages import BaseMessage

from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    Scripted,
    calling,
    planned,
    ref,
    tool,
)
from packages.runner.tests.test_explore_confirm import confirming, published
from packages.runner.tests.test_explore_graph import written

BUTTON = "the article's favorite button"
FAVORITE_PAGE = """<main><button id="favorite">Favorite Article</button>
    <a href="/page/form">Another page</a></main>
    <script>
        const button = document.querySelector("#favorite");
        if (document.cookie.includes("favorited=1")) {
            button.textContent = "Unfavorite Article";
        }
        button.addEventListener("click", () => {
            button.textContent = "Unfavorite Article";
            fetch("/write/favorite", {method: "POST"}).then((answer) => {
                if (answer.ok) document.cookie = "favorited=1; path=/";
            });
        });
    </script>"""
CONDITION = "checked after reloading the article page"
PLAN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": BUTTON,
                "claim": "reads Unfavorite Article",
                "checks": [
                    {
                        "check": "text_in_target",
                        "target_meaning": BUTTON,
                        "text": "Unfavorite Article",
                    }
                ],
            }
        ],
        "requires": [{"id": "c1", "condition": CONDITION, "kind": "reload"}],
    }
)
RESET = "  reset: { http: POST /write/reset }\n"

FAVORITE = tool(
    "click",
    lambda m: {"ref": ref(m, "button", "Favorite Article"), "meaning": BUTTON},
)
ASSERT = tool(
    "assert_check",
    lambda m: {"check_id": "a1", "ref": ref(m, "button", "Unfavorite Article")},
)
RELOAD = calling("reload", {})


def finish(*steps: int, met: int | None = None) -> Any:
    satisfies = [] if met is None else [{"condition": "c1", "step": met}]
    return calling("finish", {"steps": list(steps), "satisfies": satisfies})


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setitem(PAGES, "favorite", FAVORITE_PAGE)
    with serving_app() as served:
        yield served


def explored(app: App, tmp_path: Path, client: Scripted) -> Any:
    return confirming(app, tmp_path, client, preconditions=RESET, page="/page/favorite")


def results(request: Sequence[BaseMessage]) -> str:
    found = re.search(r"<results>\n(.*?)\n</results>", request[2].text, re.DOTALL)
    assert found is not None
    return found[1]


def kept(tmp_path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Each published step's action and the conditions it meets."""
    return [(step.action, step.satisfies) for step in published(tmp_path).steps]


RELOADED = [("click", ()), ("reload", ("c1",))]


def test_a_path_that_reloads_before_its_checks_compiles_with_satisfies_on_the_reload(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(planned(PLAN), FAVORITE, RELOAD, ASSERT, finish(1, 2, met=2))

    run = explored(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert written(run)["published"] == "confirmed"
    assert kept(tmp_path) == RELOADED
    assert published(tmp_path).coverage.model_dump(mode="json")["requires"] == [
        {"id": "c1", "condition": CONDITION, "kind": "reload"}
    ]


def test_a_path_that_drops_the_reload_is_refused_naming_the_condition_and_a_later_finish_keeps_it(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(
        planned(PLAN), FAVORITE, ASSERT, finish(1), RELOAD, finish(1, 2, met=2)
    )

    run = explored(app, tmp_path, client)

    # The check passes on the page before the reload, as the button changed
    # at the click: only the condition refuses the path.
    assert results(client.requests[4]) == (
        f'finish compiled nothing: requires c1 ("{CONDITION}"): finish keeps no '
        "step that meets it"
    )
    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert kept(tmp_path) == RELOADED


def test_a_path_that_drops_the_reload_and_marks_the_click_is_refused_naming_both(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(
        planned(PLAN), FAVORITE, ASSERT, finish(1, met=1), RELOAD, finish(1, 2, met=2)
    )

    run = explored(app, tmp_path, client)

    assert results(client.requests[4]) == (
        "finish compiled nothing: requires c1: finish says step 1 meets it, but c1 "
        f'needs a reload and step 1 isn\'t one; requires c1 ("{CONDITION}"): finish '
        "keeps no step that meets it"
    )
    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert kept(tmp_path) == RELOADED


def test_a_path_whose_reload_a_click_follows_is_refused_naming_the_step_and_a_later_finish_keeps_it(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(
        planned(PLAN),
        RELOAD,
        FAVORITE,
        ASSERT,
        finish(1, 2, met=1),
        RELOAD,
        finish(2, 3, met=3),
    )

    run = explored(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert kept(tmp_path) == RELOADED
    assert results(client.requests[5]) == (
        "finish compiled nothing: requires c1: finish says step 1 meets it, but c1 "
        "needs the reload that ends the path, and step 1 doesn't end it; requires "
        f'c1 ("{CONDITION}"): finish keeps no step that meets it'
    )


def test_a_reload_finish_names_but_leaves_out_is_refused_naming_the_step(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(
        planned(PLAN), FAVORITE, RELOAD, ASSERT, finish(1, met=2), finish(1, 2, met=2)
    )

    run = explored(app, tmp_path, client)

    assert results(client.requests[5]) == (
        "finish compiled nothing: requires c1: finish says step 2 meets it, but "
        f'doesn\'t keep step 2; requires c1 ("{CONDITION}"): finish keeps no step '
        "that meets it"
    )
    assert (run.outcome, run.exit_code) == ("compiled", 0)


def test_detours_outside_the_named_path_are_not_in_the_compiled_script(
    app: App, tmp_path: Path
) -> None:
    away = tool(
        "click",
        lambda m: {
            "ref": ref(m, "link", "Another page"),
            "meaning": "the article page's link to another page",
        },
    )
    back = calling("navigate", {"path": "/page/favorite"})
    client = Scripted(
        planned(PLAN), away, back, FAVORITE, RELOAD, ASSERT, finish(3, 4, met=4)
    )

    run = explored(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert kept(tmp_path) == RELOADED
    targets = published(tmp_path).targets.values()
    assert {target.semantic for target in targets} == {BUTTON}


def test_each_condition_finish_names_reaches_the_published_script(
    app: App, tmp_path: Path
) -> None:
    favorited = RequiredCondition(
        id="c2", condition="checked once the article is favorited"
    )
    plan = PLAN.model_copy(update={"requires": (*PLAN.requires, favorited)})
    met = [{"condition": "c1", "step": 2}, {"condition": "c2", "step": 1}]
    client = Scripted(
        planned(plan),
        FAVORITE,
        RELOAD,
        ASSERT,
        calling("finish", {"steps": [1, 2], "satisfies": met}),
    )

    run = explored(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert kept(tmp_path) == [("click", ("c2",)), ("reload", ("c1",))]
