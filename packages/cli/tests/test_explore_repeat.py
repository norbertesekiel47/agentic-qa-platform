"""`aqa explore --confirm-repeat` (API.md §7; #56): with no reset hook, the
person running explore lets one confirmation repeat the path's side effects,
so the script is confirmed and the save runs twice, once per phase, with no
warning. Real Chromium against the executor's fixture app with a scripted
model."""

import re
from collections.abc import Iterator
from pathlib import Path

import langsmith
import pytest
from aqa_core.config import ProjectConfig
from aqa_core.project import load_compiled

from packages.cli.tests.test_explore_runs import KEY, PATH, SECONDS, explore, on, shop
from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.explore_graph_fixtures import Scripted, planned


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)


def test_confirm_repeat_confirms_a_spec_with_no_reset_hook_repeating_its_save_once(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    args = on(app, shop(root), "--confirm-repeat")

    result = explore(monkeypatch, args, Scripted(planned(), *PATH))

    assert result.exit_code == 0, result.output
    target = root / ".compiled" / "shop.json"
    assert re.fullmatch(
        rf"compiled shop: {SECONDS}, \$0\.015, {target}\n", result.stdout
    )
    assert result.stderr == ""
    assert load_compiled(target, ProjectConfig()).confirmed is True
    assert app.paths() == ["/page/shop", "/did/fill", "/write/save"] * 2
