"""uv.lock keeps litellm out of the workspace (ADR-0007 and ADR-0027's amendments).

`constraint-dependencies = ["litellm<0"]` is what bans the package, and two
routes get around it: a `[tool.uv.sources]` entry replaces the constraint in
the lock's manifest (reproduced on uv 0.11.15), and an agent or a person
without Claude Code's hooks isn't stopped by policy_guard. The lock itself
shows both, so this test reads it. It checks the real `uv.lock`, and the
fixture locks written below prove each check can fail.
"""

import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LITELLM_BAN = {"name": "litellm", "specifier": "<0"}


def litellm_problems(lock: Path) -> list[str]:
    """Why `lock` doesn't keep litellm out, one reason each; none when it does.

    No `[[package]]` may be named litellm, and the `[manifest]` must record
    litellm's constraints as exactly `<0`: a different specifier, a marker
    that limits the ban, or a source in its place lets a resolution through.
    A missing `[manifest]` or `constraints` key is the ban missing, so it
    reports like any other loss of it.
    """
    data = tomllib.loads(lock.read_text())
    problems = []
    if any(package["name"] == "litellm" for package in data.get("package", [])):
        problems.append("the lock has a [[package]] named litellm")
    constraints = data.get("manifest", {}).get("constraints", [])
    ban = [constraint for constraint in constraints if constraint["name"] == "litellm"]
    if ban != [LITELLM_BAN]:
        problems.append(
            f"the [manifest] records litellm's constraints as {ban}, "
            f"not exactly [{LITELLM_BAN}]"
        )
    return problems


# --- fixture locks, in the shape uv 0.11.15 writes ---------------------------------

OTHER_CONSTRAINT = '{ name = "otherpkg", specifier = "<2" }'
HEADER = 'version = 1\nrevision = 3\nrequires-python = ">=3.14"\n'
WORKSPACE_PACKAGE = '[[package]]\nname = "aqa-workspace"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
LITELLM_PACKAGE = '[[package]]\nname = "litellm"\nversion = "0.0.1"\nsource = { directory = "../fake" }\n'


def manifest(*constraints: str) -> str:
    """A `[manifest]` table whose constraints are `constraints`, if any."""
    table = '[manifest]\nmembers = ["aqa-workspace"]\n'
    if constraints:
        table += (
            "constraints = [\n" + "".join(f"    {c},\n" for c in constraints) + "]\n"
        )
    return table


def write_lock(tmp_path: Path, manifest_table: str, *packages: str) -> Path:
    """A fixture lock in `tmp_path`: the tables, then the workspace and `packages`."""
    lock = tmp_path / "uv.lock"
    lock.write_text("\n".join([HEADER, manifest_table, WORKSPACE_PACKAGE, *packages]))
    return lock


def banned(*others: str) -> str:
    """A `[manifest]` that keeps the ban, beside `others`."""
    return manifest('{ name = "litellm", specifier = "<0" }', *others)


# --- the real lock ---------------------------------------------------------------


def test_the_lock_keeps_litellm_out() -> None:
    assert litellm_problems(REPO / "uv.lock") == []


# --- fixture locks that must pass and fail ------------------------------------------


def test_the_fixture_lock_baseline_passes(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, banned(OTHER_CONSTRAINT))

    assert litellm_problems(lock) == []


def test_a_lock_with_a_litellm_package_fails(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, banned(OTHER_CONSTRAINT), LITELLM_PACKAGE)

    problems = litellm_problems(lock)

    assert len(problems) == 1
    assert "[[package]]" in problems[0]


@pytest.mark.parametrize(
    "manifest_table",
    [
        manifest('{ name = "litellm", specifier = "<1" }', OTHER_CONSTRAINT),
        manifest(
            '{ name = "litellm", marker = "sys_platform == \'win32\'", specifier = "<0" }',
            OTHER_CONSTRAINT,
        ),
        manifest(OTHER_CONSTRAINT),
        manifest(),
        "",
    ],
    ids=["loosened", "conditional", "dropped", "no-constraints", "no-manifest"],
)
def test_a_lock_without_the_litellm_ban_fails(
    manifest_table: str, tmp_path: Path
) -> None:
    lock = write_lock(tmp_path, manifest_table)

    problems = litellm_problems(lock)

    assert len(problems) == 1
    assert "[manifest]" in problems[0]


def test_a_path_source_override_fails_both_ways(tmp_path: Path) -> None:
    # What uv 0.11.15 wrote for a `[tool.uv.sources]` path entry for litellm: the
    # package is locked, and its source took the place of the `<0` specifier.
    source = '{ name = "litellm", directory = "../fake" }'
    lock = write_lock(tmp_path, manifest(source, OTHER_CONSTRAINT), LITELLM_PACKAGE)

    problems = litellm_problems(lock)

    assert len(problems) == 2
    assert any("[[package]]" in problem for problem in problems)
    assert any("[manifest]" in problem for problem in problems)
