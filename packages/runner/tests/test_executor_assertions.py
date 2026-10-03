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
from aqa_runner.executor import RunResult

from packages.runner.tests.executor_fixtures import (
    App,
    a_spec,
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
