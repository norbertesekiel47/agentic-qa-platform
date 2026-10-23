"""CI's test shards (ADR-0029's 2026-10-23 amendment).

`--shard K/N` runs shard K of N: whole test files, longest first, each to the
shard with the fewest seconds so far, by `.github/shard-durations.json`. A file
the table doesn't list weighs its median. Without `--shard`, every test runs.
`--shard-record` writes what the shard collected and ran for
`.github/scripts/check_shards.py`, which fails CI unless each collected test
ran in exactly one shard.
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

import pytest

# Each test file's seconds in CI; check_shards.py --durations rewrites it.
DURATIONS = Path(__file__).parent / ".github/shard-durations.json"


def shard(value: str) -> tuple[int, int]:
    """K/N, with 1 <= K <= N. argparse reports the ValueError as a usage error."""
    k, n = map(int, value.split("/"))
    if not 1 <= k <= n:
        raise ValueError(value)
    return k, n


def split(files: list[str], count: int) -> list[set[str]]:
    """`files` in `count` shards whose recorded seconds are as even as can be."""
    recorded: dict[str, float] = json.loads(DURATIONS.read_text())
    unknown = statistics.median(recorded.values())
    seconds = {file: recorded.get(file, unknown) for file in files}
    shards: list[set[str]] = [set() for _ in range(count)]
    loads = [0.0] * count
    for file in sorted(files, key=lambda file: (-seconds[file], file)):
        lightest = loads.index(min(loads))
        shards[lightest].add(file)
        loads[lightest] += seconds[file]
    return shards


def file_of(nodeid: str) -> str:
    return nodeid.partition("::")[0]


class Shard:
    def __init__(self, k: int, n: int, record: Path | None) -> None:
        self.k, self.n, self.record = k, n, record
        self.collected: list[str] = []
        self.selected: list[str] = []
        self.seconds: defaultdict[str, float] = defaultdict(float)

    # Last, so the shard splits the items every other plugin has settled.
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        files = list(dict.fromkeys(file_of(item.nodeid) for item in items))
        mine = split(files, self.n)[self.k - 1]
        self.collected = [item.nodeid for item in items]
        config.hook.pytest_deselected(
            items=[item for item in items if file_of(item.nodeid) not in mine]
        )
        items[:] = [item for item in items if file_of(item.nodeid) in mine]
        self.selected = [item.nodeid for item in items]

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.seconds[file_of(report.nodeid)] += report.duration

    def pytest_sessionfinish(self) -> None:
        if self.record:
            self.record.write_text(
                json.dumps(
                    {
                        "shard": f"{self.k}/{self.n}",
                        "collected": self.collected,
                        "selected": self.selected,
                        "seconds": self.seconds,
                    }
                )
            )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("shards", "CI's test shards")
    group.addoption(
        "--shard",
        type=shard,
        metavar="K/N",
        help="run shard K of N: the test files .github/shard-durations.json gives it",
    )
    group.addoption(
        "--shard-record",
        type=Path,
        metavar="PATH",
        help="with --shard, write the tests it collected and ran, and its files' seconds",
    )


def pytest_configure(config: pytest.Config) -> None:
    k_n: tuple[int, int] | None = config.getoption("shard")
    record: Path | None = config.getoption("shard_record")
    if record and not k_n:
        raise pytest.UsageError("--shard-record needs --shard")
    if k_n:
        config.pluginmanager.register(Shard(*k_n, record), "shard")
