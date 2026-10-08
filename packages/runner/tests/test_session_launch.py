"""Session infrastructure failures use the sandbox boundary (ADR-0026)."""

import asyncio
import sys
from collections.abc import Sequence
from typing import Any

import pytest
from aqa_runner import sandbox
from aqa_runner.browser_session import open_browser_session
from aqa_runner.sandbox import (
    HOST_FIXES,
    OTHER_FIX,
    Environment,
    SandboxObservations,
    SandboxUnavailableError,
)
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error,
    ProxySettings,
    async_playwright,
)

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.test_sandbox import (
    LINUX_BROWSER,
    LINUX_RENDERER,
    RecordingChromium,
)


class FaultyChromium:
    """A launch fault before any browser exists."""

    def __init__(self, fault: Exception) -> None:
        self.fault = fault

    async def launch(
        self,
        *,
        chromium_sandbox: bool,
        env: Environment,
        args: Sequence[str],
        proxy: ProxySettings,
    ) -> Browser:
        del chromium_sandbox, env, args, proxy
        raise self.fault


@pytest.mark.parametrize(
    "kind", [Error, KeyError, ValueError, OSError, ProcessLookupError]
)
def test_a_launch_or_check_failure_is_a_sandbox_error(kind: type[Exception]) -> None:
    fault = kind("raw launch advice must not reach the diagnostic")

    async def scenario() -> SandboxUnavailableError:
        async with egress_proxy() as proxy:
            with pytest.raises(SandboxUnavailableError) as raised:
                async with open_browser_session(FaultyChromium(fault), egress=proxy):
                    pytest.fail("a failed launch yielded a session")
            return raised.value

    error = asyncio.run(scenario())
    assert error.exit_code == 10
    assert str(error) == (
        f"Chromium could not be launched or its sandbox checked ({kind.__name__}). "
        f"{HOST_FIXES.get(sys.platform, OTHER_FIX)}"
    )
    assert "raw launch advice" not in str(error)
    assert error.__cause__ is fault


def test_an_existing_sandbox_error_keeps_its_observations_and_message() -> None:
    observed = SandboxObservations(LINUX_BROWSER, [LINUX_RENDERER])
    fault = SandboxUnavailableError("the original sandbox refusal", observed=observed)

    async def scenario() -> SandboxUnavailableError:
        async with egress_proxy() as proxy:
            with pytest.raises(SandboxUnavailableError) as raised:
                async with open_browser_session(FaultyChromium(fault), egress=proxy):
                    pytest.fail("a refused sandbox yielded a session")
            return raised.value

    error = asyncio.run(scenario())
    assert error is fault
    assert str(error) == "the original sandbox refusal"
    assert error.observed is observed


def test_a_check_failure_closes_the_browser_before_the_session_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_browser: Browser) -> tuple[SandboxObservations | None, list[str]]:
        raise KeyError("raw process check advice")

    monkeypatch.setattr(sandbox, "_observe", broken)

    async def scenario() -> None:
        async with async_playwright() as playwright, egress_proxy() as proxy:
            chromium = RecordingChromium(playwright.chromium)
            with pytest.raises(SandboxUnavailableError) as raised:
                async with open_browser_session(chromium, egress=proxy):
                    pytest.fail("a failed sandbox check yielded a session")
            assert raised.value.exit_code == 10
            assert "(KeyError)" in str(raised.value)
            assert "raw process check advice" not in str(raised.value)
            assert len(chromium.launched) == 1
            assert not chromium.launched[0].is_connected()

    asyncio.run(scenario())


def test_a_failure_after_launch_is_not_mapped_to_a_sandbox_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Browser.new_context
    calls = 0
    fault = ValueError("context setup fault")

    async def broken(browser: Browser, **options: Any) -> BrowserContext:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise fault
        return await original(browser, **options)

    monkeypatch.setattr(Browser, "new_context", broken)

    async def scenario() -> None:
        async with async_playwright() as playwright, egress_proxy() as proxy:
            chromium = RecordingChromium(playwright.chromium)
            with pytest.raises(ValueError, match="context setup fault") as raised:
                async with open_browser_session(chromium, egress=proxy):
                    pytest.fail("a failed context yielded a session")
            assert raised.value is fault
            assert not chromium.launched[0].is_connected()

    asyncio.run(scenario())
