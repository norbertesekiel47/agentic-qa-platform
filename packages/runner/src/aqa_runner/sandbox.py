"""Chromium's sandbox: always on, and proved by the sandbox check before any
page loads (ADR-0026 and its 2026-09-30 amendment; SECURITY.md §6). The same
launch gives the browser an empty environment (ADR-0026's 2026-10-01
amendment; SECURITY.md §5)."""

import ctypes
import sys
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.async_api import Browser, Error

# Chromium's namespace sandbox gives each renderer its own user, pid and net
# namespace. Its renderers are forked from the zygote, which already lives in
# the new namespaces, so they are compared with the browser, never with their
# parent.
NAMESPACES = ("user", "pid", "net")

# With nothing to compare, the check fails rather than passing by default.
NO_RENDERER = "no renderer process to check"

# libproc's buffer size for a process's executable path (<sys/proc_info.h>).
PROC_PIDPATHINFO_MAXSIZE = 4096

# How to fix a host where Chromium can't sandbox, by OS (ADR-0026).
LINUX_FIX = (
    "On Linux, Chromium's sandbox needs unprivileged user namespaces. On a "
    "throwaway host such as a CI runner, where AppArmor restricts them (Ubuntu "
    "23.10 and later), allow them with "
    "`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. On a "
    "machine you keep, give Chromium an AppArmor profile instead: "
    "https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md. "
    "In a container, run with Playwright's seccomp profile: "
    "https://playwright.dev/python/docs/docker."
)
HOST_FIXES = {
    "linux": LINUX_FIX,
    "darwin": (
        "On macOS, Chromium sandboxes each renderer itself, which fails when "
        "the browser already runs inside another sandbox: start the runner "
        "outside it."
    ),
}
OTHER_FIX = "Run the runner on Linux or macOS."

# The lines Chromium's zygote logs on Linux when its sandbox can't start, and
# how to fix the host for each. Playwright passes the browser's log through in
# its error. Chromium's root message goes on to name the switch that turns the
# sandbox off, so only its start is matched.
NO_SANDBOX_LOGS = {
    "No usable sandbox": LINUX_FIX,
    "Running as root without": (
        "Chromium won't sandbox a browser that runs as root: run the runner as "
        "a non-root user."
    ),
}


@dataclass(frozen=True)
class LinuxProcess:
    """What the sandbox check compares on Linux: a process's namespaces, by
    name (`user`, `pid`, `net`), and the seccomp filters attached to it."""

    pid: int
    namespaces: Mapping[str, str]
    seccomp_filters: int


def compare_linux(
    browser: LinuxProcess, renderers: Sequence[LinuxProcess]
) -> list[str]:
    """Why the renderers can't be shown to be sandboxed; empty when they are."""
    if not renderers:
        return [NO_RENDERER]
    problems: list[str] = []
    for renderer in renderers:
        problems += [
            f"renderer {renderer.pid} shares the browser's {name} namespace "
            f"({renderer.namespaces[name]})"
            for name in NAMESPACES
            if renderer.namespaces[name] == browser.namespaces[name]
        ]
        # A count, not the seccomp mode: in a container every process, the
        # browser included, already runs under a filter.
        if renderer.seccomp_filters <= browser.seccomp_filters:
            problems.append(
                f"renderer {renderer.pid} has no seccomp filter of its own "
                f"({renderer.seccomp_filters} filters; the browser has "
                f"{browser.seccomp_filters})"
            )
    return problems


@dataclass(frozen=True)
class MacProcess:
    """What the sandbox check compares on macOS: whether `sandbox_check`
    reports the process as sandboxed."""

    pid: int
    sandboxed: bool


def compare_macos(browser: MacProcess, renderers: Sequence[MacProcess]) -> list[str]:
    """Why the renderers can't be shown to be sandboxed; empty when they are."""
    if not renderers:
        return [NO_RENDERER]
    problems = [
        f"renderer {renderer.pid} isn't sandboxed"
        for renderer in renderers
        if not renderer.sandboxed
    ]
    # An unsandboxed browser also shows `sandbox_check` can answer no.
    if browser.sandboxed:
        problems.append(
            f"the browser process {browser.pid} is sandboxed too, so the check "
            "can't tell its renderers apart from it"
        )
    return problems


class SandboxUnavailableError(RuntimeError):
    """Chromium can't run with its sandbox on this host: an infrastructure
    error, never a run outcome (API.md §7, ADR-0026)."""

    exit_code = 10


class Chromium(Protocol):
    """The part of Playwright's `BrowserType` that `launch` uses."""

    async def launch(
        self, *, chromium_sandbox: bool, env: dict[str, str | float | bool]
    ) -> Browser: ...


async def launch(chromium: Chromium) -> Browser:
    """Launch Chromium with its sandbox on and an empty environment, and return
    it only once the sandbox check has proved the sandbox. Nothing skips the
    check: `launch` takes no option and reads no setting or environment
    variable (ADR-0026)."""
    try:
        # Playwright's default environment for the browser is the runner's
        # own, provider keys and cloud credentials included
        # (https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-option-env).
        # An empty one keeps them, and every AQA_SECRET_* value, out of every
        # Chromium process; a test secret reaches a page only through
        # fill_secret.
        browser = await chromium.launch(chromium_sandbox=True, env={})
    except Error as error:
        fixes = [fix for line, fix in NO_SANDBOX_LOGS.items() if line in error.message]
        if not fixes:
            raise
        raise SandboxUnavailableError(
            f"Chromium's sandbox can't start on this host. {fixes[0]}"
        ) from error
    async with AsyncExitStack() as on_failure:
        on_failure.push_async_callback(browser.close)
        if problems := await check_sandbox(browser):
            raise SandboxUnavailableError(
                "Chromium started without a sandbox the sandbox check can prove: "
                f"{'; '.join(problems)}. {HOST_FIXES.get(sys.platform, OTHER_FIX)}"
            )
        on_failure.pop_all()
    return browser


async def check_sandbox(browser: Browser) -> list[str]:
    """Why the browser's renderers can't be shown to be sandboxed; empty when
    they are. The browser must have been launched on this host, not connected
    to. A renderer exists only once a page does, so the check opens a blank
    page in a context of its own and closes it before returning."""
    context = await browser.new_context()
    try:
        await context.new_page()
        browser_pid, renderer_pids = await _process_ids(browser)
        return check_processes(browser_pid, renderer_pids)
    finally:
        await context.close()


async def _process_ids(browser: Browser) -> tuple[int, list[int]]:
    """The browser's PID and its renderers' PIDs, as the host sees them.

    CDP's `SystemInfo.getProcessInfo` lists every process the browser runs
    (https://chromedevtools.github.io/devtools-protocol/tot/SystemInfo/#method-getProcessInfo),
    so the check never guesses from the process tree, where a renderer's
    parent is the zygote, not the browser."""
    session = await browser.new_browser_cdp_session()
    try:
        info = await session.send("SystemInfo.getProcessInfo")
    finally:
        await session.detach()
    processes = [
        (str(process["type"]), int(process["id"])) for process in info["processInfo"]
    ]
    [browser_pid] = [pid for kind, pid in processes if kind == "browser"]
    return browser_pid, [pid for kind, pid in processes if kind == "renderer"]


def check_processes(browser_pid: int, renderer_pids: list[int]) -> list[str]:
    """Why the renderer processes can't be shown to be sandboxed, compared
    with the browser process; empty when they are. The PIDs are this host's,
    so the browser must have been launched here, not connected to. A process
    that has exited stops the check with an `OSError`."""
    if sys.platform == "linux":
        return compare_linux(
            _read_linux(browser_pid), [_read_linux(pid) for pid in renderer_pids]
        )
    if sys.platform == "darwin":
        return compare_macos(
            _read_macos(browser_pid), [_read_macos(pid) for pid in renderer_pids]
        )
    return [f"no sandbox check exists for {sys.platform}"]


def _read_linux(pid: int) -> LinuxProcess:
    proc = Path("/proc", str(pid))
    status = {
        key: value.strip()
        for key, _, value in (
            line.partition(":") for line in (proc / "status").read_text().splitlines()
        )
    }
    return LinuxProcess(
        pid=pid,
        namespaces={name: str((proc / "ns" / name).readlink()) for name in NAMESPACES},
        # Linux 5.9 and later report the count (ADR-0026 amendment).
        seccomp_filters=int(status["Seccomp_filters"]),
    )


def _read_macos(pid: int) -> MacProcess:
    # sandbox_check(pid, NULL, SANDBOX_FILTER_NONE) is 1 when the process is
    # sandboxed. libSystem exports it but Apple doesn't document it (ADR-0026
    # amendment), and it also answers 1 for a PID with no running process: a
    # zombie, or one already reaped. So the process must still be running
    # once it has answered, which proc_pidpath reports.
    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    sandbox_check = libsystem.sandbox_check
    sandbox_check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    sandbox_check.restype = ctypes.c_int
    proc_pidpath = libsystem.proc_pidpath
    proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
    proc_pidpath.restype = ctypes.c_int
    sandboxed = sandbox_check(pid, None, 0) == 1
    path = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    if proc_pidpath(pid, path, PROC_PIDPATHINFO_MAXSIZE) <= 0:
        raise ProcessLookupError(f"process {pid} exited during the sandbox check")
    return MacProcess(pid=pid, sandboxed=sandboxed)
