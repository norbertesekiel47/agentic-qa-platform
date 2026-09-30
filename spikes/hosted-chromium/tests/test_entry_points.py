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
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path

import pytest
from aqa_hosted_chromium_spike.lambda_function import handler
from aqa_hosted_chromium_spike.microvm import HOOK_PREFIX, server
from aqa_hosted_chromium_spike.trial import (
    Report,
    Sandbox,
    tree_pss,
    trial_on_this_host,
)

LINUX_ONLY = "the trial runs on Linux only"


def test_the_trial_refuses_a_host_other_than_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(RuntimeError, match=f"{LINUX_ONLY}, not darwin"):
        trial_on_this_host()


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
    before = tree_pss(Path("/proc"), os.getpid())
    report = handler({}, None)
    assert report["sandbox"] == {"on": True}
    assert report["earlier_runs"] == []
    assert report["run_id"] in (tmp_path / "aqa-spike-runs").read_text()
    # The peak counts the browser and its page, not only this process.
    assert report["peak_memory_bytes"] > before + 100 * 2**20


# The MicroVM's server, with a trial that only counts its calls.
REPORT = Report(
    run_id="r1",
    boot_id="b1",
    earlier_runs=[],
    sandbox=Sandbox(on=True),
    ready_seconds=0.5,
    peak_memory_bytes=1,
)


@dataclass(frozen=True)
class MicroVM:
    """A connection to the MicroVM's server, and the reports of the trials it
    ran."""

    connection: HTTPConnection
    reports: list[Report]

    def post(self, path: str, body: bytes = b"") -> int:
        self.connection.request("POST", path, body=body)
        return self.connection.getresponse().status


@pytest.fixture
def microvm() -> Iterator[MicroVM]:
    """The MicroVM's server on a free local port, with a trial that returns
    REPORT."""
    reports: list[Report] = []

    def trial() -> Report:
        reports.append(REPORT)
        return REPORT

    running = server(("127.0.0.1", 0), trial)
    thread = threading.Thread(target=running.serve_forever)
    thread.start()
    try:
        port = running.server_address[1]
        yield MicroVM(HTTPConnection("127.0.0.1", port, timeout=10), reports)
    finally:
        running.shutdown()
        thread.join()
        running.server_close()


@pytest.mark.parametrize("hook", ["ready", "run", "resume", "suspend", "terminate"])
def test_the_microvm_answers_its_lifecycle_hooks_without_a_trial(
    microvm: MicroVM, hook: str
) -> None:
    status = microvm.post(f"{HOOK_PREFIX}{hook}", body=b'{"microvmId": "mvm-1"}')

    assert status == 200
    assert microvm.reports == [], "a hook ran a trial: a browser in the snapshot"


# Lambda's /run carries a JSON body with a payload of up to 16 KB. A server
# that replies without reading a body closes a socket with unread bytes, and
# the reset that follows can lose its reply. A body larger than the socket's
# buffers makes that loss certain.
def test_the_microvm_reads_a_hook_body_before_it_replies(microvm: MicroVM) -> None:
    assert microvm.post(f"{HOOK_PREFIX}run", body=b"x" * 2**25) == 200


def test_the_microvm_runs_a_trial_per_request(microvm: MicroVM) -> None:
    microvm.connection.request("POST", "/trial")
    response = microvm.connection.getresponse()

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json"
    assert json.loads(response.read()) == REPORT
    assert microvm.reports == [REPORT]


# A trial changes state (the run marker), so only a POST to /trial runs one.
@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/"), ("POST", f"{HOOK_PREFIX}validate"), ("GET", "/trial")],
)
def test_the_microvm_runs_no_trial_for_other_requests(
    microvm: MicroVM, method: str, path: str
) -> None:
    microvm.connection.request(method, path)

    assert microvm.connection.getresponse().status >= 400
    assert microvm.reports == []
