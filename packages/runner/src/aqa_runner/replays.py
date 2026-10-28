"""One replay of a compiled script in a run (#237): the spec's reset hook,
sent through the run's egress gate before anything is opened, then the
script replayed in what the caller opens once the reset has passed,
first-match or with every locator. Explore's confirmation and the bench
pilot replay through here, each opening its own browser, proxy and record
(`explore._confirming`, `pilot_replay._opened`)."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.spec import Spec
from playwright.async_api import Browser, BrowserType

from aqa_runner import executor
from aqa_runner.egress import EgressGate
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.reset import reset
from aqa_runner.run_record import RunRecord


@dataclass(frozen=True)
class Replaying:
    """What a replay is for and goes through: the spec and project config,
    the run's start origin (`aqa_core.project.start_origin`), and the run's
    egress gate, which sends the reset and every request the replay makes."""

    spec: Spec
    config: ProjectConfig
    start: str
    gate: EgressGate


@dataclass(frozen=True)
class Opened:
    """What a caller opened for its replay once the reset passed: the
    Chromium the replay launches its browser from, or a browser launched for
    it (`executor.replay`'s `chromium`); the egress proxy on the run's gate,
    with its phase open; and the record the replay writes."""

    chromium: BrowserType | Browser
    proxy: EgressProxy
    record: RunRecord


async def replay_run(
    script: CompiledScript,
    replaying: Replaying,
    opening: Callable[[], AbstractAsyncContextManager[Opened]],
    *,
    every_locator: bool,
) -> executor.RunResult:
    """Send the spec's reset hook, when it has one, through the run's gate
    within `resolve_seconds` (`aqa_runner.reset.reset`), then enter
    `opening()` and replay `script` in what it yields (`executor.replay`):
    with every locator at each use when `every_locator`, else first-match.
    A failed reset raises `ResetFailedError` before `opening` is entered, so
    no browser starts before the reset passed. What replay raises reaches
    `opening`'s exit, so the caller can tell it before what it opened
    closes."""
    spec, config, start = replaying.spec, replaying.config, replaying.start
    hook = spec.frontmatter.preconditions.reset
    if hook is not None:
        seconds = config.budgets.resolve_seconds
        await reset(replaying.gate, hook, start, seconds=seconds)
    async with opening() as opened:
        return await executor.replay(
            script,
            executor.RunSetup(spec, config, start, opened.record),
            chromium=opened.chromium,
            proxy=opened.proxy,
            gate=replaying.gate,
            every_locator=every_locator,
        )
