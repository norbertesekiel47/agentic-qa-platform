"""The spike's trial (ADR-0008 amendment, 2026-09-30): what it reports about
sandboxed Chromium, its readiness, its memory and the environment it ran in.

How a launch is judged sandboxed belongs to `aqa_runner.sandbox` and its tests;
the trial reports what `launch` decides."""

import asyncio
import os
from pathlib import Path

import pytest
from aqa_hosted_chromium_spike.trial import Report, run_trial
from aqa_runner.sandbox import SandboxUnavailableError
from playwright.async_api import Browser, Error, async_playwright

BOOT_ID = "8c0e4c6a-6a3c-4a8e-9d0c-3f1b7e2a5d10"
REFUSED = "fake: this host can't create user namespaces"


def fake_proc(root: Path, processes: dict[int, tuple[int, int | None]]) -> Path:
    """A /proc listing each process's parent and its PSS in kB, as Linux shows
    them. A PSS of None is a process with no memory map, such as a zombie."""
    for pid, (parent, pss_kb) in processes.items():
        entry = root / str(pid)
        entry.mkdir(parents=True)
        # A command name may hold spaces and parentheses; Linux wraps it in ().
        (entry / "stat").write_text(f"{pid} (a (b) c) S {parent} {pid} 0 0 -1\n")
        rollup = "" if pss_kb is None else f"Rss: {pss_kb * 2} kB\nPss: {pss_kb} kB\n"
        (entry / "smaps_rollup").write_text(rollup)
    (root / "sys/kernel/random").mkdir(parents=True)
    (root / "sys/kernel/random/boot_id").write_text(BOOT_ID + "\n")
    return root


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    """A /proc in which the trial's process, at 512 kB, is the only one."""
    return fake_proc(tmp_path / "proc", {os.getpid(): (1, 512)})


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    """The run marker of an environment that hasn't run a trial yet."""
    return tmp_path / "runs"


class FailingChromium:
    """A launch that raises `error` before any browser runs. It records each
    sandbox request."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.requested: list[bool] = []

    async def launch(self, *, chromium_sandbox: bool) -> Browser:
        self.requested.append(chromium_sandbox)
        raise self.error


def refused() -> FailingChromium:
    """A launch that `aqa_runner.sandbox` refuses."""
    return FailingChromium(SandboxUnavailableError(REFUSED))


def test_trial_reports_a_sandboxed_browser(tmp_path: Path, runs: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 2048)})

    async def scenario() -> Report:
        async with async_playwright() as playwright:
            return await run_trial(playwright.chromium, proc, runs)

    report = asyncio.run(scenario())

    assert report["sandbox"] == {"on": True}
    assert report["ready_seconds"] is not None
    assert report["ready_seconds"] > 0
    assert report["peak_memory_bytes"] == 2048 * 1024
    assert report["boot_id"] == BOOT_ID


def test_trial_reports_a_sandbox_the_runner_refuses(proc: Path, runs: Path) -> None:
    chromium = refused()

    report = asyncio.run(run_trial(chromium, proc, runs))

    assert chromium.requested == [True], "the trial didn't ask for the sandbox"
    assert report["sandbox"] == {"on": False, "error": REFUSED}
    assert report["ready_seconds"] is None
    assert report["peak_memory_bytes"] == 512 * 1024


def test_other_launch_errors_stop_the_trial(proc: Path, runs: Path) -> None:
    missing = "BrowserType.launch: Executable doesn't exist at /ms-playwright/chrome"

    with pytest.raises(Error, match="Executable doesn't exist"):
        asyncio.run(run_trial(FailingChromium(Error(missing)), proc, runs))


# The run marker is the fresh-VM evidence a second trial can compare: an
# environment that ran a trial before shows it. The sandbox's outcome doesn't
# matter to it, so these trials end without a browser.


def test_a_fresh_environment_shows_no_earlier_runs(proc: Path, runs: Path) -> None:
    report = asyncio.run(run_trial(refused(), proc, runs))

    assert report["earlier_runs"] == []
    assert report["run_id"]


def test_a_second_trial_in_the_same_environment_sees_the_first(
    proc: Path, runs: Path
) -> None:
    first, second, third = (
        asyncio.run(run_trial(refused(), proc, runs)) for _ in range(3)
    )

    assert second["run_id"] != first["run_id"]
    assert second["earlier_runs"] == [first["run_id"]]
    assert third["earlier_runs"] == [first["run_id"], second["run_id"]]


def test_memory_is_the_pss_of_the_trial_and_its_descendants_only(
    tmp_path: Path, runs: Path
) -> None:
    me = os.getpid()
    # Playwright's driver, the browser under it and a renderer under that;
    # an exited child with no memory map; and a process outside the trial.
    driver, browser, renderer, zombie, stranger = 900001, 900002, 900003, 900004, 900005
    proc = fake_proc(
        tmp_path / "proc",
        {
            me: (1, 1000),
            driver: (me, 2000),
            browser: (driver, 4000),
            renderer: (browser, 8000),
            zombie: (me, None),
            stranger: (1, 64000),
        },
    )

    report = asyncio.run(run_trial(refused(), proc, runs))

    assert report["peak_memory_bytes"] == (1000 + 2000 + 4000 + 8000) * 1024


@pytest.mark.parametrize("gone", ["stat", "smaps_rollup"])
def test_a_process_that_exits_mid_sample_is_left_out(
    tmp_path: Path, runs: Path, gone: str
) -> None:
    me, child = os.getpid(), 900001
    proc = fake_proc(tmp_path / "proc", {me: (1, 1000), child: (me, 2000)})
    # The process was listed, then exited before this file was read.
    (proc / str(child) / gone).unlink()

    report = asyncio.run(run_trial(refused(), proc, runs))

    assert report["peak_memory_bytes"] == 1000 * 1024
