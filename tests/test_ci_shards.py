"""CI's test shards (ADR-0029's 2026-10-23 amendment).

Every collected test runs in exactly one shard: `.github/scripts/check_shards.py`
fails the `python` job unless the shards' records say so.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECK = REPO / ".github/scripts/check_shards.py"

COLLECTED = ["a.py::t1", "a.py::t2", "b.py::t1", "c.py::t1"]


def run(args: list[str], cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    """Run `args` outside this suite's own coverage run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    return subprocess.run(
        args, capture_output=True, text=True, cwd=cwd, env=env, timeout=120, check=False
    )


def record(
    shards: Path,
    shard: str,
    selected: list[str],
    collected: list[str] = COLLECTED,
    seconds: dict[str, float] | None = None,
) -> None:
    """A shard's record as CI downloads it: shards/<artifact>/shard-record.json."""
    artifact = shards / f"shard-{len(list(shards.glob('*')))}"
    artifact.mkdir(parents=True)
    (artifact / "shard-record.json").write_text(
        json.dumps(
            {
                "shard": shard,
                "collected": collected,
                "selected": selected,
                "seconds": seconds or {},
            }
        )
    )


def check(shards: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, str(CHECK), str(shards), *args])


def test_records_that_split_the_collection_pass(tmp_path: Path) -> None:
    record(tmp_path, "1/2", ["a.py::t1", "a.py::t2"])
    record(tmp_path, "2/2", ["b.py::t1", "c.py::t1"])

    result = check(tmp_path)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("second", "problem", "nodeid"),
    [
        (["b.py::t1"], "in no shard", "c.py::t1"),
        (["b.py::t1", "c.py::t1", "a.py::t2"], "in more than one shard", "a.py::t2"),
    ],
    ids=["no-shard", "two-shards"],
)
def test_a_test_in_no_shard_or_in_two_fails(
    tmp_path: Path, second: list[str], problem: str, nodeid: str
) -> None:
    record(tmp_path, "1/2", ["a.py::t1", "a.py::t2"])
    record(tmp_path, "2/2", second)

    result = check(tmp_path)

    assert result.returncode == 1
    assert problem in result.stderr
    assert nodeid in result.stderr


@pytest.mark.parametrize(
    ("shards", "named"),
    [
        (["1/3", "3/3"], "2/3"),
        (["1/2", "1/2", "2/2"], "1/2"),
        (["1/2", "2/3"], "2/3"),
    ],
    ids=["missing", "twice", "different-counts"],
)
def test_records_that_are_not_one_per_shard_fail(
    tmp_path: Path, shards: list[str], named: str
) -> None:
    for shard in shards:
        record(tmp_path, shard, [])

    result = check(tmp_path)

    assert result.returncode == 1
    assert named in result.stderr


def test_no_records_fail(tmp_path: Path) -> None:
    # The python job skips the download unless every shard passed.
    result = check(tmp_path / "shards")

    assert result.returncode == 1
    assert "no shard records" in result.stderr


def test_shards_that_collected_different_tests_fail(tmp_path: Path) -> None:
    record(tmp_path, "1/2", ["a.py::t1", "a.py::t2"])
    record(tmp_path, "2/2", ["b.py::t1"], collected=COLLECTED[:3])

    result = check(tmp_path)

    assert result.returncode == 1
    assert "collected different tests" in result.stderr


def test_the_records_rewrite_the_durations_table(tmp_path: Path) -> None:
    shards = tmp_path / "shards"
    record(shards, "1/2", ["a.py::t1", "a.py::t2"], seconds={"a.py": 12.34})
    record(shards, "2/2", ["b.py::t1", "c.py::t1"], seconds={"c.py": 0.04, "b.py": 3.0})
    table = tmp_path / "shard-durations.json"

    result = check(shards, "--durations", str(table))

    assert result.returncode == 0, result.stderr
    written = json.loads(table.read_text())
    assert written == {"a.py": 12.3, "b.py": 3.0, "c.py": 0.0}
    assert list(written) == ["a.py", "b.py", "c.py"]  # sorted, for readable diffs
