"""A local run's record (ARCHITECTURE §3.3): one directory per run, under
`.aqa/runs/` in the spec root. The spec root is in the user's repository, so
`.aqa/` holds a `.gitignore` of its own and the repository needn't list it.

#41's `aqa explore --plan-only` writes the plan and its cost records here; the
strict executor (#46) and exploring (#53) add their own documents."""

import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self


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
        overwritten."""
        records = base / ".aqa"
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
        FileExistsError."""
        written = self.path / name
        with written.open("x", encoding="utf-8") as file:
            json.dump(document, file, indent=2, sort_keys=True, ensure_ascii=False)
            file.write("\n")
        return written
