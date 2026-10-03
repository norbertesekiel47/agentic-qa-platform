"""The M1 strict executor's assertions (#46; DATA_MODEL §7, Replay outcomes;
ADR-0024's #46 amendment, "The executor"): after the last step, every
assertion is evaluated, `pass`, `failed`, `binding_unresolved` or
`check_timed_out`, and a run passes only when every one does. The scripts
are written by hand, and the browser tests launch real Chromium on the OS
that runs them: Linux in CI, macOS locally."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner import text_search
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import DocumentChangedError, PolicyEvent
from aqa_runner.executor import RunResult

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
        assertions=[
            # Backtracks for exponential time on forty a's and a b.
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_visible",
                "pattern": "(a+)+$",
            },
            {
                "id": "a2",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": "catastrophic",
            },
        ],
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/catastrophic")

    result = run(app, tmp_path, script, spec=spec).result

    # Neither a pass nor a failure, and the next check is still evaluated.
    assert outcomes(result) == [("a1", "check_timed_out"), ("a2", "pass")]
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
) -> None:
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
    # The status area appears 1.5 s after the page loads, with no error in it.
    spec = a_spec(tmp_path, config, start_url="/page/late-area")

    result = run(app, tmp_path, script, config=config, spec=spec).result

    assert outcomes(result) == [("a1", outcome)]
    assert result.assertions[0].misses == misses


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
    assert result.assertions[1].error is None
    assert result.policy_events == (
        PolicyEvent("document", "chrome-error://chromewebdata/", None),
    )
    assert result.outcome == "errored"


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
    assert result.outcome == ("passed" if outcome == "pass" else "errored")
