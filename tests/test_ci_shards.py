"""CI's test shards (ADR-0029's 2026-10-23 amendment).

The root conftest.py's `--shard K/N` gives each shard whole test files, balanced
on the seconds in `.github/shard-durations.json`. Every collected test runs in
exactly one shard: `.github/scripts/check_shards.py` fails the `python` job
unless the shards' records say so.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECK = REPO / ".github/scripts/check_shards.py"
TEST = "def test_{i}() -> None:\n    assert len('ab') == 2\n\n\n"

COLLECTED = ["a.py::t1", "a.py::t2", "b.py::t1", "c.py::t1"]


def run(args: list[str], cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    """Run `args` outside this suite's own coverage run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    return subprocess.run(
        args, capture_output=True, text=True, cwd=cwd, env=env, timeout=120, check=False
    )


# --- the split -------------------------------------------------------------------


def project(root: Path, tests: dict[str, int], seconds: Mapping[str, float]) -> Path:
    """A project with the repo's conftest.py, `tests[name]` tests in each file
    `name`, and `seconds` as its durations table."""
    shutil.copy(REPO / "conftest.py", root / "conftest.py")
    (root / ".github").mkdir()
    (root / ".github/shard-durations.json").write_text(json.dumps(seconds))
    for name, count in tests.items():
        (root / name).write_text("".join(TEST.format(i=i) for i in range(count)))
    return root


def pytest_in(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], root)


def collected(root: Path, *args: str) -> list[str]:
    result = pytest_in(root, "--collect-only", "-q", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


ABCD = {"test_a.py": 2, "test_b.py": 1, "test_c.py": 1, "test_d.py": 1}
ABCD_SECONDS = {"test_a.py": 10, "test_b.py": 6, "test_c.py": 5, "test_d.py": 1}


def test_without_a_shard_every_test_runs(tmp_path: Path) -> None:
    root = project(tmp_path, ABCD, ABCD_SECONDS)

    assert len(collected(root)) == 5


def test_each_shard_runs_the_files_the_split_gives_it(tmp_path: Path) -> None:
    # Longest first, each to the lighter shard: a (10), b (6), c (5), then d (1).
    root = project(tmp_path, ABCD, ABCD_SECONDS)

    assert collected(root, "--shard", "1/2") == [
        "test_a.py::test_0",
        "test_a.py::test_1",
        "test_d.py::test_0",
    ]
    assert collected(root, "--shard", "2/2") == [
        "test_b.py::test_0",
        "test_c.py::test_0",
    ]


def test_a_file_with_no_recorded_seconds_weighs_the_median(tmp_path: Path) -> None:
    # n weighs 5, so it gets the third shard to itself; at 0 or at the mean (3.7)
    # c would join it.
    files = {"test_a.py": 1, "test_b.py": 1, "test_c.py": 1, "test_n.py": 1}
    root = project(tmp_path, files, {"test_a.py": 5, "test_b.py": 5, "test_c.py": 1})

    assert collected(root, "--shard", "3/3") == ["test_n.py::test_0"]


@pytest.mark.parametrize(
    "args",
    [
        ["--shard", "0/2"],
        ["--shard", "3/2"],
        ["--shard", "2"],
        ["--shard-record", "record.json"],
    ],
    ids=["zero", "past-the-count", "no-count", "record-without-a-shard"],
)
def test_a_bad_shard_is_a_usage_error(tmp_path: Path, args: list[str]) -> None:
    root = project(tmp_path, ABCD, ABCD_SECONDS)

    result = pytest_in(root, "--collect-only", *args)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout


def test_the_record_lists_the_collection_the_selection_and_seconds(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, ABCD, ABCD_SECONDS)
    (root / "test_c.py").write_text(
        "import time\n\n\ndef test_0() -> None:\n    time.sleep(0.2)\n"
    )

    result = pytest_in(root, "--shard", "2/2", "--shard-record", "record.json")

    assert result.returncode == 0, result.stdout + result.stderr
    written = json.loads((root / "record.json").read_text())
    assert written["shard"] == "2/2"
    assert written["collected"] == [
        "test_a.py::test_0",
        "test_a.py::test_1",
        "test_b.py::test_0",
        "test_c.py::test_0",
        "test_d.py::test_0",
    ]
    assert written["selected"] == ["test_b.py::test_0", "test_c.py::test_0"]
    assert set(written["seconds"]) == {"test_b.py", "test_c.py"}
    assert written["seconds"]["test_c.py"] >= 0.2


# --- the check -------------------------------------------------------------------


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
