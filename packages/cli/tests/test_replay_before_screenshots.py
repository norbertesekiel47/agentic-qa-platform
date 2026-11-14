"""`--before-screenshots` (#314; ADR-0033's #314 amendment): `aqa replay`,
`aqa run` and `aqa heal <spec>` take it, off by default. With it, a step that
fails saves the page as it found it, `evidence/<seq>/before_screenshot.png`,
beside its `screenshot.png`, and its verdict cites it in
`evidence.before_screenshot`; a withheld capture says why there and changes no
verdict, reason, outcome or exit code. Without it, nothing is captured ahead of
a step. Real Chromium against the executor's fixture app and #240's drift
pages."""

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import langsmith
import pytest
import typer.testing
from aqa_cli import common
from aqa_cli import run as run_module
from aqa_cli.main import app as aqa
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.model_router import ModelRouter
from aqa_runner.strict_replay import strict_replay
from langchain_anthropic import ChatAnthropic
from playwright.async_api import CDPSession, Error

from packages.cli.tests.test_explore_runs import record_in, shop
from packages.cli.tests.test_replay import committed, document, on, replay
from packages.cli.tests.test_replay_screenshots import pngs, unscreenshotted
from packages.cli.tests.test_run import ORDER, order_at
from packages.runner.tests.drift_fixtures import serving
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    App,
    compiled,
    serving_app,
)
from packages.runner.tests.explore_graph_fixtures import Scripted

# A fill on a page whose own execCommand throws, so its dispatch fails.
FILL = {
    "seq": 1,
    "action": "fill",
    "target": "name",
    "value": "Ada",
    "side_effect": False,
}
FAILS = compiled([FILL], targets={"name": FORM_TARGETS["name"]})
FLAG = "--before-screenshots"


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def refusing_model_clients(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make building any model client fail, and note each one built in the
    list given."""
    built: list[str] = []

    class ModelClientBuiltError(Exception):
        pass

    def refuse(name: str) -> Callable[..., None]:
        def constructor(*_: object, **__: object) -> None:
            built.append(name)
            raise ModelClientBuiltError(name)

        return constructor

    clients: list[type] = [
        ModelRouter,
        AnthropicClient,
        ChatAnthropic,
        anthropic.Anthropic,
        anthropic.AsyncAnthropic,
    ]
    for client in clients:
        monkeypatch.setattr(client, "__init__", refuse(client.__name__))
    # The control: building any of them now is caught.
    for client in clients:
        with pytest.raises(ModelClientBuiltError):
            client()
    built.clear()
    return built


def test_a_failed_step_keeps_its_pair_and_builds_no_model_client(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    built = refusing_model_clients(monkeypatch)
    unflagged, flagged = tmp_path / "unflagged" / "qa", tmp_path / "flagged" / "qa"
    without = replay(
        monkeypatch, on(app, committed(shop(unflagged, page="/page/throws"), FAILS))
    )
    spec = committed(shop(flagged, page="/page/throws"), FAILS)

    result = replay(monkeypatch, [*on(app, spec), FLAG])

    assert (without.exit_code, result.exit_code) == (3, 3), result.output
    assert built == []
    assert (pngs(record_in(unflagged)), pngs(record_in(flagged))) == (
        ["evidence/1/screenshot.png"],
        ["evidence/1/before_screenshot.png", "evidence/1/screenshot.png"],
    )
    assert [
        (verdict.get("step"), verdict["evidence"]["before_screenshot"])
        for verdict in document(flagged)["verdicts"]
    ] == [
        (0, None),
        (1, {"path": "evidence/1/before_screenshot.png", "withheld": None}),
    ]
    # Without the flag, every verdict says it kept none.
    assert {
        verdict["evidence"]["before_screenshot"] is None
        for verdict in document(unflagged)["verdicts"]
    } == {True}
    assert unscreenshotted(document(flagged)) == unscreenshotted(document(unflagged))


def test_a_withheld_before_screenshot_says_why_and_changes_nothing_else(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = tmp_path / "taken" / "qa"
    replayed = replay(
        monkeypatch,
        [*on(app, committed(shop(taken, page="/page/throws"), FAILS)), FLAG],
    )
    send = CDPSession.send

    async def refusing(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureScreenshot":
            raise Error("Protocol error (Page.captureScreenshot): refused")
        return await send(devtools, method, params)

    monkeypatch.setattr(CDPSession, "send", refusing)
    refused = tmp_path / "refused" / "qa"
    spec = committed(shop(refused, page="/page/throws"), FAILS)

    withheld = replay(monkeypatch, [*on(app, spec), FLAG])

    assert (replayed.exit_code, withheld.exit_code) == (3, 3), withheld.output
    assert (pngs(record_in(taken)), pngs(record_in(refused))) == (
        ["evidence/1/before_screenshot.png", "evidence/1/screenshot.png"],
        [],
    )
    [_, step] = document(refused)["verdicts"]
    assert step["evidence"]["before_screenshot"] == {
        "path": None,
        "withheld": "a DevTools call or the render failed",
    }
    assert unscreenshotted(document(refused)) == unscreenshotted(document(taken))


def test_aqa_report_writes_the_viewer_of_a_run_that_kept_a_before_screenshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The report reads `evidence.path` and `evidence.screenshot` (#313), so the
    # new key and file leave it as it was.
    root = tmp_path / "qa"
    spec = committed(shop(root, page="/page/throws"), FAILS)
    replayed = replay(monkeypatch, [*on(app, spec), FLAG])
    record = record_in(root)
    monkeypatch.chdir(root)

    result = typer.testing.CliRunner().invoke(aqa, ["report", record.name])

    assert replayed.exit_code == 3, replayed.output
    assert (record / "evidence" / "1" / "before_screenshot.png").is_file()
    assert result.exit_code == 0, result.output
    assert result.stdout == f"report {record.name}: {record / 'report.html'}\n"


@pytest.mark.parametrize(
    ("command", "flagged"),
    [
        pytest.param("run", [FLAG], id="run"),
        pytest.param("heal", [FLAG], id="heal"),
        pytest.param("run", [], id="run-without"),
    ],
)
def test_run_and_heal_pass_the_flag_to_strict_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, flagged: list[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-aqa-before-screenshots")
    monkeypatch.setattr(run_module, "AnthropicClient", Scripted())
    asked: list[object] = []

    def spying(*args: Any, **kwargs: Any) -> Any:
        asked.append(kwargs.get("before_screenshots"))
        return strict_replay(*args, **kwargs)

    monkeypatch.setattr(common, "strict_replay", spying)
    spec = order_at(tmp_path / "qa")

    with serving(ORDER) as served:
        result = typer.testing.CliRunner().invoke(
            aqa, [command, str(spec), "--url", served.origin, *flagged]
        )
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)

    assert result.exit_code == 0, result.output
    assert asked == [bool(flagged)]


@pytest.mark.parametrize("command", ["replay", "run", "heal"])
def test_each_command_that_replays_strictly_lists_the_flag(command: str) -> None:
    result = typer.testing.CliRunner().invoke(aqa, [command, "--help"])

    assert result.exit_code == 0, result.output
    # Under GITHUB_ACTIONS Typer forces colour, which splits an option from its
    # dashes.
    assert FLAG in re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
