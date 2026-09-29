"""Fixtures for the aqa command's tests."""

import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture
def aqa() -> str:
    """The installed aqa console script: the interface these tests exercise."""
    script = shutil.which("aqa", path=str(Path(sys.executable).parent))
    assert script is not None, "the aqa console script is not installed; run `uv sync`"
    return script
