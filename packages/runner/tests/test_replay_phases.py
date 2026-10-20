"""Replay ends its egress proxy's phase however it leaves, a refusal
before the launch included (ADR-0024's #53 P16 amendment): once its
browser has closed, so the proxy keeps that phase's traffic for
`EgressProxy.ended_traffic` and no caller ends it twice. In a browser its
caller launched, it ends the phase on a return and leaves it to that caller
on a raise (#204). Real Chromium on the fixture app's shop page; no model."""

import asyncio
import errno
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError
from aqa_runner.browser_session import launched_browser
from aqa_runner.egress_proxy import EgressProxy, PhaseTraffic
from aqa_runner.executor import RunResult, RunSetup, replay
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.run_record import PendingIntent, RunRecord
from aqa_runner.sandbox import SandboxUnavailableError
from playwright.async_api import BrowserType, async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import (
    SHOP_TARGETS,
    App,
    a_spec,
    compiled,
    for_spec,
    serving_app,
)
from packages.runner.tests.test_session_failures import (
    UNPREPARED,
    exiting_after_the_check,
)

BASIS = "network: POST /write/save"
SAVE = [
    {
        "seq": 1,
        "action": "fill",
        "target": "name",
        "value": "Ada",
        "side_effect": False,
    },
    {
        "seq": 2,
        "action": "click",
        "target": "save",
        "side_effect": True,
        "side_effect_basis": BASIS,
    },
]
SAVED = [
    {
        "id": "a1",
        "expect_index": 0,
        "check": "text_in_target",
        "target": "status",
        "pattern": r"Saved Ada in size S\b",
    }
]
# A navigation the fixture app holds unanswered until the test ends.
HELD = [
    {
        "seq": 1,
        "action": "navigate",
        "url": "/held/x",
        "side_effect": True,
        "side_effect_basis": BASIS,
    }
]


@dataclass(frozen=True)
class Left:
    """How a replay left: what it returned or raised, the traffic its
    phase left, and the intents its record left unresolved."""

    outcome: RunResult | BaseException
    traffic: PhaseTraffic
    unresolved: tuple[PendingIntent, ...]


@pytest.fixture
def app() -> Any:
    with serving_app() as served:
        yield served


def replayed(
    app: App,
    tmp_path: Path,
    script: CompiledScript,
    *,
    chromium: BrowserType | None = None,
    cancel_at: str | None = None,
    stale: bool = False,
) -> Left:
    """Replay `script`, compiled from the spec as it is unless `stale`,
    in the proxy's open phase, cancelling it once the app has seen
    `cancel_at`, and see how it left: the proxy must then have no phase to
    end."""
    config = ProjectConfig()
    spec = a_spec(tmp_path, config, start_url="/page/shop")
    run_gate = gate(allowed=(app.origin,))
    record = RunRecord.create(tmp_path)

    async def scenario() -> Left:
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            await register_identity_engine(playwright)
            replaying = asyncio.create_task(
                replay(
                    script if stale else for_spec(script, spec),
                    RunSetup(spec, config, app.origin, record),
                    chromium=chromium or playwright.chromium,
                    proxy=proxy,
                    gate=run_gate,
                )
            )
            if cancel_at is not None:
                await until(lambda: cancel_at in app.paths())
                replaying.cancel()
            (outcome,) = await asyncio.gather(replaying, return_exceptions=True)
            traffic = proxy.ended_traffic()
            with pytest.raises(RuntimeError, match="no open phase to end"):
                await proxy.end_phase()
            return Left(outcome, traffic, record.unresolved())

    async def bounded() -> Left:
        # No replay test may hang the suite.
        async with asyncio.timeout(60):
            return await scenario()

    return asyncio.run(bounded())


@dataclass(frozen=True)
class LeftInItsCallersBrowser:
    """How a replay in its caller's browser left: what it returned or
    raised, whether that browser was still connected and how many contexts
    it held, whether replay ended the phase, and the phase's traffic."""

    outcome: RunResult | BaseException
    connected: bool
    contexts: int
    ended_by_replay: bool
    traffic: PhaseTraffic


def replayed_in_its_callers_browser(
    app: App, tmp_path: Path, script: CompiledScript, *, cancel_at: str | None = None
) -> LeftInItsCallersBrowser:
    """Replay `script` in a browser launched as explore's confirmation
    launches it, cancelling it once the app has seen `cancel_at`. A phase
    replay leaves open, the caller ends once that browser has closed."""
    config = ProjectConfig()
    spec = a_spec(tmp_path, config, start_url="/page/shop")
    run_gate = gate(allowed=(app.origin,))

    async def scenario() -> LeftInItsCallersBrowser:
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            await register_identity_engine(playwright)
            async with launched_browser(playwright.chromium) as browser:
                replaying = asyncio.create_task(
                    replay(
                        for_spec(script, spec),
                        RunSetup(spec, config, app.origin, RunRecord.create(tmp_path)),
                        chromium=browser,
                        proxy=proxy,
                        gate=run_gate,
                    )
                )
                if cancel_at is not None:
                    await until(lambda: cancel_at in app.paths())
                    replaying.cancel()
                (outcome,) = await asyncio.gather(replaying, return_exceptions=True)
                connected, contexts = browser.is_connected(), len(browser.contexts)
                ended = None
                with suppress(RuntimeError):  # the phase is still open
                    ended = proxy.ended_traffic()
            traffic = ended if ended is not None else await proxy.end_phase()
            return LeftInItsCallersBrowser(
                outcome, connected, contexts, ended is not None, traffic
            )

    async def bounded() -> LeftInItsCallersBrowser:
        async with asyncio.timeout(60):
            return await scenario()

    return asyncio.run(bounded())


def shop(steps: list[dict[str, Any]]) -> CompiledScript:
    return compiled(steps, targets=SHOP_TARGETS, assertions=SAVED)


def no_launch(error: BaseException) -> BrowserType:
    chromium = MagicMock(spec=BrowserType)
    chromium.launch = AsyncMock(side_effect=error)
    return cast(BrowserType, chromium)


def test_a_replay_that_returns_leaves_its_phase_ended_with_its_counts(
    app: App, tmp_path: Path
) -> None:
    left = replayed(app, tmp_path, shop(SAVE))

    assert isinstance(left.outcome, RunResult)
    assert left.outcome.outcome == "passed"
    assert left.traffic == PhaseTraffic(0, 0)
    assert left.unresolved == ()


def test_a_replay_cancelled_mid_dispatch_ends_its_phase(
    app: App, tmp_path: Path
) -> None:
    left = replayed(app, tmp_path, shop(HELD), cancel_at="/held/x")

    assert isinstance(left.outcome, asyncio.CancelledError)
    # The held navigation went upstream and its answer never came back.
    assert left.traffic == PhaseTraffic(plain_uncertain=1, tunnel_uncertain=0)
    assert left.unresolved == (PendingIntent(1, True),)


def test_a_replay_in_its_callers_browser_that_returns_ends_its_phase_with_its_context_closed(
    app: App, tmp_path: Path
) -> None:
    left = replayed_in_its_callers_browser(app, tmp_path, shop(SAVE))

    assert isinstance(left.outcome, RunResult)
    assert left.outcome.outcome == "passed"
    assert (left.connected, left.contexts, left.ended_by_replay) == (True, 0, True)
    assert left.traffic == PhaseTraffic(0, 0)


def test_a_replay_in_its_callers_browser_that_raises_leaves_its_phase_to_its_caller(
    app: App, tmp_path: Path
) -> None:
    left = replayed_in_its_callers_browser(
        app, tmp_path, shop(HELD), cancel_at="/held/x"
    )

    assert isinstance(left.outcome, asyncio.CancelledError)
    # The session's context is still live until the browser closes.
    assert (left.connected, left.contexts, left.ended_by_replay) == (True, 1, False)
    assert left.traffic == PhaseTraffic(plain_uncertain=1, tunnel_uncertain=0)


def test_a_replay_whose_launch_fails_ends_its_phase(app: App, tmp_path: Path) -> None:
    chromium = no_launch(OSError(errno.EPERM, "the sandbox couldn't start"))

    left = replayed(app, tmp_path, shop(SAVE), chromium=chromium)

    assert isinstance(left.outcome, SandboxUnavailableError)
    assert left.traffic == PhaseTraffic(0, 0)
    assert app.paths() == []


def test_a_replay_whose_chromium_exits_after_its_check_ends_its_phase(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exiting_after_the_check(monkeypatch)

    left = replayed(app, tmp_path, shop(SAVE))

    assert isinstance(left.outcome, SandboxUnavailableError)
    assert left.outcome.args == (UNPREPARED,)
    assert left.traffic == PhaseTraffic(0, 0)
    assert app.paths() == []


def test_a_replay_whose_journal_takes_no_lines_ends_its_phase(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_handle: int) -> None:
        raise OSError(errno.EIO, "the disk failed")

    monkeypatch.setattr(os, "fsync", failing)

    left = replayed(app, tmp_path, shop(SAVE))

    assert type(left.outcome) is OSError
    assert left.outcome.errno == errno.EIO
    assert left.traffic == PhaseTraffic(0, 0)
    # The start navigation's intent never reached the disk, so nothing was
    # dispatched.
    assert app.paths() == []


def test_a_script_refused_before_the_launch_ends_its_phase(
    app: App, tmp_path: Path
) -> None:
    chromium = no_launch(AssertionError("a refused script launched Chromium"))

    left = replayed(app, tmp_path, shop(SAVE), chromium=chromium, stale=True)

    assert isinstance(left.outcome, SpecError)
    assert left.outcome.problems == (
        "compiled from another version of the spec: explore it again",
    )
    assert left.traffic == PhaseTraffic(0, 0)
    assert app.paths() == []


def test_a_refusal_before_the_launch_is_kept_when_there_is_no_phase_to_end(
    app: App, tmp_path: Path
) -> None:
    config = ProjectConfig()
    spec = a_spec(tmp_path, config, start_url="/page/shop")
    run_gate = gate(allowed=(app.origin,))
    chromium = no_launch(AssertionError("a refused script launched Chromium"))

    async def scenario() -> None:
        async with EgressProxy(run_gate) as proxy:
            # A caller that broke replay's precondition: no phase is open.
            await proxy.end_phase()
            await replay(
                shop(SAVE),
                RunSetup(spec, config, app.origin, RunRecord.create(tmp_path)),
                chromium=chromium,
                proxy=proxy,
                gate=run_gate,
            )

    with pytest.raises(SpecError) as refused:
        asyncio.run(scenario())

    assert refused.value.problems == (
        "compiled from another version of the spec: explore it again",
    )
