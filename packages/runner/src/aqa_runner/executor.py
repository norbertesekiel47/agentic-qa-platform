"""The M1 strict executor (ADR-0024's Consequences and its #46 amendment): a
compiled script's steps, run in a fresh browser session with the script's
recorded settings, each step's intent on disk before its action and its
completion after (ARCHITECTURE §3.3). No model is involved: nothing here
imports or builds a model client.

It acts and observes only through the browser session's methods, which check
every document they touch (ADR-0026's amendments on document origins)."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from aqa_core.compiled import (
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    Navigate,
    Press,
    Reload,
    Select,
    Target,
)
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, path_on_origin, start_url
from aqa_core.spec import Spec
from playwright.async_api import BrowserType, ElementHandle, Error

from aqa_runner.browser_session import BrowserSession, one_key, open_browser_session
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
)
from aqa_runner.egress import EgressGate, InfrastructureEvent
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.locators import Miss, Resolved, Unresolved
from aqa_runner.run_record import RunRecord
from aqa_runner.settling import Settled, Window

# How often resolution looks again for a target that hasn't resolved.
LOOK_SECONDS = 0.1

# How long `navigate` and `reload` may wait for the page to load, holding the
# session's lock: Playwright's default, made explicit. An action's wait is
# bounded by the run's `resolve_seconds` instead (ADR-0024's #46 amendment).
NAVIGATION_SECONDS = 30

# The checks M1 evaluates; #48 adds the others (DATA_MODEL §7).
EVALUATED = frozenset({"text_visible", "text_in_target", "not_visible", "url_matches"})

# The steps the executor runs: those that act on a target, and the rest.
type Targeted = Click | Fill | Select
type Untargeted = Navigate | Reload | Press

# How a step ended: dispatched and settled; its target never resolved, so
# nothing was dispatched; or dispatching or settling raised, so its outcome
# is unknown.
type StepOutcome = Literal["completed", "drifted", "failed"]

# What became of an assertion.
type AssertionOutcome = Literal["not_evaluated"]

# How the run ended (DATA_MODEL's `runs.status`).
type RunOutcome = Literal["passed", "failed", "errored"]


@dataclass(frozen=True)
class RunSetup:
    """What a replay runs for: the spec and project config, the run's start
    origin (`aqa_core.project.start_origin`), and the run's record."""

    spec: Spec
    config: ProjectConfig
    start: str
    record: RunRecord


@dataclass(frozen=True)
class _Intent:
    """A step about to be dispatched, as its intent line records it."""

    seq: int
    action: dict[str, object]
    side_effect: bool
    target: str | None = None


@dataclass(frozen=True)
class StepResult:
    """One step: `seq` 0 is the start URL's navigation. A completed step
    has its settle `window`, whose requests are the step's, the index of
    the locator that found its target, if it had one, and how settling
    ended. A drifted step has each locator's miss; a failed one, what
    raised."""

    seq: int
    outcome: StepOutcome
    window: Window | None = None
    locator_index: int | None = None
    settled: Settled | None = None
    misses: tuple[Miss, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class AssertionResult:
    """One assertion. `stopped_at` is the step that stopped the run before
    any assertion was evaluated, if one did."""

    id: str
    outcome: AssertionOutcome
    stopped_at: int | None = None


@dataclass(frozen=True)
class RunResult:
    """A replay: every step that ran and every assertion, each with its
    outcome, and the run's; the egress gate's infrastructure events and the
    session's policy events (the first 100)."""

    run_id: str
    outcome: RunOutcome
    steps: tuple[StepResult, ...]
    assertions: tuple[AssertionResult, ...]
    infrastructure_events: tuple[InfrastructureEvent, ...]
    policy_events: tuple[PolicyEvent, ...]


async def replay(
    script: CompiledScript,
    setup: RunSetup,
    *,
    chromium: BrowserType,
    proxy: EgressProxy,
    gate: EgressGate,
) -> RunResult:
    """Run `script` once for `setup`, through `proxy`, the run's egress
    proxy, whose gate is `gate`, recording each step in the setup's record.
    The session uses `script.browser`, never the project's or the spec's
    settings (ADR-0025)."""
    runnable = _runnable(script)
    record = setup.record
    steps: list[StepResult] = []
    async with open_browser_session(
        chromium, egress=proxy, settings=script.browser
    ) as session:
        session.limit_waits(
            action_seconds=setup.config.budgets.resolve_seconds,
            navigation_seconds=NAVIGATION_SECONDS,
        )
        first = start_url(setup.spec, setup.start)
        path = setup.spec.frontmatter.preconditions.start_url
        steps.append(
            await _dispatched(
                session,
                record,
                _Intent(0, {"action": "navigate", "url": path}, side_effect=False),
                lambda: session.navigate(first),
            )
        )
        for step in runnable:
            if _stopped(steps, session, gate):
                break
            steps.append(await _run(session, setup, script, step))
        stopped = steps[-1].seq if _stopped(steps, session, gate) else None
        policy_events = tuple(session.policy_events.kept)
    assertions = tuple(
        AssertionResult(assertion.id, "not_evaluated", stopped)
        for assertion in script.assertions
    )
    errored = bool(gate.infrastructure_events or policy_events) or any(
        step.outcome == "failed" for step in steps
    )
    return RunResult(
        record.run_id,
        _outcome(errored=errored),
        tuple(steps),
        assertions,
        tuple(gate.infrastructure_events),
        policy_events,
    )


def _stopped(
    steps: Sequence[StepResult], session: BrowserSession, gate: EgressGate
) -> bool:
    """Whether the run stops after the last step: it didn't complete, or the
    gate couldn't reach an allowed host, or the session met a document off
    the allowed origins (what a policy event does to a run is #47's; until
    then it ends the run errored)."""
    return (
        steps[-1].outcome != "completed"
        or bool(gate.infrastructure_events)
        or session.policy_events.total > 0
    )


def _runnable(script: CompiledScript) -> list[Targeted | Untargeted]:
    """`script`'s steps, or `SpecError` naming each step and check M1 can't
    run, before anything is opened (ADR-0024's #46 amendment)."""
    runnable: list[Targeted | Untargeted] = []
    problems: list[str] = []
    for index, step in enumerate(script.steps):
        where = f"steps[{index}] (seq {step.seq})"
        if isinstance(step, FillSecret):
            problems.append(f"{where}: fill_secret is not run until #49")
        elif isinstance(step, Press) and not one_key(step.key):
            problems.append(
                f"{where}: press takes one key, with only modifiers held before "
                f"it: {step.key[:40]!r}"
            )
        else:
            runnable.append(step)
    problems.extend(
        f"assertions[{index}] ({assertion.id}): {assertion.check} is not evaluated until #48"
        for index, assertion in enumerate(script.assertions)
        if assertion.check not in EVALUATED
    )
    if problems:
        raise SpecError(problems)
    return runnable


async def _run(
    session: BrowserSession,
    setup: RunSetup,
    script: CompiledScript,
    step: Targeted | Untargeted,
) -> StepResult:
    """Resolve `step`'s target, if it has one, then record, dispatch and
    settle it."""
    action = step.model_dump(
        mode="json", exclude={"seq", "side_effect", "side_effect_basis", "satisfies"}
    )
    if isinstance(step, Navigate | Reload | Press):
        return await _dispatched(
            session,
            setup.record,
            _Intent(step.seq, action, step.side_effect),
            lambda: _untargeted(session, step, setup.start),
        )
    target = script.targets[step.target]
    found = await _resolve(session, target, setup.config.budgets.resolve_seconds)
    if not isinstance(found, Resolved):
        return StepResult(step.seq, "drifted", misses=found.misses)
    return await _dispatched(
        session,
        setup.record,
        _Intent(step.seq, action, step.side_effect, step.target),
        lambda: _targeted(session, step, found.element),
        found.locator_index,
    )


def _untargeted(
    session: BrowserSession, step: Untargeted, start: str
) -> Awaitable[Window]:
    """`step`'s action, which acts on no target, through the session."""
    if isinstance(step, Navigate):
        return session.navigate(path_on_origin(start, step.url))
    if isinstance(step, Reload):
        return session.reload()
    return session.press(step.key)


def _targeted(
    session: BrowserSession, step: Targeted, element: ElementHandle
) -> Awaitable[Window]:
    """`step`'s action on `element`, its target's, through the session."""
    if isinstance(step, Click):
        return session.click(element)
    if isinstance(step, Fill):
        return session.fill(element, step.value)
    return session.select(element, step.option)


async def _resolve(
    session: BrowserSession, target: Target, budget: float
) -> Resolved | Unresolved:
    """`target`'s element for an action, looked for every `LOOK_SECONDS`
    until it resolves or `budget` seconds have passed: then the last look's
    misses, or none when no look finished.

    Each look is cut off when the budget runs out, so a look that waits for
    good, as `is_enabled` can on an element moved into another document
    (ADR-0025's #52 amendment), can't hold the run. A look the page changed
    under (`DocumentChangedError`) is made again."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    last = Unresolved(())
    while loop.time() < deadline:
        limit = asyncio.timeout_at(deadline)
        try:
            async with limit:
                found = await session.resolve(target, "action")
        except TimeoutError:
            if not limit.expired():
                raise  # not the budget's own limit
            break
        except DocumentChangedError:
            pass  # the page changed under the look: look again
        else:
            if isinstance(found, Resolved):
                return found
            last = found
        await asyncio.sleep(LOOK_SECONDS)
    return last


async def _dispatched(
    session: BrowserSession,
    record: RunRecord,
    intent: _Intent,
    act: Callable[[], Awaitable[Window]],
    locator_index: int | None = None,
) -> StepResult:
    """Record `intent`, then dispatch its step with `act`, settle it, and
    record its completion with the locator that found its target."""
    seq = intent.seq
    record.step_intent(
        seq, intent.action, side_effect=intent.side_effect, target_used=intent.target
    )
    try:
        window = await act()
        settled = await session.settle(window)
    except (Error, PolicyEventError) as error:
        # Playwright's general error type (a failed navigation, an action
        # the page refused, a crashed page) or a document off the allowed
        # origins, raised between the intent and the completion: whether
        # the action took effect is unknown, so its intent stays unresolved
        # and the run stops.
        return StepResult(seq, "failed", error=str(error))
    record.step_completed(seq, locator_used=locator_index, settled=settled)
    return StepResult(seq, "completed", window, locator_index, settled)


def _outcome(*, errored: bool) -> RunOutcome:
    """`errored` when told. Otherwise `failed`: a run passes only when every
    step completed and every assertion passed, and no assertion is
    evaluated yet."""
    return "errored" if errored else "failed"
