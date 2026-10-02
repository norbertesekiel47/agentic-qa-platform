"""Chromium's sandbox: always on, and proved by the sandbox check before any
page loads (ADR-0026 and its 2026-09-30 amendment; SECURITY.md §6). The same
launch gives the browser an empty environment (ADR-0026's 2026-10-01
amendment; SECURITY.md §5) and closes its ways out other than a context's own
proxy (ADR-0026's 2026-10-02 amendment; SECURITY.md §7)."""

import ctypes
import sys
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.async_api import Browser, Error, ProxySettings

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


@dataclass(frozen=True)
class SandboxObservations:
    """What the sandbox check read on this host: the browser process and each
    renderer it compared with it. The check's reasons come from these same
    reads."""

    browser: LinuxProcess | MacProcess
    renderers: Sequence[LinuxProcess | MacProcess]


class SandboxUnavailableError(RuntimeError):
    """Chromium can't run with its sandbox on this host: an infrastructure
    error, never a run outcome (API.md §7, ADR-0026). `observed` is what a
    sandbox check that refused the browser read; `None` when the check read
    nothing: the sandbox couldn't start, or the OS has no sandbox check."""

    exit_code = 10

    def __init__(
        self, message: str, *, observed: SandboxObservations | None = None
    ) -> None:
        super().__init__(message)
        self.observed = observed


# The environment variables a launch gives the browser, in Playwright's type.
type Environment = dict[str, str | float | bool]

# The switches every launch gives Chromium, which close the browser's ways out
# other than a proxy (ADR-0026 amendment, 2026-10-02; SECURITY.md §7).
# Playwright appends them to its own
# (https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-option-args).
TRANSPORT_SWITCHES = (
    # WebRTC "should only use TCP to contact peers or servers unless the proxy
    # server supports UDP" (Chromium's kWebRTCIPHandlingDisableNonProxiedUdp),
    # and an HTTP proxy carries no UDP. The headless shell, which Playwright
    # runs headless, reads the first switch (headless_web_contents_impl.cc);
    # full Chromium, which it runs headed (PWDEBUG) or by channel, reads the
    # second into its preference (chrome_command_line_pref_store.cc).
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    # "Disables the QUIC protocol" (Chromium's network_switch_list.h). QUIC
    # runs over UDP, which an HTTP proxy doesn't carry.
    "--disable-quic",
    # The browser resolves no name itself, so nothing it looks up (DNS
    # prefetch, a STUN server's name) sends a DNS query: every name is the
    # egress proxy's to resolve and pin. `^NOTFOUND` fails a lookup with
    # ERR_NAME_NOT_RESOLVED (Chromium's net/dns/mapped_host_resolver.cc; any
    # other host, `~NOTFOUND` included, would be looked up). The rule maps
    # address literals too, so it spares the one the egress proxy listens on
    # (`EgressProxy`).
    "--host-resolver-rules=MAP * ^NOTFOUND, EXCLUDE 127.0.0.1",
)

# The proxy of every browser context that names none of its own, such as one
# opened outside the browser session: one that can't be reached, so such a
# context's pages have no way out (ERR_PROXY_CONNECTION_FAILED). `.invalid`
# never resolves (RFC 6761), and the resolver rule above stops Chromium's
# lookup before any query leaves. Loopback goes to it too, as through the
# session's own proxy.
# https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-option-proxy
FAIL_CLOSED_PROXY: ProxySettings = {
    "server": "http://launch-proxy.invalid:1",
    "bypass": "<-loopback>",
}


class Chromium(Protocol):
    """The part of Playwright's `BrowserType` that `launch` uses."""

    async def launch(
        self,
        *,
        chromium_sandbox: bool,
        env: Environment,
        args: Sequence[str],
        proxy: ProxySettings,
    ) -> Browser: ...


async def launch(chromium: Chromium) -> Browser:
    """Launch Chromium with its sandbox on, an empty environment, the transport
    switches and the fail-closed proxy, and return it only once the sandbox
    check has proved the sandbox. Nothing skips the check: `launch` takes no
    option and reads no setting or environment variable (ADR-0026)."""
    browser, _ = await launch_with_observations(chromium)
    return browser


async def launch_with_observations(
    chromium: Chromium,
) -> tuple[Browser, SandboxObservations]:
    """`launch`, which also returns what the sandbox check observed. Like
    `launch`, it takes no option and reads no setting or environment
    variable."""
    try:
        # Playwright's default environment for the browser is the runner's
        # own, provider keys and cloud credentials included
        # (https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-option-env).
        # An empty one keeps them, and every AQA_SECRET_* value, out of every
        # Chromium process; a test secret reaches a page only through
        # fill_secret.
        browser = await chromium.launch(
            chromium_sandbox=True,
            env={},
            args=TRANSPORT_SWITCHES,
            proxy=FAIL_CLOSED_PROXY,
        )
    except Error as error:
        fixes = [fix for line, fix in NO_SANDBOX_LOGS.items() if line in error.message]
        if not fixes:
            raise
        raise SandboxUnavailableError(
            f"Chromium's sandbox can't start on this host. {fixes[0]}"
        ) from error
    async with AsyncExitStack() as on_failure:
        on_failure.push_async_callback(browser.close)
        observed, problems = await _observe(browser)
        # A check that read nothing has proved nothing.
        if problems or observed is None:
            raise SandboxUnavailableError(
                "Chromium started without a sandbox the sandbox check can prove: "
                f"{'; '.join(problems)}. {HOST_FIXES.get(sys.platform, OTHER_FIX)}",
                observed=observed,
            )
        on_failure.pop_all()
    return browser, observed


async def check_sandbox(browser: Browser) -> list[str]:
    """Why the browser's renderers can't be shown to be sandboxed; empty when
    they are. The browser must have been launched on this host, not connected
    to. A renderer exists only once a page does, so the check opens a blank
    page in a context of its own and closes it before returning."""
    _, problems = await _observe(browser)
    return problems


async def _observe(browser: Browser) -> tuple[SandboxObservations | None, list[str]]:
    """`check_sandbox`, which also returns what the check observed."""
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


def check_processes(
    browser_pid: int, renderer_pids: list[int]
) -> tuple[SandboxObservations | None, list[str]]:
    """What the check read of the browser and renderer processes, and why the
    renderers can't be shown to be sandboxed, compared with the browser: no
    reasons when they are. Each process is read once, and the reasons come from
    exactly those reads. On an OS with no sandbox check nothing is read. The
    PIDs are this host's, so the browser must have been launched here, not
    connected to. A process that has exited stops the check with an
    `OSError`."""
    if sys.platform == "linux":
        linux = _read_linux(browser_pid), [_read_linux(pid) for pid in renderer_pids]
        return SandboxObservations(*linux), compare_linux(*linux)
    if sys.platform == "darwin":
        macos = _read_macos(browser_pid), [_read_macos(pid) for pid in renderer_pids]
        return SandboxObservations(*macos), compare_macos(*macos)
    return None, [f"no sandbox check exists for {sys.platform}"]


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
