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

# PEP 508 allows extras and spaces around `==`, and Renovate reads both.
EXACT_PIN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>[0-9][^\s;,]*)"
)
PIN_HOW_TO_FIX = (
    "left: the exact pins in the pyproject.toml files; right: the versions TECH_STACK.md "
    "tracks. A tracked version is a version in §1 followed by an HTML comment naming "
    "the PyPI package, as in 1.0.0<!-- renovate: NAME -->"
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
    "tag and digest there. This check compares only the versions the three files name"
)


def normalize(name: str) -> str:
    """The package name as PEP 503 spells it, so `Types_PyYAML` finds `types-pyyaml`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def renovate_config() -> dict[str, Any]:
    """Renovate's configuration for this repository."""
    config: dict[str, Any] = json.loads(RENOVATE.read_text())
    return config


def regex_manager() -> dict[str, Any]:
    """The one custom manager, which reads TECH_STACK.md."""
    [manager] = [
        m for m in renovate_config()["customManagers"] if m["customType"] == "regex"
    ]
    regex: dict[str, Any] = manager
    return regex


def uv_settings() -> dict[str, Any]:
    """The root pyproject.toml's [tool.uv] table."""
    root: dict[str, Any] = tomllib.loads((REPO / "pyproject.toml").read_text())
    settings: dict[str, Any] = root["tool"]["uv"]
    return settings


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
        for extra in (
            project.get("project", {}).get("optional-dependencies", {}).values()
        ):
            requirements += extra
        for group in project.get("dependency-groups", {}).values():
            requirements += [item for item in group if isinstance(item, str)]
        for requirement in requirements:
            if pin := EXACT_PIN.match(requirement):
                pins.setdefault(normalize(pin["name"]), set()).add(pin["version"])
    return pins


def tracked_versions(document: str) -> dict[str, set[str]]:
    """The versions the document tracks, found by the regex in Renovate's own
    configuration, so this check and the bot can't disagree about what is tracked."""
    tracked: dict[str, set[str]] = {}
    for pattern in regex_manager()["matchStrings"]:
        # Renovate names a group (?<name>...), Python (?P<name>...).
        python_pattern = re.sub(r"\(\?<(?=\w+>)", "(?P<", pattern)
        for match in re.finditer(python_pattern, document):
            tracked.setdefault(normalize(match["depName"]), set()).add(
                match["currentValue"]
            )
    return tracked


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


def test_every_exact_pin_is_tracked_in_tech_stack_at_its_version() -> None:
    pins = workspace_pins(REPO)

    assert pins
    assert pins == tracked_versions(TECH_STACK.read_text()), PIN_HOW_TO_FIX


def test_workspace_pins_reads_members_groups_extras_and_only_exact_pins(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "0"\n'
        'dependencies = ["Typer==0.27.2", "rich>=15", "psycopg[binary] == 3.3.6"]\n'
        '[project.optional-dependencies]\nspeed = ["orjson==3.12.0"]\n'
        '[dependency-groups]\ndev = ["ruff==0.16.9", {include-group = "stubs"}]\n'
        'stubs = ["Types_PyYAML==6.0.12.20260906"]\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    member = tmp_path / "packages" / "a"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        '[project]\nname = "a"\nversion = "0"\n'
        'dependencies = ["awslambdaric==4.1.0; sys_platform == \'linux\'", "ruff==0.16.8", "other"]\n'
    )

    assert workspace_pins(tmp_path) == {
        "typer": {"0.27.2"},
        "psycopg": {"3.3.6"},
        "orjson": {"3.12.0"},
        "ruff": {"0.16.8", "0.16.9"},
        "types-pyyaml": {"6.0.12.20260906"},
        "awslambdaric": {"4.1.0"},
    }


def test_tracked_versions_keeps_every_version_a_document_tracks_for_a_package() -> None:
    document = (
        "Ruff 0.16.9<!-- renovate: Ruff -->, ruff 0.16.8<!-- renovate: ruff -->, "
        "stubs 6.0.12<!-- renovate: Types_PyYAML -->, 1.0 with no comment"
    )

    assert tracked_versions(document) == {
        "ruff": {"0.16.8", "0.16.9"},
        "types-pyyaml": {"6.0.12"},
    }


def test_renovate_reads_tech_stack_for_pypi_versions() -> None:
    manager = regex_manager()

    assert any(
        re.search(p.strip("/"), TECH_STACK.name) for p in manager["managerFilePatterns"]
    )
    assert manager["datasourceTemplate"] == "pypi"
    assert manager["versioningTemplate"] == "pep440"


def test_renovate_runs_the_uv_the_workspace_requires() -> None:
    # Renovate reads required-version only from the pyproject it is updating, and
    # only the root has one. Members get this constraint instead, or the latest uv,
    # which refuses the workspace.
    assert renovate_config()["constraints"]["uv"] == uv_settings()["required-version"]


def test_renovate_waits_three_days_after_a_release() -> None:
    config = renovate_config()

    assert config["minimumReleaseAge"] == "3 days"
    # Renovate's defaults: a release that is still pending gets no pull request, and
    # one with no PyPI timestamp counts as pending. "none", "flexible" and
    # "timestamp-optional" would let them through.
    assert config.get("internalChecksFilter", "strict") == "strict"
    assert (
        config.get("minimumReleaseAgeBehaviour", "timestamp-required")
        == "timestamp-required"
    )
    assert [
        rule for rule in config["packageRules"] if "minimumReleaseAge" in rule
    ] == []


def test_pre_1_0_minor_updates_get_their_own_pull_request() -> None:
    rules = renovate_config()["packageRules"]
    [group_at] = [
        i
        for i, rule in enumerate(rules)
        if rule.get("groupName") == "python dependencies"
    ]
    # A later rule's `groupName: null` takes the update out of the group.
    pre_1_0_rules = [
        rule
        for rule in rules[group_at + 1 :]
        if rule.get("matchUpdateTypes") == ["minor"]
        and "groupName" in rule
        and rule["groupName"] is None
    ]

    assert len(pre_1_0_rules) == 1
    pre_1_0 = re.compile(pre_1_0_rules[0]["matchCurrentVersion"].strip("/"))
    assert pre_1_0.search("0.16.9")
    assert not pre_1_0.search("1.2.0")
    assert not pre_1_0.search("10.0.0")


def test_renovate_touches_only_what_adr_0031_allows() -> None:
    config = renovate_config()
    rules = config["packageRules"]
    group_names = [rule.get("groupName") for rule in rules]

    assert config["enabledManagers"] == ["pep621", "custom.regex"]
    assert config["lockFileMaintenance"]["enabled"] is True
    assert config["semanticCommits"] == "enabled"
    assert [rule for rule in [config, *rules] if rule.get("automerge")] == []
    disabled = [rule["matchDepTypes"] for rule in rules if rule.get("enabled") is False]
    assert disabled == [["requires-python", "build-system.requires"]]
    # Later rules win, so Playwright's own group must come after the weekly one.
    assert group_names.index("python dependencies") < group_names.index("playwright")


def test_the_checks_image_follows_the_playwright_pin() -> None:
    found = {
        name: set(re.findall(pattern, (HARNESS / name).read_text(), flags=re.MULTILINE))
        for name, pattern in CHECKS_IMAGE_PLAYWRIGHT.items()
    }

    assert found == dict.fromkeys(
        CHECKS_IMAGE_PLAYWRIGHT, workspace_pins(REPO)["playwright"]
    ), CHECKS_IMAGE_HOW_TO_FIX


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
