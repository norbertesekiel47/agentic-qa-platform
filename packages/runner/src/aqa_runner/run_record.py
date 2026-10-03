"""A local run's record (ARCHITECTURE §3.3): one directory per run, under
`.aqa/runs/` in the spec root. The spec root is in the user's repository, so
`.aqa/` holds a `.gitignore` of its own and the repository needn't list it.

#41's `aqa explore --plan-only` writes the plan and its cost records here; the
strict executor (#46) records each step's intent and completion in
`steps.jsonl`, and exploring (#53) adds its own documents."""

import json
import os
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from aqa_runner.settling import Settled

# Where a run's step intents and completions go, one JSON line each.
STEPS = "steps.jsonl"


@dataclass(frozen=True)
class RunRecord:
    """The directory one run writes what it leaves behind to."""

    run_id: str
    path: Path

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

    def write(self, name: str, document: object) -> Path:
        """Write `document` to the record as the JSON file `name`, once: a
        record keeps what a run wrote, so a second write of a name is a
        FileExistsError. The JSON is made before the file is opened, so a
        document that can't be written leaves no file behind."""
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
        written = self.path / name
        with written.open("x", encoding="utf-8") as file:
            file.write(text + "\n")
        return written

    def step_intent(
        self,
        seq: int,
        action: Mapping[str, object],
        *,
        side_effect: bool,
        target_used: str | None,
    ) -> None:
        """Record that step `seq` is about to be dispatched: its `action` as
        compiled, its `side_effect` flag and the target it acts on, as
        DATA_MODEL's `run_steps` intent row holds them (ARCHITECTURE §3.3).
        The line is on disk when this returns, so an action is dispatched
        only after its intent is; one with no completion after it is
        unresolved."""
        self._append(
            {
                "seq": seq,
                "state": "intent",
                "action": dict(action),
                "side_effect": side_effect,
                "target_used": target_used,
            }
        )

    def step_completed(
        self, seq: int, *, locator_used: int | None, settled: Settled
    ) -> None:
        """Record that step `seq` was dispatched and settled: the index of
        the locator that found its target, if it had one, and how settling
        ended. On disk when this returns."""
        self._append(
            {
                "seq": seq,
                "state": "completed",
                "locator_used": locator_used,
                "settled": settled,
            }
        )

    def _append(self, line: Mapping[str, object]) -> None:
        """Append `line`, with the time, to `STEPS` and force it to disk,
        and the directory too when the file is new. The JSON is made before
        the file is opened, so a line that can't be written leaves the file
        as it was."""
        text = json.dumps(
            {**line, "at": datetime.now(UTC).isoformat()},
            sort_keys=True,
            ensure_ascii=False,
        )
        steps = self.path / STEPS
        new = not steps.exists()
        with steps.open("a", encoding="utf-8") as file:
            file.write(text + "\n")
            file.flush()
            # https://docs.python.org/3.14/library/os.html#os.fsync
            os.fsync(file.fileno())
        if new:
            directory = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
