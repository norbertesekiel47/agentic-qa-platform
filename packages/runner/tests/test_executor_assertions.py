"""The M1 strict executor's assertions (#46; DATA_MODEL §7, Replay outcomes;
ADR-0024's #46 amendment, "The executor"): after the last step, every
assertion is evaluated, `pass`, `failed`, `binding_unresolved` or
`check_timed_out`, and a run passes only when every one does. The scripts
are written by hand, and the browser tests launch real Chromium on the OS
that runs them: Linux in CI, macOS locally."""

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from aqa_core.compiled import Target
from aqa_core.config import ProjectConfig
from aqa_runner import settling, text_search
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import DocumentChangedError, PolicyEvent
from aqa_runner.egress_proxy import RefusedHost
from aqa_runner.executor import RunResult
from aqa_runner.locators import Absent, Resolved, Unresolved, Use
from aqa_runner.settling import Window
from playwright.async_api import Error, Page

from packages.runner.tests.executor_fixtures import (
    SHOP_TARGETS,
    App,
    a_spec,
    compiled,
    run,
    serving_app,
    shop_script,
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def outcomes(result: RunResult) -> list[tuple[str, str]]:
    return [(assertion.id, assertion.outcome) for assertion in result.assertions]


def test_a_hand_written_script_passes_on_its_clean_page(
    app: App, tmp_path: Path
) -> None:
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/shop")

    result = run(app, tmp_path, shop_script("shop"), spec=spec).result

    assert [step.outcome for step in result.steps] == ["completed"] * 7
    assert outcomes(result) == [
        ("a1", "pass"),
        ("a2", "pass"),
        ("a3", "pass"),
        ("a4", "pass"),
    ]
    assert result.outcome == "passed"


def test_wrong_content_fails_the_named_assertion_and_every_other_is_still_evaluated(
    app: App, tmp_path: Path
) -> None:
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/shop-wrong")

    result = run(app, tmp_path, shop_script("shop-wrong"), spec=spec).result

    # The status says "Saved nobody": only the check of what was saved fails.
    assert outcomes(result) == [
        ("a1", "pass"),
        ("a2", "failed"),
        ("a3", "pass"),
        ("a4", "pass"),
    ]
    assert result.outcome == "failed"


def test_an_assertion_whose_element_is_gone_reports_binding_unresolved(
    app: App, tmp_path: Path
) -> None:
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    spec = a_spec(tmp_path, config, start_url="/page/shop-gone")

    result = run(
        app, tmp_path, shop_script("shop-gone"), config=config, spec=spec
    ).result

    # The save removed the status: no locator finds it, and no text says
    # "saved"; the other checks are still evaluated.
    assert outcomes(result) == [
        ("a1", "failed"),
        ("a2", "binding_unresolved"),
        ("a3", "pass"),
        ("a4", "pass"),
    ]
    assert result.assertions[1].misses == ("no match", "no match")
    assert result.outcome == "failed"


def test_a_search_that_runs_out_of_time_is_check_timed_out_and_the_run_cannot_pass(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than the 2 s rule, which test_text_search measures.
    monkeypatch.setattr(text_search, "SEARCH_SECONDS", 1)
    script = compiled(
        [],
        targets={"line": {"semantic": "the line of a's", "locators": [{"css": "p"}]}},
        assertions=[
            # Each backtracks for exponential time on forty a's and a b.
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_visible",
                "pattern": "(a+)+$",
            },
            {
                "id": "a2",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "line",
                "pattern": "(a+)+$",
            },
            {
                "id": "a3",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": "catastrophic",
            },
        ],
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/catastrophic")

    result = run(app, tmp_path, script, spec=spec).result

    # Neither a pass nor a failure, and the next check is still evaluated.
    assert outcomes(result) == [
        ("a1", "check_timed_out"),
        ("a2", "check_timed_out"),
        ("a3", "pass"),
    ]
    assert result.outcome == "failed"


def test_url_matches_fails_on_a_url_the_pattern_isnt_found_in(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [],
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": "/checkout",
            },
            {
                "id": "a2",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": r"/page/form\Z",
            },
        ],
    )

    result = run(app, tmp_path, script).result

    assert outcomes(result) == [("a1", "failed"), ("a2", "pass")]
    assert result.outcome == "failed"


def test_not_visible_fails_at_once_on_an_error_that_shows_and_later_fades(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [{"seq": 1, "action": "click", "target": "save", "side_effect": False}],
        targets=SHOP_TARGETS,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "not_visible", "target": "error"}
        ],
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/toast")

    result = run(app, tmp_path, script, spec=spec).result

    # Evaluated once, while the error shows: waiting for it to fade would
    # pass a bug the page did show (ADR-0024's #46 amendment).
    assert outcomes(result) == [("a1", "failed")]


@pytest.mark.parametrize(
    ("resolve_seconds", "outcome", "misses"),
    [(10, "pass", ()), (1, "binding_unresolved", ("no scope",))],
)
def test_not_visible_waits_out_a_scope_that_isnt_there_yet(
    app: App,
    tmp_path: Path,
    resolve_seconds: int,
    outcome: str,
    misses: tuple[str, ...],
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = BrowserSession.resolve
    first_lookup_at: float | None = None
    lookup_results: list[Resolved | Absent | Unresolved] = []

    async def resolving(
        session: BrowserSession, target: Target, use: Use
    ) -> Resolved | Absent | Unresolved:
        nonlocal first_lookup_at
        if first_lookup_at is None:
            first_lookup_at = time.monotonic()
        elif time.monotonic() - first_lookup_at >= 1.5:
            await session.page.evaluate("dispatchEvent(new Event('show-status-area'))")
        found = await resolve(session, target, use)
        lookup_results.append(found)
        return found

    monkeypatch.setattr(BrowserSession, "resolve", resolving)
    script = compiled(
        [],
        targets=SHOP_TARGETS,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "not_visible", "target": "error"}
        ],
    )
    config = ProjectConfig.model_validate(
        {"budgets": {"resolve_seconds": resolve_seconds}}
    )
    spec = a_spec(tmp_path, config, start_url="/page/late-area")

    started = time.monotonic()

    result = run(app, tmp_path, script, config=config, spec=spec).result

    assert lookup_results[:1] == [Unresolved(("no scope",))]
    assert outcomes(result) == [("a1", outcome)]
    assert result.assertions[0].misses == misses
    # Absent is a result at the look that finds it, not after the budget.
    assert time.monotonic() - started < 8


def test_an_assertion_that_meets_a_page_off_the_allowed_origins_ends_the_evaluation(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [],
        targets={
            "never": {
                "semantic": "text that never comes",
                "locators": [{"css": "#never"}],
            }
        },
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "never",
                "text": "never",
            },
            {"id": "a2", "expect_index": 0, "check": "url_matches", "pattern": "/"},
        ],
    )
    # The page leaves for a host the run doesn't allow 1.5 s after it loads,
    # while the first assertion's target is still looked for.
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/leaves")

    result = run(app, tmp_path, script, spec=spec).result

    assert outcomes(result) == [("a1", "not_evaluated"), ("a2", "not_evaluated")]
    assert (result.assertions[0].error or "").startswith("the page is on no origin")
    assert (
        result.assertions[1].error == "not evaluated after a1's look at the page raised"
    )
    assert result.policy_events == (
        PolicyEvent("document", "chrome-error://chromewebdata/", None),
    )
    assert result.outcome == "errored"
    assert result.error_code == "egress_blocked"


@pytest.mark.parametrize(("changes", "outcome"), [(1, "pass"), (1000, "not_evaluated")])
def test_page_text_is_read_again_while_the_page_changes_under_the_read(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: int,
    outcome: str,
) -> None:
    visible_text = BrowserSession.visible_text
    reads: list[None] = []

    async def changing(session: BrowserSession) -> str:
        reads.append(None)
        if len(reads) <= changes:
            raise DocumentChangedError
        return await visible_text(session)

    monkeypatch.setattr(BrowserSession, "visible_text", changing)
    script = compiled(
        [],
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "text_visible", "text": "Name"}
        ],
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    result = run(app, tmp_path, script, config=config).result

    # Once it changed under the read, read again; while it keeps changing
    # past resolve_seconds, nothing about the check can be said.
    assert outcomes(result) == [("a1", outcome)]
    if outcome == "not_evaluated":
        assert result.assertions[0].error == str(DocumentChangedError())
    assert result.outcome == ("passed" if outcome == "pass" else "errored")


def test_a_look_that_raises_leaves_every_later_assertion_unlooked_at(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    looks: list[str] = []

    async def crashed(_: BrowserSession) -> str:
        looks.append("url")
        raise Error("Page.evaluate: Target crashed")

    async def text(_: BrowserSession) -> str:
        looks.append("text")
        return "Name"

    monkeypatch.setattr(BrowserSession, "url", crashed)
    monkeypatch.setattr(BrowserSession, "visible_text", text)
    script = compiled(
        [],
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"},
            {"id": "a2", "expect_index": 0, "check": "text_visible", "text": "Name"},
            {"id": "a3", "expect_index": 0, "check": "url_matches", "pattern": "/"},
        ],
    )

    result = run(app, tmp_path, script).result

    assert outcomes(result) == [
        ("a1", "not_evaluated"),
        ("a2", "not_evaluated"),
        ("a3", "not_evaluated"),
    ]
    assert result.assertions[0].error == "Error: Page.evaluate: Target crashed"
    assert [assertion.error for assertion in result.assertions[1:]] == [
        "not evaluated after a1's look at the page raised"
    ] * 2
    # The look that shows the page answers, then a1's, and nothing after it.
    assert looks == ["text", "url"]
    assert result.outcome == "errored"


@pytest.mark.parametrize("read", ["visible_text", "text_of"])
def test_a_read_the_page_never_answers_ends_with_the_budget(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    async def never(*_: object) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(BrowserSession, read, never)
    script = compiled(
        [],
        targets=SHOP_TARGETS,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "text_visible", "text": "Name"}
            if read == "visible_text"
            else {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "status",
                "text": "saved",
            },
        ],
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    spec = a_spec(tmp_path, config, start_url="/page/shop")
    started = time.monotonic()

    result = run(app, tmp_path, script, config=config, spec=spec).result

    assert outcomes(result) == [("a1", "not_evaluated")]
    assert result.assertions[0].error == "the page didn't answer within 2 s"
    assert result.outcome == "errored"
    assert time.monotonic() - started < 20


def test_text_in_target_reads_only_its_targets_text(app: App, tmp_path: Path) -> None:
    script = compiled(
        [],
        targets={
            "save": {
                "semantic": "the save button",
                "locators": [{"css": "button.save"}],
            }
        },
        assertions=[
            # "Size" is on the page, in a label, not in the button.
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "save",
                "text": "Size",
            },
            {
                "id": "a2",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "save",
                "text": "Save",
            },
        ],
    )

    result = run(app, tmp_path, script).result

    assert outcomes(result) == [("a1", "failed"), ("a2", "pass")]


def test_not_visible_passes_on_an_error_the_page_never_shows(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [],
        targets=SHOP_TARGETS,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "not_visible", "target": "error"}
        ],
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/hidden-error")

    result = run(app, tmp_path, script, spec=spec).result

    # Only what is on screen counts: the hidden error is absent.
    assert outcomes(result) == [("a1", "pass")]


def test_a_page_that_hit_an_egress_block_never_passes(app: App, tmp_path: Path) -> None:
    script = compiled(
        [],
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"}
        ],
    )
    # The start page fetches from a host the run doesn't declare.
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url=f"/page/fetches?to={quote('http://undeclared.example.test/x', safe='')}",
    )

    result = run(app, tmp_path, script, spec=spec).result

    assert outcomes(result) == [("a1", "not_evaluated")]
    assert result.outcome == "errored"
    assert result.error_code == "egress_blocked"
    assert result.egress_blocks.refused == (RefusedHost("undeclared.example.test", 80),)


def test_a_page_that_stops_answering_is_never_passed_on_its_url(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than settling's 10 s: the busy page settles only as a timeout.
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)
    script = compiled(
        [],
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "busy"}
        ],
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    spec = a_spec(tmp_path, config, start_url="/page/busy")

    result = run(app, tmp_path, script, config=config, spec=spec).result

    # Its URL still reads busy, but a page that doesn't answer is unobserved.
    assert outcomes(result) == [("a1", "not_evaluated")]
    assert result.assertions[0].error == "the page didn't answer within 2 s"
    assert result.outcome == "errored"


def test_a_page_whose_renderer_crashed_unreported_is_never_passed_on_its_url(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CI's Ubuntu 24.04 host on any host: the session never hears of the crash.
    listen: Callable[..., object] = Page.on

    def deaf_to_crashes(
        page: Page, event: str, handler: Callable[[Page], object]
    ) -> None:
        if event != "crash":
            listen(page, event, handler)

    settle = BrowserSession.settle

    async def then_crash(session: BrowserSession, window: Window) -> Any:
        settled = await settle(session, window)
        # Chromium's page that crashes the renderer, which never loads.
        with contextlib.suppress(Error):
            await session.page.goto("chrome://crash")
        return settled

    monkeypatch.setattr(Page, "on", deaf_to_crashes)
    monkeypatch.setattr(BrowserSession, "settle", then_crash)
    script = compiled(
        [],
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"}
        ],
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    result = run(app, tmp_path, script, config=config).result

    assert outcomes(result) == [("a1", "not_evaluated")]
    assert result.outcome == "errored"


def test_text_in_target_fails_on_text_the_page_never_shows_as_text_visible_does(
    app: App, tmp_path: Path
) -> None:
    targets = {
        name: {"semantic": f"the {name} status", "locators": [{"css": f"#{name}"}]}
        for name in ("confirmed", "inner", "pending")
    }
    script = compiled(
        [],
        targets=targets,
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "confirmed",
                "text": "Order confirmed",
            },
            {
                "id": "a2",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "inner",
                "text": "Order confirmed",
            },
            {
                "id": "a3",
                "expect_index": 0,
                "check": "text_visible",
                "text": "Order confirmed",
            },
            {
                "id": "a4",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "pending",
                "text": "Order pending",
            },
        ],
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/pre-rendered")

    result = run(app, tmp_path, script, spec=spec).result

    assert outcomes(result) == [
        ("a1", "failed"),
        ("a2", "failed"),
        ("a3", "failed"),
        ("a4", "pass"),
    ]


def test_a_target_lookup_the_page_never_answers_is_a_look_that_raised(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(*_: object) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(BrowserSession, "resolve", never)
    script = compiled(
        [],
        targets=SHOP_TARGETS,
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "status",
                "text": "saved",
            },
            {"id": "a2", "expect_index": 0, "check": "url_matches", "pattern": "/"},
        ],
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    spec = a_spec(tmp_path, config, start_url="/page/shop")

    result = run(app, tmp_path, script, config=config, spec=spec).result

    # Not drift: no look finished, so the page didn't answer.
    assert outcomes(result) == [("a1", "not_evaluated"), ("a2", "not_evaluated")]
    assert result.assertions[0].error == "the page didn't answer within 1 s"
    assert result.outcome == "errored"
