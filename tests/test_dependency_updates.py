"""Dependency updates (ADR-0031): what keeps TECH_STACK.md, the checks image and
Renovate's configuration true to the workspace's pins.

Renovate's weekly pull requests bump a pin, `uv.lock` and the version TECH_STACK.md
tracks for it, all in one branch. These tests fail when one of them is left
behind, so a merged bump can't leave a document or an image false. They also
prove the other constraints the bot must respect: the uv it runs, and litellm.
"""

import json
import os
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
TECH_STACK = REPO / "TECH_STACK.md"
RENOVATE = REPO / ".github" / "renovate.json"
HARNESS = REPO / "bench" / "harness"

# A version in TECH_STACK.md that follows a pin: `1.63.0` then an HTML comment
# naming the PyPI package. The comment is invisible when the file is rendered.
TRACKED_VERSION = re.compile(
    r"(?P<version>\d[\w.]*)<!-- renovate: (?P<name>[\w.-]+) -->"
)
# PEP 508 allows extras and spaces around `==`, and Renovate reads both.
EXACT_PIN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>[0-9][^\s;,]*)"
)

# Where the checks image (ADR-0023) names Playwright, by file in bench/harness. The
# image's Playwright must be the workspace's, but nothing in CI builds or runs it
# (ADR-0029), so only this check notices a bump that left it behind.
CHECKS_IMAGE_PLAYWRIGHT = {
    "checks-requirements.in": r"^playwright==(\S+)",
    "checks-requirements.txt": r"^playwright==(\S+)",
    "checks.Dockerfile": r"playwright/python:v(\S+?)-",
}
CHECKS_IMAGE_HOW_TO_FIX = (
    "Renovate doesn't update the checks image: regenerate checks-requirements.txt "
    "with the command in bench/harness/checks.Dockerfile, and bump the base image's "
    "tag and digest there"
)


def normalize(name: str) -> str:
    """The package name as PEP 503 spells it, so `Types_PyYAML` finds `types-pyyaml`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def workspace_pins(root: Path) -> dict[str, set[str]]:
    """Every exact pin (`name==version`) in the root's and the members' pyproject.toml
    files, as the versions each package is pinned to."""
    members = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"][
        "workspace"
    ]["members"]
    files = [root / "pyproject.toml"]
    for pattern in members:
        files += sorted(root.glob(f"{pattern}/pyproject.toml"))
    pins: dict[str, set[str]] = {}
    for file in files:
        project = tomllib.loads(file.read_text())
        requirements: list[str] = list(
            project.get("project", {}).get("dependencies", [])
        )
        for group in project.get("dependency-groups", {}).values():
            requirements += [item for item in group if isinstance(item, str)]
        for requirement in requirements:
            if pin := EXACT_PIN.match(requirement):
                pins.setdefault(normalize(pin["name"]), set()).add(pin["version"])
    return pins


def tracked_versions(document: str) -> dict[str, set[str]]:
    """The versions a document tracks, by package."""
    tracked: dict[str, set[str]] = {}
    for found in TRACKED_VERSION.finditer(document):
        tracked.setdefault(normalize(found["name"]), set()).add(found["version"])
    return tracked


def pin_mismatches(
    pins: dict[str, set[str]], tracked: dict[str, set[str]]
) -> list[str]:
    """What keeps the tracked versions from being the pins, one line each."""
    problems: list[str] = []
    for name, versions in sorted(pins.items()):
        pinned = " and ".join(sorted(versions))
        if len(versions) > 1:
            problems.append(f"{name} is pinned to both {pinned}")
        elif name not in tracked:
            problems.append(f"{name}=={pinned} has no tracked version in TECH_STACK.md")
        elif tracked[name] != versions:
            tracked_as = " and ".join(sorted(tracked[name]))
            problems.append(
                f"{name} is pinned to {pinned}, but TECH_STACK.md tracks {tracked_as}"
            )
    problems += [
        f"TECH_STACK.md tracks {name} {' and '.join(sorted(versions))}, which no pyproject.toml pins"
        for name, versions in sorted(tracked.items())
        if name not in pins
    ]
    return problems


def checks_image_mismatches(pin: str, files: dict[str, str]) -> list[str]:
    """The checks image's files that name a Playwright other than the pinned one."""
    problems: list[str] = []
    for name, text in sorted(files.items()):
        versions = set(
            re.findall(CHECKS_IMAGE_PLAYWRIGHT[name], text, flags=re.MULTILINE)
        )
        if not versions:
            problems.append(
                f"bench/harness/{name} names no Playwright, but the workspace pins {pin}"
            )
        elif versions != {pin}:
            found = " and ".join(sorted(versions))
            problems.append(
                f"bench/harness/{name} has Playwright {found}, but the workspace pins {pin}"
            )
    return problems


def fake_wheel(directory: Path, name: str, requires: list[str]) -> None:
    """An empty wheel of `name` 1.0.0 that requires `requires`, for `uv --find-links`."""
    dist = f"{name}-1.0.0"
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0.0"]
    metadata += [f"Requires-Dist: {requirement}" for requirement in requires]
    wheel = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    with zipfile.ZipFile(directory / f"{dist}-py3-none-any.whl", "w") as archive:
        archive.writestr(f"{name}/__init__.py", "")
        archive.writestr(f"{dist}.dist-info/METADATA", "\n".join(metadata) + "\n")
        archive.writestr(f"{dist}.dist-info/WHEEL", wheel)
        archive.writestr(f"{dist}.dist-info/RECORD", "")


def resolve(
    project: Path, constraints: list[str], links: Path
) -> subprocess.CompletedProcess[str]:
    """`uv lock` for a project that depends on needs-litellm, offline, with `constraints`
    as its constraint-dependencies and only the wheels in `links` available."""
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["needs-litellm"]\n'
        f"[tool.uv]\nconstraint-dependencies = {json.dumps(constraints)}\n"
    )
    return subprocess.run(
        ["uv", "lock", "--offline", "--no-index", "--find-links", str(links)],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "UV_PYTHON_DOWNLOADS": "never"},
        timeout=120,
        check=False,
    )


def uv_settings() -> dict[str, Any]:
    """The root pyproject.toml's [tool.uv] table."""
    root: dict[str, Any] = tomllib.loads((REPO / "pyproject.toml").read_text())
    settings: dict[str, Any] = root["tool"]["uv"]
    return settings


def renovate_config() -> dict[str, Any]:
    """Renovate's configuration for this repository."""
    config: dict[str, Any] = json.loads(RENOVATE.read_text())
    return config


def test_every_exact_pin_is_tracked_in_tech_stack_at_its_version() -> None:
    problems = pin_mismatches(
        workspace_pins(REPO), tracked_versions(TECH_STACK.read_text())
    )

    assert not problems, "\n".join(problems)


def test_a_pin_with_no_tracked_version_is_named() -> None:
    document = "| Validation | Pydantic | 2.13.5<!-- renovate: pydantic --> |"

    problems = pin_mismatches(
        {"pydantic": {"2.13.5"}, "rich": {"15.0.0"}}, tracked_versions(document)
    )

    assert problems == ["rich==15.0.0 has no tracked version in TECH_STACK.md"]


def test_a_bumped_pin_is_named_when_its_tracked_version_lags() -> None:
    document = "| Validation | Pydantic | 2.13.5<!-- renovate: pydantic --> |"

    problems = pin_mismatches({"pydantic": {"2.13.6"}}, tracked_versions(document))

    assert problems == ["pydantic is pinned to 2.13.6, but TECH_STACK.md tracks 2.13.5"]


def test_a_tracked_version_with_no_pin_is_named() -> None:
    document = "Hypothesis 6.168.2<!-- renovate: hypothesis -->, respx 0.23.1<!-- renovate: respx -->"

    problems = pin_mismatches({"respx": {"0.23.1"}}, tracked_versions(document))

    assert problems == [
        "TECH_STACK.md tracks hypothesis 6.168.2, which no pyproject.toml pins"
    ]


def test_a_package_pinned_two_ways_is_named() -> None:
    document = "| Playwright for Python | 1.63.0<!-- renovate: playwright --> |"

    problems = pin_mismatches(
        {"playwright": {"1.63.0", "1.64.0"}}, tracked_versions(document)
    )

    assert problems == ["playwright is pinned to both 1.63.0 and 1.64.0"]


def test_workspace_pins_reads_members_and_groups_and_only_exact_pins(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "0"\ndependencies = ["Typer==0.27.2", "rich>=15", "psycopg[binary] == 3.3.6"]\n'
        '[dependency-groups]\ndev = ["ruff==0.16.9", {include-group = "stubs"}]\n'
        'stubs = ["Types_PyYAML==6.0.12.20260906"]\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    member = tmp_path / "packages" / "a"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        '[project]\nname = "a"\nversion = "0"\n'
        'dependencies = ["awslambdaric==4.1.0; sys_platform == \'linux\'", "ruff==0.16.9", "other"]\n'
    )

    assert workspace_pins(tmp_path) == {
        "typer": {"0.27.2"},
        "psycopg": {"3.3.6"},
        "ruff": {"0.16.9"},
        "types-pyyaml": {"6.0.12.20260906"},
        "awslambdaric": {"4.1.0"},
    }


def test_renovate_waits_three_days_after_a_release() -> None:
    config = renovate_config()

    assert config["minimumReleaseAge"] == "3 days"
    # "strict" is Renovate's default: it opens no pull request for a release that
    # is still pending. "none" and "flexible" would.
    assert config.get("internalChecksFilter", "strict") == "strict"
    assert [
        rule for rule in config["packageRules"] if "minimumReleaseAge" in rule
    ] == []


def test_pre_1_0_minor_updates_get_their_own_pull_request() -> None:
    rules = renovate_config()["packageRules"]
    group_rule = next(
        i
        for i, rule in enumerate(rules)
        if rule.get("groupName") == "python dependencies"
    )
    # A later rule's `groupName: null` takes the update out of the group.
    pre_1_0_rules = [
        rule
        for rule in rules[group_rule + 1 :]
        if rule.get("matchUpdateTypes") == ["minor"]
        and "groupName" in rule
        and rule["groupName"] is None
    ]

    assert len(pre_1_0_rules) == 1
    pre_1_0 = re.compile(pre_1_0_rules[0]["matchCurrentVersion"].strip("/"))
    assert pre_1_0.search("0.16.9")
    assert not pre_1_0.search("1.2.0")
    assert not pre_1_0.search("10.0.0")


def test_renovate_runs_the_uv_the_workspace_requires() -> None:
    required = uv_settings()["required-version"]

    # Renovate reads required-version only from the pyproject it is updating, and
    # only the root has one. Members get this constraint instead, or the latest uv,
    # which refuses the workspace.
    assert renovate_config()["constraints"]["uv"] == required


def test_renovate_finds_the_versions_the_check_reads() -> None:
    config = renovate_config()
    [manager] = [m for m in config["customManagers"] if m["customType"] == "regex"]
    document = TECH_STACK.read_text()
    found: dict[str, set[str]] = {}
    for pattern in manager["matchStrings"]:
        # Renovate names a group (?<name>...), Python (?P<name>...).
        python_pattern = re.sub(r"\(\?<(?=\w+>)", "(?P<", pattern)
        for match in re.finditer(python_pattern, document):
            found.setdefault(normalize(match["depName"]), set()).add(
                match["currentValue"]
            )

    assert manager["datasourceTemplate"] == "pypi"
    assert any(
        re.search(p.strip("/"), TECH_STACK.name) for p in manager["managerFilePatterns"]
    )
    assert found == tracked_versions(document)


def test_the_checks_image_follows_the_playwright_pin() -> None:
    [pin] = workspace_pins(REPO)["playwright"]
    files = {name: (HARNESS / name).read_text() for name in CHECKS_IMAGE_PLAYWRIGHT}

    problems = checks_image_mismatches(pin, files)

    assert not problems, "\n".join([*problems, CHECKS_IMAGE_HOW_TO_FIX])


def test_a_checks_image_behind_the_pin_is_named() -> None:
    files = {
        "checks-requirements.in": "playwright==1.64.0\n",
        "checks-requirements.txt": "playwright==1.64.0 \\\n    --hash=sha256:00\n",
        "checks.Dockerfile": "FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:00\n",
    }

    problems = checks_image_mismatches("1.64.0", files)

    assert problems == [
        "bench/harness/checks.Dockerfile has Playwright 1.63.0, but the workspace pins 1.64.0"
    ]


def test_a_checks_image_file_that_names_no_playwright_is_named() -> None:
    files = {"checks-requirements.in": "pyee==13.0.1\n"}

    problems = checks_image_mismatches("1.63.0", files)

    assert problems == [
        "bench/harness/checks-requirements.in names no Playwright, but the workspace pins 1.63.0"
    ]


def test_an_update_that_needs_litellm_fails_to_resolve(tmp_path: Path) -> None:
    litellm_constraint = uv_settings()["constraint-dependencies"]
    links = tmp_path / "links"
    links.mkdir()
    fake_wheel(links, "litellm", [])
    fake_wheel(links, "needs_litellm", ["litellm"])

    refused = resolve(tmp_path / "constrained", litellm_constraint, links)
    allowed = resolve(tmp_path / "unconstrained", [], links)

    # Same project, same index; only the workspace's constraint differs.
    assert refused.returncode == 1
    assert "litellm<0" in refused.stderr
    assert allowed.returncode == 0, allowed.stderr
