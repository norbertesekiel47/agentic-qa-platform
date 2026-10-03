"""A local run's record (ARCHITECTURE §3.3; #41): a directory per run under the
spec root's `.aqa/runs/`, which tells git to ignore it."""

import json
import os
import uuid
from pathlib import Path

import pytest
from aqa_runner.run_record import RunRecord


def test_a_record_is_a_new_uuid7_directory_under_the_spec_roots_runs(
    tmp_path: Path,
) -> None:
    record = RunRecord.create(tmp_path)

    assert record.path == tmp_path / ".aqa" / "runs" / record.run_id
    assert record.path.is_dir()
    assert uuid.UUID(record.run_id).version == 7


def test_each_run_gets_a_record_of_its_own(tmp_path: Path) -> None:
    first, second = RunRecord.create(tmp_path), RunRecord.create(tmp_path)

    assert first.run_id != second.run_id
    assert first.path.is_dir()
    assert second.path.is_dir()


def test_the_records_tell_git_to_ignore_them(tmp_path: Path) -> None:
    # The spec root is in the user's repository, which shouldn't have to know
    # about run records.
    RunRecord.create(tmp_path)

    assert (tmp_path / ".aqa" / ".gitignore").read_text() == "*\n"


def test_an_ignore_file_already_there_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / ".aqa").mkdir()
    (tmp_path / ".aqa" / ".gitignore").write_text("runs/\n!keep.txt\n")

    RunRecord.create(tmp_path)

    assert (tmp_path / ".aqa" / ".gitignore").read_text() == "runs/\n!keep.txt\n"


def test_a_record_writes_a_document_as_json(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)

    written = record.write("plan.json", {"outcome": "planned", "calls": []})

    assert written == record.path / "plan.json"
    assert json.loads(written.read_text()) == {"outcome": "planned", "calls": []}


def test_a_record_never_writes_a_document_twice(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)
    record.write("plan.json", {"outcome": "planned"})

    with pytest.raises(FileExistsError):
        record.write("plan.json", {"outcome": "gave_up"})

    assert json.loads((record.path / "plan.json").read_text()) == {"outcome": "planned"}


@pytest.mark.parametrize("linked", [".aqa", ".aqa/runs"])
def test_a_record_refuses_to_follow_a_link_out_of_the_spec_root(
    tmp_path: Path, linked: str
) -> None:
    # A repository can commit .aqa, or .aqa/runs, as a link to anywhere.
    root, elsewhere = tmp_path / "qa", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / linked).parent.mkdir(parents=True, exist_ok=True)
    (root / linked).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ValueError, match="a link"):
        RunRecord.create(root)

    assert list(elsewhere.iterdir()) == []


def test_a_document_that_cannot_be_written_leaves_no_file(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)

    with pytest.raises(TypeError):
        record.write("plan.json", {"plan": object()})

    assert not (record.path / "plan.json").exists()


def lines(record: RunRecord) -> list[dict[str, object]]:
    """The record's step lines, each without its time."""
    text = (record.path / "steps.jsonl").read_text(encoding="utf-8")
    return [
        {key: value for key, value in json.loads(line).items() if key != "at"}
        for line in text.splitlines()
    ]


def test_a_step_is_recorded_as_an_intent_and_then_its_completion(
    tmp_path: Path,
) -> None:
    record = RunRecord.create(tmp_path)

    record.step_intent(
        1, {"action": "click", "target": "save"}, side_effect=True, target_used="save"
    )
    record.step_completed(1, locator_used=2, settled="idle")

    assert lines(record) == [
        {
            "seq": 1,
            "state": "intent",
            "action": {"action": "click", "target": "save"},
            "side_effect": True,
            "target_used": "save",
        },
        {"seq": 1, "state": "completed", "locator_used": 2, "settled": "idle"},
    ]


def test_each_step_line_says_when_in_utc(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)

    record.step_intent(0, {"action": "reload"}, side_effect=False, target_used=None)

    [line] = (record.path / "steps.jsonl").read_text().splitlines()
    at = json.loads(line)["at"]
    assert at.endswith("+00:00")


def test_an_intent_without_its_completion_stays_unresolved(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)

    record.step_intent(0, {"action": "reload"}, side_effect=False, target_used=None)
    record.step_completed(0, locator_used=None, settled="timeout")
    record.step_intent(
        1, {"action": "click", "target": "pay"}, side_effect=True, target_used="pay"
    )

    # A record only appends: the last intent has no completion after it.
    assert [(line["seq"], line["state"]) for line in lines(record)] == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
    ]


def test_each_step_line_is_on_disk_before_the_call_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RunRecord.create(tmp_path)
    steps = record.path / "steps.jsonl"
    # What the file held each time something was forced to disk.
    synced: list[str] = []
    fsync = os.fsync

    def recording(fd: int) -> None:
        synced.append(steps.read_text() if steps.exists() else "")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", recording)

    record.step_intent(0, {"action": "reload"}, side_effect=False, target_used=None)
    first = list(synced)
    record.step_completed(0, locator_used=None, settled="idle")

    # The line was written before the file was forced to disk, and the
    # directory that now holds the file was forced too.
    assert first
    assert all('"state": "intent"' in text for text in first)
    assert len(first) == 2
    assert len(synced) == 3
    assert '"state": "completed"' in synced[-1]


def test_a_step_line_that_cannot_be_written_leaves_the_file_as_it_was(
    tmp_path: Path,
) -> None:
    record = RunRecord.create(tmp_path)
    record.step_intent(0, {"action": "reload"}, side_effect=False, target_used=None)

    with pytest.raises(TypeError):
        record.step_intent(1, {"action": object()}, side_effect=False, target_used=None)

    assert [line["seq"] for line in lines(record)] == [0]
