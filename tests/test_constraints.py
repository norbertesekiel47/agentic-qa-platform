"""CONSTRAINTS.md's thresholds and floor, proven against the real gate commands.

Each threshold test reads its number from CONSTRAINTS.md and checks both sides
of it: at the number the gate passes, one step past it the gate fails. So a
config that drifts from CONSTRAINTS.md in either direction fails a test.
"""

import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

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


@pytest.mark.parametrize(
    ("handler", "rules"),
    [("except Exception:", {"BLE001", "S110"}), ("except:", {"E722"})],
    ids=["broad", "bare"],
)
def test_swallowed_exception_fails_ruff(handler: str, rules: set[str]) -> None:
    source = f"def f(text: str) -> None:\n    try:\n        int(text)\n    {handler}\n        pass\n"

    exit_code, codes = ruff_codes(source)

    assert rules <= codes
    assert exit_code == 1


def test_assert_and_print_fail_ruff_in_package_code() -> None:
    # Tests may assert and scripts may print; package code raises and logs.
    exit_code, codes = ruff_codes(
        "def f(x: int) -> None:\n    assert x\n    print(x)\n"
    )

    assert {"S101", "T201"} <= codes
    assert exit_code == 1


def mypy(tmp_path: Path, name: str, source: str) -> subprocess.CompletedProcess[str]:
    """mypy, with the repo's config, on `source` as the module `name`."""
    # mypy's -c conflicts with the config's `files`, so each source is a file.
    module = tmp_path / name
    module.write_text(source)
    config = ["--config-file=pyproject.toml", f"--cache-dir={tmp_path / 'cache'}"]
    return run([sys.executable, "-m", "mypy", *config, str(module)])


@pytest.mark.parametrize("body", ["...", "pass"])
def test_empty_body_fails_mypy(body: str, tmp_path: Path) -> None:
    empty = mypy(tmp_path, "empty.py", f"def f() -> int:\n    {body}\n")
    control = mypy(tmp_path, "control.py", "def f() -> int:\n    return 1\n")

    assert empty.returncode == 1
    assert "[empty-body]" in empty.stdout
    assert control.returncode == 0, control.stdout


def test_package_code_cannot_import_a_script(tmp_path: Path) -> None:
    # The scripts are off mypy_path, so an import that fails for users of the
    # packages fails the type check too (ADR-0029).
    leak = mypy(tmp_path, "leak.py", "import manifest\nimport policy_guard\n")
    control = mypy(tmp_path, "control.py", "import aqa_cli\nimport aqa_core\n")

    not_found = {
        line.split('"')[1]
        for line in leak.stdout.splitlines()
        if line.endswith("[import-not-found]")
    }
    assert leak.returncode == 1
    assert not_found == {"manifest", "policy_guard"}
    assert control.returncode == 0, control.stdout


# --- coverage ------------------------------------------------------------------------


def covered_package(root: Path, functions: int, called: int) -> None:
    """A workspace member under `root` whose tests call `called` of its `functions`.

    Each function is two statements and the import runs every `def`, so the
    package is (functions + called) / (2 * functions) covered.
    """
    source = "".join(
        f"def f{i}() -> int:\n    return {i}\n\n\n" for i in range(functions)
    )
    calls = "".join(f"    mod.f{i}()\n" for i in range(called))
    package = root / "packages/demo"
    (package / "src/demo").mkdir(parents=True)
    (package / "src/demo/mod.py").write_text(source)
    (package / "tests").mkdir()
    (package / "tests/test_mod.py").write_text(
        f"from demo import mod\n\n\ndef test_mod() -> None:\n{calls}"
    )
    # The config's other source directories: coverage warns when one is missing.
    (root / "spikes").mkdir()
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
    covered_package(tmp_path, 50, floor - 50)

    result = pytest_cov(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Required test coverage of" in result.stdout  # a floor is configured


def test_coverage_below_the_floor_fails(tmp_path: Path) -> None:
    floor = threshold("Coverage, overall")
    covered_package(tmp_path, 50, floor - 51)

    result = pytest_cov(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Coverage failure" in result.stdout  # the floor failed it, not a test


def test_coverage_rounds_to_a_whole_percent(tmp_path: Path) -> None:
    floor = threshold("Coverage, overall")
    # 500 statements, 0.4 points short of the floor, which rounds up to it.
    covered_package(tmp_path, 250, 5 * floor - 252)

    result = pytest_cov(tmp_path)

    assert f"Total coverage: {floor - 0.4:.2f}%" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_unimported_module_counts_against_the_floor(tmp_path: Path) -> None:
    covered_package(tmp_path, 50, threshold("Coverage, overall") - 50)
    # Like every workspace member, packages/demo has no __init__.py.
    unimported = "packages/demo/src/demo/unimported.py"
    (tmp_path / unimported).write_text("def f() -> int:\n    return 0\n")

    result = pytest_cov(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert unimported in result.stdout


def test_an_omitted_file_stays_omitted_in_a_subprocess(tmp_path: Path) -> None:
    covered_package(tmp_path, 50, threshold("Coverage, overall") - 50)
    # toggle_checks.py is omitted; a measured subprocess started in another
    # directory must not count it, which a cwd-relative omit pattern would.
    (tmp_path / "bench/harness/toggle_checks.py").write_text(
        "".join(f"x{i} = {i}\n" for i in range(50))
    )
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "packages/demo/tests/test_child.py").write_text(
        "import subprocess\nimport sys\n\n\ndef test_child() -> None:\n"
        "    subprocess.run([sys.executable, '-c', 'pass'], cwd='elsewhere', check=True)\n"
    )

    result = pytest_cov(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "toggle_checks.py" not in result.stdout


def test_a_branch_never_taken_counts_against_the_floor(tmp_path: Path) -> None:
    covered_package(tmp_path, 1, 1)
    # Every line of g runs, but its `if` never takes the False branch: all
    # lines are covered, one of two branches isn't (8 of 9, below the floor).
    demo = tmp_path / "packages/demo"
    (demo / "src/demo/branchy.py").write_text(
        "def g(x: bool) -> int:\n    y = 0\n    if x:\n        y = 1\n    return y\n"
    )
    (demo / "tests/test_branchy.py").write_text(
        "from demo import branchy\n\n\ndef test_g() -> None:\n    branchy.g(True)\n"
    )

    result = pytest_cov(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Coverage failure" in result.stdout


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

    assert result.returncode == pytest.ExitCode.INTERRUPTED, result.stdout
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


def test_the_test_gate_collects_the_harness_the_guard_and_these_tests() -> None:
    # CI runs the harness's, the guard's and the spikes' tests through pytest
    # (ADR-0029; ADR-0008 amendment, 2026-09-30).
    result = run([*PYTEST, "--collect-only", "-q"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "spikes/hosted-chromium/tests/test_trial.py" in result.stdout
    assert "bench/harness/test_manifest.py" in result.stdout
    assert ".claude/hooks/test_policy_guard.py" in result.stdout
    assert "tests/test_constraints.py" in result.stdout


# --- dependency audit ------------------------------------------------------------------

AUDIT = REPO / ".github/scripts/audit-lockfile.sh"


def audit_cut() -> float:
    """The CVSS score from which CONSTRAINTS.md's dependency audit fails."""
    for line in CONSTRAINTS.read_text().splitlines():
        cut = re.search(r"A score of (\d+\.\d) or more", line)
        if line.startswith("| Dependency audit |") and cut:
            return float(cut.group(1))
    raise LookupError("CONSTRAINTS.md's dependency audit row names no CVSS score")


CUT = audit_cut()
CHECKS_LOCK = "bench/harness/checks-requirements.txt"


def ci_lockfiles() -> list[str]:
    """The lockfiles CI's dependency-audit step hands the audit script."""
    workflow = (REPO / ".github/workflows/ci.yml").read_text()
    step = re.search(r'audit-lockfile\.sh "\$RUNNER_TEMP/osv-scanner" (.+)', workflow)
    if not step:
        raise LookupError(
            'ci.yml must run audit-lockfile.sh "$RUNNER_TEMP/osv-scanner" and its '
            "lockfiles on one line"
        )
    return step.group(1).split()


CI_LOCKFILES = ci_lockfiles()


def test_ci_audits_the_checks_image_lock() -> None:
    assert {"uv.lock", CHECKS_LOCK} <= set(CI_LOCKFILES)


def test_every_lockfile_ci_audits_exists() -> None:
    assert all((REPO / lockfile).is_file() for lockfile in CI_LOCKFILES)


def osv_report(*scores: str) -> dict[str, Any]:
    """osv-scanner's JSON: one package, an advisory group per score ("" = none)."""
    groups = [
        {"ids": [f"GHSA-fake-{i}"], "max_severity": score}
        for i, score in enumerate(scores)
    ]
    package = {"package": {"name": "demo", "version": "1.0"}, "groups": groups}
    return {"results": [{"packages": [package]}]}


Reports = dict[str, tuple[int, dict[str, Any] | None]]


def audit_files(tmp_path: Path, reports: Reports) -> subprocess.CompletedProcess[str]:
    """The audit script over `reports`' lockfiles, with a fake osv-scanner.

    For each lockfile the fake exits with its code and writes its report where
    `--output-file` says (None writes nothing), and every call's arguments go to
    `calls.txt`. Without jq the script can't judge anything, and a failure to run
    it would pass every case that expects the audit to fail, so both are errors
    here.
    """
    assert shutil.which("jq"), "the dependency audit needs jq on PATH (ADR-0029)"
    cases = ""
    for i, (lockfile, (code, report)) in enumerate(reports.items()):
        copy = ""
        if report is not None:
            written = tmp_path / f"report-{i}.json"
            written.write_text(json.dumps(report))
            copy = f'cp "{written}" "$out"; '
        cases += f"  {shlex.quote(lockfile)}) {copy}exit {code};;\n"
    scanner = tmp_path / "osv-scanner"
    scanner.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{tmp_path / "calls.txt"}"\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in --output-file) out=$2;; --lockfile) lockfile=$2;; esac\n'
        "  shift\n"
        "done\n"
        'case "$lockfile" in\n'
        f"{cases}"
        "esac\n"
        'echo "unexpected lockfile $lockfile" >&2\n'
        "exit 99\n"
    )
    scanner.chmod(0o755)
    result = run([str(AUDIT), str(scanner), *reports])
    # 126 and 127 are the shell's "can't execute" and "command not found".
    assert result.returncode not in {126, 127}, result.stderr
    return result


def audit(
    tmp_path: Path, scanner_exit: int, report: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    """The audit script over CI's lockfiles, the fake scanner giving each `report`."""
    return audit_files(tmp_path, dict.fromkeys(CI_LOCKFILES, (scanner_exit, report)))


@pytest.mark.parametrize(
    ("scanner_exit", "report", "passes"),
    [
        (0, {"results": []}, True),
        (1, osv_report(f"{CUT - 0.1:.1f}"), True),
        (1, osv_report(f"{CUT:.1f}"), False),
        (1, osv_report(""), False),
        (1, osv_report("5.4", f"{CUT:.1f}"), False),
        # osv-scanner found something, but the filter read nothing: fail closed.
        (1, {"results": []}, False),
        (1, {"findings": []}, False),
        # A shape the filter doesn't know fails even on a clean exit, so a
        # scanner upgrade that moves the results can't pass silently.
        (0, {"findings": []}, False),
    ],
    ids=[
        "clean",
        "below-the-cut",
        "at-the-cut",
        "unscored",
        "high-in-a-later-group",
        "findings-without-rows",
        "unknown-shape",
        "unknown-shape-on-a-clean-exit",
    ],
)
def test_the_audit_fails_on_a_high_or_unscored_advisory(
    tmp_path: Path, scanner_exit: int, report: dict[str, Any], *, passes: bool
) -> None:
    result = audit(tmp_path, scanner_exit, report)

    assert (result.returncode == 0) is passes, result.stdout + result.stderr


def test_the_audit_lists_every_finding_with_its_score(tmp_path: Path) -> None:
    result = audit(tmp_path, 1, osv_report("5.4", ""))

    assert result.returncode == 1
    assert "demo 1.0: GHSA-fake-0 (CVSS 5.4)" in result.stdout
    assert "demo 1.0: GHSA-fake-1 (CVSS none)" in result.stdout


def test_a_scanner_error_fails_the_audit_with_its_exit_code(tmp_path: Path) -> None:
    # 128 is osv-scanner's "no packages found"; its JSON is empty but valid.
    result = audit(tmp_path, 128, {"results": []})

    assert result.returncode == 128, result.stdout + result.stderr


def clean_reports() -> Reports:
    """Every lockfile CI audits, each with a clean scan."""
    return dict.fromkeys(CI_LOCKFILES, (0, {"results": []}))


@pytest.mark.parametrize("lockfile", CI_LOCKFILES)
@pytest.mark.parametrize(
    ("score", "passes"),
    [(f"{CUT - 0.1:.1f}", True), (f"{CUT:.1f}", False), ("", False)],
    ids=["below-the-cut", "at-the-cut", "unscored"],
)
def test_an_advisory_in_one_audited_lockfile_is_judged_by_its_score(
    tmp_path: Path, lockfile: str, score: str, *, passes: bool
) -> None:
    reports = clean_reports() | {lockfile: (1, osv_report(score))}

    result = audit_files(tmp_path, reports)

    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    # The finding says which lockfile it is in, whether or not it fails the audit.
    # The score isn't compared: jq's rendering of 7.0 depends on its version.
    assert f"{lockfile}: demo 1.0: GHSA-fake-0 (CVSS " in result.stdout


@pytest.mark.parametrize("lockfile", CI_LOCKFILES)
@pytest.mark.parametrize("scanner_exit", [2, 128])
def test_a_scanner_error_on_one_audited_lockfile_fails_the_audit(
    tmp_path: Path, lockfile: str, scanner_exit: int
) -> None:
    # 2 is the least exit code past the 1 that means findings.
    reports = clean_reports() | {lockfile: (scanner_exit, {"results": []})}

    result = audit_files(tmp_path, reports)

    assert result.returncode == scanner_exit, result.stdout + result.stderr


def test_the_audit_lists_the_findings_of_every_lockfile(tmp_path: Path) -> None:
    # A lockfile that fails the audit doesn't hide the findings in the next one.
    reports: Reports = dict.fromkeys(CI_LOCKFILES, (1, osv_report(f"{CUT:.1f}")))

    result = audit_files(tmp_path, reports)

    assert result.returncode == 1, result.stdout + result.stderr
    for lockfile in CI_LOCKFILES:
        assert f"{lockfile}: demo 1.0: GHSA-fake-0 (CVSS " in result.stdout


def test_a_lockfile_with_no_report_fails_the_audit(tmp_path: Path) -> None:
    # The scanner exits 0 for the last lockfile and writes nothing. The clean
    # report that the lockfile before it left must not be judged in its place.
    reports = clean_reports() | {CI_LOCKFILES[-1]: (0, None)}

    result = audit_files(tmp_path, reports)

    assert result.returncode != 0, result.stdout + result.stderr


def test_the_audit_needs_a_lockfile(tmp_path: Path) -> None:
    result = audit_files(tmp_path, {})

    assert result.returncode != 0
    assert "no lockfile" in result.stdout


def test_the_scanner_reads_the_root_waivers_for_every_lockfile(tmp_path: Path) -> None:
    audit_files(tmp_path, clean_reports())

    text = (tmp_path / "calls.txt").read_text().replace("=", " ")
    calls = [line.split() for line in text.splitlines()]
    # One scan per lockfile: osv-scanner skips a file that parses to nothing when
    # it scans two at once, and `--config` makes the root file the only waivers.
    for call, lockfile in zip(calls, CI_LOCKFILES, strict=True):
        pairs = set(itertools.pairwise(call))
        assert ("--config", WAIVERS.name) in pairs
        assert ("--lockfile", lockfile) in pairs


# --- dependency audit waivers ----------------------------------------------------------

WAIVERS = REPO / "osv-scanner.toml"
TODAY = date(2026, 9, 29)


WAIVER_KEYS = {"id", "reason", "ignoreUntil"}


def audit_exceptions(exceptions: str) -> set[str]:
    """The IDs of the Exceptions rows whose rule is the dependency audit."""
    rows = [line.strip("| ").split("|") for line in exceptions.splitlines()]
    return {
        cells[0].strip()
        for cells in rows
        if len(cells) > 1 and cells[1].strip() == "Dependency audit"
    }


def entry_problems(
    entry: dict[str, Any], waived: set[str], today: date, latest: date
) -> list[str]:
    """How one [[IgnoredVulns]] entry falls short of the waiver rule."""
    vuln = str(entry.get("id", ""))
    until = entry.get("ignoreUntil")
    if isinstance(until, datetime):
        until = until.astimezone(UTC).date() if until.tzinfo else until.date()
    problems = []
    # osv-scanner's TOML decoder matches keys case-insensitively, so an extra
    # `ID` or `IgnoreUntil` could override what this check reads.
    if extra := sorted(set(entry) - WAIVER_KEYS):
        problems.append(f"{vuln} has keys other than {sorted(WAIVER_KEYS)}: {extra}")
    if not vuln or not str(entry.get("reason", "")).strip():
        problems.append(f"{vuln or 'an entry'} needs an id and a reason")
    # A zero date means "never expires" to osv-scanner, so there's a floor too.
    if not isinstance(until, date) or not today <= until <= latest:
        problems.append(f"{vuln} needs an ignoreUntil from {today} to {latest}")
    if vuln not in waived:
        problems.append(f"{vuln} needs a row in CONSTRAINTS.md's Exceptions")
    return problems


def waiver_problems(config: dict[str, Any], exceptions: str, today: date) -> list[str]:
    """How an osv-scanner.toml falls short of CONSTRAINTS.md's waiver rule.

    osv-scanner itself accepts waivers that never expire and overrides that
    cover every package, so the rule is checked here (ADR-0029).
    """
    days = re.search(r"an expiry at most (\d+) days out", CONSTRAINTS.read_text())
    if not days:
        raise LookupError("CONSTRAINTS.md's Exceptions name no longest expiry")
    latest = today + timedelta(days=int(days.group(1)))
    problems = [
        f"[[{key}]] isn't a waiver of one advisory"
        for key in config
        if key != "IgnoredVulns"
    ]
    entries = config.get("IgnoredVulns", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return [*problems, "IgnoredVulns must be [[IgnoredVulns]] tables"]
    waived = audit_exceptions(exceptions)
    for entry in entries:
        problems += entry_problems(entry, waived, today, latest)
    return problems


EXCEPTIONS = (
    "| GHSA-fake-0000-0000 | Dependency audit | uv.lock | fake | maintainer | 2026-10-29 |\n"
    "| GHSA-fake-3333-3333 | Coverage, overall | packages | fake | maintainer | 2026-10-29 |\n"
)


def ignored(**changes: object) -> dict[str, Any]:
    """An osv-scanner.toml waiving one advisory within the rule, then `changes`."""
    entry = {
        "id": "GHSA-fake-0000-0000",
        "reason": "fake: the vulnerable function is never called",
        "ignoreUntil": TODAY + timedelta(days=30),
    } | changes
    return {"IgnoredVulns": [{k: v for k, v in entry.items() if v is not None}]}


def test_a_waiver_within_the_rule_passes() -> None:
    assert waiver_problems(ignored(), EXCEPTIONS, TODAY) == []


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        (
            {
                "PackageOverrides": [
                    {"vulnerability": {"ignore": True}, "reason": "fake"}
                ]
            },
            "[[PackageOverrides]] isn't a waiver of one advisory",
        ),
        (ignored(ignoreUntil=None), "needs an ignoreUntil"),
        (ignored(ignoreUntil=TODAY + timedelta(days=91)), "needs an ignoreUntil"),
        # osv-scanner reads a zero date as "never expires".
        (ignored(ignoreUntil=date(1, 1, 1)), "needs an ignoreUntil"),
        (ignored(ignoreUntil=TODAY - timedelta(days=1)), "needs an ignoreUntil"),
        # osv-scanner's TOML decoder also fills `id` from `ID`, picking one at random.
        (ignored(ID="GHSA-fake-9999-9999"), "keys other than"),
        ({"IgnoredVulns": {"id": "GHSA-fake-0000-0000"}}, "[[IgnoredVulns]] tables"),
        (ignored(reason=" "), "needs an id and a reason"),
        (
            ignored(id="GHSA-fake-1111-1111"),
            "needs a row in CONSTRAINTS.md's Exceptions",
        ),
        (
            ignored(id="GHSA-fake-0000-000"),
            "needs a row in CONSTRAINTS.md's Exceptions",
        ),
        (
            ignored(id="GHSA-fake-3333-3333"),
            "needs a row in CONSTRAINTS.md's Exceptions",
        ),
    ],
    ids=[
        "blanket-override",
        "no-expiry",
        "expiry-too-late",
        "zero-date",
        "expired",
        "case-variant-key",
        "not-a-list",
        "no-reason",
        "no-exception-row",
        "exception-row-for-a-longer-id",
        "exception-row-for-another-rule",
    ],
)
def test_a_waiver_outside_the_rule_is_named(
    config: dict[str, Any], problem: str
) -> None:
    problems = waiver_problems(config, EXCEPTIONS, TODAY)

    assert any(problem in found for found in problems), problems


def test_the_repo_waivers_follow_the_rule() -> None:
    # The audit passes this file to osv-scanner with --config, which fails on a
    # missing one, so it must exist, and no other waiver file is read.
    config = tomllib.loads(WAIVERS.read_text())
    exceptions = CONSTRAINTS.read_text().partition("## Exceptions")[2]

    assert waiver_problems(config, exceptions, datetime.now(UTC).date()) == []
