"""`aqa report [<run_id>]` (API.md §7; #249): which run it reports, the file it
writes and rewrites, and what it refuses. No browser, model or test secret. Each
record here is written by hand, as a person's edit or a committed record would be;
`test_report_page.py` holds what the page shows of real runs."""

import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

import pytest
import typer.testing
from aqa_cli import common
from aqa_cli import run as run_module
from aqa_cli.main import app as aqa
from playwright._impl._browser_type import BrowserType

# What every refusal ends with.
NONE = "no report was written"


def run_ids(count: int) -> list[str]:
    """`count` new run ids, oldest first, as `RunRecord.create` makes them."""
    return [str(uuid.uuid7()) for _ in range(count)]


def finished(run_id: str, **fields: object) -> dict[str, object]:
    """A strict replay's `replay.json` for the run `run_id` that passed, with
    nothing replayed, then `fields`."""
    return {
        "schema_version": 1,
        "run_id": run_id,
        "spec_id": "order",
        "mode": "strict",
        "outcome": "passed",
        "exit_code": 0,
        "withheld": False,
        "error_code": None,
        "reasons": [],
        "non_resumable": [],
        "infrastructure_events": {"count": 0, "events": []},
        "verdicts": [],
    } | fields


def spec_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text("budgets: { resolve_seconds: 1 }\n")
    return path


def recorded(root: Path, run_id: str, document: object | None = None) -> Path:
    """The run `run_id`'s record under the spec root `root`, holding `document`
    as its `replay.json`, or a finished replay's. Its directory."""
    record = root / ".aqa" / "runs" / run_id
    record.mkdir(parents=True)
    written = finished(run_id) if document is None else document
    (record / "replay.json").write_text(json.dumps(written, indent=2) + "\n")
    return record


def report(
    cwd: Path, monkeypatch: pytest.MonkeyPatch, *args: str
) -> typer.testing.Result:
    """`aqa report <args>` in this process, from the directory `cwd`."""
    monkeypatch.chdir(cwd)
    return typer.testing.CliRunner().invoke(aqa, ["report", *args])


def left_behind(root: Path) -> list[Path]:
    """Every report and temporary file under `root`."""
    return [
        each
        for each in root.rglob("*")
        if each.name == "report.html" or each.name.endswith(".tmp")
    ]


def plain(text: str) -> str:
    """Typer's output, without its box and colours, on one line."""
    unboxed = re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", text))
    return " ".join(unboxed.split())


def test_report_is_a_command_with_an_optional_run_id(aqa: str) -> None:
    # Coloured, as Rich colours it under GitHub Actions: a colour code before a
    # word leaves no word boundary there.
    coloured = os.environ | {"FORCE_COLOR": "1"}
    listed = subprocess.run(
        [aqa, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=coloured,
    )
    helped = subprocess.run(
        [aqa, "report", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=coloured,
    )

    assert listed.returncode == 0, listed.stderr
    assert "\x1b[" in listed.stdout
    assert re.search(r"\breport\b", plain(listed.stdout))
    assert helped.returncode == 0, helped.stderr
    assert "[RUN_ID]" in plain(helped.stdout)


@pytest.mark.parametrize("where", ["spec-root", "subdirectory"])
def test_with_no_id_it_reports_the_newest_replay_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    root = spec_root(tmp_path / "qa")
    older, newer, explored = run_ids(3)
    recorded(root, older)
    newest = recorded(root, newer)
    # The newest record is an explore run's, which has no replay.json.
    (root / ".aqa" / "runs" / explored).mkdir()
    (root / ".aqa" / "runs" / explored / "explore.json").write_text("{}\n")
    # A repository whose qa/ holds config.yaml, run from a directory below it.
    cwd = root if where == "spec-root" else tmp_path / "src" / "deep"
    cwd.mkdir(parents=True, exist_ok=True)

    result = report(cwd, monkeypatch)

    assert result.exit_code == 0, result.output
    assert result.stdout == f"report {newer}: {newest / 'report.html'}\n"
    assert result.stderr == ""
    assert [each.parent for each in left_behind(tmp_path)] == [newest]
    assert (newest / "report.html").read_text().startswith("<!doctype html>")


def test_it_skips_a_linked_run_and_a_linked_document_when_choosing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = spec_root(tmp_path / "qa")
    oldest, linked_document, linked_run = run_ids(3)
    chosen = recorded(root, oldest)
    elsewhere = recorded(spec_root(tmp_path / "other"), linked_run)
    (root / ".aqa" / "runs" / linked_run).symlink_to(elsewhere)
    held = recorded(tmp_path / "other", linked_document)
    record = root / ".aqa" / "runs" / linked_document
    record.mkdir()
    (record / "replay.json").symlink_to(held / "replay.json")

    result = report(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert result.stdout == f"report {oldest}: {chosen / 'report.html'}\n"
    assert left_behind(tmp_path) == [chosen / "report.html"]


def test_running_it_again_replaces_the_report_and_never_follows_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = spec_root(tmp_path / "qa")
    [run_id] = run_ids(1)
    record = recorded(root, run_id)
    first = report(root, monkeypatch, run_id)
    assert first.exit_code == 0, first.output
    (record / "report.html").write_text("an older report\n")

    again = report(root, monkeypatch, run_id)

    assert again.exit_code == 0, again.output
    assert (record / "report.html").read_text().startswith("<!doctype html>")
    target = tmp_path / "not-in-the-record.txt"
    target.write_bytes(b"a file the report must not touch\n")
    (record / "report.html").unlink()
    (record / "report.html").symlink_to(target)

    linked = report(root, monkeypatch, run_id)

    assert linked.exit_code == 0, linked.output
    assert not (record / "report.html").is_symlink()
    assert (record / "report.html").read_text().startswith("<!doctype html>")
    assert target.read_bytes() == b"a file the report must not touch\n"
    assert [each for each in record.iterdir() if each.name.endswith(".tmp")] == []


@pytest.mark.parametrize(
    "given",
    [
        "../x",
        str(uuid.uuid7()).upper(),
        str(uuid.uuid4()),
        f"{uuid.uuid7()}/..",
    ],
    ids=["a-path", "uppercase", "a-uuid4", "a-uuid7-and-more"],
)
def test_a_malformed_id_is_a_usage_error_before_any_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    # No spec root: a path built from the id would be refused as exit 5.
    result = report(tmp_path, monkeypatch, given)

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert (
        plain(result.stderr).count(
            "Invalid value for 'RUN_ID': a run's id is a lowercase UUIDv7, as its "
            "record's directory is named"
        )
        == 1
    )
    assert left_behind(tmp_path) == []


def unknown(root: Path) -> tuple[str, str]:
    [run_id] = run_ids(1)
    recorded(root, str(uuid.uuid7()))
    return run_id, f"no run record under {root / '.aqa' / 'runs'} has this id"


def explore_run(root: Path) -> tuple[str, str]:
    [run_id] = run_ids(1)
    record = root / ".aqa" / "runs" / run_id
    (record / "attempt-1").mkdir(parents=True)
    return run_id, "an explore run's record, which aqa report doesn't read yet"


def planned(root: Path) -> tuple[str, str]:
    [run_id] = run_ids(1)
    record = root / ".aqa" / "runs" / run_id
    record.mkdir(parents=True)
    (record / "plan.json").write_text("{}\n")
    return run_id, "an explore run's record, which aqa report doesn't read yet"


def unfinished(root: Path) -> tuple[str, str]:
    [run_id] = run_ids(1)
    record = root / ".aqa" / "runs" / run_id
    record.mkdir(parents=True)
    (record / "steps.jsonl").write_text("")
    return run_id, "a run that didn't finish: its record has no replay.json"


def linked_run(root: Path) -> tuple[str, str]:
    [run_id] = run_ids(1)
    elsewhere = recorded(spec_root(root.parent / "elsewhere"), run_id)
    record = root / ".aqa" / "runs" / run_id
    record.parent.mkdir(parents=True)
    record.symlink_to(elsewhere)
    return run_id, f"{record}: a link, and a run record stays in {root}"


def linked_document(root: Path) -> tuple[str, str]:
    [run_id] = run_ids(1)
    elsewhere = recorded(spec_root(root.parent / "elsewhere"), run_id)
    record = root / ".aqa" / "runs" / run_id
    record.mkdir(parents=True)
    (record / "replay.json").symlink_to(elsewhere / "replay.json")
    return run_id, "its replay.json is a link, which aqa report doesn't follow"


def linked(name: str) -> Callable[[Path], tuple[str, str]]:
    """A run record whose `name`, `.aqa` or `.aqa/runs`, is a link to one
    outside the spec root."""

    def planted(root: Path) -> tuple[str, str]:
        [run_id] = run_ids(1)
        other = spec_root(root.parent / "elsewhere")
        recorded(other, run_id)
        place = root / name
        place.parent.mkdir(parents=True, exist_ok=True)
        place.symlink_to(other / name)
        return run_id, f"{place}: a link, and a run record stays in {root}"

    return planted


def written(raw: bytes) -> Callable[[Path], tuple[str, str]]:
    def holding(root: Path) -> tuple[str, str]:
        [run_id] = run_ids(1)
        record = recorded(root, run_id)
        (record / "replay.json").write_bytes(raw)
        return run_id, "its replay.json could not be read"

    return holding


def shaped(**fields: object) -> Callable[[Path], tuple[str, str]]:
    def holding(root: Path) -> tuple[str, str]:
        [run_id] = run_ids(1)
        recorded(root, run_id, finished(run_id, **fields))
        return run_id, "its replay.json isn't a document aqa writes"

    return holding


def a_list(root: Path) -> tuple[str, str]:
    [run_id] = run_ids(1)
    recorded(root, run_id, [finished(run_id)])
    return run_id, "its replay.json isn't a document aqa writes"


def another_run(root: Path) -> tuple[str, str]:
    [run_id, other] = run_ids(2)
    recorded(root, run_id, finished(other))
    return run_id, "its replay.json isn't a document aqa writes"


@pytest.mark.parametrize(
    ("record", "code", "outcome"),
    [
        pytest.param(unknown, 5, "spec_error", id="unknown-id"),
        pytest.param(explore_run, 5, "spec_error", id="explore-attempt"),
        pytest.param(planned, 5, "spec_error", id="explore-plan"),
        pytest.param(unfinished, 5, "spec_error", id="unfinished"),
        pytest.param(linked_run, 5, "spec_error", id="linked-run"),
        pytest.param(linked_document, 5, "spec_error", id="linked-document"),
        pytest.param(linked(".aqa"), 5, "spec_error", id="linked-aqa"),
        pytest.param(linked(".aqa/runs"), 5, "spec_error", id="linked-runs"),
        pytest.param(written(b"\xff\xfe{}"), 15, "record_error", id="not-utf8"),
        pytest.param(written(b"{"), 15, "record_error", id="not-json"),
        pytest.param(written(b"[" * 100_000), 15, "record_error", id="too-deep"),
        pytest.param(shaped(schema_version=2), 5, "spec_error", id="schema-2"),
        pytest.param(shaped(schema_version=True), 5, "spec_error", id="schema-true"),
        pytest.param(shaped(schema_version=1.0), 5, "spec_error", id="schema-float"),
        pytest.param(shaped(mode="verified"), 5, "spec_error", id="mode"),
        pytest.param(shaped(outcome="won"), 5, "spec_error", id="outcome"),
        pytest.param(shaped(verdicts={}), 5, "spec_error", id="verdicts"),
        pytest.param(another_run, 5, "spec_error", id="run-id"),
        pytest.param(a_list, 5, "spec_error", id="not-an-object"),
    ],
)
def test_a_named_run_it_cant_report_is_refused_in_one_fixed_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: Callable[[Path], tuple[str, str]],
    code: int,
    outcome: str,
) -> None:
    root = spec_root(tmp_path / "qa")
    run_id, why = record(root)

    result = report(root, monkeypatch, run_id)

    assert result.exit_code == code, result.output
    assert result.stdout == ""
    assert result.stderr == f"{outcome} {run_id}: {why}, so {NONE}\n"
    assert left_behind(tmp_path) == []


def test_with_no_reportable_run_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = spec_root(tmp_path / "qa")
    explore_run(root)
    unfinished(root)

    result = report(root, monkeypatch)

    runs = root / ".aqa" / "runs"
    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error report: no run record under {runs} has a replay.json, so {NONE}\n"
    )
    assert left_behind(tmp_path) == []


def test_with_no_run_records_at_all_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = spec_root(tmp_path / "qa")

    result = report(root, monkeypatch)

    runs = root / ".aqa" / "runs"
    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error report: no run record under {runs} has a replay.json, so {NONE}\n"
    )


def test_with_no_spec_root_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [run_id] = run_ids(1)

    result = report(tmp_path, monkeypatch, run_id)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {run_id}: {tmp_path}: no config.yaml in it, in its qa/ or "
        f"above it, so {NONE}\n"
    )


def test_a_linked_qa_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = spec_root(tmp_path / "elsewhere")
    [run_id] = run_ids(1)
    recorded(other, run_id)
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "qa").symlink_to(other)

    result = report(tmp_path / "repo", monkeypatch)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error report: {tmp_path / 'repo' / 'qa'}: a link, which could lead "
        f"into another checkout: run this inside the project you mean, so {NONE}\n"
    )
    assert left_behind(tmp_path) == []


@pytest.fixture
def unreadable(tmp_path: Path) -> Iterator[Path]:
    """The spec root's run records, which can't be listed."""
    root = spec_root(tmp_path / "qa")
    recorded(root, str(uuid.uuid7()))
    runs = root / ".aqa" / "runs"
    runs.chmod(0)
    yield root
    runs.chmod(0o755)


def test_run_records_that_cant_be_listed_exit_15(
    unreadable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = report(unreadable, monkeypatch)

    assert result.exit_code == 15, result.output
    assert (
        result.stderr
        == f"record_error report: the run records could not be read, so {NONE}\n"
    )


def test_a_document_that_cant_be_opened_exits_15(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = spec_root(tmp_path / "qa")
    [run_id] = run_ids(1)
    record = recorded(root, run_id)
    (record / "replay.json").chmod(0)

    result = report(root, monkeypatch, run_id)

    assert result.exit_code == 15, result.output
    assert result.stderr == (
        f"record_error {run_id}: its replay.json could not be read, so {NONE}\n"
    )


def test_a_write_whose_rename_fails_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = spec_root(tmp_path / "qa")
    [run_id] = run_ids(1)
    recorded(root, run_id)

    def failing(*_: object) -> NoReturn:
        raise OSError(5, "fake rename failure 4u49")

    monkeypatch.setattr(os, "replace", failing)

    result = report(root, monkeypatch, run_id)

    assert result.exit_code == 15, result.output
    assert result.stderr == f"record_error {run_id}: the report could not be written\n"
    assert "4u49" not in result.output
    assert left_behind(tmp_path) == []


def test_it_starts_no_browser_and_builds_no_model_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[str] = []

    def refuse(name: str) -> Callable[..., NoReturn]:
        def refused(*_: object, **__: object) -> NoReturn:
            used.append(name)
            raise AssertionError(f"{name} was started")

        return refused

    monkeypatch.setattr(BrowserType, "launch", refuse("launch"))
    monkeypatch.setattr(common, "async_playwright", refuse("the driver"))
    monkeypatch.setattr(run_module, "AnthropicClient", refuse("a model client"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    root = spec_root(tmp_path / "qa")
    [run_id] = run_ids(1)
    record = recorded(root, run_id)

    result = report(root, monkeypatch, run_id)

    assert result.exit_code == 0, result.output
    assert used == []
    assert (record / "report.html").is_file()


def test_it_reads_no_spec_config_or_compiled_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = spec_root(tmp_path / "qa")
    # Files that would stop any command that read them.
    (root / "config.yaml").write_text("colour: red\n")
    (root / "order.spec.md").write_text("not a spec\n")
    (root / ".compiled").mkdir()
    (root / ".compiled" / "order.json").write_text("{\n")
    [run_id] = run_ids(1)
    record = recorded(root, run_id)

    result = report(root, monkeypatch, run_id)

    assert result.exit_code == 0, result.output
    assert (record / "report.html").is_file()
