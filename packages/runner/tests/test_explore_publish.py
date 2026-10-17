"""Publishing what explore confirmed (#53 P16b; ADR-0024's #53 P16
amendments): the compiled file read once the record is made, before
planning; the spec, its contracts and the file compared under the spec's
lock; the published text checked as written; what each failure on either
side of the install point exits with; and whether `egress.json` was saved.
Real Chromium against the executor's fixture app, with a scripted model."""

import asyncio
import errno
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError
from aqa_runner import publication
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.run_record import RunRecord

from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.explore_graph_fixtures import FILL, Scripted, tool
from packages.runner.tests.secret_fixtures import FAKE_VALUE
from packages.runner.tests.test_explore_confirm import (
    PATH,
    RESET,
    compiled_file,
    confirming,
    published,
    resets,
)
from packages.runner.tests.test_explore_graph import (
    ACCOUNT,
    SECRETS,
    UNWRITTEN,
    written,
)
from packages.runner.tests.test_explore_preflight import planned


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
