"""A local run's record (ARCHITECTURE §3.3): one directory per run, under
`.aqa/runs/` in the spec root. The spec root is in the user's repository, so
`.aqa/` holds a `.gitignore` of its own and the repository needn't list it.

#41's `aqa explore --plan-only` writes the plan and its cost records here; the
strict executor (#46) records each step's intent and completion in
`steps.jsonl` and each step's evidence (`aqa_runner.evidence`), and
exploring (#53) adds its own documents, a line in `costs.jsonl` per priced
model call, and a part per phase, a record of its own (ADR-0024's #53 P5
amendment).

Every file but the two journals, `steps.jsonl` and `costs.jsonl`, is
scanned as it is written, and refused while a bound value's supported
spelling is left; a journal line, and a document written with a check, is
checked by its caller as the exact text written. A record trusts the
directories above its own and writes through no link below it (ADR-0026's
#50 B amendment)."""

import json
import os
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from aqa_core.model_costs import CostRecord

from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.settling import Settled

# Where a run's step intents and completions go, one JSON line each.
STEPS = "steps.jsonl"

# Where explore keeps the cost record of each priced model call, one JSON
# line each (#53).
COSTS = "costs.jsonl"

# The parts an explored run keeps its phases in, each a record of its own.
PARTS = ("attempt-1", "confirmation")

# What a caller checks a journal line or a document with before it is
# written: whatever it raises leaves the file as it was. What it raises
# mustn't quote the text, as `require_clean`'s doesn't.
type LineCheck = Callable[[str], object]


class RefusedWriteError(ValueError):
    """What a record won't write: text in which a supported spelling of a
    bound value is left after its scan, a document whose scan broke its
    JSON, text with no UTF-8 form, or text `require_clean` refused. The
    message names the file or the caller's place, never the value, and
    nothing is written."""


class BrokenJournalError(OSError):
    """A journal an earlier write to failed: what reached its file may be
    a cut line, or a line in a file whose entry isn't on disk, so nothing
    more is appended to it."""


class UnrecordedCostError(Exception):
    """A priced call whose cost line isn't on disk: its check refused the
    line, or writing it failed, perhaps after part of it reached the file;
    neither a `ValueError` nor an `OSError`, since it stands for both.
    `record` keeps the call in memory; the message never holds it, since a
    refused line may spell a bound value."""

    def __init__(self, record: CostRecord) -> None:
        super().__init__(f"{COSTS}: a priced call's cost line isn't on disk")
        self.record = record


@dataclass(frozen=True)
class PendingIntent:
    """A step whose intent a record wrote with no completion after it."""

    seq: int
    side_effect: bool


@dataclass
class _Journals:
    """What a record holds of its journals in memory, shared by every copy
    `redacting` makes of it, since they write the same files: each step
    whose intent is on disk with no completion yet, by seq, with its
    `side_effect` flag, and each journal a write to failed."""

    pending: dict[int, bool] = field(default_factory=dict)
    broken: set[str] = field(default_factory=set)


def journal_line(entry: Mapping[str, object]) -> str:
    """The exact text of a journal's line for `entry`, its newline aside:
    JSON with sorted keys, in ASCII, so no character of a value, such as
    U+2028, can end the line for a reader. A record never scans it, so the
    compiled script's data a steps line holds stays executable; a caller
    that must keep bound values out of it checks the line through `check`."""
    return json.dumps(entry, sort_keys=True)


def require_clean(redactor: Redactor, place: str) -> LineCheck:
    """A check that refuses text holding any spelling of a value `redactor`
    binds, with a `RefusedWriteError` naming `place`, never the value."""

    def check(text: str) -> None:
        if redactor.finds(text):
            raise RefusedWriteError(
                f"{place}: a bound value's spelling is in it, so it isn't written"
            )

    return check


@dataclass(frozen=True)
class RunRecord:
    """The directory one run writes what it leaves behind to, and the scan
    of every secret the run binds (`NO_SECRETS` until `redacting`). A
    record made from this one with `dataclasses.replace`, such as a part
    in a directory below it, keeps the scan, and a copy of the same record,
    such as `redacting` makes, shares its journals' state."""

    run_id: str
    path: Path
    redactor: Redactor = NO_SECRETS
    _journals: _Journals = field(
        default_factory=_Journals, kw_only=True, repr=False, compare=False
    )
    # How many directories above this record's own a new journal forces:
    # `runs`, `.aqa` and the spec root, which existed before the run
    # (`create`), and the run's own for a part.
    _above: int = field(default=3, kw_only=True, repr=False, compare=False)

    @classmethod
    def create(cls, base: Path) -> Self:
        """A new run's record: `base/.aqa/runs/<run_id>`, where `run_id` is a
        UUIDv7 (time-ordered, DATA_MODEL's internal IDs). `base/.aqa/` ignores
        itself: its `.gitignore` is `*`, written if it is missing and never
        overwritten. A `.aqa` or `.aqa/runs` that is a link is refused: a
        repository can commit one pointing anywhere, and a record stays in its
        spec root."""
        records = base / ".aqa"
        for place in (records, records / "runs"):
            if place.is_symlink():
                raise ValueError(f"{place}: a link, and a run record stays in {base}")
        records.mkdir(exist_ok=True)
        # Another run may write it first; either way the user's own file stays.
        with suppress(FileExistsError), (records / ".gitignore").open("x") as ignore:
            ignore.write("*\n")
        run_id = str(uuid.uuid7())
        path = records / "runs" / run_id
        path.mkdir(parents=True)
        return cls(run_id, path)

    def redacting(self, redactor: Redactor) -> Self:
        """This record, scanning what it writes with `redactor`."""
        return replace(self, redactor=redactor)

    def part(self, name: str) -> Self:
        """A part of this run (#53): a record in the new directory `name`,
        one of `PARTS`, with this record's `run_id` and scan. The directory
        is made here and only once; a link in its place is refused."""
        if name not in PARTS:
            raise ValueError(f"{name!r} isn't a part of a run record")
        place = self._place(name)
        place.mkdir()
        return replace(self, path=place, _journals=_Journals(), _above=self._above + 1)

    def unresolved(self) -> tuple[PendingIntent, ...]:
        """Each step whose intent this record wrote with no completion
        written after it, by seq. Kept in memory, never read back from the
        journal: a completion whose write failed leaves its step here."""
        return tuple(
            PendingIntent(seq, side_effect)
            for seq, side_effect in sorted(self._journals.pending.items())
        )

    def write(
        self, name: str, document: object, *, check: LineCheck | None = None
    ) -> Path:
        """Write `document` to the record as the JSON file `name`, once: a
        record keeps what a run wrote, so a second write of a name is a
        FileExistsError. `name` is a path inside the record, its directories
        made as needed. The JSON is made and scanned before the file is
        opened, so a document that can't be written leaves no file behind.

        The scan runs over the JSON as written, so it also finds a value
        that JSON's escapes spell (a line feed written as a backslash and an
        n); a scan that breaks the JSON is refused.

        With a `check`, the JSON goes to `check` instead of the scan, and
        is written as `check` saw it, with a final newline, or not at all."""
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
        if check is not None:
            check(text)
            return self._create(name, text + "\n")
        scanned = self.redactor.redact(text)
        if scanned != text:
            try:
                json.loads(scanned)
            except ValueError:
                raise RefusedWriteError(
                    f"{name}: a bound value spans its JSON, so it isn't written"
                ) from None
        return self._create(name, scanned + "\n")

    def write_text(self, name: str, text: str) -> Path:
        """Write `text`, scanned, to the record as the file `name`, once, as
        `write` writes a document."""
        return self._create(name, self.redactor.redact(text))

    def step_intent(
        self,
        seq: int,
        action: Mapping[str, object],
        *,
        side_effect: bool,
        target_used: str | None,
        check: LineCheck | None = None,
    ) -> None:
        """Record that step `seq` is about to be dispatched: its `action` as
        compiled, its `side_effect` flag and the target it acts on, as
        DATA_MODEL's `run_steps` intent row holds them (ARCHITECTURE §3.3).
        The line is on disk when this returns, so an action is dispatched
        only after its intent is; one with no completion after it is
        unresolved until its completion is on disk. `check` gets the exact
        line first. A step whose intent is still unresolved can't have
        another."""
        if seq in self._journals.pending:
            raise ValueError(f"step {seq}'s intent is still unresolved")
        self._append(
            STEPS,
            {
                "seq": seq,
                "state": "intent",
                "action": dict(action),
                "side_effect": side_effect,
                "target_used": target_used,
                "at": datetime.now(UTC).isoformat(),
            },
            check,
        )
        self._journals.pending[seq] = side_effect

    def step_completed(
        self,
        seq: int,
        *,
        locator_used: int | None,
        settled: Settled,
        check: LineCheck | None = None,
    ) -> None:
        """Record that step `seq` was dispatched and settled: the index of
        the locator that found its target, if it had one, and how settling
        ended. On disk when this returns. `check` gets the exact line first."""
        self._append(
            STEPS,
            {
                "seq": seq,
                "state": "completed",
                "locator_used": locator_used,
                "settled": settled,
                "at": datetime.now(UTC).isoformat(),
            },
            check,
        )
        self._journals.pending.pop(seq, None)

    def cost(self, call: CostRecord, *, check: LineCheck | None = None) -> None:
        """Record a priced model call in `COSTS`: its fields, and no time,
        so its line reads back as the `CostRecord`. On disk when this
        returns. `check` gets the exact line first. A refused line, or a
        write that failed, raises `UnrecordedCostError` with `call`; the
        line is never tried again."""
        try:
            self._append(COSTS, call.model_dump(mode="json"), check)
        except Exception as error:
            # Whatever stopped it (a check refuses by raising anything), the
            # billed call has no line to count, and its caller needs the call.
            raise UnrecordedCostError(call) from error

    def _create(self, name: str, text: str) -> Path:
        """Write `text`, checked and encoded before the file opens, to the new
        file `name`."""
        if self.redactor.finds(text):
            raise RefusedWriteError(
                f"{name}: a bound value's spelling is left after its scan, so it "
                "isn't written"
            )
        try:
            data = text.encode()
        except UnicodeEncodeError:
            raise RefusedWriteError(f"{name}: text with no UTF-8 form") from None
        written = self._place(name)
        with written.open("xb") as file:
            file.write(data)
        return written

    def _place(self, name: str) -> Path:
        """Where `name` goes in the record: a relative path of names, none
        empty, `.` or `..`. The record's directory and each directory below
        it on the way there are made if missing; none of them, and not the
        file, may be a link."""
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"{name!r} isn't a name inside the record")
        places = [self.path]
        for part in parts:
            places.append(places[-1] / part)
        for place in places:
            if place.is_symlink():
                raise ValueError(f"{place}: a link, and a record writes only in it")
            if place != places[-1]:
                place.mkdir(exist_ok=True)
        return places[-1]

    def _append(
        self, name: str, line: Mapping[str, object], check: LineCheck | None
    ) -> None:
        """Append `line` to the journal `name` and force it to disk, once
        `check`, if any, has passed the exact text. When the file is new,
        every directory from the record's up to the spec root is forced
        too: a new entry is on disk only once its directory is. The line is
        made before the file is opened, so one that can't be made, or that
        `check` refuses, leaves the file as it was and the journal open.
        A write that fails once it began breaks the journal for good
        (`BrokenJournalError`), for every copy of this record."""
        if name in self._journals.broken:
            raise BrokenJournalError(
                f"{name}: an earlier write to it failed, so nothing more is appended"
            )
        text = journal_line(line)
        if check is not None:
            check(text)
        journal = self._place(name)
        new = not journal.exists()
        try:
            with journal.open("a", encoding="utf-8") as file:
                file.write(text + "\n")
                file.flush()
                # https://docs.python.org/3.14/library/os.html#os.fsync
                os.fsync(file.fileno())
            if new:
                for directory in (self.path, *self.path.parents[: self._above]):
                    handle = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(handle)
                    finally:
                        os.close(handle)
        except OSError:
            self._journals.broken.add(name)
            raise
