"""One trial of the hosted-compute spike (ADR-0008 amendment, 2026-09-30).

A trial launches Chromium with its sandbox on through `aqa_runner.sandbox`,
whose sandbox check proves the first half of the fresh-VM predicate, and
reports as JSON how long the browser took to be ready and the peak memory
of the trial and its browser with a page open."""

import asyncio
import os
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import NotRequired, TypedDict

from aqa_runner.sandbox import (
    Chromium,
    SandboxObservations,
    SandboxUnavailableError,
    launch_with_observations,
)
from playwright.async_api import Browser, async_playwright

# How often, and for how long after the launch, the trial samples its memory.
SAMPLE_SECONDS = 0.05
MEMORY_SECONDS = 1.0


class Sandbox(TypedDict):
    """Whether the sandbox check proved the sandbox, and if not, why."""

    on: bool
    error: NotRequired[str]


class Report(TypedDict):
    """A trial's JSON report."""

    run_id: str
    # Fresh-VM evidence a second trial can compare: the kernel's boot ID, and
    # the run IDs of trials this environment ran before this one, in the order
    # they ran.
    boot_id: str
    earlier_runs: list[str]
    sandbox: Sandbox
    # What the sandbox check read, whether it proved the sandbox or refused it:
    # the browser process and each renderer it compared with it (the README
    # gives the fields). None when it read nothing: the sandbox couldn't
    # start, or the OS has no sandbox check.
    sandbox_observed: dict[str, object] | None
    # From asking Playwright to launch until the sandbox check passed, and when
    # it passed, in seconds since the epoch by the host's clock: the scripts
    # time a run from their request to this moment.
    ready_seconds: float | None
    ready_at: float | None
    # The largest PSS sample of the trial's process and its descendants, over
    # MEMORY_SECONDS after the launch, with a blank page open in the browser.
    peak_memory_bytes: int


def trial_on_this_host() -> Report:
    """One trial on this host, as each candidate's entry point runs it. The
    run marker lives in the temporary directory, /tmp on every candidate."""
    if sys.platform != "linux":
        raise RuntimeError(f"the trial runs on Linux only, not {sys.platform}")
    return asyncio.run(
        _trial_on_this_host(Path(tempfile.gettempdir(), "aqa-spike-runs"))
    )


async def _trial_on_this_host(runs_file: Path) -> Report:
    async with async_playwright() as playwright:
        return await run_trial(playwright.chromium, Path("/proc"), runs_file)


async def run_trial(chromium: Chromium, proc: Path, runs_file: Path) -> Report:
    """Launch Chromium with its sandbox on, measure it, close it, and report.
    `proc` is where /proc is mounted, and `runs_file` the run marker: the file
    in which each trial in this environment records its run ID."""
    run_id = uuid.uuid4().hex
    earlier_runs = mark_run(runs_file, run_id)
    # Nothing else runs in the trial while the launch is timed: sampling memory
    # takes CPU that a small candidate would charge to the launch.
    started = time.perf_counter()
    browser: Browser | None
    observed: SandboxObservations | None
    try:
        browser, observed = await launch_with_observations(chromium)
    except SandboxUnavailableError as error:
        # The finding the spike looks for on a candidate, so it is reported.
        # Every other error still stops the trial.
        sandbox, observed = Sandbox(on=False, error=str(error)), error.observed
        ready = ready_at = browser = None
    else:
        sandbox, ready = Sandbox(on=True), time.perf_counter() - started
        ready_at = time.time()
    try:
        peak = await memory_with_a_page(proc, browser)
    finally:
        if browser is not None:
            await browser.close()
    return Report(
        run_id=run_id,
        earlier_runs=earlier_runs,
        boot_id=boot_id(proc),
        sandbox=sandbox,
        sandbox_observed=None if observed is None else asdict(observed),
        ready_seconds=ready,
        ready_at=ready_at,
        peak_memory_bytes=peak,
    )


async def memory_with_a_page(proc: Path, browser: Browser | None) -> int:
    """The trial's peak PSS over MEMORY_SECONDS, with a blank page open in
    `browser`, as a run's browser has at least one page. Without a browser
    (the sandbox didn't start), the trial's own."""
    async with peak_memory(proc, os.getpid()) as peak:
        if browser is not None:
            context = await browser.new_context()
            await context.new_page()
        await asyncio.sleep(MEMORY_SECONDS)
    return peak()


def mark_run(runs_file: Path, run_id: str) -> list[str]:
    """Record `run_id` in the run marker, and return the run IDs it held."""
    earlier_runs = runs_file.read_text().split() if runs_file.exists() else []
    with runs_file.open("a") as marker:
        marker.write(f"{run_id}\n")
    return earlier_runs


def boot_id(proc: Path) -> str:
    return (proc / "sys/kernel/random/boot_id").read_text().strip()


@asynccontextmanager
async def peak_memory(proc: Path, pid: int) -> AsyncIterator[Callable[[], int]]:
    """Samples the PSS of process `pid` and its descendants while the block
    runs, and yields a function that returns the largest sample. Summing PSS,
    unlike RSS, counts a page the browser's processes share once."""
    samples = [tree_pss(proc, pid)]

    async def sample() -> None:
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            samples.append(tree_pss(proc, pid))

    task = asyncio.create_task(sample())
    try:
        yield lambda: max(samples)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def tree_pss(proc: Path, root: int) -> int:
    """The PSS, in bytes, of process `root` and every descendant it has now."""
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if entry.name.isdigit() and (parent := _parent(entry)) is not None:
            children.setdefault(parent, []).append(int(entry.name))
    total, tree = 0, [root]
    while tree:
        pid = tree.pop()
        tree += children.get(pid, [])
        total += _pss(proc / str(pid))
    return total


def _parent(entry: Path) -> int | None:
    """The process's parent PID, or None once the process has exited."""
    try:
        stat = (entry / "stat").read_text()
    # Processes exit while the trial samples: /proc drops their entries.
    except FileNotFoundError, ProcessLookupError:
        return None
    # The command name, in parentheses, may itself hold spaces and ")".
    return int(stat.rpartition(")")[2].split()[1])


def _pss(entry: Path) -> int:
    """The process's PSS in bytes. A process that has exited holds no memory,
    and neither does one without a memory map (a zombie), which has no Pss
    line."""
    try:
        rollup = (entry / "smaps_rollup").read_text()
    except FileNotFoundError, ProcessLookupError:
        return 0
    for line in rollup.splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1]) * 1024
    return 0
