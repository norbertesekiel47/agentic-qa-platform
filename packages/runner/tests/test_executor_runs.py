"""The M1 strict executor's runs (#46; ADR-0024's Consequences and its #46
amendment): each intent on disk before its action and its completion after,
a crash between them, the requests each step owns, how settling ended, no
model involved, and the script's own browser settings. The browser tests
launch real Chromium on the OS that runs them: Linux in CI, macOS locally."""

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anthropic
import pytest
from aqa_core.config import ProjectConfig
from aqa_runner import settling
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.model_router import ModelRouter
from aqa_runner.run_record import RunRecord
from langchain_anthropic import ChatAnthropic

from packages.runner.tests.executor_fixtures import (
    FORM_STEPS,
    FORM_TARGETS,
    App,
    a_spec,
    by_role,
    compiled,
    paths,
    read_steps,
    run,
    serving_app,
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def test_each_step_is_completed_after_its_action_with_the_locator_used(
    app: App, tmp_path: Path
) -> None:
    done = run(app, tmp_path, compiled(FORM_STEPS, targets=FORM_TARGETS))

    lines = read_steps(done.record.path)
    assert [(line["seq"], line["state"]) for line in lines] == [
        (seq, state) for seq in range(7) for state in ("intent", "completed")
    ]
    intents = [line for line in lines if line["state"] == "intent"]
    assert intents[0] == {
        "seq": 0,
        "state": "intent",
        "action": {"action": "navigate", "url": "/page/form"},
        "side_effect": False,
        "target_used": None,
    }
    assert intents[4] == {
        "seq": 4,
        "state": "intent",
        "action": {"action": "click", "target": "save"},
        "side_effect": True,
        "target_used": "save",
    }
    completions = [line for line in lines if line["state"] == "completed"]
    assert [line["locator_used"] for line in completions] == [
        None,
        0,
        0,
        None,
        1,
        None,
        None,
    ]
    assert {line["settled"] for line in completions} == {"idle"}
    assert done.result.run_id == done.record.run_id


def test_each_intent_is_on_disk_before_its_action_reaches_the_app(
    app: App, tmp_path: Path
) -> None:
    run(app, tmp_path, compiled(FORM_STEPS, targets=FORM_TARGETS))

    # The record as the app found it when each step's request arrived: the
    # navigation's while `navigate` was still waiting for the page, and the
    # click's write.
    found = {(method, path): lines for method, path, lines in app.records}
    assert found["GET", "/page/start"][-1] == {
        "seq": 6,
        "state": "intent",
        "action": {"action": "navigate", "url": "/page/start"},
        "side_effect": False,
        "target_used": None,
    }
    assert found["POST", "/write/save"][-1] == {
        "seq": 4,
        "state": "intent",
        "action": {"action": "click", "target": "save"},
        "side_effect": True,
        "target_used": "save",
    }


def test_a_crash_between_intent_and_completion_leaves_the_intent_unresolved(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "pay": {"semantic": "the pay button", "locators": [by_role("button", "Pay")]}
    }
    script = compiled(
        [
            {
                "seq": 1,
                "action": "click",
                "target": "pay",
                "side_effect": True,
                "side_effect_basis": "network: POST /held/pay",
            }
        ],
        targets=targets,
    )
    config = ProjectConfig()
    spec = a_spec(tmp_path, config, start_url="/page/pay")
    record = RunRecord.create(tmp_path)
    arguments = tmp_path / "arguments.json"
    arguments.write_text(
        json.dumps(
            {
                "config": config.model_dump(mode="json"),
                "script": script.model_dump_json(),
                "spec": str(spec.path),
                "origin": app.origin,
                "run_id": record.run_id,
                "record": str(record.path),
            }
        )
    )
    root = Path(__file__).resolve().parents[3]
    with subprocess.Popen(
        [sys.executable, "-m", "packages.runner.tests.executor_crash", str(arguments)],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as child:
        # The payment reached the app, which holds it: the run is settling
        # the click, between its intent and its completion.
        with app.arrived:
            paid = app.arrived.wait_for(lambda: ("POST", "/held/pay") in app.seen, 60)
        # The runner's process alone dies, as in a crash. Playwright's driver,
        # its child, sees its input close and closes the browser.
        child.kill()
        output, _ = child.communicate()
    assert paid, output.decode(errors="replace")

    assert [(line["seq"], line["state"]) for line in read_steps(record.path)] == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
    ]


def test_a_write_after_the_dom_settles_belongs_to_the_step_before_the_next_action(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "save": {
            "semantic": "the save button",
            "locators": [by_role("button", "Save")],
        },
        "next": {
            "semantic": "the next button",
            "locators": [by_role("button", "Next")],
        },
    }
    script = compiled(
        [
            {
                "seq": 1,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/late",
            },
            {"seq": 2, "action": "click", "target": "next", "side_effect": False},
        ],
        targets=targets,
    )

    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/late")
    result = run(app, tmp_path, script, spec=spec).result

    save, following = result.steps[1], result.steps[2]
    # Settling said idle before the write, and the next button appeared
    # later still, within resolve_seconds: the write is the click's.
    assert (save.outcome, save.settled) == ("completed", "idle")
    assert paths(save.window) == [("POST", "/write/late")]
    assert following.outcome == "completed"
    assert paths(following.window) == [("GET", "/did/next")]


def test_a_step_whose_requests_never_finish_is_recorded_as_a_timeout(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than settling's 10 s, which test_settling measures.
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)
    targets = {
        "wait": {"semantic": "the wait button", "locators": [by_role("button", "Wait")]}
    }
    script = compiled(
        [{"seq": 1, "action": "click", "target": "wait", "side_effect": False}],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/wait")

    done = run(app, tmp_path, script, spec=spec)

    step = done.result.steps[1]
    assert (step.outcome, step.settled) == ("completed", "timeout")
    assert read_steps(done.record.path)[-1] == {
        "seq": 1,
        "state": "completed",
        "locator_used": 0,
        "settled": "timeout",
    }


def test_a_run_constructs_no_model_client(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    result = run(app, tmp_path, compiled(FORM_STEPS, targets=FORM_TARGETS)).result

    assert [step.outcome for step in result.steps] == ["completed"] * 7
    assert built == []


def test_the_run_uses_the_scripts_browser_settings_not_the_project_or_spec_overrides(
    app: App, tmp_path: Path
) -> None:
    recorded = {
        "timezone": "Asia/Tokyo",
        "locale": "fr-FR",
        "viewport": [900, 700],
        "device_scale_factor": 2,
        "color_scheme": "dark",
    }
    config = ProjectConfig.model_validate(
        {
            "browser": {
                "timezone": "America/New_York",
                "locale": "de-DE",
                "viewport": [1000, 800],
                "device_scale_factor": 1,
                "color_scheme": "light",
            }
        }
    )
    spec = a_spec(
        tmp_path,
        config,
        start_url="/page/env",
        extra=(
            "browser: { timezone: Europe/Paris, locale: es-ES, viewport: [1100, 900],"
            " device_scale_factor: 3, color_scheme: light }\n"
        ),
    )

    run(app, tmp_path, compiled([], browser=recorded), config=config, spec=spec)

    [env] = [path for path in app.paths() if path.startswith("/did/env?")]
    assert parse_qs(urlsplit(env).query) == {
        "timezone": ["Asia/Tokyo"],
        "locale": ["fr-FR"],
        "viewport": ["900x700"],
        "scale": ["2"],
        "scheme": ["dark"],
    }
