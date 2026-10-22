"""Publishing what explore confirmed (#53 P16b; ADR-0024's #53 P16
amendments): the compiled file read once the record is made, before
planning; nothing published after a stop, a block or a dropped target, and
every locator that spells a bound value dropped first; the spec, its
contracts and the file compared under the spec's lock; the published text
checked as written; what each failure on either side of the install point
exits with; and whether `egress.json` was saved. Real Chromium against the
executor's fixture app, with a scripted model."""

import asyncio
import errno
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError
from aqa_runner import executor, explore, publication, settling
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.explore_turns import Turning, take_turns
from aqa_runner.navigator_tools import Attempt
from aqa_runner.run_record import RunRecord

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import get
from packages.runner.tests.executor_fixtures import PAGES, SHOPS, App, serving_app
from packages.runner.tests.explore_graph_fixtures import FILL, Scripted, planned, tool
from packages.runner.tests.secret_fixtures import FAKE_VALUE, copies_found
from packages.runner.tests.test_executor_runs import while_closing
from packages.runner.tests.test_explore_confirm import (
    HELD_LATE,
    PASS,
    PATH,
    RESET,
    compiled_file,
    confirming,
    published,
    resets,
)
from packages.runner.tests.test_explore_graph import (
    ACCOUNT,
    PLAIN,
    SECRETS,
    UNWRITTEN,
    files,
    proxies,
    written,
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)


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


@pytest.mark.usefixtures("secret")
def test_no_written_script_or_record_holds_a_secret_copy(
    app: App, tmp_path: Path
) -> None:
    run = confirming(
        app,
        tmp_path,
        Scripted(planned(), *PATH),
        config=SECRETS,
        preconditions=ACCOUNT + RESET,
    )

    assert written(run)["published"] == "confirmed"
    written_files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert compiled_file(tmp_path) in written_files
    assert copies_found(FAKE_VALUE, files=written_files) == []


# A value the status paragraph's ID and test ID spell, bound as the test
# secret: a locator by either spells it.
SPELLED = "fake-status-value-49"
SPELLING = (
    SHOPS["shop"].replace('"status"', f'"{SPELLED}"').replace("#status", f"#{SPELLED}")
)


def spelling(monkeypatch: pytest.MonkeyPatch, page: str) -> None:
    """Bind `SPELLED`, serve `page` as the shop, and let the attempt keep a
    locator that spells a bound value, which its own drop would refuse, so
    only explore's drop before the confirmation stands between it and the
    published script."""
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", SPELLED)
    monkeypatch.setitem(PAGES, "shop", page)
    monkeypatch.setattr(Attempt, "_spelled", lambda _attempt, _locator: False)


def test_a_locator_spelling_a_bound_value_is_dropped_before_the_confirmation(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spelling(monkeypatch, SPELLING)
    compiled: list[CompiledScript | None] = []

    async def taking(turning: Turning) -> None:
        await take_turns(turning)
        compiled.append(turning.script)

    monkeypatch.setattr(explore, "take_turns", taking)

    run = confirming(
        app,
        tmp_path,
        Scripted(planned(), *PATH),
        config=SECRETS,
        preconditions=ACCOUNT + RESET,
    )

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    # The script the attempt compiled spells the value, so it isn't returned.
    assert run.script is None
    assert compiled[0] is not None
    compiled_locators = [
        locator.model_dump(exclude_none=True)
        for locator in compiled[0].targets["t3"].locators
    ]
    assert compiled_locators == [
        {"testid": SPELLED},
        {"css": f"#{SPELLED}"},
        {"role": "paragraph"},
        {"css": "p"},
    ]
    script = published(tmp_path)
    assert script.confirmed is True
    assert [
        locator.model_dump(exclude_none=True)
        for locator in script.targets["t3"].locators
    ] == [{"role": "paragraph"}, {"css": "p"}]
    assert {
        name: target for name, target in script.targets.items() if name != "t3"
    } == {name: target for name, target in compiled[0].targets.items() if name != "t3"}


def test_a_target_left_with_no_locator_publishes_nothing_naming_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The status's section spells the value too, and a second paragraph
    # outside it: no locator by role or tag finds the status alone.
    page = SPELLING.replace('"status-area"', f'"{SPELLED}-area"') + "<p>Elsewhere</p>"
    spelling(monkeypatch, page)

    run = confirming(
        app,
        tmp_path,
        Scripted(planned(), *PATH),
        config=SECRETS,
        preconditions=ACCOUNT + RESET,
    )

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    document = written(run)
    assert document["reasons"] == [
        "target t3 has no locator that spells no bound value"
    ]
    assert (document["confirmation"], document["published"]) == (None, None)
    assert [name for name in files(run) if name.startswith("confirmation")] == []
    assert app.paths().count("/write/reset") == 1
    assert not compiled_file(tmp_path).parent.exists()


@pytest.mark.parametrize("preconditions", ["", RESET], ids=["no hook", "a hook"])
def test_uncertain_traffic_left_by_the_attempt_after_finish_publishes_nothing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preconditions: str
) -> None:
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 1)
    monkeypatch.setitem(PAGES, "shop", HELD_LATE)

    run = confirming(
        app, tmp_path, Scripted(planned(), *PATH), preconditions=preconditions
    )

    assert (run.outcome, run.exit_code) == ("reset_unsafe", 14)
    document = written(run)
    assert document["reasons"] == [PLAIN]
    assert (document["confirmation"], document["published"]) == (None, None)
    assert run.script is not None
    # No reset follows uncertain traffic, and nothing is replayed (D29).
    assert app.paths().count("/write/reset") == (1 if preconditions else 0)
    assert app.paths().count("/write/save") == 1
    assert not compiled_file(tmp_path).parent.exists()


def test_a_block_recorded_as_the_attempts_browser_closes_after_finish_publishes_nothing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = proxies(monkeypatch)
    late: list[socket.socket] = []

    async def refused() -> None:
        # A late request of the browser's, as it closes.
        late.append(socket.create_connection(("127.0.0.1", opened[0][1])))
        late[0].sendall(get("http://evil.example.test/", close=True))
        await until(lambda: "evil.example.test" in json.dumps(written_blocks(opened)))

    while_closing(monkeypatch, refused)

    try:
        run = confirming(app, tmp_path, Scripted(planned(), *PATH), preconditions="")
    finally:
        for connection in late:
            connection.close()

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    document = written(run)
    assert document["reasons"] == []
    assert document["egress"]["refused"] == [{"host": "evil.example.test", "port": 80}]
    assert document["published"] is None
    assert not compiled_file(tmp_path).parent.exists()


def written_blocks(opened: list[tuple[EgressProxy, int]]) -> list[str]:
    """The hosts the run's proxy has refused so far."""
    blocks = opened[0][0].egress_blocks(())
    return [refused.host for refused in blocks.refused]


def test_a_timeout_that_isnt_the_minutes_budgets_own_propagates_from_the_confirmation(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def foreign(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError

    monkeypatch.setattr(executor, "replay", foreign)

    with pytest.raises(TimeoutError):
        confirming(app, tmp_path, Scripted(planned(), *PATH))

    assert app.paths() == [*PASS, "/write/reset"]
    assert not compiled_file(tmp_path).parent.exists()
