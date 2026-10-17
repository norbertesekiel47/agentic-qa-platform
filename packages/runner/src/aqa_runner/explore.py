"""Exploring a spec (ADR-0024 and its #53 P15b amendment): the attempt's
phase, and how the run ended.

`run_attempt` takes a `Run` that preflight made, with its record and that
record's `attempt-1` part, and the frozen plan it explores by. In order,
each stop with its exit code (API.md §7):

1. Open the egress proxy. Within the minutes budget: send the spec's reset
   hook (14), register the selector engines, open the browser (10, and 10
   for an engine left unregistered), load the start URL as step 0, and,
   unless something stopped or interrupted the run, take the navigator's
   turns (`explore_turns`).
2. Close the browser, end the phase, read the egress blocks and the
   infrastructure events once, and write `explore.json`, and `egress.json`
   when blocked, from `explore_result.present`; its outcome decides. A
   record that doesn't scan with the run's redactor is refused there,
   whether or not a browser opened.

Nothing here writes a compiled script: the path `finish` compiled stays in
memory (`Explored.script`) for the confirmation (#53 P16)."""

from asyncio import timeout_at
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan, planned_checks
from aqa_core.project import effective_browser, start_url, subject_contracts
from aqa_core.schema import Contract
from aqa_core.spec import Spec
from playwright.async_api import Playwright

from aqa_runner import executor
from aqa_runner.binding import UnregisteredEngineError
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.egress import EgressGate
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.evaluation import AssertionResult
from aqa_runner.explore_result import (
    EXIT_CODES,
    ExploreEnd,
    Outcome,
    Phase,
    Stop,
    Stoppage,
    final_outcome,
    present,
)
from aqa_runner.explore_turns import UNWRITTEN, Spending, Turning, take_turns
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.model_router import ModelRouter
from aqa_runner.navigator import navigator_prefix
from aqa_runner.navigator_tools import Attempt
from aqa_runner.redaction import Redactor
from aqa_runner.reset import ResetFailedError, reset
from aqa_runner.run_record import RefusedWriteError, RunRecord, require_clean
from aqa_runner.sandbox import SandboxUnavailableError

# How a run ends: as `explore_result.final_outcome` says once the egress
# proxy opened, or a spec error before it (#53 P15c).
type Ended = Outcome | Literal["spec_error"]

# Why the run stopped, in fixed words.
_MINUTES: Final = "the minutes budget ran out"
# What a phase's uncertain traffic means, plain, then tunnelled.
_UNCERTAIN: Final = (
    "a request was still being answered when the browser closed",
    (
        "an encrypted or WebSocket connection carried data, and the proxy can't "
        "see whether its requests were answered"
    ),
)


def _exit(outcome: Ended) -> int:
    """`outcome`'s exit code (API.md §7)."""
    if outcome == "spec_error":
        return 5
    return EXIT_CODES[outcome]


@dataclass(frozen=True)
class Explored:
    """How the run ended: its outcome and exit code; its document,
    `explore.json` as written or as the record couldn't take it, whose
    `outcome` and `reasons` say why; its record, None when none could be
    made; what its model calls cost; and the script its attempt compiled,
    in memory only, when `finish` compiled one."""

    outcome: Ended
    exit_code: int
    document: Mapping[str, object]
    record: Path | None
    cost: Decimal
    script: CompiledScript | None = None


@dataclass(frozen=True)
class Explorable:
    """The frozen plan, and each meaning it checks with its contract
    (`aqa_core.compiler.contracts_by_meaning`)."""

    plan: CoveragePlan
    contracts: Mapping[str, Contract | None]


@dataclass
class Run:
    """What one run holds while it explores, made once its record is: the
    redactor binding every test secret the spec references, filled or not,
    the authority every scan of the run answers to; the record and its
    `attempt-1` part, made from it; the secrets the attempt may fill; the
    router; the Playwright whose Chromium it browses with; the minutes
    budget's deadline, on the event loop's clock; and the egress gate.
    Its spend is recorded in its record, and it keeps each stop it made
    and the script `finish` compiled."""

    spec: Spec
    config: ProjectConfig
    start: str
    redactor: Redactor
    record: RunRecord
    attempt: RunRecord
    secrets: Mapping[str, BoundSecret]
    router: ModelRouter
    playwright: Playwright
    deadline: float
    gate: EgressGate
    spending: Spending = field(init=False)
    stops: list[Stop] = field(init=False, default_factory=list)
    script: CompiledScript | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        budget = Decimal(str(self.config.budgets.model_usd))
        self.spending = Spending(self.record, budget)

    def stopped(self, outcome: Stoppage, why: str) -> None:
        """Keep a stop, its text scanned with the run's redactor."""
        self.stops.append(Stop(outcome, self.redactor.redact(why)))


async def run_attempt(run: Run, explorable: Explorable) -> Explored:
    """Run the attempt's phase by `explorable`, from the egress proxy's
    opening to its end, and say how the run ended (module docstring)."""
    return _ended(run, await _attempted(run, explorable))


async def _attempted(run: Run, explorable: Explorable) -> ExploreEnd:
    """The attempt's phase, from the egress proxy's opening to its end."""
    expected = run.config.egress.expected_blocked
    async with EgressProxy(run.gate) as proxy:

        def interrupted() -> bool:
            return (
                bool(run.gate.infrastructure_events)
                or proxy.egress_blocks(expected).blocked
            )

        checks, events = await _phase(run, explorable, proxy, interrupted)
        # The browser has closed. Ending the phase joins every connection,
        # so nothing records a block or an event after the snapshot below.
        traffic = await proxy.end_phase()
        blocks = proxy.egress_blocks(expected)
    uncertain = (traffic.plain_uncertain, traffic.tunnel_uncertain)
    for why, count in zip(_UNCERTAIN, uncertain, strict=True):
        if count:
            run.stopped("reset_unsafe", why)
    return ExploreEnd(
        run.record,
        Phase(run.attempt, traffic, events),
        confirmation=None,
        blocks=blocks,
        infrastructure_events=tuple(run.gate.infrastructure_events),
        checks=checks,
        stops=tuple(run.stops),
        published=None,
    )


async def _phase(
    run: Run,
    explorable: Explorable,
    proxy: EgressProxy,
    interrupted: Callable[[], bool],
) -> tuple[Mapping[str, AssertionResult | None], int]:
    """Reset, open the browser, load the start URL and take the turns, all
    within the minutes budget: each planned check's last evaluation, None
    while never bound, kept however the phase ended, and how many policy
    events the session recorded."""
    session: BrowserSession | None = None
    attempt: Attempt | None = None
    limit = timeout_at(run.deadline)
    # The browser closes once the budget's span has ended, so the budget
    # never cancels its close and never takes the place of an error the
    # phase raised as it closes.
    async with AsyncExitStack() as browser:
        try:
            async with limit:
                if await _reset(run):
                    await register_identity_engine(run.playwright)
                    session = await browser.enter_async_context(
                        open_browser_session(
                            run.playwright.chromium,
                            egress=proxy,
                            settings=effective_browser(run.spec, run.config),
                            redactor=run.redactor,
                        )
                    )
                    if session.redactor is not run.redactor:
                        raise ValueError(
                            "the browser session must scan with the run's redactor"
                        )
                    attempt = _attempt(run, session, explorable, interrupted)
                    await _started(run, session)
                    if not run.stops and not interrupted():
                        await _turns(run, attempt, explorable.plan)
        except (SandboxUnavailableError, UnregisteredEngineError) as error:
            # The browser isn't as the run requires: launched, or prepared.
            run.stopped("no_sandbox", str(error))
        except TimeoutError:
            if not limit.expired():
                raise  # not the minutes budget's own
            run.stopped("gave_up", _MINUTES)
    checks = (
        dict.fromkeys(planned_checks(explorable.plan))
        if attempt is None
        else attempt.checks
    )
    return checks, 0 if session is None else session.policy_events.total


async def _reset(run: Run) -> bool:
    """Send the spec's reset hook, if it has one, before any browser (D19);
    whether the attempt may go."""
    hook = run.spec.frontmatter.preconditions.reset
    if hook is None:
        return True
    seconds = run.config.budgets.resolve_seconds
    try:
        await reset(run.gate, hook, run.start, seconds=seconds)
    except ResetFailedError as error:
        run.stopped("reset_unsafe", str(error))
        return False
    return True


def _attempt(
    run: Run,
    session: BrowserSession,
    explorable: Explorable,
    interrupted: Callable[[], bool],
) -> Attempt:
    seconds = run.config.budgets.resolve_seconds
    session.limit_waits(
        action_seconds=seconds, navigation_seconds=executor.NAVIGATION_SECONDS
    )
    return Attempt(
        session,
        run.attempt,
        run.start,
        explorable.plan,
        explorable.contracts,
        run.secrets,
        budget=seconds,
        spec=run.spec,
        config=run.config,
        gate=run.gate,
        interrupted=interrupted,
    )


async def _started(run: Run, session: BrowserSession) -> None:
    """Load the start URL as step 0, no side-effect step (DATA_MODEL §7),
    as replay does."""
    path = run.spec.frontmatter.preconditions.start_url
    try:
        step = await executor.dispatched(
            session,
            run.attempt,
            executor.Dispatch(
                0,
                {"action": "navigate", "url": path},
                False,
                executor.NAVIGATION_SECONDS,
            ),
            lambda: session.navigate(start_url(run.spec, run.start)),
        )
    except OSError:
        # A journal line the record couldn't take: nothing more may go.
        run.stopped("record_error", UNWRITTEN)
        return
    if step.outcome != "completed":
        run.stopped("gave_up", f"the start URL didn't load: {step.error}")


async def _turns(run: Run, attempt: Attempt, plan: CoveragePlan) -> None:
    rows = subject_contracts(run.config, run.spec.frontmatter.id)
    turning = Turning(
        attempt,
        run.router,
        navigator_prefix(run.spec, plan, rows, redactor=run.redactor),
        run.spending,
        run.config.budgets.actions_per_attempt,
        run.stops,
    )
    await take_turns(turning)
    run.script = turning.script


def _ended(run: Run, end: ExploreEnd) -> Explored:
    """Write the run's document, and its egress record when blocked, each
    checked as written, and say how the run ended. A record that doesn't
    scan with the run's redactor is refused before anything is written,
    with or without a browser (`ValueError`). One the record refuses or
    can't write adds a record error, which decides unless a more decisive
    outcome holds."""
    if end.record.redactor is not run.redactor:
        raise ValueError("the run's record must scan with the run's redactor")
    document = present(end)
    try:
        run.record.write(
            "explore.json", document, check=require_clean(run.redactor, "explore.json")
        )
        if (egress := document["egress"]) is not None:
            check = require_clean(run.redactor, "egress.json")
            run.record.write("egress.json", egress, check=check)
    except OSError, RefusedWriteError:
        run.stopped("record_error", UNWRITTEN)
        end = replace(end, stops=tuple(run.stops))
        document = present(end)
    outcome = final_outcome(end)
    return Explored(
        outcome,
        _exit(outcome),
        document,
        run.record.path,
        sum((call.cost_usd for call in run.spending.calls), Decimal(0)),
        run.script,
    )
