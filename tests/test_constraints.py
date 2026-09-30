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
# An inner pytest run, which mustn't write a cache into the directory it tests.
PYTEST = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]


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
    args: list[str], *, stdin: str | None = None, cwd: Path = REPO, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run a gate command outside this suite's own coverage run."""
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    return subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=inherited | env,
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


def test_unformatted_code_fails_ruff_format() -> None:
    check = [sys.executable, "-m", "ruff", "format", "--check", "--no-cache"]
    check += [f"--stdin-filename={PACKAGE_MODULE}", "-"]

    unformatted = run(check, stdin="x=1\n")
    formatted = run(check, stdin="x = 1\n")

    assert unformatted.returncode == 1, unformatted.stderr
    assert formatted.returncode == 0, formatted.stderr


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


# --- coverage ------------------------------------------------------------------------


def covered_package(root: Path, percent: int) -> None:
    """A workspace member under `root` whose tests cover `percent`% of its code."""
    # Fifty one-line functions are 100 statements. The import runs the 50 `def`
    # lines, so calling (percent - 50) of the functions covers percent% exactly.
    functions = "".join(f"def f{i}() -> int:\n    return {i}\n\n\n" for i in range(50))
    calls = "".join(f"    mod.f{i}()\n" for i in range(percent - 50))
    package = root / "packages/demo"
    (package / "src/demo").mkdir(parents=True)
    (package / "src/demo/mod.py").write_text(functions)
    (package / "tests").mkdir()
    (package / "tests/test_mod.py").write_text(
        f"from demo import mod\n\n\ndef test_mod() -> None:\n{calls}"
    )
    # The config's other source directories: coverage warns when one is missing.
    (root / "bench/harness").mkdir(parents=True)
    (root / ".claude/hooks").mkdir(parents=True)


def pytest_cov(root: Path) -> subprocess.CompletedProcess[str]:
    """`pytest --cov` in `root`, with the repo's coverage config."""
    config = f"--cov-config={REPO / 'pyproject.toml'}"
    return run(
        [*PYTEST, "--cov", config],
        cwd=root,
        PYTHONPATH=str(root / "packages/demo/src"),
    )


def test_coverage_at_the_floor_passes(tmp_path: Path) -> None:
    floor = threshold("Coverage, overall")
    covered_package(tmp_path, floor)

    result = pytest_cov(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    # pytest-cov prints the configured floor as a float.
    assert f"Required test coverage of {float(floor)}% reached" in result.stdout


def test_coverage_below_the_floor_fails(tmp_path: Path) -> None:
    floor = threshold("Coverage, overall")
    covered_package(tmp_path, floor - 1)

    result = pytest_cov(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    failure = f"Coverage failure: total of {floor - 1} is less than fail-under={floor}"
    assert failure in result.stdout


def test_an_unimported_module_counts_against_the_floor(tmp_path: Path) -> None:
    covered_package(tmp_path, threshold("Coverage, overall"))
    # Like every workspace member, packages/demo has no __init__.py.
    unimported = "packages/demo/src/demo/unimported.py"
    (tmp_path / unimported).write_text("def f() -> int:\n    return 0\n")

    result = pytest_cov(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert unimported in result.stdout


# --- pytest strictness ---------------------------------------------------------------

PLAIN_TEST = "def test_plain() -> None:\n    int('1')\n"


def pytest_repo_config(
    root: Path, name: str, test: str
) -> subprocess.CompletedProcess[str]:
    """pytest with the repo's [tool.pytest] config, on one test file in `root`."""
    (root / name).write_text(test)
    config = ["-c", str(REPO / "pyproject.toml"), f"--rootdir={root}"]
    return run(
        [*PYTEST, *config, name],
        cwd=root,
    )


def test_unregistered_marker_fails_under_strict_mode(tmp_path: Path) -> None:
    marked = f"import pytest\n\n\n@pytest.mark.unregistered\n{PLAIN_TEST}"

    result = pytest_repo_config(tmp_path, "test_marked.py", marked)
    control = pytest_repo_config(tmp_path, "test_plain.py", PLAIN_TEST)

    assert result.returncode != 0
    assert "'unregistered' not found in `markers`" in result.stdout
    assert control.returncode == 0, control.stdout


def test_a_warning_fails_the_test_run(tmp_path: Path) -> None:
    warns = (
        "import warnings\n\n\n"
        "def test_warns() -> None:\n"
        "    warnings.warn('deprecated', DeprecationWarning, stacklevel=1)\n"
    )

    result = pytest_repo_config(tmp_path, "test_warns.py", warns)
    control = pytest_repo_config(tmp_path, "test_plain.py", PLAIN_TEST)

    assert result.returncode == 1, result.stdout
    assert "DeprecationWarning: deprecated" in result.stdout
    assert control.returncode == 0, control.stdout
