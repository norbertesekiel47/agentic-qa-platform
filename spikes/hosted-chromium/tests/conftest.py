"""The dry run's fixture for the spike's script tests (dry_run.py)."""

import shutil
import sys
from pathlib import Path

import pytest
from dry_run import COMMIT, FAKE, SPIKE, DryRun


@pytest.fixture
def dry_run(tmp_path: Path) -> DryRun:
    shutil.copytree(SPIKE / "scripts", tmp_path / "spike/scripts")
    (tmp_path / "spike/build").mkdir()
    (tmp_path / f"spike/build/microvm-{COMMIT}.zip").write_bytes(b"fake zip")
    (tmp_path / "bin").mkdir()
    # sleep too, so a poll or an IAM settle takes no time.
    for tool in ("aws", "docker", "curl", "sleep"):
        shim = tmp_path / "bin" / tool
        # -S: the fake needs only the standard library, and starts faster.
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "{FAKE}" {tool} "$@"\n')
        shim.chmod(0o755)
    return DryRun(tmp_path)
