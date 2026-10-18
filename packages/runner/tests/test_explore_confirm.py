"""Confirming and publishing an explored path (#53 P16b; ADR-0024's #53 P16
amendment): once the attempt compiled a path that nothing stopped,
`explore.explore` drops every locator that spells a bound value, resets the
app when the spec has a hook, replays the path with every locator in a
fresh browser into the record's `confirmation` part with no model, and
publishes it under the spec's lock, confirmed when that replay passed. A
side-effect path whose spec has no reset hook is published unconfirmed,
never replayed. Real Chromium against the executor's fixture app, with a
scripted model."""

import asyncio
import errno
import json
import re
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import load_compiled, load_config
from aqa_core.spec import Reset
from aqa_runner import executor, explore, settling
from aqa_runner.chat_client import ChatClient
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.explore import Explored
from aqa_runner.loopback_server import LoopbackServer
from aqa_runner.model_router import ModelRouter
from aqa_runner.redaction import Redactor
from aqa_runner.reset import reset
from aqa_runner.run_record import RunRecord
from aqa_runner.sandbox import SandboxUnavailableError, launch
from langchain_core.messages import BaseMessage
from playwright.async_api import Browser, async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.executor_fixtures import (
    PAGES,
    SHOP,
    SHOPS,
    App,
    read_steps,
    serving_app,
)
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    STATUS,
    Move,
    Scripted,
    calling,
    ref,
    tool,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE
from packages.runner.tests.test_describe_parts import (
    COUNTED,
    COUNTER,
    PARTS_PAGES,
    favorite_in,
)
from packages.runner.tests.test_explore_graph import (
    ACCOUNT,
    ASSERT,
    CLEAN_PHASE,
    FINISH,
    PLAIN,
    SECRETS,
    UNWRITTEN,
    configured,
    deadlines,
    files,
    shop_spec,
    written,
)
from packages.runner.tests.test_explore_preflight import planned
from packages.runner.tests.test_navigator_checks import ARTICLE, BUTTON_ROW, FAVORITE

RESET = "  reset: { http: POST /write/reset }\n"
# The path the scripted attempt explores and compiles: fill, save, check.
PATH = (FILL, CLICK, ASSERT, FINISH)
# Each request one pass over the path makes of the app, after its reset.
PASS = ["/write/reset", "/page/shop", "/did/fill", "/write/save"]
CONFIRMATION_PHASE = CLEAN_PHASE | {"part": "confirmation"}
# A confirmation's phase whose replay raised: no policy-event total.
UNCOUNTED_PHASE = CONFIRMATION_PHASE | {"policy_events": None}


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)


def confirming(
    app: App,
    tmp_path: Path,
    client: Scripted,
    *,
    preconditions: str = RESET,
    config: ProjectConfig | None = None,
    page: str = "/page/shop",
) -> Explored:
    """How `explore` ended a run of the shop spec with `preconditions`
    under the spec root `tmp_path / "qa"`, whose `config.yaml` holds
    `config`, from the app's origin, with `client` as its model."""
    config = config or ProjectConfig()
    root = configured(tmp_path / "qa", config)
    spec = shop_spec(root, config, page, preconditions)

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


def compiled_file(tmp_path: Path) -> Path:
    return tmp_path / "qa" / ".compiled" / "shop.json"


def published(tmp_path: Path) -> CompiledScript:
    """The compiled script the run published, read as replay reads it."""
    config = load_config(tmp_path / "qa" / "config.yaml")
    return load_compiled(compiled_file(tmp_path), config)


def resets(
    monkeypatch: pytest.MonkeyPatch,
    second: Callable[[], object] = lambda: None,
    *,
    hook: Reset | None = None,
) -> list[str]:
    """Each reset hook's path as the run sends it. Before the second, the
    confirmation's, `second` runs, and `hook`, if given, is sent instead."""
    sent: list[str] = []

    async def resetting(gate: Any, spec_hook: Reset, start: str, **kwargs: Any) -> None:
        if sent:
            second()
            spec_hook = hook or spec_hook
        sent.append(spec_hook.path)
        await reset(gate, spec_hook, start, **kwargs)

    monkeypatch.setattr(explore, "reset", resetting)
    return sent


def test_an_explored_script_replays_with_every_locator_resolving_at_every_use(
    app: App, tmp_path: Path
) -> None:
    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    document = written(run)
    assert run.document == document
    assert (document["published"], document["reasons"]) == ("confirmed", [])
    assert document["phases"] == [CLEAN_PHASE, CONFIRMATION_PHASE]
    confirmation = document["confirmation"]
    assert (confirmation["outcome"], confirmation["error_code"]) == ("passed", None)
    assert [
        (step["seq"], step["outcome"], step["locator_index"])
        for step in confirmation["steps"]
    ] == [(0, "completed", None), (1, "completed", 0), (2, "completed", 0)]
    assert [(each["id"], each["outcome"]) for each in confirmation["assertions"]] == [
        ("a1", "pass")
    ]
    assert app.paths() == PASS * 2
    assert run.script is not None
    assert published(tmp_path) == run.script.model_copy(update={"confirmed": True})
    assert "confirmation/steps.jsonl" in files(run)


# The shop page whose save also adds a paragraph, so a status locator by
# role or tag finds two elements after it, while its test ID still finds one.
MOVED = SHOP.replace(
    "SAID",
    "status.textContent = `Saved ${name} in size ${size}`; "
    "document.body.append(Object.assign(document.createElement('p'), "
    "{textContent: 'thanks'}))",
)


def test_a_non_first_locator_moved_after_the_save_fails_the_confirmation_at_its_later_use(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", MOVED))
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})

    run = confirming(app, tmp_path, Scripted(planned(), *PATH), config=config)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == [
        (
            "confirmation a1: binding_unresolved (not unanimous, not unanimous, "
            "ambiguous, ambiguous)"
        )
    ]
    assert written(run)["published"] is None
    assert not compiled_file(tmp_path).parent.exists()


def watched(
    monkeypatch: pytest.MonkeyPatch, traps: tuple[tuple[type, str], ...]
) -> dict[str, list[str]]:
    """What each of `traps`, a method a model needs, saw while the
    confirmation replayed: each trap raises, and is armed only then, proved
    by one call of each that it refuses; and how many clients the run had
    built, and requests sent, before it."""
    seen: dict[str, list[str]] = {"before": [], "during": []}
    replay = executor.replay

    def refusing(name: str) -> Callable[..., Any]:
        def trap(*_args: Any, **_kwargs: Any) -> Any:
            seen["during"].append(name)
            raise AssertionError(f"{name} during the confirmation")

        return trap

    async def replaying(*args: Any, **kwargs: Any) -> Any:
        with monkeypatch.context() as window:
            for owner, name in traps:
                window.setattr(owner, name, refusing(f"{owner.__name__}.{name}"))
            for owner, name in traps:
                control: Any = getattr(owner, name)
                with pytest.raises(AssertionError):
                    control(None)
            seen["before"] = list(seen["during"])
            seen["during"].clear()
            return await replay(*args, **kwargs)

    monkeypatch.setattr(executor, "replay", replaying)
    return seen


def test_the_confirmation_replay_constructs_no_model_client(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    traps = (
        (ModelRouter, "__init__"),
        (Scripted, "__call__"),
        (ChatClient, "__init__"),
    )
    seen = watched(monkeypatch, traps)
    client = Scripted(planned(), *PATH)

    run = confirming(app, tmp_path, client)

    assert seen == {
        "before": ["ModelRouter.__init__", "Scripted.__call__", "ChatClient.__init__"],
        "during": [],
    }
    assert client.models == ["claude-sonnet-5-5 at effort None"]
    assert written(run)["published"] == "confirmed"


def test_the_confirmation_makes_no_call_on_an_existing_client_or_router(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = watched(monkeypatch, ((ModelRouter, "call"), (Scripted, "call")))
    client = Scripted(planned(), *PATH)

    run = confirming(app, tmp_path, client)

    assert seen == {"before": ["ModelRouter.call", "Scripted.call"], "during": []}
    # The plan's call and the attempt's four turns, each once.
    assert len(client.requests) == 5
    assert written(run)["published"] == "confirmed"


def test_a_spec_without_a_reset_hook_is_written_unconfirmed(
    app: App, tmp_path: Path
) -> None:
    run = confirming(app, tmp_path, Scripted(planned(), *PATH), preconditions="")

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    document = written(run)
    assert (document["published"], document["confirmation"]) == ("unconfirmed", None)
    assert document["phases"] == [CLEAN_PHASE]
    assert run.script is not None
    assert run.script.confirmed is False
    assert published(tmp_path) == run.script
    assert [name for name in files(run) if name.startswith("confirmation")] == []
    # Never replayed: the save was sent once.
    assert app.paths() == ["/page/shop", "/did/fill", "/write/save"]


# A plan whose check reads the status before anything is saved.
UNSAVED = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": STATUS,
                "claim": "says nothing was saved yet",
                "checks": [
                    {"check": "text_in_target", "target_meaning": STATUS, "text": "Not"}
                ],
            }
        ],
        "requires": [],
    }
)
ASSERT_NOW = tool(
    "assert_check",
    lambda m: {"check_id": "a1", "ref": ref(m, "paragraph", "Not saved")},
)


def test_a_zero_step_path_without_a_reset_hook_confirms_with_no_reset(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(planned(UNSAVED), ASSERT_NOW, calling("finish", {"steps": []}))

    run = confirming(app, tmp_path, client, preconditions="")

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert written(run)["published"] == "confirmed"
    assert published(tmp_path).steps == ()
    assert run.record is not None
    lines = (run.record / "confirmation" / "steps.jsonl").read_text().splitlines()
    assert [(json.loads(line)["seq"], json.loads(line)["state"]) for line in lines] == [
        (0, "intent"),
        (0, "completed"),
    ]
    assert app.paths() == ["/page/shop", "/page/shop"]


def test_a_failed_confirmation_writes_nothing_and_gives_up_with_its_reasons(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = PAGES["shop-wrong"]
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", wrong))
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})

    run = confirming(app, tmp_path, Scripted(planned(), *PATH), config=config)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    document = written(run)
    assert document["reasons"] == ["confirmation a1: failed"]
    assert (document["published"], document["confirmation"]["outcome"]) == (
        None,
        "failed",
    )
    assert not compiled_file(tmp_path).parent.exists()


def test_a_failed_reset_before_the_confirmation_writes_nothing_with_exit_14(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = resets(monkeypatch, hook=Reset(http="POST /status/500"))

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("reset_unsafe", 14)
    document = written(run)
    assert document["reasons"] == ["the reset hook answered 500"]
    assert (document["phases"], document["published"]) == ([CLEAN_PHASE], None)
    assert sent == ["/write/reset", "/status/500"]
    assert [name for name in files(run) if name.startswith("confirmation")] == []
    assert not compiled_file(tmp_path).parent.exists()


# The shop page whose save also sends a write the app holds to the end.
HELD_LATE = SHOP.replace(
    "SAID",
    "status.textContent = `Saved ${name} in size ${size}`; "
    "fetch('/held/late', {method: 'POST'})",
)


def test_uncertain_traffic_after_the_confirmation_writes_nothing_with_exit_14(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 1)
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", HELD_LATE))

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("reset_unsafe", 14)
    document = written(run)
    assert document["reasons"] == [PLAIN]
    assert document["confirmation"]["outcome"] == "passed"
    assert document["phases"] == [
        CLEAN_PHASE,
        CONFIRMATION_PHASE | {"plain_uncertain": 1},
    ]
    assert document["published"] is None
    assert not compiled_file(tmp_path).parent.exists()


# The shop page whose save is held by the app until the test ends.
HELD_SAVE = SHOP.replace("/write/save", "/held/save").replace(
    "SAID", "status.textContent = 'Saved'"
)


def test_the_minutes_budget_expiring_mid_confirmation_dispatch_is_exit_4(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = deadlines(monkeypatch)
    expiry: list[asyncio.Task[None]] = []

    async def expire() -> None:
        await until(lambda: "/held/save" in app.paths())
        limits[-1].reschedule(asyncio.get_running_loop().time())

    def held() -> None:
        monkeypatch.setitem(PAGES, "shop", HELD_SAVE)
        expiry.append(asyncio.get_running_loop().create_task(expire()))

    resets(monkeypatch, held)

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("non_resumable", 4)
    document = written(run)
    assert document["reasons"] == ["the minutes budget ran out", PLAIN]
    assert document["non_resumable"] == [{"part": "confirmation", "seq": 2}]
    assert document["phases"] == [CLEAN_PHASE, UNCOUNTED_PHASE | {"plain_uncertain": 1}]
    assert (document["published"], document["confirmation"]) == (None, None)
    assert not compiled_file(tmp_path).parent.exists()


def test_the_minutes_budget_expiring_during_the_confirmations_reset_begins_no_phase(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = deadlines(monkeypatch)

    async def expiring(gate: Any, hook: Reset, start: str, **kwargs: Any) -> None:
        if app.paths().count("/write/reset"):
            # The confirmation's reset: the budget runs out while it waits.
            limits[-1].reschedule(asyncio.get_running_loop().time())
            await asyncio.sleep(3600)
        await reset(gate, hook, start, **kwargs)

    monkeypatch.setattr(explore, "reset", expiring)

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    document = written(run)
    assert document["reasons"] == ["the minutes budget ran out"]
    assert (document["phases"], document["confirmation"]) == ([CLEAN_PHASE], None)
    assert [name for name in files(run) if name.startswith("confirmation")] == []
    assert app.paths().count("/write/reset") == 1
    assert not compiled_file(tmp_path).parent.exists()


def test_a_confirmation_journal_that_takes_no_more_lines_exits_15_and_writes_nothing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = RunRecord.step_completed

    def failing(record: RunRecord, step: int, **kwargs: Any) -> None:
        if record.path.name == "confirmation":
            raise OSError(errno.EIO, "fake completion failure")
        completed(record, step, **kwargs)

    monkeypatch.setattr(RunRecord, "step_completed", failing)

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    document = written(run)
    assert document["reasons"] == [UNWRITTEN]
    assert document["phases"] == [CLEAN_PHASE, UNCOUNTED_PHASE]
    assert (document["non_resumable"], document["published"]) == ([], None)
    assert not compiled_file(tmp_path).parent.exists()


@pytest.mark.usefixtures("secret")
def test_a_confirmation_whose_launch_fails_is_exit_10_with_its_phase_ended(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[str] = []

    async def unproven_second(chromium: Any) -> Browser:
        launches.append("launch")
        if len(launches) == 2:
            raise SandboxUnavailableError("fake: no sandbox could be proved")
        return await launch(chromium)

    monkeypatch.setattr("aqa_runner.browser_session.launch", unproven_second)

    run = confirming(
        app,
        tmp_path,
        Scripted(planned(), *PATH),
        config=SECRETS,
        preconditions=ACCOUNT + RESET,
    )

    assert (run.outcome, run.exit_code) == ("no_sandbox", 10)
    document = written(run)
    assert document["reasons"] == ["fake: no sandbox could be proved"]
    assert document["phases"] == [CLEAN_PHASE, UNCOUNTED_PHASE]
    assert (document["published"], document["confirmation"]) == (None, None)
    assert not compiled_file(tmp_path).parent.exists()


def test_a_confirmation_listener_that_cannot_open_propagates_and_publishes_nothing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enter = LoopbackServer.__aenter__
    opened: list[LoopbackServer] = []

    async def second_unavailable(server: LoopbackServer) -> LoopbackServer:
        opened.append(server)
        if len(opened) == 2:
            raise OSError(errno.EMFILE, "Too many open files")
        return await enter(server)

    monkeypatch.setattr(LoopbackServer, "__aenter__", second_unavailable)

    with pytest.raises(OSError, match="Too many open files"):
        confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert len(opened) == 2
    assert app.paths() == [*PASS, "/write/reset"]
    assert not compiled_file(tmp_path).parent.exists()


def test_a_confirmation_part_that_cannot_be_made_is_exit_15_with_no_confirmation_phase(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part = RunRecord.part

    def unmade(record: RunRecord, name: str) -> RunRecord:
        if name == "confirmation":
            raise OSError(errno.EIO, "fake mkdir failure")
        return part(record, name)

    monkeypatch.setattr(RunRecord, "part", unmade)

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    document = written(run)
    assert document["reasons"] == [UNWRITTEN]
    assert (document["phases"], document["published"]) == ([CLEAN_PHASE], None)
    assert not compiled_file(tmp_path).parent.exists()


@pytest.mark.usefixtures("secret")
def test_the_confirmation_scans_with_the_same_bound_values_as_the_run(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[Redactor] = []

    class Kept(Redactor):
        def __init__(self, *args: Any) -> None:
            super().__init__(*args)
            made.append(self)

    monkeypatch.setattr(executor, "Redactor", Kept)

    run = confirming(
        app,
        tmp_path,
        Scripted(planned(), *PATH),
        config=SECRETS,
        preconditions=ACCOUNT + RESET,
    )

    assert written(run)["published"] == "confirmed"
    assert len(made) == 1
    assert made[0].spells(FAKE_VALUE)
    assert made[0].redact(f"x {FAKE_VALUE} y") == "x [SECRET:TEST_PASSWORD] y"


def test_the_record_keeps_each_phases_steps_and_intents(
    app: App, tmp_path: Path
) -> None:
    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert run.record is not None
    attempt = read_steps(run.record / "attempt-1")
    assert [(line["seq"], line["state"]) for line in attempt] == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
        (1, "completed"),
        (2, "intent"),
        (2, "completed"),
    ]
    assert read_steps(run.record / "confirmation") == [
        {
            "action": {"action": "navigate", "url": "/page/shop"},
            "seq": 0,
            "side_effect": False,
            "state": "intent",
            "target_used": None,
        },
        {"locator_used": None, "seq": 0, "settled": "idle", "state": "completed"},
        {
            "action": {"action": "fill", "target": "t1", "value": "Ada"},
            "seq": 1,
            "side_effect": True,
            "state": "intent",
            "target_used": "t1",
        },
        {"locator_used": 0, "seq": 1, "settled": "idle", "state": "completed"},
        {
            "action": {"action": "click", "target": "t2"},
            "seq": 2,
            "side_effect": True,
            "state": "intent",
            "target_used": "t2",
        },
        {"locator_used": 0, "seq": 2, "settled": "idle", "state": "completed"},
    ]


def test_every_sink_gets_the_presented_result_never_the_run_result(
    app: App, tmp_path: Path
) -> None:
    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    document = written(run)
    assert sorted(document) == [
        "confirmation",
        "egress",
        "exit_code",
        "infrastructure_events",
        "non_resumable",
        "outcome",
        "phases",
        "published",
        "reasons",
        "run_id",
        "withheld",
    ]
    assert sorted(document["confirmation"]) == [
        "assertions",
        "error_code",
        "invariants",
        "outcome",
        "steps",
    ]
    assert document["phases"][1] == CONFIRMATION_PHASE
    # No settle window, request or page URL leaves through the run's
    # document or the confirmation's journal; the step evidence is
    # `aqa_runner.evidence`'s own.
    assert run.record is not None
    saved = [run.record / "explore.json", run.record / "confirmation" / "steps.jsonl"]
    assert [
        path for path in saved if app.origin in path.read_text(errors="replace")
    ] == []


def test_the_reset_waits_for_the_attempts_browser_to_close_and_its_phase_to_end(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    end_phase = EgressProxy.end_phase
    begin_phase = EgressProxy.begin_phase
    seen: list[int] = []

    async def held(proxy: EgressProxy) -> Any:
        if seen:
            return await end_phase(proxy)
        seen.append(int(proxy.url.rsplit(":", 1)[1]))
        # The attempt's phase is held ending: no reset may go meanwhile.
        await asyncio.sleep(0.5)
        seen.append(app.paths().count("/write/reset"))
        traffic = await end_phase(proxy)
        seen.append(len(app.paths()))
        return traffic

    async def begun(proxy: EgressProxy) -> None:
        await begin_phase(proxy)
        seen.append(int(proxy.url.rsplit(":", 1)[1]))

    monkeypatch.setattr(EgressProxy, "end_phase", held)
    monkeypatch.setattr(EgressProxy, "begin_phase", begun)

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert written(run)["published"] == "confirmed"
    attempt_port, resets_while_held, ended_at, confirmation_port = seen
    assert resets_while_held == 1
    assert app.paths()[ended_at:] == PASS
    assert confirmation_port != attempt_port


# The shop page whose save also logs a console error.
CONSOLE_ERROR = SHOP.replace(
    "SAID",
    "status.textContent = `Saved ${name} in size ${size}`; "
    "console.error('fake: the save logged an error')",
)


def test_an_enabled_invariant_violated_during_confirmation_blocks_the_write(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", CONSOLE_ERROR))

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    document = written(run)
    assert document["reasons"] == ["confirmation invariant console_errors: violated"]
    assert document["published"] is None
    assert not compiled_file(tmp_path).parent.exists()


def test_an_invariant_violated_on_a_detour_does_not_count(
    app: App, tmp_path: Path
) -> None:
    detour = tool("navigate", lambda _: {"path": "/page/console-error"})
    back = tool("navigate", lambda _: {"path": "/page/shop"})
    client = Scripted(
        planned(),
        detour,
        back,
        FILL,
        CLICK,
        ASSERT,
        calling("finish", {"steps": [3, 4]}),
    )

    run = confirming(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert written(run)["published"] == "confirmed"
    assert [step.action for step in published(tmp_path).steps] == ["fill", "click"]


# The shop page whose save first asks a host no origin of the run allows.
BEACON = SHOPS["shop"].replace(
    'document.querySelector("#save").addEventListener("click", () => {',
    'document.querySelector("#save").addEventListener("click", () => {\n'
    "            fetch('http://analytics.example.test/beacon').catch(() => {});",
)
REFUSED_RECORD = {
    "error_code": "egress_blocked",
    "refused": [{"host": "analytics.example.test", "port": 80}],
    "overflowed": False,
    "hosts_and_ports_withheld": False,
}


def test_an_undeclared_host_during_confirmation_ends_the_run(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", BEACON))

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code, run.egress_saved) == ("egress_blocked", 6, True)
    document = written(run)
    assert document["reasons"] == [
        "confirmation a1: not_evaluated: step 2 stopped the run"
    ]
    assert (document["egress"], document["published"]) == (REFUSED_RECORD, None)
    assert run.record is not None
    assert json.loads((run.record / "egress.json").read_text()) == REFUSED_RECORD
    assert not compiled_file(tmp_path).parent.exists()


@pytest.mark.usefixtures("secret")
def test_no_file_in_the_explore_record_names_a_refused_host_when_a_secret_is_bound(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", BEACON))

    run = confirming(
        app,
        tmp_path,
        Scripted(planned(), *PATH),
        config=SECRETS,
        preconditions=ACCOUNT + RESET,
    )

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    withheld = {
        "error_code": "egress_blocked",
        "refused_count": 1,
        "overflowed": False,
        "hosts_and_ports_withheld": True,
    }
    assert run.record is not None
    assert written(run)["egress"] == withheld
    assert json.loads((run.record / "egress.json").read_text()) == withheld
    saved = [path for path in run.record.rglob("*") if path.is_file()]
    assert "confirmation/egress.json" in files(run)
    assert [path for path in saved if "analytics" in path.read_text()] == []


# The banner's favorite button and the article actions' copy of it, and the
# plan whose check reads the banner's count through its subject contract.
FAVORITES = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": FAVORITE,
                "claim": "shows one favorite",
                "checks": [
                    {
                        "check": "text_in_target",
                        "target_meaning": FAVORITE,
                        "text": "Unfavorite Article (1)",
                    }
                ],
            }
        ],
        "requires": [],
    }
)
BANNER = ProjectConfig.model_validate(
    {"subjects": [{"spec": "shop", "expect": 0} | BUTTON_ROW.model_dump()]}
)


def banner_button(messages: Sequence[BaseMessage]) -> dict[str, object]:
    refs = re.findall(
        r'button "Unfavorite Article \(1\)"[^\n]*?\[ref=(e\d+)\]', messages[-1].text
    )
    return {"check_id": "a1", "ref": refs[0]}


@pytest.mark.parametrize(
    "case",
    [
        (ARTICLE.replace("(1)", "(3)", 1), ("gave_up", 3), ["confirmation a1: failed"]),
        ("(3)".join(ARTICLE.rsplit("(1)", 1)), ("compiled", 0), []),
    ],
    ids=["the banner's copy", "the article actions' copy"],
)
def test_a_count_changed_after_the_attempt_fails_the_confirmation_only_in_its_region(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, tuple[str, int], list[str]],
) -> None:
    changed, outcome, reasons = case
    monkeypatch.setitem(PAGES, "article", ARTICLE)
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "article", changed))
    client = Scripted(
        planned(FAVORITES),
        tool("assert_check", banner_button),
        calling("finish", {"steps": []}),
    )

    run = confirming(
        app,
        tmp_path,
        client,
        config=BANNER.model_copy(
            update={"budgets": BANNER.budgets.model_copy(update={"resolve_seconds": 2})}
        ),
        page="/page/article",
    )

    assert (run.outcome, run.exit_code) == outcome
    assert written(run)["reasons"] == reasons
    assert compiled_file(tmp_path).exists() is (outcome[1] == 0)


# The favorites count, a part of the banner's favorite button, bound
# through `describe_parts` under its leaf contract (ADR-0025's #53 P19
# amendment), on the article page with the button in the banner and again
# in the article's actions.
COUNTING = ProjectConfig.model_validate(
    {
        "subjects": [
            {
                "spec": "shop",
                "expect": 0,
                "region": "div.banner",
                "part": "span.counter",
                "leaf": True,
            }
        ],
        "budgets": {"resolve_seconds": 2},
    }
)
WHOLE = tool("assert_check", lambda m: {"check_id": "a1", "ref": favorite_in(m)})
DESCRIBE = tool("describe_parts", lambda m: {"ref": favorite_in(m)})
ASSERT_PART = calling("assert_check", {"check_id": "a1", "ref": "p1.2"})
NOTHING_KEPT = calling("finish", {"steps": []})


def counting(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *moves: Move,
    config: ProjectConfig = COUNTING,
) -> Explored:
    """A run that binds the count on the article, then confirms it on the
    page the second reset serves as the article, `PAGES["parts-confirmed"]`."""
    monkeypatch.setitem(PAGES, "parts-article", PARTS_PAGES["parts-article"])
    resets(
        monkeypatch,
        lambda: monkeypatch.setitem(PAGES, "parts-article", PAGES["parts-confirmed"]),
    )
    return confirming(
        app,
        tmp_path,
        Scripted(planned(COUNTED), *moves),
        config=config,
        page="/page/parts-article",
    )


def test_a_bound_count_confirms_and_publishes_its_part(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "parts-confirmed", PARTS_PAGES["parts-article"])

    run = counting(app, tmp_path, monkeypatch, DESCRIBE, ASSERT_PART, NOTHING_KEPT)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert published(tmp_path).targets == {"t1": COUNTER}


def test_a_count_offered_only_as_its_button_is_never_published(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "parts-confirmed", PARTS_PAGES["parts-article"])

    budgets = COUNTING.budgets.model_copy(update={"actions_per_attempt": 2})
    config = COUNTING.model_copy(update={"budgets": budgets})

    run = counting(app, tmp_path, monkeypatch, WHOLE, NOTHING_KEPT, config=config)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == [
        "the attempt took all 2 of its turns",
        "a1: never bound",
    ]
    assert (written(run)["confirmation"], run.script) == (None, None)
    assert not compiled_file(tmp_path).parent.exists()


@pytest.mark.parametrize(
    "case",
    [
        (PARTS_PAGES["parts-flattened"], "binding_unresolved (no match)"),
        (
            PARTS_PAGES["parts-article"].replace(
                '<div class="banner">',
                '<div class="banner"><span class="counter">(1)</span>',
            ),
            "binding_unresolved (ambiguous)",
        ),
    ],
    ids=["its button flattened", "a childless decoy beside it"],
)
def test_a_confirmation_whose_banner_no_longer_holds_the_count_as_its_part_does_not_confirm(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str],
) -> None:
    page, words = case
    monkeypatch.setitem(PAGES, "parts-confirmed", page)

    run = counting(app, tmp_path, monkeypatch, DESCRIBE, ASSERT_PART, NOTHING_KEPT)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == [f"confirmation a1: {words}"]
    assert not compiled_file(tmp_path).parent.exists()
