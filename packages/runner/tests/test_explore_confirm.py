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
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import SpecError, load_compiled, load_config
from aqa_core.spec import Reset
from aqa_runner import executor, explore, publication, settling
from aqa_runner.chat_client import ChatClient
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.explore import Explored
from aqa_runner.loopback_server import LoopbackServer
from aqa_runner.model_router import ModelRouter
from aqa_runner.redaction import Redactor
from aqa_runner.reset import reset
from aqa_runner.run_record import RunRecord
from aqa_runner.sandbox import SandboxUnavailableError, launch
from playwright.async_api import Browser, async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.executor_fixtures import PAGES, SHOP, App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    STATUS,
    Scripted,
    calling,
    ref,
    tool,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE
from packages.runner.tests.test_explore_graph import (
    ACCOUNT,
    ASSERT,
    CLEAN_PHASE,
    FINISH,
    PLAIN,
    SECRETS,
    UNWRITTEN,
    deadlines,
    files,
    shop_spec,
    written,
)
from packages.runner.tests.test_explore_preflight import planned

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
    root = tmp_path / "qa"
    root.mkdir(parents=True, exist_ok=True)
    held = config.model_dump(mode="json", exclude_unset=True)
    (root / "config.yaml").write_text(json.dumps(held))
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


def test_a_subject_contract_changed_during_the_run_publishes_nothing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = {"spec": "shop", "expect": 0, "region": "main", "part": "p"}
    config_file = tmp_path / "qa" / "config.yaml"
    resets(monkeypatch, lambda: config_file.write_text(json.dumps({"subjects": [row]})))

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    document = written(run)
    assert document["reasons"] == [
        "the project's subject contracts changed during the run"
    ]
    assert document["confirmation"]["outcome"] == "passed"
    assert document["published"] is None
    assert not compiled_file(tmp_path).exists()


def test_a_compiled_script_written_by_another_writer_during_the_run_is_not_overwritten(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def theirs() -> None:
        compiled_file(tmp_path).parent.mkdir()
        compiled_file(tmp_path).write_text('{"theirs": 1}\n')

    resets(monkeypatch, theirs)

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == ["the compiled script changed during the run"]
    assert compiled_file(tmp_path).read_text() == '{"theirs": 1}\n'


@pytest.mark.usefixtures("secret")
def test_a_published_text_that_spells_a_bound_value_exits_15(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every source of the text is checked before it (the locators dropped,
    # the plan's and the turns' requests and the tool arguments scanned), so
    # the text is made to spell the value, to show the check is on it.
    text = publication.published_text
    monkeypatch.setattr(
        publication, "published_text", lambda script: text(script) + FAKE_VALUE
    )

    run = confirming(
        app,
        tmp_path,
        Scripted(planned(), *PATH),
        config=SECRETS,
        preconditions=ACCOUNT + RESET,
    )

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    document = written(run)
    assert document["reasons"] == [
        ".compiled/shop.json: a bound value's spelling is in it, so it isn't written"
    ]
    assert (document["withheld"], document["published"]) == (True, None)
    assert not compiled_file(tmp_path).parent.exists()
    assert not (tmp_path / "qa" / ".aqa" / "locks").exists()


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


def test_the_minutes_budget_expiring_during_teardown_publishes_nothing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    end_phase = EgressProxy.end_phase

    async def late(proxy: EgressProxy) -> Any:
        traffic = await end_phase(proxy)
        # The attempt's teardown took past the one minute the run has.
        loop = asyncio.get_running_loop()
        now = loop.time
        monkeypatch.setattr(loop, "time", lambda: now() + 61)
        return traffic

    monkeypatch.setattr(EgressProxy, "end_phase", late)
    config = ProjectConfig.model_validate({"budgets": {"minutes": 1}})

    run = confirming(
        app, tmp_path, Scripted(planned(), *PATH), preconditions="", config=config
    )

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == [
        "the minutes budget ran out before the script was published"
    ]
    assert not compiled_file(tmp_path).parent.exists()


def test_a_compiled_file_that_cannot_be_read_at_admission_exits_15_before_planning(
    app: App, tmp_path: Path
) -> None:
    # A directory where the compiled file goes: reading it fails.
    compiled_file(tmp_path).mkdir(parents=True)
    client = Scripted(planned(), *PATH)

    run = confirming(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.document == {
        "outcome": "record_error",
        "reasons": ["the compiled script could not be read; nothing was planned"],
    }
    assert (client.requests, app.paths()) == ([], [])
    # Read once the record and its part are made, before planning.
    assert run.record is not None
    assert sorted(path.name for path in run.record.iterdir()) == ["attempt-1"]
    assert list((run.record / "attempt-1").iterdir()) == []


def test_a_compiled_directory_that_is_a_link_is_a_spec_error_before_planning(
    app: App, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / ".compiled").symlink_to(elsewhere)
    client = Scripted(planned(), *PATH)

    with pytest.raises(SpecError) as refused:
        confirming(app, tmp_path, client)

    root = tmp_path / "qa"
    assert refused.value.problems == (
        f"{root / '.compiled'}: a link, and explore writes only in {root}",
    )
    assert (client.requests, app.paths()) == ([], [])
    # Refused once the record and its part are made, before planning.
    records = list((root / ".aqa" / "runs").iterdir())
    assert [sorted(path.name for path in record.iterdir()) for record in records] == [
        ["attempt-1"]
    ]


OLD = '{"old": 1}\n'


@pytest.mark.parametrize("existing", [None, OLD])
def test_an_unwritable_compiled_directory_exits_15_and_publishes_nothing(
    app: App, tmp_path: Path, existing: str | None
) -> None:
    directory = compiled_file(tmp_path).parent
    directory.mkdir(parents=True)
    if existing is not None:
        compiled_file(tmp_path).write_text(existing)
    directory.chmod(0o555)
    try:
        run = confirming(app, tmp_path, Scripted(planned(), *PATH), preconditions="")
    finally:
        directory.chmod(0o755)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    document = written(run)
    assert document["reasons"] == ["the compiled script couldn't be written"]
    assert document["published"] is None
    held = compiled_file(tmp_path)
    assert (held.read_text() if held.exists() else None) == existing
    assert sorted(path.name for path in directory.iterdir()) == (
        [] if existing is None else ["shop.json"]
    )


def test_a_script_installed_before_its_directory_sync_failed_is_published_with_exit_15(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync = os.fsync
    directory = compiled_file(tmp_path).parent

    def failing(handle: int) -> None:
        if directory.exists() and os.fstat(handle).st_ino == directory.stat().st_ino:
            raise OSError(errno.EIO, "fake directory fsync failure")
        fsync(handle)

    monkeypatch.setattr(os, "fsync", failing)

    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    document = written(run)
    assert document["published"] == "confirmed"
    assert document["reasons"] == [
        (
            "the compiled script was published, but the file system failed after "
            "it, so it may not survive a crash"
        )
    ]
    assert run.script is not None
    assert published(tmp_path) == run.script.model_copy(update={"confirmed": True})


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


def test_a_run_whose_egress_record_is_written_says_it_was_saved(
    app: App, tmp_path: Path
) -> None:
    away = tool("navigate", lambda _: {"path": "/page/refused-script"})

    run = confirming(app, tmp_path, Scripted(planned(), away, FILL))

    assert (run.outcome, run.exit_code, run.egress_saved) == ("egress_blocked", 6, True)
    assert run.record is not None
    assert json.loads((run.record / "egress.json").read_text())["refused"] == [
        {"host": "analytics.example.test", "port": 80}
    ]


def test_a_run_whose_egress_record_could_not_be_written_says_it_was_not_saved(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = RunRecord.write

    def unwritten(record: RunRecord, name: str, *args: Any, **kwargs: Any) -> None:
        if name == "egress.json":
            raise OSError(errno.ENOSPC, "fake full disk")
        write(record, name, *args, **kwargs)

    monkeypatch.setattr(RunRecord, "write", unwritten)
    away = tool("navigate", lambda _: {"path": "/page/refused-script"})

    run = confirming(app, tmp_path, Scripted(planned(), away, FILL))

    assert (run.outcome, run.exit_code, run.egress_saved) == (
        "egress_blocked",
        6,
        False,
    )
    assert run.record is not None
    assert not (run.record / "egress.json").exists()
    # `explore.json` was written first; the result says what failed after.
    assert run.document["reasons"] == [UNWRITTEN, "a1: never bound"]


def test_a_run_with_no_block_saves_no_egress_record(app: App, tmp_path: Path) -> None:
    run = confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert (run.exit_code, run.egress_saved) == (0, False)
    assert run.record is not None
    assert not (run.record / "egress.json").exists()
