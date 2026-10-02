"""The sandbox check and the sandboxed launch (ADR-0026, SECURITY.md §6)."""

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import replace
from typing import override

import pytest
from aqa_runner.sandbox import (
    Environment,
    LinuxProcess,
    MacProcess,
    SandboxObservations,
    SandboxUnavailableError,
    check_processes,
    check_sandbox,
    compare_linux,
    compare_macos,
    launch,
    launch_with_observations,
)
from playwright.async_api import Browser, BrowserType, Error, async_playwright

# /proc facts recorded from Playwright 1.63's headless shell on Linux 6.12:
# a sandboxed renderer lives in its own user, pid and net namespaces and adds
# a seccomp filter of its own.
LINUX_BROWSER = LinuxProcess(
    pid=19,
    namespaces={
        "user": "user:[4026531837]",
        "pid": "pid:[4026532559]",
        "net": "net:[4026532561]",
    },
    seccomp_filters=0,
)
LINUX_RENDERER = LinuxProcess(
    pid=77,
    namespaces={
        "user": "user:[4026532823]",
        "pid": "pid:[4026532691]",
        "net": "net:[4026532695]",
    },
    seccomp_filters=1,
)


def test_renderer_in_its_own_namespaces_with_its_own_seccomp_filter_passes() -> None:
    assert compare_linux(LINUX_BROWSER, [LINUX_RENDERER]) == []


@pytest.mark.parametrize("namespace", ["user", "pid", "net"])
def test_renderer_sharing_a_browser_namespace_fails(namespace: str) -> None:
    shared = LINUX_BROWSER.namespaces[namespace]
    renderer = replace(
        LINUX_RENDERER,
        namespaces={**LINUX_RENDERER.namespaces, namespace: shared},
    )

    assert compare_linux(LINUX_BROWSER, [renderer]) == [
        f"renderer 77 shares the browser's {namespace} namespace ({shared})"
    ]


# In a container whose seccomp profile lets the sandbox start, such as
# Playwright's, every process, the browser included, already has a filter (and
# seccomp mode 2), so only a count above the browser's shows the renderer's own.
@pytest.mark.parametrize(
    ("browser_filters", "renderer_filters"),
    [(0, 0), (1, 1)],
    ids=["host", "container"],
)
def test_renderer_without_a_seccomp_filter_of_its_own_fails(
    browser_filters: int, renderer_filters: int
) -> None:
    browser = replace(LINUX_BROWSER, seccomp_filters=browser_filters)
    renderer = replace(LINUX_RENDERER, seccomp_filters=renderer_filters)

    assert compare_linux(browser, [renderer]) == [
        (
            f"renderer 77 has no seccomp filter of its own ({renderer_filters} "
            f"filters; the browser has {browser_filters})"
        )
    ]


def test_no_renderer_fails_on_linux() -> None:
    assert compare_linux(LINUX_BROWSER, []) == ["no renderer process to check"]


def test_every_renderer_is_compared_on_linux() -> None:
    unsandboxed = LinuxProcess(
        pid=78, namespaces=LINUX_BROWSER.namespaces, seccomp_filters=0
    )

    problems = compare_linux(LINUX_BROWSER, [LINUX_RENDERER, unsandboxed])

    assert problems, "a sandboxed renderer vouched for an unsandboxed one"
    assert all(problem.startswith("renderer 78 ") for problem in problems), problems


def test_container_renderer_with_its_own_seccomp_filter_passes() -> None:
    browser = replace(LINUX_BROWSER, seccomp_filters=1)
    renderer = replace(LINUX_RENDERER, seccomp_filters=2)

    assert compare_linux(browser, [renderer]) == []


# sandbox_check results recorded on macOS: 1 for a sandboxed renderer, 0 for
# the browser, and 0 for the renderer when the sandbox is off.
MAC_BROWSER = MacProcess(pid=4663, sandboxed=False)
MAC_RENDERER = MacProcess(pid=4686, sandboxed=True)


def test_sandboxed_renderer_of_an_unsandboxed_browser_passes_on_macos() -> None:
    assert compare_macos(MAC_BROWSER, [MAC_RENDERER]) == []


def test_unsandboxed_renderer_fails_on_macos() -> None:
    renderer = replace(MAC_RENDERER, sandboxed=False)

    assert compare_macos(MAC_BROWSER, [renderer]) == ["renderer 4686 isn't sandboxed"]


def test_every_renderer_is_compared_on_macos() -> None:
    unsandboxed = MacProcess(pid=4687, sandboxed=False)

    assert compare_macos(MAC_BROWSER, [MAC_RENDERER, unsandboxed]) == [
        "renderer 4687 isn't sandboxed"
    ]


# A browser that is itself sandboxed leaves nothing to compare the renderer with.
def test_sandboxed_browser_fails_on_macos() -> None:
    browser = replace(MAC_BROWSER, sandboxed=True)

    assert compare_macos(browser, [MAC_RENDERER]) == [
        (
            "the browser process 4663 is sandboxed too, so the check can't tell "
            "its renderers apart from it"
        )
    ]


def test_no_renderer_fails_on_macos() -> None:
    assert compare_macos(MAC_BROWSER, []) == ["no renderer process to check"]


# macOS's sandbox_check answers "sandboxed" for a PID with no running process,
# so a renderer that exits during the check must stop it, on every OS.
@pytest.mark.parametrize("reaped", [True, False], ids=["reaped", "zombie"])
def test_renderer_that_has_exited_stops_the_check(reaped: bool) -> None:
    child = subprocess.Popen([sys.executable, "-c", ""])
    # WNOWAIT waits for the exit but leaves the child a zombie.
    os.waitid(os.P_PID, child.pid, os.WEXITED | (0 if reaped else os.WNOWAIT))
    try:
        with pytest.raises((ProcessLookupError, FileNotFoundError)):
            check_processes(os.getpid(), [child.pid])
    finally:
        child.wait()


# The browser tests below launch real Chromium on the OS that runs them: Linux
# in CI, macOS locally.


class RecordingChromium:
    """Real Chromium that records what `launch` asks for and what it launched."""

    def __init__(self, chromium: BrowserType) -> None:
        self.chromium = chromium
        self.requested: list[bool] = []
        self.launched: list[Browser] = []

    async def launch(self, *, chromium_sandbox: bool, env: Environment) -> Browser:
        self.requested.append(chromium_sandbox)
        browser = await self.start(chromium_sandbox=chromium_sandbox, env=env)
        self.launched.append(browser)
        return browser

    async def start(self, *, chromium_sandbox: bool, env: Environment) -> Browser:
        return await self.chromium.launch(chromium_sandbox=chromium_sandbox, env=env)


class UnsandboxedChromium(RecordingChromium):
    """Real Chromium that ignores `launch`'s sandbox request: however the
    sandbox ends up off, `launch` must refuse the browser."""

    @override
    async def start(self, *, chromium_sandbox: bool, env: Environment) -> Browser:
        return await self.chromium.launch(
            chromium_sandbox=False,  # ADR-0026: a negative control for launch
            env=env,
        )


def test_launched_browser_passes_the_sandbox_check() -> None:
    async def scenario() -> tuple[list[bool], int, list[str]]:
        async with async_playwright() as playwright:
            chromium = RecordingChromium(playwright.chromium)
            browser = await launch(chromium)
            try:
                contexts = len(browser.contexts)
                return chromium.requested, contexts, await check_sandbox(browser)
            finally:
                await browser.close()

    requested, contexts, problems = asyncio.run(scenario())

    assert requested == [True], "launch didn't ask for the sandbox"
    assert contexts == 0, "launch left the sandbox check's context open"
    assert problems == []


# What each OS's check reports about a renderer launched without its sandbox.
UNSANDBOXED_ON = {
    "linux": "shares the browser's pid namespace",
    "darwin": "isn't sandboxed",
}


def test_check_reports_failure_when_the_sandbox_is_off() -> None:
    async def scenario() -> list[str]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                chromium_sandbox=False,  # ADR-0026: the check's negative control
            )
            try:
                return await check_sandbox(browser)
            finally:
                await browser.close()

    problems = asyncio.run(scenario())

    assert any(UNSANDBOXED_ON[sys.platform] in problem for problem in problems), (
        problems
    )
    assert all(problem.startswith("renderer ") for problem in problems), problems


# The fix each OS's refusal names: the AppArmor sysctl on Linux, and on macOS
# running outside any other sandbox.
HOST_FIX_ON = {
    "linux": "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0",
    "darwin": "start the runner outside it",
}


def test_launch_refuses_a_browser_whose_sandbox_is_off() -> None:
    async def scenario() -> tuple[SandboxUnavailableError, list[bool], list[bool]]:
        async with async_playwright() as playwright:
            chromium = UnsandboxedChromium(playwright.chromium)
            with pytest.raises(SandboxUnavailableError) as refused:
                await launch(chromium)
            connected = [browser.is_connected() for browser in chromium.launched]
            return refused.value, chromium.requested, connected

    error, requested, connected = asyncio.run(scenario())

    assert requested == [True], "launch didn't ask for the sandbox"
    assert error.exit_code >= 10  # an infrastructure error (API.md §7)
    assert UNSANDBOXED_ON[sys.platform] in str(error), str(error)
    assert HOST_FIX_ON[sys.platform] in str(error), str(error)
    assert connected == [False], "launch left the refused browser running"


def test_launch_refuses_an_os_with_no_sandbox_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[SandboxUnavailableError, list[bool]]:
        async with async_playwright() as playwright:
            chromium = RecordingChromium(playwright.chromium)
            # Only around launch: the driver starts and stops as on this OS.
            monkeypatch.setattr(sys, "platform", "win32")
            try:
                with pytest.raises(SandboxUnavailableError) as refused:
                    await launch(chromium)
            finally:
                monkeypatch.undo()
            connected = [browser.is_connected() for browser in chromium.launched]
            return refused.value, connected

    error, connected = asyncio.run(scenario())

    assert error.exit_code >= 10  # an infrastructure error (API.md §7)
    assert "no sandbox check exists for win32" in str(error), str(error)
    assert connected == [False], "launch left the refused browser running"


# The error Playwright 1.63 raised on Linux when Chromium's sandbox couldn't
# start (Docker's default seccomp profile). Its long lines are cut short, and
# Playwright's advice, which includes turning the sandbox off, is left out.
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
        self.requested: list[bool] = []
        self.environments: list[Environment] = []

    async def launch(self, *, chromium_sandbox: bool, env: Environment) -> Browser:
        self.requested.append(chromium_sandbox)
        self.environments.append(env)
        raise Error(self.message)


def test_launch_that_cannot_start_the_sandbox_names_the_host_fix() -> None:
    chromium = FailingChromium(NO_USABLE_SANDBOX)

    with pytest.raises(SandboxUnavailableError) as refused:
        asyncio.run(launch(chromium))

    assert chromium.requested == [True]
    assert chromium.environments == [{}], "launch passed the browser an environment"
    assert refused.value.exit_code >= 10  # an infrastructure error (API.md §7)
    assert HOST_FIX_ON["linux"] in str(refused.value), str(refused.value)


# The error Playwright 1.63 raised when Chromium ran as root (a container run
# with --user root). Chromium's line is cut short before it names the switch
# that turns the sandbox off, and Playwright's advice is left out.
RUNNING_AS_ROOT = """\
BrowserType.launch: Target page, context or browser has been closed
Browser logs:
Chromium sandboxing failed!

Call log:
  - <launched> pid=20
  - [pid=20][err] [0930/200339.264143:ERROR:content/browser/zygote_host/zygote_host_impl_linux.cc:102] Running as root without
  - [pid=20] <process did exit: exitCode=1, signal=null>
"""


def test_launch_as_root_is_an_infrastructure_error_naming_the_fix() -> None:
    chromium = FailingChromium(RUNNING_AS_ROOT)

    with pytest.raises(SandboxUnavailableError) as refused:
        asyncio.run(launch(chromium))

    assert chromium.requested == [True]
    assert refused.value.exit_code >= 10  # an infrastructure error (API.md §7)
    assert "run the runner as a non-root user" in str(refused.value), str(refused.value)


def test_other_launch_errors_pass_through() -> None:
    missing = "BrowserType.launch: Executable doesn't exist at /ms-playwright/chrome"

    with pytest.raises(Error, match="Executable doesn't exist"):
        asyncio.run(launch(FailingChromium(missing)))


# What the sandbox check observed (#81): the reads its reasons come from,
# reported alongside them, so they can never vouch for a renderer it refuses.


# A child inherits its parent's namespaces, seccomp filters and sandbox, so
# the check refuses it as a renderer, and its observations show why.
def test_check_reports_the_processes_it_compared() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        observed, problems = check_processes(os.getpid(), [child.pid])
    finally:
        child.kill()
        child.wait()

    assert observed is not None
    assert observed.browser.pid == os.getpid()
    [renderer] = observed.renderers
    assert renderer.pid == child.pid
    assert replace(renderer, pid=os.getpid()) == observed.browser
    assert problems, "the check passed a renderer that isn't sandboxed"
    assert all(problem.startswith(f"renderer {child.pid} ") for problem in problems)


def test_launch_returns_what_the_sandbox_check_observed() -> None:
    async def scenario() -> tuple[list[bool], bool, int, SandboxObservations | None]:
        async with async_playwright() as playwright:
            chromium = RecordingChromium(playwright.chromium)
            browser, observed = await launch_with_observations(chromium)
            try:
                return (
                    chromium.requested,
                    browser.is_connected(),
                    len(browser.contexts),
                    observed,
                )
            finally:
                await browser.close()

    requested, connected, contexts, observed = asyncio.run(scenario())

    assert requested == [True], "launch didn't ask for the sandbox"
    assert connected, "launch returned a browser that isn't running"
    assert contexts == 0, "launch left the sandbox check's context open"
    assert observed is not None, "launch didn't return what the check observed"
    renderers = [renderer.pid for renderer in observed.renderers]
    assert renderers, "the check observed no renderer"
    assert observed.browser.pid not in renderers


def test_launch_refusal_carries_what_the_check_observed() -> None:
    async def scenario() -> SandboxUnavailableError:
        async with async_playwright() as playwright:
            with pytest.raises(SandboxUnavailableError) as refused:
                await launch(UnsandboxedChromium(playwright.chromium))
            return refused.value

    error = asyncio.run(scenario())

    observed = error.observed
    assert observed is not None, "the refusal didn't carry what the check observed"
    renderers = {renderer.pid for renderer in observed.renderers}
    assert renderers, "the check observed no renderer"
    assert observed.browser.pid not in renderers
    # The refusal's reasons come from these reads: it names every renderer
    # observed, each of them without its sandbox, and no other.
    refused = {int(pid) for pid in re.findall(r"renderer (\d+)", str(error))}
    assert refused == renderers, str(error)
    # Without its sandbox, a renderer is observed just as the browser is.
    for renderer in observed.renderers:
        assert replace(renderer, pid=observed.browser.pid) == observed.browser
