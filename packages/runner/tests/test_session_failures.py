"""How a browser session ends once Playwright's driver has gone, and when
Chromium exits after its sandbox check (#204; ADR-0026's #204 amendment).
A terminal's Ctrl-C reaches the driver too, since Playwright 1.63 starts it
in the runner's process group, and the browser's close then fails. That
failure never takes the place of an error or a cancellation already
propagating, and still raises after a session that ended normally. Real
Chromium, its driver interrupted as the terminal interrupts it, with SIGINT."""

import asyncio
import itertools
import os
import re
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aqa_runner import browser_session, sandbox
from aqa_runner.browser_session import open_browser_session
from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Error,
    async_playwright,
)

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import egress_proxy

# What Playwright 1.63 raises closing a browser whose driver has gone.
BROWSER_GONE = ("Browser.close: Connection closed while reading from the driver",)
# Why a session couldn't be prepared once its Chromium had exited.
UNPREPARED = (
    "Chromium's session couldn't be prepared after its sandbox check "
    "(TargetClosedError)"
)


def exactly(args: tuple[object, ...]) -> str:
    """The pattern `pytest.raises` needs for an error whose one argument is
    `args`'s; the test then compares the whole tuple."""
    [text] = args
    return f"^{re.escape(str(text))}$"


def drivers(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    """Each Playwright driver started from now on, as it starts."""
    started: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def spawned(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        if "run-driver" in args:
            started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawned)
    return started


async def interrupt(driver: asyncio.subprocess.Process) -> None:
    """Interrupt `driver` as a terminal's Ctrl-C does, and wait until it
    has exited."""
    driver.send_signal(signal.SIGINT)
    assert await driver.wait() == 130


async def cancelled() -> None:
    """Cancel the running task, as `asyncio.run` does on Ctrl-C."""
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    await asyncio.sleep(60)


def exiting_after_the_check(
    monkeypatch: pytest.MonkeyPatch, *, from_launch: int = 1
) -> None:
    """Have Chromium exit right after its sandbox check, from the
    `from_launch`th launch on."""
    launches = itertools.count(1)

    async def launched(chromium: Any) -> Browser:
        browser, observed = await sandbox.launch_with_observations(chromium)
        if next(launches) >= from_launch:
            os.kill(observed.browser.pid, signal.SIGKILL)
            await until(lambda: not browser.is_connected())
        return browser

    monkeypatch.setattr(browser_session, "launch", launched)


def session(
    monkeypatch: pytest.MonkeyPatch, body: Callable[[], Awaitable[None]]
) -> None:
    """Open a session in real Chromium, interrupt its driver, then run
    `body`, inside the session."""
    started = drivers(monkeypatch)

    async def scenario() -> None:
        async with (
            async_playwright() as playwright,
            egress_proxy() as proxy,
            open_browser_session(playwright.chromium, egress=proxy),
        ):
            await interrupt(started[0])
            await body()

    asyncio.run(scenario())


def test_a_browser_left_normally_raises_when_its_close_finds_the_driver_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = drivers(monkeypatch)

    async def scenario() -> None:
        async with (
            async_playwright() as playwright,
            browser_session.launched_browser(playwright.chromium),
        ):
            await interrupt(started[0])

    with pytest.raises(Exception, match=exactly(BROWSER_GONE)) as raised:
        asyncio.run(scenario())

    assert (type(raised.value), raised.value.args) == (Exception, BROWSER_GONE)


def test_a_session_that_ends_normally_closes_its_context_before_its_browser() -> None:
    async def scenario() -> tuple[bool, list[BrowserContext]]:
        async with (
            async_playwright() as playwright,
            egress_proxy() as proxy,
            browser_session.launched_browser(playwright.chromium) as browser,
        ):
            async with browser_session.prepared_session(
                browser, egress=proxy
            ) as opened:
                held = browser.contexts == [opened.page.context]
            return held, browser.contexts

    assert asyncio.run(scenario()) == (True, [])


def test_an_error_the_session_raises_is_kept_when_its_close_finds_the_driver_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = ValueError("the session's own error")

    async def failing() -> None:
        raise fault

    with pytest.raises(ValueError, match=exactly(fault.args)) as raised:
        session(monkeypatch, failing)

    assert raised.value is fault


def test_a_cancellation_is_kept_when_the_close_finds_the_driver_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(asyncio.CancelledError):
        session(monkeypatch, cancelled)


def test_a_cancellation_in_the_sandbox_check_is_kept_when_its_cleanup_finds_the_driver_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = drivers(monkeypatch)
    send = CDPSession.send

    async def interrupted(cdp: CDPSession, method: str, params: Any = None) -> Any:
        # The check's page and its CDP session are open: each has a cleanup.
        await interrupt(started[0])
        await cancelled()
        return await send(cdp, method, params)

    monkeypatch.setattr(CDPSession, "send", interrupted)

    async def scenario() -> None:
        async with (
            async_playwright() as playwright,
            egress_proxy() as proxy,
            open_browser_session(playwright.chromium, egress=proxy),
        ):
            pytest.fail("a session opened through an interrupted check")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [Error(BROWSER_GONE[0]), Exception(f"{BROWSER_GONE[0]}."), Exception("x")],
    ids=["playwright error", "other words", "another failure"],
)
def test_any_other_close_failure_still_takes_the_errors_place(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    close = Browser.close

    async def failing(browser: Browser, *, reason: str | None = None) -> None:
        await close(browser, reason=reason)
        raise failure

    monkeypatch.setattr(Browser, "close", failing)

    async def scenario() -> None:
        async with (
            async_playwright() as playwright,
            egress_proxy() as proxy,
            open_browser_session(playwright.chromium, egress=proxy),
        ):
            raise ValueError("the session's own error")

    with pytest.raises(Exception, match=exactly(failure.args)) as raised:
        asyncio.run(scenario())

    assert raised.value is failure
    assert type(raised.value.__context__) is ValueError


def test_chromium_exiting_after_its_check_is_a_sandbox_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exiting_after_the_check(monkeypatch)

    async def scenario() -> None:
        async with (
            async_playwright() as playwright,
            egress_proxy() as proxy,
            open_browser_session(playwright.chromium, egress=proxy),
        ):
            pytest.fail("a session opened on a Chromium that had exited")

    with pytest.raises(sandbox.SandboxUnavailableError) as raised:
        asyncio.run(scenario())

    assert raised.value.exit_code == 10
    assert raised.value.args == (UNPREPARED,)
    assert type(raised.value.__cause__).__name__ == "TargetClosedError"
