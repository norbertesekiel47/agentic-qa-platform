"""A local run's record (ARCHITECTURE §3.3): one directory per run, under
`.aqa/runs/` in the spec root. The spec root is in the user's repository, so
`.aqa/` holds a `.gitignore` of its own and the repository needn't list it.

#41's `aqa explore --plan-only` writes the plan and its cost records here; the
strict executor (#46) records each step's intent and completion in
`steps.jsonl` and each step's evidence (`aqa_runner.evidence`), and
exploring (#53) adds its own documents.

Every file but `steps.jsonl` is scanned as it is written, and refused while
a bound value's supported spelling is left; a record trusts the directories
above its own and writes through no link below it (ADR-0026's #50 B
amendment)."""

import json
import os
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.settling import Settled

# Where a run's step intents and completions go, one JSON line each.
STEPS = "steps.jsonl"

# What a caller checks a steps line with before it is written: whatever it
# raises leaves the file as it was.
type LineCheck = Callable[[str], object]


class RefusedWriteError(ValueError):
    """What a record won't write: text in which a supported spelling of a
    bound value is left after its scan, a document whose scan broke its
    JSON, or text with no UTF-8 form. The message names the file, never the value, and no file is
    made."""


def journal_line(entry: Mapping[str, object]) -> str:
    """The exact text of the `STEPS` line for `entry`, its newline aside:
    JSON with sorted keys, in ASCII, so no character of a value, such as
    U+2028, can end the line for a reader. A record never scans it, so the
    compiled script's data it holds stays executable; a caller that must
    keep bound values out of it checks the line through a step's `check`."""
    return json.dumps(entry, sort_keys=True)


@dataclass(frozen=True)
class RunRecord:
    """The directory one run writes what it leaves behind to, and the scan
    of every secret the run binds (`NO_SECRETS` until `redacting`). A
    record made from this one with `dataclasses.replace`, such as a part
    in a directory below it, keeps the scan."""

    run_id: str
    path: Path
    redactor: Redactor = NO_SECRETS

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

    def write(self, name: str, document: object) -> Path:
        """Write `document` to the record as the JSON file `name`, once: a
        record keeps what a run wrote, so a second write of a name is a
        FileExistsError. `name` is a path inside the record, its directories
        made as needed. The JSON is made and scanned before the file is
        opened, so a document that can't be written leaves no file behind.

        The scan runs over the JSON as written, so it also finds a value
        that JSON's escapes spell (a line feed written as a backslash and an
        n); a scan that breaks the JSON is refused."""
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
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
        unresolved. `check` gets the exact line first."""
        self._append(
            {
                "seq": seq,
                "state": "intent",
                "action": dict(action),
                "side_effect": side_effect,
                "target_used": target_used,
            },
            check,
        )

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
            {
                "seq": seq,
                "state": "completed",
                "locator_used": locator_used,
                "settled": settled,
            },
            check,
        )

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

    def _append(self, line: Mapping[str, object], check: LineCheck | None) -> None:
        """Append `line`, with the time, to `STEPS` and force it to disk,
        once `check`, if any, has passed the exact text. When the file is
        new, every directory from the run's up to the spec root, which
        existed before the run, is forced too: a new entry is on disk only
        once its directory is. The line is made before the file is opened,
        so one that can't be written leaves the file as it was."""
        text = journal_line({**line, "at": datetime.now(UTC).isoformat()})
        if check is not None:
            check(text)
        steps = self._place(STEPS)
        new = not steps.exists()
        with steps.open("a", encoding="utf-8") as file:
            file.write(text + "\n")
            file.flush()
            # https://docs.python.org/3.14/library/os.html#os.fsync
            os.fsync(file.fileno())
        if new:
            # The run's directory, `runs`, `.aqa` and the spec root (`create`).
            for directory in (self.path, *self.path.parents[:3]):
                handle = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
