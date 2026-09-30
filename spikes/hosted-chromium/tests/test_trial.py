"""The spike's trial (ADR-0008 amendment, 2026-09-30): what it reports about
sandboxed Chromium, its readiness, its memory and the environment it ran in."""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from aqa_hosted_chromium_spike.trial import Report, run_trial
from playwright.async_api import Browser, BrowserType, Error, async_playwright

BOOT_ID = "8c0e4c6a-6a3c-4a8e-9d0c-3f1b7e2a5d10"


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


def in_playwright(trial: Callable[[BrowserType], Awaitable[Report]]) -> Report:
    """Runs `trial` with Playwright's Chromium on this host."""

    async def scenario() -> Report:
        async with async_playwright() as playwright:
            return await trial(playwright.chromium)

    return asyncio.run(scenario())


def test_trial_reports_a_sandboxed_browser(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 2048)})

    report = in_playwright(
        lambda chromium: run_trial(chromium, proc, tmp_path / "runs")
    )

    assert report["sandbox"] == {"on": True}
    assert report["ready_seconds"] is not None
    assert report["ready_seconds"] > 0
    assert report["peak_memory_bytes"] == 2048 * 1024
    assert report["boot_id"] == BOOT_ID


# The error Playwright 1.63 raised on Linux when Chromium's sandbox couldn't
# start (Docker's default seccomp profile), cut short as in the runner's tests.
NO_USABLE_SANDBOX = """\
BrowserType.launch: Target page, context or browser has been closed
Browser logs:
Chromium sandboxing failed!

Call log:
  - <launched> pid=19
  - [pid=19][err] [0930/154953.535932:FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] No usable sandbox! If you are running on Ubuntu 23.10+ or another Linux distro that has disabled unprivileged user namespaces with AppArmor, see https://chromium.googlesource.com/chromium/src/+/mai
  - [pid=19] <process did exit: exitCode=null, signal=SIGTRAP>
"""


class FailingChromium:
    """A launch that fails with Playwright's error before any browser runs."""

    def __init__(self, message: str) -> None:
        self.message = message

    async def launch(self, *, chromium_sandbox: bool) -> Browser:
        raise Error(f"{self.message} (sandbox requested: {chromium_sandbox})")


def test_trial_reports_a_sandbox_that_cannot_start(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 512)})

    report = asyncio.run(
        run_trial(FailingChromium(NO_USABLE_SANDBOX), proc, tmp_path / "runs")
    )

    assert report["sandbox"]["on"] is False
    assert "Chromium's sandbox can't start on this host" in report["sandbox"]["error"]
    assert report["ready_seconds"] is None
    assert report["peak_memory_bytes"] == 512 * 1024


class UnsandboxedChromium:
    """Real Chromium that ignores the sandbox request."""

    def __init__(self, chromium: BrowserType) -> None:
        self.chromium = chromium

    async def launch(self, *, chromium_sandbox: bool) -> Browser:
        assert chromium_sandbox, "the trial didn't ask for the sandbox"
        return await self.chromium.launch(
            chromium_sandbox=False,  # ADR-0026: a negative control for the trial
        )


# What each OS's sandbox check reports about a renderer without its sandbox.
UNSANDBOXED_ON = {
    "linux": "shares the browser's pid namespace",
    "darwin": "isn't sandboxed",
}


def test_trial_reports_a_browser_the_sandbox_check_refuses(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 512)})

    report = in_playwright(
        lambda chromium: run_trial(
            UnsandboxedChromium(chromium), proc, tmp_path / "runs"
        )
    )

    assert report["sandbox"]["on"] is False
    assert UNSANDBOXED_ON[sys.platform] in report["sandbox"]["error"]
    assert report["ready_seconds"] is None


def test_other_launch_errors_stop_the_trial(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 512)})
    missing = "BrowserType.launch: Executable doesn't exist at /ms-playwright/chrome"

    with pytest.raises(Error, match="Executable doesn't exist"):
        asyncio.run(run_trial(FailingChromium(missing), proc, tmp_path / "runs"))


# The run marker is the fresh-VM evidence a second trial can compare: an
# environment that ran a trial before shows it. The sandbox's outcome doesn't
# matter to it, so these trials fail fast without a browser.


def test_a_fresh_environment_shows_no_earlier_runs(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 512)})
    chromium = FailingChromium(NO_USABLE_SANDBOX)

    report = asyncio.run(run_trial(chromium, proc, tmp_path / "runs"))

    assert report["earlier_runs"] == []
    assert report["run_id"]


def test_a_second_trial_in_the_same_environment_sees_the_first(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", {os.getpid(): (1, 512)})
    chromium = FailingChromium(NO_USABLE_SANDBOX)

    first = asyncio.run(run_trial(chromium, proc, tmp_path / "runs"))
    second = asyncio.run(run_trial(chromium, proc, tmp_path / "runs"))
    third = asyncio.run(run_trial(chromium, proc, tmp_path / "runs"))

    assert second["run_id"] != first["run_id"]
    assert second["earlier_runs"] == [first["run_id"]]
    assert third["earlier_runs"] == [first["run_id"], second["run_id"]]


def test_memory_is_the_pss_of_the_trial_and_its_descendants_only(
    tmp_path: Path,
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
    chromium = FailingChromium(NO_USABLE_SANDBOX)

    report = asyncio.run(run_trial(chromium, proc, tmp_path / "runs"))

    assert report["peak_memory_bytes"] == (1000 + 2000 + 4000 + 8000) * 1024


@pytest.mark.parametrize("gone", ["stat", "smaps_rollup"])
def test_a_process_that_exits_mid_sample_is_left_out(tmp_path: Path, gone: str) -> None:
    me, child = os.getpid(), 900001
    proc = fake_proc(tmp_path / "proc", {me: (1, 1000), child: (me, 2000)})
    # The process was listed, then exited before this file was read.
    (proc / str(child) / gone).unlink()
    chromium = FailingChromium(NO_USABLE_SANDBOX)

    report = asyncio.run(run_trial(chromium, proc, tmp_path / "runs"))

    assert report["peak_memory_bytes"] == 1000 * 1024
