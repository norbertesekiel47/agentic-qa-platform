"""A local run's record (ARCHITECTURE §3.3; #41): a directory per run under the
spec root's `.aqa/runs/`, which tells git to ignore it."""

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RefusedWriteError, RunRecord, journal_line

from packages.runner.tests.result_redaction_fixtures import fake_secret

# Where the fake secrets these tests bind may go: nowhere a test visits.
ORIGIN = "http://app.example.test"


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
    # What was forced to disk, by inode, and what the file held then.
    synced: list[tuple[int, str]] = []
    fsync = os.fsync

    def recording(fd: int) -> None:
        synced.append(
            (os.fstat(fd).st_ino, steps.read_text() if steps.exists() else "")
        )
        fsync(fd)

    monkeypatch.setattr(os, "fsync", recording)

    record.step_intent(0, {"action": "reload"}, side_effect=False, target_used=None)
    first = list(synced)
    record.step_completed(0, locator_used=None, settled="idle")

    def inode(path: Path) -> int:
        return path.stat().st_ino

    # The file, with the intent in it, then every directory from the run's
    # up to the spec root, which existed before the run: a new entry is on
    # disk only once its directory is.
    assert [ino for ino, _ in first] == [
        inode(steps),
        inode(record.path),
        inode(record.path.parent),
        inode(tmp_path / ".aqa"),
        inode(tmp_path),
    ]
    assert '"state": "intent"' in first[0][1]
    # Later lines force only the file.
    assert synced[len(first) :] == [(inode(steps), synced[-1][1])]
    assert '"state": "completed"' in synced[-1][1]


def test_a_value_with_a_line_separator_stays_on_one_line(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)
    value = "a\u2028b\u2029c\x85d\re"

    record.step_intent(
        1, {"action": "fill", "value": value}, side_effect=False, target_used="name"
    )

    # Whatever reader splits the record into lines finds one line per call.
    [line] = (record.path / "steps.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["action"]["value"] == value


def test_a_step_line_that_cannot_be_written_leaves_the_file_as_it_was(
    tmp_path: Path,
) -> None:
    record = RunRecord.create(tmp_path)
    record.step_intent(0, {"action": "reload"}, side_effect=False, target_used=None)

    with pytest.raises(TypeError):
        record.step_intent(1, {"action": object()}, side_effect=False, target_used=None)

    assert [line["seq"] for line in lines(record)] == [0]


def scanning(tmp_path: Path, *secrets: tuple[str, str]) -> RunRecord:
    """A new record that scans what it writes for each fake (name, value)."""
    return RunRecord.create(tmp_path).redacting(
        Redactor(fake_secret(name, value, ORIGIN) for name, value in secrets)
    )


def test_a_document_is_redacted_before_it_is_written(tmp_path: Path) -> None:
    record = scanning(tmp_path, ("FAKE_TOKEN", "fake-token-value"))

    written = record.write(
        "logs/console.json",
        {"fake-token-value": "said fake-token-value twice: FAKE-TOKEN-VALUE"},
    )

    assert written == record.path / "logs" / "console.json"
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "[SECRET:FAKE_TOKEN]": "said [SECRET:FAKE_TOKEN] twice: [SECRET:FAKE_TOKEN]"
    }


def test_a_text_is_redacted_before_it_is_written(tmp_path: Path) -> None:
    record = scanning(tmp_path, ("FAKE_TOKEN", "fake-token-value"))

    written = record.write_text("evidence/0/a11y.yaml", "- text: fake-token-value\n")

    assert written == record.path / "evidence" / "0" / "a11y.yaml"
    assert written.read_text(encoding="utf-8") == "- text: [SECRET:FAKE_TOKEN]\n"


def test_a_value_json_escaping_would_write_is_redacted(tmp_path: Path) -> None:
    # The bound value holds a backslash and an n; the page wrote a line feed
    # there, which JSON writes as a backslash and an n.
    value = "fake\\nvalue"
    record = scanning(tmp_path, ("FAKE_ESCAPED", value))

    written = record.write("console.json", {"text": "fake\nvalue"})

    assert value not in written.read_text(encoding="utf-8")
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "text": "[SECRET:FAKE_ESCAPED]"
    }


def test_a_document_whose_scan_breaks_its_json_is_refused(tmp_path: Path) -> None:
    # The bound value spans the document's own syntax once it is written.
    value = 'fake", "b'
    record = scanning(tmp_path, ("FAKE_SPAN", value))

    with pytest.raises(RefusedWriteError, match=r"span\.json") as refused:
        record.write("span.json", {"a": "fake", "b": "x"})

    assert value not in str(refused.value)
    assert list(record.path.iterdir()) == []


@pytest.mark.parametrize(
    "secrets",
    [
        (("FAKE_MARKER_VALUE", "FAKE_MARKER_VALUE"),),
        (("FAKE", "SECRET:FAKE"),),
        (("FAKE_A", "SECRET:FAKE_B"), ("FAKE_B", "fake-b-value")),
    ],
    ids=["its-own-marker", "the-marker-literal", "another-secrets-marker"],
)
@pytest.mark.parametrize("as_text", [False, True], ids=["document", "text"])
def test_a_spelling_left_after_the_scan_refuses_the_write(
    tmp_path: Path, secrets: tuple[tuple[str, str], ...], as_text: bool
) -> None:
    record = scanning(tmp_path, *secrets)
    said = f"said {secrets[-1][1]}"
    writes: dict[bool, Callable[[], object]] = {
        True: lambda: record.write_text("left.txt", said),
        False: lambda: record.write("left.json", {"text": said}),
    }

    with pytest.raises(RefusedWriteError, match=r"left\.") as refused:
        writes[as_text]()

    assert list(record.path.iterdir()) == []
    assert [value for _, value in secrets if value in str(refused.value)] == []


@pytest.mark.parametrize(
    "name",
    [
        "../outside.json",
        "{tmp}/outside.json",
        "a/../b.json",
        "a//b.json",
        "./a.json",
        "a/",
    ],
)
def test_a_name_that_leaves_the_record_is_refused(tmp_path: Path, name: str) -> None:
    (tmp_path / "qa").mkdir()
    record = RunRecord.create(tmp_path / "qa")

    with pytest.raises(ValueError, match="isn't a name inside the record"):
        record.write(name.format(tmp=tmp_path), {"outcome": "planned"})

    assert list(record.path.iterdir()) == []
    assert [path.name for path in tmp_path.iterdir()] == ["qa"]


@pytest.mark.parametrize(
    ("linked", "refused"),
    [
        ("evidence", ValueError),
        ("part", ValueError),
        ("part-steps", ValueError),
        ("leaf", ValueError),
    ],
)
def test_a_record_writes_through_no_link(
    tmp_path: Path, linked: str, refused: type[Exception]
) -> None:
    # Links a record never follows: a directory below it, the directory of a
    # part, and a file in the way, each pointing outside the record.
    (tmp_path / "qa").mkdir()
    record = RunRecord.create(tmp_path / "qa")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    part = replace(record, path=record.path / "attempt-1")
    if linked == "evidence":
        (record.path / "evidence").symlink_to(elsewhere, target_is_directory=True)
    elif linked == "leaf":
        (record.path / "plan.json").symlink_to(elsewhere / "plan.json")
    else:
        part.path.symlink_to(elsewhere, target_is_directory=True)

    writes: dict[str, Callable[[], object]] = {
        "evidence": lambda: record.write(
            "evidence/0/console_log.json", {"entries": []}
        ),
        "leaf": lambda: record.write("plan.json", {"outcome": "planned"}),
        "part": lambda: part.write("plan.json", {"outcome": "planned"}),
        "part-steps": lambda: part.step_intent(
            0, {"action": "reload"}, side_effect=False, target_used=None
        ),
    }

    with pytest.raises(refused):
        writes[linked]()

    assert list(elsewhere.iterdir()) == []


def test_a_steps_line_holds_only_the_record_keys(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)

    record.step_intent(
        1, {"action": "click", "target": "save"}, side_effect=True, target_used="save"
    )
    record.step_completed(1, locator_used=0, settled="idle")

    raw = (record.path / "steps.jsonl").read_text(encoding="utf-8").splitlines()
    assert [sorted(json.loads(line)) for line in raw] == [
        ["action", "at", "seq", "side_effect", "state", "target_used"],
        ["at", "locator_used", "seq", "settled", "state"],
    ]


def test_a_steps_line_is_written_as_given_and_its_check_sees_the_exact_line(
    tmp_path: Path,
) -> None:
    # A target named as a bound value is one: the record scans no steps
    # line, so the compiled script's data stays as it was (ruling 5).
    record = scanning(tmp_path, ("FAKE_TARGET", "fake-target"))
    seen: list[str] = []

    record.step_intent(
        1,
        {"action": "click", "target": "fake-target"},
        side_effect=False,
        target_used="fake-target",
        check=seen.append,
    )
    record.step_completed(1, locator_used=0, settled="idle", check=seen.append)

    def refuse(_: str) -> None:
        raise ValueError("refused by the caller")

    with pytest.raises(ValueError, match="refused by the caller"):
        record.step_intent(
            2, {"action": "reload"}, side_effect=False, target_used=None, check=refuse
        )

    assert (
        seen == (record.path / "steps.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert [(line["seq"], line.get("target_used")) for line in lines(record)] == [
        (1, "fake-target"),
        (1, None),
    ]


def test_a_check_on_the_line_finds_a_spelling_its_strings_hide(tmp_path: Path) -> None:
    # Codex's case: the bound value holds a backslash and an n, the fill
    # holds a line feed there, and the line's JSON writes the bound spelling.
    redactor = Redactor([fake_secret("FAKE_ESCAPED", "fake\\nvalue", ORIGIN)])
    action = {"action": "fill", "target": "name", "value": "fake\nvalue"}
    record = RunRecord.create(tmp_path)

    def refuse_a_spelling(line: str) -> None:
        if redactor.finds(line):
            raise ValueError("a bound value's spelling is in the line")

    with pytest.raises(ValueError, match="spelling"):
        record.step_intent(
            1, action, side_effect=False, target_used="name", check=refuse_a_spelling
        )

    assert [
        text for text in ("fill", "name", "fake\nvalue") if redactor.finds(text)
    ] == []
    assert redactor.finds(journal_line({"action": action}))
    assert not (record.path / "steps.jsonl").exists()
