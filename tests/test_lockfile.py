"""uv.lock keeps litellm out of the workspace (ADR-0007, ADR-0027).

`constraint-dependencies = ["litellm<0"]` is the ban, and a `[tool.uv.sources]`
entry or a hand edit can take it out of the lock without a resolution failing,
so this test reads the lock itself. ADR-0027's 2026-10-01 amendment says why.
The fixture locks below prove that each check can fail.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LITELLM_BAN = {"name": "litellm", "specifier": "<0"}


def litellm_problems(lock: Path) -> list[str]:
    """Why `lock` doesn't keep litellm out, one reason each; none when it does.

    No `[[package]]` may be named litellm, and the `[manifest]` must record
    litellm's constraints as exactly `[LITELLM_BAN]`. A missing `[manifest]` or
    `constraints` key is the ban missing, so it reports like any other loss of
    it. A lock with no `[[package]]` is one this check doesn't understand, so
    it raises rather than pass without checking anything.
    """
    data = tomllib.loads(lock.read_text())
    problems = []
    # uv reads names case-insensitively, so `LiteLLM` installs litellm. The name
    # has no `-`, `_` or `.`, so case is the only spelling that can alias it.
    if any(package["name"].lower() == "litellm" for package in data["package"]):
        problems.append("the lock has a [[package]] named litellm")
    constraints = data.get("manifest", {}).get("constraints", [])
    ban = [c for c in constraints if c["name"].lower() == "litellm"]
    if ban != [LITELLM_BAN]:
        problems.append(
            f"the [manifest] records litellm's constraints as {ban}, "
            f"not exactly [{LITELLM_BAN}]"
        )
    return problems


def pinned_names(requirements: str) -> set[str]:
    """The lowercased names a pip requirements file lists, one per requirement.

    A requirement starts its line, so the `--hash` lines under it, which are
    indented or start with `-`, and comments don't name anything. Lowercasing
    suffices for litellm, as in `litellm_problems`.
    """
    names = re.findall(r"^[A-Za-z0-9][A-Za-z0-9._-]*", requirements, re.MULTILINE)
    return {name.lower() for name in names}


# --- fixture locks, in the shape uv 0.11.15 writes ---------------------------------

BAN_ENTRY = '{ name = "litellm", specifier = "<0" }'
CONDITIONAL_BAN_ENTRY = (
    '{ name = "litellm", marker = "sys_platform == \'win32\'", specifier = "<0" }'
)
OTHER_CONSTRAINT = '{ name = "otherpkg", specifier = "<2" }'
HEADER = 'version = 1\nrevision = 3\nrequires-python = ">=3.14"\n'
WORKSPACE_PACKAGE = '[[package]]\nname = "aqa-workspace"\nversion = "0.1.0"\nsource = { virtual = "." }\n'


def package(name: str) -> str:
    """A `[[package]]` table for `name`, locked from a directory."""
    return f'[[package]]\nname = "{name}"\nversion = "0.0.1"\nsource = {{ directory = "../fake" }}\n'


def manifest(*constraints: str) -> str:
    """A `[manifest]` table whose constraints are `constraints`, if any."""
    table = '[manifest]\nmembers = ["aqa-workspace"]\n'
    if constraints:
        table += (
            "constraints = [\n" + "".join(f"    {c},\n" for c in constraints) + "]\n"
        )
    return table


MANIFEST_WITH_BAN = manifest(BAN_ENTRY, OTHER_CONSTRAINT)


def write_lock(tmp_path: Path, manifest_table: str, *packages: str) -> Path:
    """A fixture lock in `tmp_path`: the tables, then the workspace and `packages`."""
    lock = tmp_path / "uv.lock"
    lock.write_text("\n".join([HEADER, manifest_table, WORKSPACE_PACKAGE, *packages]))
    return lock


# --- the real lock ---------------------------------------------------------------


def test_the_lock_keeps_litellm_out() -> None:
    assert litellm_problems(REPO / "uv.lock") == []


# --- fixture locks that must pass and fail ------------------------------------------


def test_the_fixture_lock_baseline_passes(tmp_path: Path) -> None:
    lock = write_lock(tmp_path, MANIFEST_WITH_BAN)

    assert litellm_problems(lock) == []


def test_a_package_that_only_starts_like_litellm_passes(tmp_path: Path) -> None:
    lookalike = '{ name = "litellm-extras", specifier = "<9" }'
    table = manifest(BAN_ENTRY, lookalike)
    lock = write_lock(tmp_path, table, package("litellm-extras"))

    assert litellm_problems(lock) == []


@pytest.mark.parametrize("name", ["litellm", "LiteLLM"], ids=["lower", "mixed-case"])
def test_a_lock_with_a_litellm_package_fails(name: str, tmp_path: Path) -> None:
    lock = write_lock(tmp_path, MANIFEST_WITH_BAN, package(name))

    problems = litellm_problems(lock)

    assert len(problems) == 1
    assert "[[package]]" in problems[0]


@pytest.mark.parametrize(
    "manifest_table",
    [
        manifest('{ name = "litellm", specifier = "<1" }', OTHER_CONSTRAINT),
        manifest(CONDITIONAL_BAN_ENTRY, OTHER_CONSTRAINT),
        manifest(OTHER_CONSTRAINT),
        manifest(),
        "",
        manifest(BAN_ENTRY, BAN_ENTRY),
        manifest(BAN_ENTRY, CONDITIONAL_BAN_ENTRY),
        manifest(BAN_ENTRY, '{ name = "LiteLLM", specifier = ">=1" }'),
    ],
    ids=[
        "loosened",
        "conditional",
        "dropped",
        "no-constraints",
        "no-manifest",
        "duplicate",
        "extra-entry",
        "other-case-entry",
    ],
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
    lock = write_lock(tmp_path, manifest(source, OTHER_CONSTRAINT), package("litellm"))

    problems = litellm_problems(lock)

    assert len(problems) == 2
    assert any("[[package]]" in problem for problem in problems)
    assert any("[manifest]" in problem for problem in problems)


def test_a_lock_with_no_packages_is_not_a_pass(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(HEADER + "\n" + MANIFEST_WITH_BAN)

    with pytest.raises(KeyError, match="package"):
        litellm_problems(lock)


# --- the checks image's hash-locked requirements ------------------------------------

CHECKS_LOCK = REPO / "bench/harness/checks-requirements.txt"


def test_the_checks_lock_keeps_litellm_out() -> None:
    # checks.Dockerfile installs this file with `--require-hashes`, a second
    # install path that `constraint-dependencies` doesn't reach (ADR-0023, #71).
    names = pinned_names(CHECKS_LOCK.read_text())

    # The check read the pins, not nothing: playwright is what the image is for.
    assert "playwright" in names
    assert "litellm" not in names


@pytest.mark.parametrize(
    "line",
    [
        "litellm==1.82.7 \\",
        "LiteLLM==1.82.7 \\",
        "litellm[proxy]==1.82.7 \\",
        "litellm>=1.82",
        "litellm @ https://example.invalid/litellm.whl",
    ],
    ids=["pinned", "mixed-case", "extra", "range", "url"],
)
def test_litellm_is_read_from_a_requirements_file_in_any_spelling(line: str) -> None:
    lines = ["greenlet==3.5.6 \\", "    --hash=sha256:00", line, "    --hash=sha256:11"]

    assert pinned_names("\n".join(lines)) == {"greenlet", "litellm"}


def test_a_hash_a_comment_and_a_lookalike_name_are_not_litellm() -> None:
    lines = ["litellm-extras==1.0 \\", "    --hash=sha256:litellm", "# litellm"]

    assert pinned_names("\n".join(lines)) == {"litellm-extras"}
