"""The M1 strict executor's runs (#46; ADR-0024's Consequences and its #46
amendment): each intent on disk before its action and its completion after,
a crash between them, the requests each step owns, how settling ended, no
model involved, and the script's own browser settings. The browser tests
launch real Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import errno
import json
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import anthropic
import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig, SubjectContract
from aqa_core.project import SpecError, contracts_fingerprint, parse_compiled
from aqa_runner import settling
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.egress import EgressGate, InfrastructureEvent
from aqa_runner.egress_proxy import EgressBlocks, EgressProxy, PhaseTraffic, RefusedHost
from aqa_runner.executor import RunResult, RunSetup, replay
from aqa_runner.invariants import InvariantResult
from aqa_runner.model_router import ModelRouter
from aqa_runner.run_record import RunRecord
from aqa_runner.sandbox import Chromium, launch
from langchain_anthropic import ChatAnthropic
from playwright.async_api import Browser, BrowserType, async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import gate, get, unused_port
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
    # Each step's side_effect flag as compiled, the press's included.
    assert [line["side_effect"] for line in intents] == [
        False,
        False,
        False,
        True,
        True,
        False,
        False,
    ]
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


def test_a_disabled_invariant_doesnt_keep_a_run_from_passing(
    app: App, tmp_path: Path
) -> None:
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url="/page/console-error",
        extra="invariants: {disable: [console_errors]}\n",
    )

    result = run(app, tmp_path, compiled([]), spec=spec).result

    assert result.outcome == "passed"
    assert result.error_code is None
    assert [assertion.outcome for assertion in result.assertions] == ["pass"]
    assert result.invariants == (
        InvariantResult("console_errors", "disabled", ("console-trigger",), 1),
        InvariantResult("js_exceptions", "held", (), 0),
        InvariantResult("http_5xx", "held", (), 0),
        InvariantResult("broken_images", "held", (), 0),
    )


def test_an_invariant_violation_fails_a_run_whose_assertions_pass(
    app: App, tmp_path: Path
) -> None:
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/console-error")

    result = run(app, tmp_path, compiled([]), spec=spec).result

    assert [assertion.outcome for assertion in result.assertions] == ["pass"]
    assert result.invariants == (
        InvariantResult("console_errors", "violated", ("console-trigger",), 1),
        InvariantResult("js_exceptions", "held", (), 0),
        InvariantResult("http_5xx", "held", (), 0),
        InvariantResult("broken_images", "held", (), 0),
    )
    assert result.outcome == "failed"
    assert result.error_code is None


def test_an_expected_blocked_request_doesnt_end_the_run(
    app: App, tmp_path: Path
) -> None:
    config = ProjectConfig.model_validate(
        {"egress": {"expected_blocked": ["analytics.example.test"]}}
    )
    spec = a_spec(tmp_path, config, start_url="/page/expected-blocked")
    script = compiled([{"seq": 1, "action": "reload", "side_effect": False}])

    done = run(app, tmp_path, script, config=config, spec=spec)

    assert done.result.outcome == "passed"
    assert [(step.seq, step.outcome) for step in done.result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]
    assert [assertion.outcome for assertion in done.result.assertions] == ["pass"]
    assert done.result.error_code is None
    assert done.result.egress_blocks == EgressBlocks((), False)
    assert done.result.invariants == (
        InvariantResult("console_errors", "held", (), 0),
        InvariantResult("js_exceptions", "held", (), 0),
        InvariantResult("http_5xx", "held", (), 0),
        InvariantResult("broken_images", "held", (), 0),
    )
    assert not (done.record.path / "egress.json").exists()


SUBJECT_CONFIG = ProjectConfig(
    subjects=(
        SubjectContract(spec="replay", expect=0, region="div.banner", part="a.author"),
    )
)


def stamped(script: CompiledScript, config: ProjectConfig) -> CompiledScript:
    data = script.model_dump(mode="json")
    data["compiled_by"]["subject_contracts"] = contracts_fingerprint(
        config, script.spec_id
    )
    return parse_compiled(json.dumps(data), config, source=Path("fixture.json"))


def refused_before_browser(
    tmp_path: Path, script: CompiledScript, config: ProjectConfig
) -> tuple[str, ...]:
    chromium = MagicMock(spec=BrowserType)
    chromium.launch = AsyncMock(
        side_effect=AssertionError("browser launched before contract agreement")
    )
    proxy = MagicMock(spec=EgressProxy)
    proxy.url = "http://127.0.0.1:4100"
    record = RunRecord.create(tmp_path)
    spec = a_spec(tmp_path, config, start_url="/page/form")
    setup = RunSetup(spec, config, "http://127.0.0.1:4100", record)
    with pytest.raises(SpecError) as raised:
        asyncio.run(
            replay(
                script,
                setup,
                chromium=cast(BrowserType, chromium),
                proxy=cast(EgressProxy, proxy),
                gate=gate(allowed=(setup.start,)),
            )
        )
    chromium.launch.assert_not_awaited()
    assert read_steps(record.path) == []
    return raised.value.problems


@pytest.mark.parametrize(
    "assertion",
    [
        {
            "id": "a1",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "shown",
            "text": "saved",
        },
        {"id": "a1", "expect_index": 0, "check": "not_visible", "target": "shown"},
        {
            "id": "a1",
            "expect_index": 0,
            "check": "visible_unoccluded",
            "target": "shown",
            "min_size_px": [1, 1],
            "in_viewport": True,
        },
    ],
)
def test_replay_refuses_a_listed_expectations_target_without_its_contract_before_the_browser(
    tmp_path: Path, assertion: dict[str, object]
) -> None:
    script = compiled(
        [],
        targets={
            "shown": {
                "semantic": "the form",
                "locators": [
                    {**by_role("button", "Save"), "scope": {"css": "div.banner"}}
                ],
            }
        },
        assertions=[assertion],
    )
    problems = refused_before_browser(
        tmp_path, stamped(script, SUBJECT_CONFIG), SUBJECT_CONFIG
    )
    assert problems == ("targets.shown: lacks subject contract for replay expect 0",)


def test_replay_refuses_other_subject_contracts_before_unsupported_steps(
    tmp_path: Path,
) -> None:
    script = compiled(
        [{"seq": 1, "action": "press", "key": "a+b", "side_effect": False}]
    )
    data = script.model_dump(mode="json")
    data["compiled_by"]["subject_contracts"] = "sha256:" + "0" * 64
    script = parse_compiled(
        json.dumps(data), ProjectConfig(), source=Path("fixture.json")
    )
    problems = refused_before_browser(tmp_path, script, ProjectConfig())
    assert problems == ("compiled under other subject contracts: explore it again",)


def test_replay_refuses_a_target_sharing_a_listed_meaning_without_its_contract(
    tmp_path: Path,
) -> None:
    script = compiled(
        [],
        targets={
            "shown": {"semantic": "the form", "locators": [by_role("button", "Save")]},
            "decoy": {"semantic": "the form", "locators": [by_role("button", "Other")]},
            "unrelated": {
                "semantic": "another meaning",
                "locators": [by_role("button", "Else")],
            },
        },
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "shown",
                "text": "saved",
            }
        ],
    )
    problems = refused_before_browser(
        tmp_path, stamped(script, SUBJECT_CONFIG), SUBJECT_CONFIG
    )
    assert problems == (
        "targets.decoy: lacks subject contract for replay expect 0",
        "targets.shown: lacks subject contract for replay expect 0",
    )


def test_replay_refuses_a_listed_expectation_checked_without_a_target(
    tmp_path: Path,
) -> None:
    problems = refused_before_browser(
        tmp_path, stamped(compiled([]), SUBJECT_CONFIG), SUBJECT_CONFIG
    )
    assert problems == (
        "subjects replay expect 0: no target carries its subject contract",
    )


RETIRED = (
    "the egress proxy stopped accepting the browser's connections after an "
    "accept error (EMFILE)"
)


def port_of(proxy: EgressProxy) -> int:
    return int(urlsplit(proxy.url).port or 0)


def replay_with(
    app: App,
    tmp_path: Path,
    script: CompiledScript,
    prepare: Callable[[EgressProxy, EgressGate], None],
    *,
    also: tuple[str, ...] = (),
) -> tuple[RunResult, RunRecord, int]:
    """Replay `script` from the app's start page as `run` does, once
    `prepare` has had the run's proxy and gate, with the origins in `also`
    allowed too; with the proxy's first port."""
    config = ProjectConfig()
    run_gate = gate(allowed=(app.origin, *also), private=also)
    record = RunRecord.create(tmp_path)
    app.record = record.path
    setup = RunSetup(
        a_spec(tmp_path, config, start_url="/page/start"), config, app.origin, record
    )

    async def scenario() -> tuple[RunResult, int]:
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            port = port_of(proxy)
            prepare(proxy, run_gate)
            async with asyncio.timeout(60):
                result = await replay(
                    script,
                    setup,
                    chromium=playwright.chromium,
                    proxy=proxy,
                    gate=run_gate,
                )
            return result, port

    result, port = asyncio.run(scenario())
    return result, record, port


def test_emfile_on_accept_after_the_first_navigation_makes_the_replay_an_infrastructure_error(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accept = socket.socket.accept

    def prepare(proxy: EgressProxy, _gate: EgressGate) -> None:
        port = port_of(proxy)

        def accepting(listener: socket.socket) -> tuple[socket.socket, object]:
            # Once the app has served the start page, the proxy's listener
            # runs out of file descriptors.
            if listener.getsockname()[1] == port and "/page/start" in app.paths():
                raise OSError(errno.EMFILE, "Too many open files")
            return accept(listener)

        monkeypatch.setattr(socket.socket, "accept", accepting)

    script = compiled([{"seq": 1, "action": "reload", "side_effect": False}])

    result, _, port = replay_with(app, tmp_path, script, prepare)

    assert result.outcome == "errored"
    assert result.infrastructure_events == (
        InfrastructureEvent("127.0.0.1", port, RETIRED),
    )
    assert (result.steps[0].seq, result.steps[0].outcome) == (0, "completed")
    assert [assertion.outcome for assertion in result.assertions] == ["not_evaluated"]
    assert result.error_code is None


def while_closing(
    monkeypatch: pytest.MonkeyPatch, act: Callable[[], Awaitable[None]]
) -> None:
    """Have each browser the session launches run `act` as its close
    begins, before the browser goes."""

    async def launching(chromium: Chromium) -> Browser:
        browser = await launch(chromium)
        close = browser.close

        async def closing(reason: str | None = None) -> None:
            await act()
            await close(reason=reason)

        monkeypatch.setattr(browser, "close", closing)
        return browser

    monkeypatch.setattr("aqa_runner.browser_session.launch", launching)


def through(proxy: EgressProxy, request: bytes) -> None:
    """Send `request` to the proxy's open phase on a connection of its own
    that this leaves open, as a browser's late request would."""
    browser = socket.create_connection(("127.0.0.1", port_of(proxy)))
    browser.sendall(request)
    LATE.append(browser)


LATE: list[socket.socket] = []


@pytest.fixture(autouse=True)
def late_connections_closed() -> Iterator[None]:
    yield
    while LATE:
        LATE.pop().close()


def test_a_listener_retired_while_the_browser_closes_leaves_the_replay_errored(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accept = socket.socket.accept
    runs: list[tuple[EgressProxy, EgressGate]] = []
    closing: list[bool] = []

    def prepare(proxy: EgressProxy, run_gate: EgressGate) -> None:
        runs.append((proxy, run_gate))

    async def retire() -> None:
        proxy, run_gate = runs[0]
        port = port_of(proxy)

        def accepting(listener: socket.socket) -> tuple[socket.socket, object]:
            if closing and listener.getsockname()[1] == port:
                raise OSError(errno.EMFILE, "Too many open files")
            return accept(listener)

        closing.append(True)
        monkeypatch.setattr(socket.socket, "accept", accepting)
        through(proxy, b"")
        await until(lambda: bool(run_gate.infrastructure_events))

    while_closing(monkeypatch, retire)
    script = compiled([{"seq": 1, "action": "reload", "side_effect": False}])

    result, _, port = replay_with(app, tmp_path, script, prepare)

    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]
    assert [assertion.outcome for assertion in result.assertions] == ["pass"]
    assert all(invariant.outcome == "held" for invariant in result.invariants)
    assert result.outcome == "errored"
    assert result.infrastructure_events == (
        InfrastructureEvent("127.0.0.1", port, RETIRED),
    )


def test_an_egress_block_recorded_while_the_browser_closes_errs_the_replay_and_is_saved(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[tuple[EgressProxy, EgressGate]] = []

    def prepare(proxy: EgressProxy, run_gate: EgressGate) -> None:
        runs.append((proxy, run_gate))

    async def refused() -> None:
        proxy, run_gate = runs[0]
        through(proxy, get("http://evil.example.test/", close=True))
        await until(lambda: bool(run_gate.refusals))

    while_closing(monkeypatch, refused)
    script = compiled([{"seq": 1, "action": "reload", "side_effect": False}])

    result, record, _ = replay_with(app, tmp_path, script, prepare)

    assert [assertion.outcome for assertion in result.assertions] == ["pass"]
    assert result.outcome == "errored"
    assert result.error_code == "egress_blocked"
    assert result.egress_blocks == EgressBlocks(
        (RefusedHost("evil.example.test", 80),), False
    )
    assert json.loads((record.path / "egress.json").read_text()) == {
        "error_code": "egress_blocked",
        "refused": [{"host": "evil.example.test", "port": 80}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }


def test_an_infrastructure_event_recorded_while_the_replays_phase_ends_errs_the_replay(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = unused_port()

    def prepare(proxy: EgressProxy, run_gate: EgressGate) -> None:
        end_phase = proxy.end_phase

        async def ending() -> PhaseTraffic:
            # One of the phase's own connections fails upstream as it ends.
            through(proxy, get(f"http://127.0.0.1:{dead}/", close=True))
            await until(lambda: bool(run_gate.infrastructure_events))
            return await end_phase()

        monkeypatch.setattr(proxy, "end_phase", ending)

    script = compiled([{"seq": 1, "action": "reload", "side_effect": False}])

    result, _, _ = replay_with(
        app, tmp_path, script, prepare, also=(f"http://127.0.0.1:{dead}",)
    )

    assert [assertion.outcome for assertion in result.assertions] == ["pass"]
    assert result.outcome == "errored"
    assert [(event.host, event.port) for event in result.infrastructure_events] == [
        ("127.0.0.1", dead)
    ]
    assert result.error_code is None
