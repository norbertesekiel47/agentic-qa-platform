"""`aqa replay` of a `contrast_min` check on the contrast fixture pages
(#254; DATA_MODEL §7, Contrast; ADR-0025's #254 amendment): the notice's
text, against the background its block gives it, meets 4.5 to 1 or falls
short, and a gradient behind it is refused. Strict replay works it out from
styles and builds no model client. Real Chromium against the executor's
fixture app, serving one page at `/page/order`."""

import re
from collections.abc import Callable
from pathlib import Path

import anthropic
import pytest
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.model_router import ModelRouter
from langchain_anthropic import ChatAnthropic

from packages.cli.tests.test_explore_runs import record_in
from packages.cli.tests.test_replay import SECONDS, committed, on, replay, viewer
from packages.cli.tests.test_replay_drift_pages import judged
from packages.runner.tests.drift_fixtures import order_spec, serving
from packages.runner.tests.executor_fixtures import compiled

CONTRAST = Path(__file__).resolve().parents[3] / "tests/fixtures/pages/contrast"
LEGIBLE = "The shipping notice is legible"
# The notice's contrast, as explore would have compiled it.
NOTICE = compiled(
    [],
    targets={
        "notice": {
            "semantic": "the order's shipping notice",
            "locators": [{"css": "#notice"}],
        }
    },
    assertions=[
        {
            "id": "a1",
            "expect_index": 0,
            "check": "contrast_min",
            "target": "notice",
            "min_ratio": 4.5,
        }
    ],
)
INVARIANTS = [
    ("console_errors", "pass", None, None),
    ("js_exceptions", "pass", None, None),
    ("http_5xx", "pass", None, None),
    ("broken_images", "pass", None, None),
]
GRADIENT = (
    "contrast_min does not support a background image, gradient, generated "
    "background or clipped background behind its text"
)


def page(name: str) -> str:
    """The contrast page `name`."""
    return (CONTRAST / f"{name}.html").read_text()


def test_a_notice_that_meets_its_ratio_passes_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(order_spec(root, LEGIBLE), NOTICE)

    with serving(page("meets")) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 0, result.output
    assert re.fullmatch(
        rf"passed order: {SECONDS}, \$0\n{re.escape(viewer(root))}", result.stdout
    )
    assert judged(root) == [
        (0, "pass", None, None),
        ("a1", "pass", None, None),
        *INVARIANTS,
    ]


def test_a_notice_that_falls_short_is_a_visual_violation_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(order_spec(root, LEGIBLE), NOTICE)

    with serving(page("falls-short")) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    record = re.escape(str(record_in(root)))
    assert re.fullmatch(
        rf"expectation_violated order: {SECONDS}, \$0, run record: {record}\n"
        rf"  a1: expectation_violated\n{re.escape(viewer(root))}",
        result.stderr,
    )
    assert judged(root) == [
        (0, "pass", None, None),
        (
            "a1",
            "expectation_violated",
            None,
            {"category": "visual", "title": LEGIBLE, "repro_steps": [0]},
        ),
        *INVARIANTS,
    ]


def test_a_gradient_behind_the_notice_is_refused_as_inconclusive_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(order_spec(root, LEGIBLE), NOTICE)

    with serving(page("refused")) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 3, result.output
    assert result.stdout == ""
    record = re.escape(str(record_in(root)))
    assert re.fullmatch(
        rf"inconclusive order: {SECONDS}, \$0, run record: {record}\n"
        rf"  a1: inconclusive, {re.escape(GRADIENT)}\n{re.escape(viewer(root))}",
        result.stderr,
    )
    assert judged(root) == [
        (0, "pass", None, None),
        ("a1", "inconclusive", GRADIENT, None),
        *INVARIANTS,
    ]


def test_strict_replay_of_a_contrast_min_check_builds_no_model_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
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
    spec = committed(order_spec(tmp_path / "qa", LEGIBLE), NOTICE)

    with serving(page("meets")) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 0, result.output
    assert built == []
