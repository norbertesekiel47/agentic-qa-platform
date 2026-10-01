"""The spike's trial (ADR-0008 amendment, 2026-09-30): what it reports about
sandboxed Chromium, its readiness, its memory and the environment it ran in.

How a launch is judged sandboxed belongs to `aqa_runner.sandbox` and its tests;
the trial reports what `launch` decides."""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from aqa_hosted_chromium_spike import trial
from aqa_hosted_chromium_spike.trial import (
    SAMPLE_SECONDS,
    Report,
    peak_memory,
    run_trial,
)
from aqa_runner.sandbox import SandboxUnavailableError
from playwright.async_api import Browser, BrowserType, Error, async_playwright

BOOT_ID = "8c0e4c6a-6a3c-4a8e-9d0c-3f1b7e2a5d10"
REFUSED = "fake: this host can't create user namespaces"


def set_process(root: Path, pid: int, parent: int, pss_kb: int | None) -> None:
    """Show process `pid` in the fake /proc at `root`, as Linux does: its
    parent, and its PSS in kB. A PSS of None is a process with no memory map,
    such as a zombie."""
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    # A command name may hold spaces and parentheses; Linux wraps it in ().
    (entry / "stat").write_text(f"{pid} (a (b) c) S {parent} {pid} 0 0 -1\n")
    rollup = "" if pss_kb is None else f"Rss: {pss_kb * 2} kB\nPss: {pss_kb} kB\n"
    (entry / "smaps_rollup").write_text(rollup)


def fake_proc(root: Path, processes: dict[int, tuple[int, int | None]]) -> Path:
    """A /proc at `root` listing each process's parent and PSS in kB."""
    for pid, (parent, pss_kb) in processes.items():
        set_process(root, pid, parent, pss_kb)
    (root / "sys/kernel/random").mkdir(parents=True)
    (root / "sys/kernel/random/boot_id").write_text(BOOT_ID + "\n")
    return root


@pytest.fixture(autouse=True)
def short_memory_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memory window long enough for every sample these tests look for."""
    monkeypatch.setattr(trial, "MEMORY_SECONDS", 12 * SAMPLE_SECONDS)


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
        self.environments: list[dict[str, str | float | bool]] = []

    async def launch(
        self, *, chromium_sandbox: bool, env: dict[str, str | float | bool]
    ) -> Browser:
        self.requested.append(chromium_sandbox)
        self.environments.append(env)
        raise self.error


def refused() -> FailingChromium:
    """A launch that `aqa_runner.sandbox` refuses."""
    return FailingChromium(SandboxUnavailableError(REFUSED))


class DelayedChromium:
    """Playwright's Chromium, launched after a delay the trial must time."""

    def __init__(self, chromium: BrowserType, delay: float) -> None:
        self.chromium = chromium
        self.delay = delay
        self.requested: list[bool] = []

    async def launch(
        self, *, chromium_sandbox: bool, env: dict[str, str | float | bool]
    ) -> Browser:
        self.requested.append(chromium_sandbox)
        await asyncio.sleep(self.delay)
        return await self.chromium.launch(chromium_sandbox=chromium_sandbox, env=env)


def test_trial_reports_a_sandboxed_browser(tmp_path: Path, runs: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 2048)})

    async def scenario() -> tuple[Report, list[bool]]:
        async with async_playwright() as playwright:
            chromium = DelayedChromium(playwright.chromium, delay=0.3)
            return await run_trial(chromium, proc, runs), chromium.requested

    started = time.time()
    report, requested = asyncio.run(scenario())
    finished = time.time()

    assert requested == [True], "the trial didn't ask for the sandbox"
    assert report["sandbox"] == {"on": True}
    # The timer runs from before the launch until the sandbox check passed.
    assert report["ready_seconds"] is not None
    assert report["ready_seconds"] >= 0.3
    # When that was, by the host's clock: after the delay, and before the
    # memory window that follows the launch.
    assert report["ready_at"] is not None
    assert started + 0.3 <= report["ready_at"] <= finished - trial.MEMORY_SECONDS
    assert report["peak_memory_bytes"] == 2048 * 1024
    assert report["boot_id"] == BOOT_ID


# On an OS with no sandbox check, launch refuses even a sandboxed browser: the
# report follows the check, not the browser. The sandbox stays on (ADR-0026).
def test_trial_reports_what_the_sandbox_check_decides(
    proc: Path, runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> Report:
        async with async_playwright() as playwright:
            # Only around the trial: the driver starts and stops as on this OS.
            monkeypatch.setattr(sys, "platform", "win32")
            try:
                return await run_trial(playwright.chromium, proc, runs)
            finally:
                monkeypatch.undo()

    report = asyncio.run(scenario())

    assert report["sandbox"]["on"] is False
    assert "no sandbox check exists for win32" in report["sandbox"]["error"]
    assert report["ready_seconds"] is None


def test_trial_reports_a_sandbox_the_runner_refuses(proc: Path, runs: Path) -> None:
    chromium = refused()

    report = asyncio.run(run_trial(chromium, proc, runs))

    assert chromium.requested == [True], "the trial didn't ask for the sandbox"
    assert chromium.environments == [{}], "the trial's browser got an environment"
    assert report["sandbox"] == {"on": False, "error": REFUSED}
    assert report["ready_seconds"] is None
    assert report["ready_at"] is None
    assert report["peak_memory_bytes"] == 512 * 1024


@pytest.mark.parametrize(
    "error",
    [
        Error("BrowserType.launch: Executable doesn't exist at /ms-playwright/chrome"),
        RuntimeError("the driver crashed"),
    ],
    ids=["playwright", "runtime"],
)
def test_other_launch_errors_stop_the_trial(
    proc: Path, runs: Path, error: Exception
) -> None:
    with pytest.raises(type(error)) as stopped:
        asyncio.run(run_trial(FailingChromium(error), proc, runs))

    assert stopped.value is error


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


# The launch is timed with nothing else running in the trial: sampling costs
# CPU that a small candidate would charge to the launch. Memory is sampled
# afterwards, over a window with the browser (and a blank page) open.


class SwellingChromium(FailingChromium):
    """A launch during which the trial's own memory swells, then shrinks back,
    before the launch is refused."""

    def __init__(self, proc: Path) -> None:
        super().__init__(SandboxUnavailableError(REFUSED))
        self.proc = proc

    async def launch(
        self, *, chromium_sandbox: bool, env: dict[str, str | float | bool]
    ) -> Browser:
        set_process(self.proc, os.getpid(), 1, 999_999)
        await asyncio.sleep(4 * SAMPLE_SECONDS)
        set_process(self.proc, os.getpid(), 1, 512)
        return await super().launch(chromium_sandbox=chromium_sandbox, env=env)


def test_the_launch_is_timed_without_sampling_memory(proc: Path, runs: Path) -> None:
    report = asyncio.run(run_trial(SwellingChromium(proc), proc, runs))

    assert report["peak_memory_bytes"] == 512 * 1024


class LingeringChromium(FailingChromium):
    """A refused launch that leaves a child process behind for a while, as a
    browser that is still up would."""

    def __init__(self, proc: Path) -> None:
        super().__init__(SandboxUnavailableError(REFUSED))
        self.proc = proc
        self.lingering: list[asyncio.Task[None]] = []

    async def launch(
        self, *, chromium_sandbox: bool, env: dict[str, str | float | bool]
    ) -> Browser:
        self.lingering.append(asyncio.create_task(self.linger()))
        return await super().launch(chromium_sandbox=chromium_sandbox, env=env)

    async def linger(self) -> None:
        await asyncio.sleep(SAMPLE_SECONDS)
        set_process(self.proc, 900001, os.getpid(), 50_000)
        await asyncio.sleep(4 * SAMPLE_SECONDS)
        shutil.rmtree(self.proc / "900001")


def test_memory_is_sampled_after_the_launch(proc: Path, runs: Path) -> None:
    report = asyncio.run(run_trial(LingeringChromium(proc), proc, runs))

    assert report["peak_memory_bytes"] == (512 + 50_000) * 1024


def test_the_peak_is_the_largest_sample(proc: Path) -> None:
    me, child = os.getpid(), 900001

    async def scenario() -> int:
        async with peak_memory(proc, me) as peak:
            set_process(proc, child, me, 50_000)
            await asyncio.sleep(4 * SAMPLE_SECONDS)
            shutil.rmtree(proc / str(child))
            await asyncio.sleep(2 * SAMPLE_SECONDS)
        return peak()

    assert asyncio.run(scenario()) == (512 + 50_000) * 1024
