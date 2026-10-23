"""Fail unless every collected test ran in exactly one of CI's test shards.

Each `tests` job in .github/workflows/ci.yml writes a record with the root
conftest.py's `--shard-record`: its shard (K/N), every test it collected, the
ones it ran, and each of its files' seconds. The `python` job downloads the
records into DIR, one directory per shard, and runs this (ADR-0029's
2026-10-23 amendment). It downloads nothing unless every shard passed, so no
records fails too.

--durations rewrites .github/shard-durations.json, which balances the shards,
from the records of a main run: gh run download RUN --pattern 'shard-*' --dir DIR

Run: python3 .github/scripts/check_shards.py DIR [--durations FILE]
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import TypedDict


class Record(TypedDict):
    shard: str
    collected: list[str]
    selected: list[str]
    seconds: dict[str, float]


def problems(records: list[Record], where: Path) -> list[str]:
    """What keeps `records` from running each collected test exactly once."""
    if not records:
        return [
            (
                f"no shard records under {where}: a tests job failed, was cancelled "
                "or was skipped, so the python job downloaded none"
            )
        ]
    shards = Counter(record["shard"] for record in records)
    counts = {shard.split("/")[1] for shard in shards}
    if len(counts) > 1:
        return [f"the records disagree on the shard count: {', '.join(sorted(shards))}"]
    count = int(counts.pop())
    expected = [f"{k}/{count}" for k in range(1, count + 1)]
    found = [
        f"no record for shard {shard}" for shard in expected if shard not in shards
    ]
    found += [f"{n} records for shard {shard}" for shard, n in shards.items() if n > 1]
    first, *rest = records
    found += [
        f"shard {record['shard']} collected different tests from shard {first['shard']}"
        for record in rest
        if record["collected"] != first["collected"]
    ]
    if found:
        return found
    ran = Counter(nodeid for record in records for nodeid in record["selected"])
    return [
        f"in no shard: {nodeid}"
        if ran[nodeid] == 0
        else f"in more than one shard: {nodeid}"
        for nodeid in first["collected"]
        if ran[nodeid] != 1
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "shards", type=Path, help="the shards' records, one directory each"
    )
    parser.add_argument(
        "--durations", type=Path, help="write the records' per-file seconds here"
    )
    args = parser.parse_args()
    records: list[Record] = [
        json.loads(path.read_text())
        for path in sorted(args.shards.rglob("shard-record.json"))
    ]
    if found := problems(records, args.shards):
        sys.exit("\n".join(found))
    if args.durations:
        seconds = {f: s for record in records for f, s in record["seconds"].items()}
        table = {f: round(s, 1) for f, s in sorted(seconds.items())}
        args.durations.write_text(json.dumps(table, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
