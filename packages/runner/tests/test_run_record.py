"""A local run's record (ARCHITECTURE §3.3; #41): a directory per run under the
spec root's `.aqa/runs/`, which tells git to ignore it."""

import errno
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest
from aqa_core.model_costs import AppliedPrices, CostRecord
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import (
    BrokenJournalError,
    PendingIntent,
    RefusedWriteError,
    RunRecord,
    UnrecordedCostError,
    journal_line,
    require_clean,
)

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


def test_text_with_no_utf_8_form_is_refused_and_leaves_no_file(tmp_path: Path) -> None:
    # A lone surrogate: Chromium sends U+FFFD in its place (measured), but a
    # record mustn't leave an empty file behind or raise anything but a refusal.
    record = RunRecord.create(tmp_path)

    with pytest.raises(RefusedWriteError, match=r"console\.json"):
        record.write("console.json", {"text": "\ud800"})

    assert list(record.path.iterdir()) == []


def test_a_part_keeps_its_records_run_id_and_fields(tmp_path: Path) -> None:
    record = scanning(tmp_path, ("FAKE_TOKEN", "fake-token-value"))

    part = record.part("attempt-1")
    written = part.write("turn.json", {"text": "fake-token-value"})

    assert part.run_id == record.run_id
    assert part.redactor is record.redactor
    assert part.path == record.path / "attempt-1"
    assert written == record.path / "attempt-1" / "turn.json"
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "text": "[SECRET:FAKE_TOKEN]"
    }


def test_a_part_whose_directory_is_a_link_is_refused(tmp_path: Path) -> None:
    (tmp_path / "qa").mkdir()
    record = RunRecord.create(tmp_path / "qa")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (record.path / "attempt-1").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ValueError, match="a link"):
        record.part("attempt-1")

    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("name", ["attempt-2", "../attempt-1", "evidence", ""])
def test_a_part_is_made_once_and_only_under_its_names(
    tmp_path: Path, name: str
) -> None:
    record = RunRecord.create(tmp_path)
    record.part("confirmation")

    with pytest.raises(ValueError, match="isn't a part"):
        record.part(name)
    with pytest.raises(FileExistsError):
        record.part("confirmation")

    assert [path.name for path in record.path.iterdir()] == ["confirmation"]


def click(seq: int) -> dict[str, object]:
    return {"action": "click", "target": f"t{seq}"}


def test_unresolved_lists_pending_intents_with_their_side_effect(
    tmp_path: Path,
) -> None:
    record = RunRecord.create(tmp_path)
    record.step_intent(0, {"action": "reload"}, side_effect=False, target_used=None)
    record.step_intent(1, click(1), side_effect=True, target_used="t1")
    record.step_intent(2, click(2), side_effect=True, target_used="t2")
    record.step_completed(1, locator_used=0, settled="idle")

    assert record.unresolved() == (
        PendingIntent(seq=0, side_effect=False),
        PendingIntent(seq=2, side_effect=True),
    )

    record.step_completed(2, locator_used=0, settled="idle")
    record.step_completed(0, locator_used=None, settled="timeout")

    assert record.unresolved() == ()


def test_a_record_at_another_path_keeps_intents_of_its_own(tmp_path: Path) -> None:
    # Copied with `dataclasses.replace` into another directory, as #50 B's
    # tests make a part by hand: other files, so other intents.
    record = RunRecord.create(tmp_path)
    record.step_intent(1, click(1), side_effect=True, target_used="t1")
    elsewhere = replace(record, path=record.path / "attempt-1")

    elsewhere.step_intent(1, click(1), side_effect=False, target_used="t1")
    elsewhere.step_completed(1, locator_used=0, settled="idle")

    assert record.unresolved() == (PendingIntent(seq=1, side_effect=True),)
    assert elsewhere.unresolved() == ()


def test_a_rescanned_record_keeps_its_pending_intents(tmp_path: Path) -> None:
    # Replay scans the record it is given with its own redactor
    # (`executor.replay`), and explore then asks the part it gave.
    part = RunRecord.create(tmp_path).part("confirmation")

    part.redacting(Redactor([])).step_intent(
        1, click(1), side_effect=True, target_used="t1"
    )

    assert part.unresolved() == (PendingIntent(seq=1, side_effect=True),)


def test_an_intent_for_a_seq_still_pending_is_refused(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)
    record.step_intent(1, click(1), side_effect=True, target_used="t1")

    with pytest.raises(ValueError, match="step 1"):
        record.step_intent(1, click(1), side_effect=False, target_used="t1")

    assert [(line["seq"], line["side_effect"]) for line in lines(record)] == [(1, True)]
    assert record.unresolved() == (PendingIntent(seq=1, side_effect=True),)


def forced(monkeypatch: pytest.MonkeyPatch, file: Path) -> list[tuple[int, str]]:
    """Record each fsync from now: what it forced, by inode, and what
    `file` held then."""
    synced: list[tuple[int, str]] = []
    fsync = os.fsync

    def recording(fd: int) -> None:
        synced.append((os.fstat(fd).st_ino, file.read_text() if file.exists() else ""))
        fsync(fd)

    monkeypatch.setattr(os, "fsync", recording)
    return synced


def fail_fsync(monkeypatch: pytest.MonkeyPatch, at: int = 0) -> list[int]:
    """Make the `at`-th fsync from now raise EIO; return the calls seen."""
    calls: list[int] = []
    fsync = os.fsync

    def failing(fd: int) -> None:
        calls.append(fd)
        if len(calls) == at + 1:
            raise OSError(errno.EIO, "fake fsync failure")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", failing)
    return calls


def refuse(_: str) -> None:
    raise RefusedWriteError("refused by the caller")


def test_a_failed_intent_write_leaves_nothing_pending_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refused one too (`test_the_check_runs_on_the_exact_journal_line_wrapper_fields_included`).
    record = RunRecord.create(tmp_path)
    fail_fsync(monkeypatch)

    with pytest.raises(OSError, match="fake fsync failure"):
        record.step_intent(1, click(1), side_effect=True, target_used="t1")

    assert record.unresolved() == ()


@pytest.mark.parametrize(
    "made",
    [lambda record: record, lambda record: RunRecord(record.run_id, record.path)],
    ids=["created", "made-directly"],
)
def test_each_part_has_its_own_steps_and_forces_every_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    made: Callable[[RunRecord], RunRecord],
) -> None:
    record = made(RunRecord.create(tmp_path))
    attempt, confirmation = record.part("attempt-1"), record.part("confirmation")
    synced = forced(monkeypatch, attempt.path / "steps.jsonl")

    attempt.step_intent(1, click(1), side_effect=True, target_used="t1")
    confirmation.step_intent(
        0, {"action": "reload"}, side_effect=False, target_used=None
    )

    # A part's first line is on disk only once every directory from its
    # own up to the spec root is: the part's entry in the run's is new.
    chain = [attempt.path, record.path, record.path.parent, tmp_path / ".aqa", tmp_path]
    assert [ino for ino, _ in synced[:6]] == [
        path.stat().st_ino for path in (attempt.path / "steps.jsonl", *chain)
    ]
    assert [line["seq"] for line in lines(attempt)] == [1]
    assert [line["seq"] for line in lines(confirmation)] == [0]
    assert not (record.path / "steps.jsonl").exists()
    assert attempt.unresolved() == (PendingIntent(seq=1, side_effect=True),)
    assert confirmation.unresolved() == (PendingIntent(seq=0, side_effect=False),)


class _Interrupted(BaseException):
    """What stops a write without being an OSError, as a signal can."""


class _Cut:
    """An open journal whose next write puts half its text on disk, then
    fails as a full disk does, or with `error`."""

    def __init__(self, file: Any, error: BaseException) -> None:
        self.file, self.error = file, error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *raised: object) -> None:
        self.file.close()

    def write(self, text: str) -> int:
        self.file.write(text[: len(text) // 2])
        self.file.flush()
        raise self.error


def cut_the_next_line(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    opening = Path.open

    def cutting(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        file = opening(path, mode, *args, **kwargs)
        return _Cut(file, error) if mode == "a" else file

    monkeypatch.setattr(Path, "open", cutting)


def test_a_completion_cut_mid_line_leaves_its_intent_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RunRecord.create(tmp_path)
    record.step_intent(1, click(1), side_effect=True, target_used="t1")
    cut_the_next_line(monkeypatch, OSError(errno.ENOSPC, "fake full disk"))

    with pytest.raises(OSError, match="fake full disk"):
        record.step_completed(1, locator_used=0, settled="idle")

    last = (record.path / "steps.jsonl").read_text().splitlines()[-1]
    with pytest.raises(ValueError, match="Unterminated string"):
        json.loads(last)
    assert record.unresolved() == (PendingIntent(seq=1, side_effect=True),)


@pytest.mark.parametrize(
    "error",
    [OSError(errno.ENOSPC, "fake full disk"), _Interrupted("fake interruption")],
    ids=["full-disk", "interrupted"],
)
def test_a_journal_cut_mid_intent_takes_no_further_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    record = RunRecord.create(tmp_path)
    steps = record.path / "steps.jsonl"
    cut_the_next_line(monkeypatch, error)
    with pytest.raises(type(error), match="fake"):
        record.step_intent(1, click(1), side_effect=True, target_used="t1")
    monkeypatch.undo()
    cut = steps.read_bytes()

    # The next line would follow the cut one on the same line.
    with pytest.raises(BrokenJournalError, match=r"steps\.jsonl"):
        record.step_intent(2, click(2), side_effect=True, target_used="t2")

    assert steps.read_bytes() == cut
    assert record.unresolved() == ()


@pytest.mark.parametrize(
    "at", [0, 1, 2, 3, 4], ids=["file", "run", "runs", "aqa", "root"]
)
def test_a_journal_whose_entry_was_not_forced_takes_no_further_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at: int
) -> None:
    # The first line's file, then each directory up to the spec root, is
    # forced once (`test_each_step_line_is_on_disk_before_the_call_returns`);
    # a later line would find the file there and force none of them.
    record = RunRecord.create(tmp_path)
    steps = record.path / "steps.jsonl"
    fail_fsync(monkeypatch, at)
    with pytest.raises(OSError, match="fake fsync failure"):
        record.step_intent(1, click(1), side_effect=True, target_used="t1")
    monkeypatch.undo()
    written = steps.read_bytes()

    # Its copies write the same file, so they refuse too.
    with pytest.raises(BrokenJournalError):
        record.redacting(Redactor([])).step_intent(
            2, click(2), side_effect=True, target_used="t2"
        )
    with pytest.raises(BrokenJournalError):
        record.step_completed(1, locator_used=0, settled="idle")

    assert steps.read_bytes() == written
    assert record.unresolved() == ()


def test_a_completion_whose_fsync_fails_leaves_its_intent_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RunRecord.create(tmp_path)
    record.step_intent(1, click(1), side_effect=True, target_used="t1")
    fail_fsync(monkeypatch)

    with pytest.raises(OSError, match="fake fsync failure"):
        record.step_completed(1, locator_used=0, settled="idle")
    monkeypatch.undo()

    # Its line may be on disk; the record still counts the step unresolved
    # and writes nothing more after it.
    assert record.unresolved() == (PendingIntent(seq=1, side_effect=True),)
    with pytest.raises(BrokenJournalError):
        record.step_intent(2, click(2), side_effect=True, target_used="t2")


def priced(input_tokens: int) -> CostRecord:
    """A fake priced call."""
    return CostRecord(
        role="navigator",
        mode="explore",
        provider="anthropic",
        model="fake-model",
        input_tokens=input_tokens,
        cached_input_tokens=0,
        output_tokens=7,
        latency_ms=1234,
        price_map_version="fake-map-version",
        price_source="map",
        applied_prices=AppliedPrices(
            input_usd_per_mtok=Decimal(2),
            output_usd_per_mtok=Decimal(10),
            cached_input_usd_per_mtok=Decimal("0.2"),
        ),
        cost_usd=Decimal("0.000123"),
        status="ok",
    )


def test_each_cost_line_is_on_disk_when_cost_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RunRecord.create(tmp_path)
    costs = record.path / "costs.jsonl"
    synced = forced(monkeypatch, costs)

    record.cost(priced(100))
    first = list(synced)
    record.cost(priced(200))

    chain = (costs, record.path, record.path.parent, tmp_path / ".aqa", tmp_path)
    assert [ino for ino, _ in first] == [path.stat().st_ino for path in chain]
    assert synced[len(first) :] == [(costs.stat().st_ino, costs.read_text())]
    # Each line is the call, and nothing else, so it reads back as one.
    assert [
        CostRecord.model_validate_json(line) for line in synced[-1][1].splitlines()
    ] == [priced(100), priced(200)]
    assert first[0][1].count("\n") == 1


def fail_the_check(_: str) -> None:
    raise RuntimeError("a check that broke")


@pytest.mark.parametrize(
    ("check", "cause"),
    [(refuse, RefusedWriteError), (fail_the_check, RuntimeError)],
    ids=["refused", "broken-check"],
)
def test_a_cost_line_refused_by_its_check_raises_with_the_call_and_leaves_the_file(
    tmp_path: Path, check: Callable[[str], None], cause: type[Exception]
) -> None:
    record = RunRecord.create(tmp_path)
    costs = record.path / "costs.jsonl"
    record.cost(priced(100))
    before = costs.read_bytes()
    call = priced(200)

    with pytest.raises(UnrecordedCostError) as raised:
        record.cost(call, check=check)

    # The call is kept for the caller, never put in what the error says.
    assert raised.value.call is call
    assert raised.value.args == (
        "costs.jsonl: a priced call's cost line isn't on disk",
    )
    assert isinstance(raised.value.__cause__, cause)
    assert costs.read_bytes() == before
    # A refusal leaves the journal open.
    record.cost(priced(300))
    assert len(costs.read_text().splitlines()) == 2


def test_a_cost_line_whose_write_failed_raises_with_the_call_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RunRecord.create(tmp_path)
    costs = record.path / "costs.jsonl"
    call = priced(100)
    calls = fail_fsync(monkeypatch)

    # The line reached the file, but whether it is on disk is unknown.
    with pytest.raises(UnrecordedCostError) as raised:
        record.cost(call)
    monkeypatch.undo()
    written = costs.read_bytes()
    with pytest.raises(UnrecordedCostError) as again:
        record.cost(priced(200))

    assert raised.value.call is call
    assert isinstance(raised.value.__cause__, OSError)
    assert len(calls) == 1
    assert written.count(b"\n") == 1
    assert isinstance(again.value.__cause__, BrokenJournalError)
    assert costs.read_bytes() == written


@pytest.mark.parametrize(
    ("bound", "action", "target"),
    [
        (
            "fake\\nvalue",
            {"action": "fill", "target": "t", "value": "fake\nvalue"},
            "t",
        ),
        ("fake-target-value", {"action": "click", "target": "t"}, "fake-target-value"),
    ],
    ids=["escaped-in-the-action", "in-a-wrapper-field"],
)
def test_the_check_runs_on_the_exact_journal_line_wrapper_fields_included(
    tmp_path: Path, bound: str, action: dict[str, object], target: str
) -> None:
    # Neither the action's strings nor its target spell the value: the
    # first only once JSON escapes its line feed, the second only in the
    # field the record wraps the action in.
    part = scanning(tmp_path, ("FAKE_VALUE", bound)).part("attempt-1")

    with pytest.raises(RefusedWriteError, match="attempt-1 step 1"):
        part.step_intent(
            1,
            action,
            side_effect=True,
            target_used=target,
            check=require_clean(part.redactor, "attempt-1 step 1"),
        )

    assert not (part.path / "steps.jsonl").exists()
    assert part.unresolved() == ()


@pytest.mark.parametrize("journal", ["steps", "costs"])
def test_a_refused_line_leaves_the_journal_unchanged(
    tmp_path: Path, journal: str
) -> None:
    record = scanning(tmp_path, ("FAKE_SECRET", "fake-secret-model"))
    check = require_clean(record.redactor, journal)

    def append(seq: int, value: str) -> None:
        if journal == "steps":
            fill = {"action": "fill", "target": "t", "value": value}
            record.step_intent(
                seq, fill, side_effect=True, target_used="t", check=check
            )
        else:
            record.cost(priced(seq).model_copy(update={"model": value}), check=check)

    append(1, "fake-model")
    before = (record.path / f"{journal}.jsonl").read_bytes()
    with pytest.raises((RefusedWriteError, UnrecordedCostError)):
        append(2, "fake-secret-model")
    unchanged = (record.path / f"{journal}.jsonl").read_bytes()
    append(3, "fake-model")

    assert unchanged == before
    assert len((record.path / f"{journal}.jsonl").read_text().splitlines()) == 2
    assert record.unresolved() == (
        (PendingIntent(1, True), PendingIntent(3, True)) if journal == "steps" else ()
    )


def test_every_document_is_checked_as_the_exact_text_written(tmp_path: Path) -> None:
    record = scanning(tmp_path, ("FAKE_ESCAPED", "fake\\nvalue"))
    seen: list[str] = []

    written = record.write("explore.json", {"note": "fake value"}, check=seen.append)
    # Unchecked, the record would write this one with its spelling replaced
    # (`test_a_value_json_escaping_would_write_is_redacted`).
    with pytest.raises(RefusedWriteError, match=r"turns/1\.json"):
        record.write(
            "turns/1.json",
            {"text": "fake\nvalue"},
            check=require_clean(record.redactor, "turns/1.json"),
        )

    assert [text + "\n" for text in seen] == [written.read_text(encoding="utf-8")]
    assert [path.name for path in record.path.iterdir()] == ["explore.json"]


def test_a_checked_document_is_written_exactly_as_its_check_saw_it(
    tmp_path: Path,
) -> None:
    # A check that passes everything: the record may refuse what it saw,
    # but never writes anything else in its place.
    record = scanning(tmp_path, ("FAKE_TOKEN", "fake-token-value"))
    seen: list[str] = []

    with pytest.raises(RefusedWriteError, match=r"turn\.json"):
        record.write("turn.json", {"text": "said fake-token-value"}, check=seen.append)

    assert [json.loads(text) for text in seen] == [{"text": "said fake-token-value"}]
    assert list(record.path.iterdir()) == []


def test_require_clean_names_the_place_and_never_echoes_the_value() -> None:
    redactor = Redactor([fake_secret("FAKE_TOKEN", "fake-token-value", ORIGIN)])
    check = require_clean(redactor, "turns/3.json")

    with pytest.raises(RefusedWriteError) as refused:
        check("said FAKE-TOKEN-VALUE")

    assert check("said nothing") is None
    assert str(refused.value) == (
        "turns/3.json: a bound value's spelling is in it, so it isn't written"
    )


def test_a_secret_in_a_key_is_found(tmp_path: Path) -> None:
    record = scanning(tmp_path, ("FAKE_TOKEN", "fake-token-value"))

    with pytest.raises(RefusedWriteError, match="step 1"):
        record.step_intent(
            1,
            {"action": "fill", "fake-token-value": "x"},
            side_effect=True,
            target_used=None,
            check=require_clean(record.redactor, "step 1"),
        )

    assert not (record.path / "steps.jsonl").exists()
