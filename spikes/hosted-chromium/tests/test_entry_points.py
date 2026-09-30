"""How each candidate runs the trial: the Fargate task's command, the Lambda
function's handler and the MicroVM's HTTP server (ADR-0008 amendment,
2026-09-30).

The command and the handler run a real trial on Linux, where the candidates
run. On any other OS they refuse, since the trial reads Linux's /proc."""

import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path

import pytest
from aqa_hosted_chromium_spike.lambda_function import handler
from aqa_hosted_chromium_spike.microvm import HOOKS, server
from aqa_hosted_chromium_spike.trial import Report, Sandbox, measure

LINUX_ONLY = "the trial runs on Linux only"


def test_the_trial_refuses_a_host_other_than_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(RuntimeError, match=f"{LINUX_ONLY}, not darwin"):
        measure()


def test_the_command_prints_each_trial_as_json(tmp_path: Path) -> None:
    # The run marker lives in the temporary directory, as /tmp on a candidate.
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    command = [sys.executable, "-m", "aqa_hosted_chromium_spike"]

    runs = [
        subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=120, check=False
        )
        for _ in range(2)
    ]

    if sys.platform != "linux":
        assert [run.returncode for run in runs] == [1, 1]
        assert all(LINUX_ONLY in run.stderr for run in runs), runs[0].stderr
        return
    assert [run.returncode for run in runs] == [0, 0], runs[0].stderr
    first, second = (json.loads(run.stdout) for run in runs)
    assert first["sandbox"] == {"on": True}
    assert first["ready_seconds"] > 0
    # Python, Playwright's driver and Chromium take far more than 10 MB.
    assert first["peak_memory_bytes"] > 10 * 2**20
    assert (
        first["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    )
    assert second["earlier_runs"] == [first["run_id"]]


def test_the_lambda_handler_returns_the_trial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    if sys.platform != "linux":
        with pytest.raises(RuntimeError, match=LINUX_ONLY):
            handler({}, None)
        return
    report = handler({}, None)
    assert report["sandbox"] == {"on": True}
    assert report["earlier_runs"] == []
    assert (tmp_path / "aqa-spike-runs").read_text() == f"{report['run_id']}\n"


# The MicroVM's server, with a trial that only counts its calls.
REPORT = Report(
    run_id="r1",
    boot_id="b1",
    earlier_runs=[],
    sandbox=Sandbox(on=True),
    ready_seconds=0.5,
    peak_memory_bytes=1,
)


@pytest.fixture
def microvm() -> Iterator[tuple[HTTPConnection, list[Report]]]:
    """A connection to the MicroVM's server on a free local port, and the
    reports of the trials it ran."""
    reports: list[Report] = []

    def trial() -> Report:
        reports.append(REPORT)
        return REPORT

    running = server(("127.0.0.1", 0), trial)
    thread = threading.Thread(target=running.serve_forever)
    thread.start()
    try:
        yield (
            HTTPConnection("127.0.0.1", running.server_address[1], timeout=10),
            reports,
        )
    finally:
        running.shutdown()
        thread.join()
        running.server_close()


@pytest.mark.parametrize("hook", ["ready", "run"])
def test_the_microvm_answers_its_hooks_without_a_trial(
    microvm: tuple[HTTPConnection, list[Report]], hook: str
) -> None:
    connection, reports = microvm

    connection.request("POST", f"{HOOKS}{hook}", body=b'{"microvmId": "mvm-1"}')
    response = connection.getresponse()

    assert response.status == 200
    assert reports == [], "a hook ran a trial, so the snapshot would hold a browser"


def test_the_microvm_runs_a_trial_per_request(
    microvm: tuple[HTTPConnection, list[Report]],
) -> None:
    connection, reports = microvm

    connection.request("POST", "/trial")
    response = connection.getresponse()

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json"
    assert json.loads(response.read()) == REPORT
    assert reports == [REPORT]


# A trial changes state (the run marker), so only a POST runs one.
@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("POST", "/", 404), ("POST", f"{HOOKS}validate", 404), ("GET", "/trial", 501)],
)
def test_the_microvm_runs_no_trial_for_other_requests(
    microvm: tuple[HTTPConnection, list[Report]], method: str, path: str, status: int
) -> None:
    connection, reports = microvm

    connection.request(method, path)
    response = connection.getresponse()

    assert response.status == status
    assert reports == []
