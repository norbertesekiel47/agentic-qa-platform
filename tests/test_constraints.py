"""CONSTRAINTS.md's thresholds and floor, proven against the real gate commands.

Each threshold test reads its number from CONSTRAINTS.md and checks both sides
of it: at the number the gate passes, one step past it the gate fails. So a
config that drifts from CONSTRAINTS.md in either direction fails a test.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONSTRAINTS = REPO / "CONSTRAINTS.md"
# Ruff applies the repo's config for this path; the file needn't exist.
PACKAGE_MODULE = "packages/core/src/aqa_core/probe.py"


def threshold(dimension: str) -> int:
    """The number in the Threshold cell of CONSTRAINTS.md's row for `dimension`."""
    column = None
    for line in CONSTRAINTS.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if "Threshold" in cells:
            column = cells.index("Threshold")
        elif column is not None and cells[0] == dimension:
            number = re.search(r"\d+", cells[column])
            if number:
                return int(number.group())
    raise LookupError(f"CONSTRAINTS.md has no threshold for {dimension!r}")


def run(
    args: list[str], *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a gate command from the repo root, outside this suite's coverage run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    return subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=300,
        check=False,
    )


def ruff_codes(source: str) -> tuple[int, set[str]]:
    """Ruff's exit code and rule codes for `source` as a module in packages/."""
    result = run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format=json",
            f"--stdin-filename={PACKAGE_MODULE}",
            "-",
        ],
        stdin=source,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode, {finding["code"] for finding in json.loads(result.stdout)}


# --- functions of a given size, for the complexity limits --------------------------


def _ifs(count: int) -> str:
    body = "".join(f"    if x == {i}:\n        total += {i}\n" for i in range(count))
    return f"def f(x: int) -> int:\n    total = 0\n{body}    return total\n"


def _returns(count: int) -> str:
    early = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(count - 1))
    return f"def f(x: int) -> int:\n{early}    return -1\n"


def _parameters(count: int, keyword_only: int = 0) -> str:
    names = [f"a{i}" for i in range(count)]
    params = [f"{name}: int" for name in names]
    if keyword_only:
        params.insert(count - keyword_only, "*")
    return f"def f({', '.join(params)}) -> int:\n    return {' + '.join(names)}\n"


def _statements(count: int) -> str:
    # Ruff doesn't count a trailing return, so the body is assignments only.
    body = "".join(f"    v{i} = {i}\n" for i in range(count))
    return f"def f() -> None:\n{body}"


# (rule, CONSTRAINTS.md dimension, a function measuring n on that dimension)
LIMITS: list[tuple[str, str, Callable[[int], str]]] = [
    ("C901", "Cyclomatic complexity per function", lambda n: _ifs(n - 1)),
    ("PLR0911", "Return statements per function", _returns),
    ("PLR0912", "Branches per function", _ifs),
    ("PLR0913", "Arguments per function", lambda n: _parameters(n, keyword_only=1)),
    ("PLR0917", "Positional arguments per function", _parameters),
    ("PLR0915", "Statements per function", _statements),
]
limits = pytest.mark.parametrize(
    ("rule", "dimension", "function"), LIMITS, ids=[rule for rule, _, _ in LIMITS]
)


# Some fixtures trip a second limit (twelve ifs are also complexity 13), so each
# test asserts its own rule's code rather than a clean exit.
@limits
def test_complexity_at_the_limit_passes(
    rule: str, dimension: str, function: Callable[[int], str]
) -> None:
    _, codes = ruff_codes(function(threshold(dimension)))

    assert rule not in codes


@limits
def test_complexity_past_the_limit_fails(
    rule: str, dimension: str, function: Callable[[int], str]
) -> None:
    exit_code, codes = ruff_codes(function(threshold(dimension) + 1))

    assert rule in codes
    assert exit_code == 1


# --- the floor rows that Ruff and mypy enforce --------------------------------------


def test_todo_comment_fails_ruff() -> None:
    exit_code, codes = ruff_codes(
        "def f() -> int:\n    return 1  # TODO: the real value\n"
    )

    assert "FIX002" in codes
    assert exit_code == 1


def test_swallowed_exception_fails_ruff() -> None:
    source = (
        "def f(text: str) -> None:\n"
        "    try:\n"
        "        int(text)\n"
        "    except Exception:\n"
        "        pass\n"
    )

    exit_code, codes = ruff_codes(source)

    assert {"BLE001", "S110"} <= codes
    assert exit_code == 1


@pytest.mark.parametrize("body", ["...", "pass"])
def test_empty_body_fails_mypy(body: str, tmp_path: Path) -> None:
    # mypy's -c conflicts with the config's `files`, so each source is a file.
    def mypy(name: str, source: str) -> subprocess.CompletedProcess[str]:
        module = tmp_path / name
        module.write_text(source)
        config = ["--config-file=pyproject.toml", f"--cache-dir={tmp_path / 'cache'}"]
        return run([sys.executable, "-m", "mypy", *config, str(module)])

    empty = mypy("empty.py", f"def f() -> int:\n    {body}\n")
    control = mypy("control.py", "def f() -> int:\n    return 1\n")

    assert empty.returncode == 1
    assert "[empty-body]" in empty.stdout
    assert control.returncode == 0, control.stdout
