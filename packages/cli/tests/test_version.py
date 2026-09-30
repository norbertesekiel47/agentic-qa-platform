"""`aqa --version`, run through the installed console script."""

import subprocess
import tomllib
from pathlib import Path

CLI_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_prints_the_cli_package_version(aqa: str) -> None:
    declared = tomllib.loads(CLI_PYPROJECT.read_text())["project"]["version"]

    result = subprocess.run(
        [aqa, "--version"], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"aqa {declared}\n"


def test_broken() -> None:
    assert int("1") == 2
