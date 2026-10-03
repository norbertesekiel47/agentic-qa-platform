"""The M1 strict executor (ADR-0024's Consequences and its #46 amendment): a
compiled script's steps, run in a fresh browser session with the script's
recorded settings, each step's intent on disk before its action and its
completion after (ARCHITECTURE §3.3). No model is involved: nothing here
imports or builds a model client.

It acts and observes only through the browser session's methods, which check
every document they touch (ADR-0026's amendments on document origins)."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, assert_never, overload

from aqa_core.compiled import (
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    Navigate,
    NotVisible,
    Press,
    Reload,
    Select,
    Target,
    TextInTarget,
    TextVisible,
    UrlMatches,
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
from aqa_runner.locators import Absent, Miss, Resolved, Unresolved, Use
from aqa_runner.run_record import RunRecord
from aqa_runner.settling import Settled, Window
from aqa_runner.text_search import SearchTimeoutError, text_matches, url_matches

# How often resolution looks again for a target that hasn't resolved.
LOOK_SECONDS = 0.1

# How long `navigate` and `reload` may wait for the page to load, holding the
# session's lock: Playwright's default, made explicit. An action's wait is
# bounded by the run's `resolve_seconds` instead (ADR-0024's #46 amendment).
NAVIGATION_SECONDS = 30

# How long past those bounds the executor waits for an action before it stops
# waiting: Playwright's own error, which says what it waited for, comes
# first, and this catches what Playwright doesn't time, such as a page script
# that never returns while a field is filled.
MARGIN_SECONDS = 1

# The most of a failed step's reason kept: the rest of Playwright's message
# can hold what the page chose.
REASON_CHARS = 200

# The steps the executor runs: those that act on a target, and the rest.
type Targeted = Click | Fill | Select
type Untargeted = Navigate | Reload | Press

# The checks M1 evaluates; #48 adds the others (DATA_MODEL §7).
type Evaluated = TextVisible | TextInTarget | NotVisible | UrlMatches

# How a step ended: dispatched and settled; its target never resolved, so
# nothing was dispatched; or dispatching or settling raised, so its outcome
# is unknown.
type StepOutcome = Literal["completed", "drifted", "failed"]

# What became of an assertion: its check held, or it didn't; no locator gave
# its target the match its use needs within `resolve_seconds`; its text
# search ran out of time, which establishes neither (DATA_MODEL §7); or it
# wasn't evaluated, since a step stopped the run first or a look at the page
# raised.
type AssertionOutcome = Literal[
    "pass", "failed", "binding_unresolved", "check_timed_out", "not_evaluated"
]

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
class _Dispatch:
    """A step about to be dispatched: what its intent line records, how long
    its action may take, and the locator that found its target."""

    seq: int
    action: dict[str, object]
    side_effect: bool
    wait: float
    target: str | None = None
    locator_index: int | None = None


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
    """One assertion. A `binding_unresolved` one has each locator's miss
    (none when no look finished). A `not_evaluated` one has `stopped_at`,
    the step that stopped the run before any assertion was evaluated, or
    `error`, what a look at the page raised (the rest after it aren't
    evaluated either)."""

    id: str
    outcome: AssertionOutcome
    misses: tuple[Miss, ...] = ()
    stopped_at: int | None = None
    error: str | None = None


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
    runnable, checks = _accepted(script)
    record = setup.record
    steps: list[StepResult] = []
    async with open_browser_session(
        chromium, egress=proxy, settings=script.browser
    ) as session:
        session.limit_waits(
            action_seconds=setup.config.budgets.resolve_seconds,
            navigation_seconds=NAVIGATION_SECONDS,
        )

        def interrupted() -> bool:
            """Whether the gate couldn't reach an allowed host, the session
            met a document off the allowed origins, or the page hit an egress
            block (routing refused a request, or the gate did). What policy
            events and egress blocks do to a run is #47's; until then each
            ends the run errored, so an egress block never lets it pass."""
            return (
                bool(gate.infrastructure_events)
                or session.policy_events.total > 0
                or proxy.blocked_attempts.total > 0
                or bool(gate.refusals)
            )

        # Seq 0 is no compiled step: a compiled script's steps start at 1.
        first = start_url(setup.spec, setup.start)
        path = setup.spec.frontmatter.preconditions.start_url
        steps.append(
            await _dispatched(
                session,
                record,
                _Dispatch(
                    0, {"action": "navigate", "url": path}, False, NAVIGATION_SECONDS
                ),
                lambda: session.navigate(first),
            )
        )
        for step in runnable:
            if steps[-1].outcome != "completed" or interrupted():
                break
            result = await _run(session, setup, script, step, interrupted)
            if result is None:
                break
            steps.append(result)
        if steps[-1].outcome != "completed" or interrupted():
            assertions = tuple(
                AssertionResult(check.id, "not_evaluated", stopped_at=steps[-1].seq)
                for check in checks
            )
        else:
            assertions = await _evaluate_each(
                session, checks, script.targets, setup.config.budgets.resolve_seconds
            )
        errored = (
            interrupted()
            or any(step.outcome == "failed" for step in steps)
            or any(assertion.error is not None for assertion in assertions)
        )
        policy_events = tuple(session.policy_events.kept)
    passed = all(step.outcome == "completed" for step in steps) and all(
        assertion.outcome == "pass" for assertion in assertions
    )
    return RunResult(
        record.run_id,
        "errored" if errored else "passed" if passed else "failed",
        tuple(steps),
        assertions,
        tuple(gate.infrastructure_events),
        policy_events,
    )


def _accepted(
    script: CompiledScript,
) -> tuple[list[Targeted | Untargeted], list[Evaluated]]:
    """`script`'s steps and assertions, or `SpecError` naming each step and
    check M1 can't run, before anything is opened (ADR-0024's #46
    amendment)."""
    runnable: list[Targeted | Untargeted] = []
    checks: list[Evaluated] = []
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
    for index, assertion in enumerate(script.assertions):
        if isinstance(assertion, TextVisible | TextInTarget | NotVisible | UrlMatches):
            checks.append(assertion)
        else:
            problems.append(
                f"assertions[{index}] ({assertion.id}): {assertion.check} is not "
                "evaluated until #48"
            )
    if problems:
        raise SpecError(problems)
    return runnable, checks


class _UnansweredError(Exception):
    """A look at the page that didn't finish within its budget: Playwright's
    reads of a page take no timeout of their own."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"the page didn't answer within {seconds:g} s")


async def _evaluate_each(
    session: BrowserSession,
    checks: list[Evaluated],
    targets: Mapping[str, Target],
    budget: float,
) -> tuple[AssertionResult, ...]:
    """Every assertion, in order, each evaluated once, as the last step left
    the page; each gets `budget` of its own. First the page must answer one
    look (its visible text): a URL or a target's state can be read from a
    page whose renderer has stopped. Once a look at the page raises, what
    remains isn't evaluated, and each says which assertion's look it was."""
    try:
        await _bounded(_read(session, budget), budget)
    except (Error, PolicyEventError, DocumentChangedError, _UnansweredError) as error:
        # As below: the page couldn't be looked at, so nothing on it can be
        # evaluated.
        reason = _described(error)
        return tuple(
            AssertionResult(check.id, "not_evaluated", error=reason) for check in checks
        )
    results: list[AssertionResult] = []
    raised: str | None = None
    for check in checks:
        if raised is not None:
            reason = f"not evaluated after {raised}'s look at the page raised"
            results.append(AssertionResult(check.id, "not_evaluated", error=reason))
            continue
        try:
            results.append(await _evaluate(session, check, targets, budget))
        except (
            Error,
            PolicyEventError,
            DocumentChangedError,
            _UnansweredError,
        ) as error:
            # Playwright's general error type (a crashed page, or a css value
            # that isn't CSS), a page off the allowed origins, one that
            # changed under every read within the budget, or one that didn't
            # answer: the look raised, so the check can't be evaluated.
            raised = check.id
            results.append(
                AssertionResult(check.id, "not_evaluated", error=_described(error))
            )
    return tuple(results)


async def _evaluate(
    session: BrowserSession,
    check: Evaluated,
    targets: Mapping[str, Target],
    budget: float,
) -> AssertionResult:
    """`check`'s outcome (DATA_MODEL §7, Replay outcomes)."""
    try:
        held = await _held(session, check, targets, budget)
    except SearchTimeoutError:
        return AssertionResult(check.id, "check_timed_out")
    if isinstance(held, Unresolved):
        return AssertionResult(check.id, "binding_unresolved", held.misses)
    return AssertionResult(check.id, "pass" if held else "failed")


async def _held(
    session: BrowserSession,
    check: Evaluated,
    targets: Mapping[str, Target],
    budget: float,
) -> bool | Unresolved:
    """Whether `check` holds, through the session's observations and the
    bounded text search, or the drift that kept its target from being found.
    What it looks at on the page takes at most `budget` seconds and
    `MARGIN_SECONDS` past them."""
    match check:
        case TextVisible():
            text = await _bounded(_read(session, budget), budget)
            return await text_matches(check, text)
        case UrlMatches(pattern=pattern):
            return await url_matches(pattern, await _bounded(session.url(), budget))
        case TextInTarget(target=name):
            seen = await _bounded(_target_text(session, targets[name], budget), budget)
            return (
                seen
                if isinstance(seen, Unresolved)
                else await text_matches(check, seen)
            )
        case NotVisible(target=name):
            return await _bounded(_absent(session, targets[name], budget), budget)
        case _:
            assert_never(check)


async def _bounded[T](look: Awaitable[T], budget: float) -> T:
    """`look`'s result, or `_UnansweredError` when the page hasn't answered
    within `budget` seconds and `MARGIN_SECONDS` past them."""
    seconds = budget + MARGIN_SECONDS
    limit = asyncio.timeout(seconds)
    try:
        async with limit:
            return await look
    except TimeoutError:
        if not limit.expired():
            raise  # not the look's own limit
        raise _UnansweredError(seconds) from None


async def _read(session: BrowserSession, budget: float) -> str:
    """The page's visible text, read again while the page changes under the
    read, up to `budget` seconds; then the last change raises."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while True:
        try:
            return await session.visible_text()
        except DocumentChangedError:
            if loop.time() >= deadline:
                raise
        await asyncio.sleep(LOOK_SECONDS)


async def _target_text(
    session: BrowserSession, target: Target, budget: float
) -> str | Unresolved:
    """`target`'s rendered text, or why it wasn't found within `budget`."""
    found = _answered(await _resolve(session, target, "assertion", budget), budget)
    if not isinstance(found, Resolved):
        return found
    try:
        return await session.text_of(found.element)
    finally:
        await found.element.dispose()


def _answered[R: Resolved | Absent | Unresolved](found: R, budget: float) -> R:
    """`found`, unless no look at the page finished within `budget`: that
    is a page that didn't answer, not drift (every target has a locator, so
    a finished look leaves a miss)."""
    if isinstance(found, Unresolved) and not found.misses:
        raise _UnansweredError(budget)
    return found


async def _absent(
    session: BrowserSession, target: Target, budget: float
) -> bool | Unresolved:
    """Whether `target` is absent: looked at once, so a target that is there
    fails at once (ADR-0024's #46 amendment), and only drift is waited out,
    within `budget`."""
    found = _answered(await _resolve(session, target, "negative_check", budget), budget)
    if isinstance(found, Resolved):
        await found.element.dispose()
        return False
    return found if isinstance(found, Unresolved) else True


async def _run(
    session: BrowserSession,
    setup: RunSetup,
    script: CompiledScript,
    step: Targeted | Untargeted,
    interrupted: Callable[[], bool],
) -> StepResult | None:
    """Resolve `step`'s target, if it has one, then record, dispatch and
    settle it. None when the run was `interrupted` while the target was
    looked for: nothing is dispatched after that."""
    budget = setup.config.budgets.resolve_seconds
    action = step.model_dump(
        mode="json", exclude={"seq", "side_effect", "side_effect_basis", "satisfies"}
    )
    if isinstance(step, Navigate | Reload | Press):
        wait = budget if isinstance(step, Press) else NAVIGATION_SECONDS
        return await _dispatched(
            session,
            setup.record,
            _Dispatch(step.seq, action, step.side_effect, wait),
            lambda: _untargeted(session, step, setup.start),
        )
    try:
        found = await _resolve(session, script.targets[step.target], "action", budget)
    except (Error, PolicyEventError) as error:
        # A look the session refused (a page off the allowed origins), or
        # Playwright's general error type (a crashed page): nothing was
        # dispatched, so there is no intent, and the run stops.
        return StepResult(step.seq, "failed", error=_described(error))
    if not isinstance(found, Resolved):
        return StepResult(step.seq, "drifted", misses=found.misses)
    if interrupted():
        return None
    return await _dispatched(
        session,
        setup.record,
        _Dispatch(
            step.seq, action, step.side_effect, budget, step.target, found.locator_index
        ),
        lambda: _targeted(session, step, found.element),
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


@overload
async def _resolve(
    session: BrowserSession,
    target: Target,
    use: Literal["action", "assertion"],
    budget: float,
) -> Resolved | Unresolved: ...


@overload
async def _resolve(
    session: BrowserSession,
    target: Target,
    use: Literal["negative_check"],
    budget: float,
) -> Resolved | Absent | Unresolved: ...


async def _resolve(
    session: BrowserSession, target: Target, use: Use, budget: float
) -> Resolved | Absent | Unresolved:
    """`target`'s element for `use`, looked for every `LOOK_SECONDS` while
    no locator gives the match the use needs, until `budget` seconds have
    passed: then the last look's misses, or none when no look finished.

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
                found = await session.resolve(target, use)
        except TimeoutError:
            if not limit.expired():
                raise  # not the budget's own limit
            break
        except DocumentChangedError:
            pass  # the page changed under the look: look again
        else:
            if not isinstance(found, Unresolved):
                return found
            last = found
        await asyncio.sleep(min(LOOK_SECONDS, max(0.0, deadline - loop.time())))
    return last


async def _dispatched(
    session: BrowserSession,
    record: RunRecord,
    dispatch: _Dispatch,
    act: Callable[[], Awaitable[Window]],
) -> StepResult:
    """Record `dispatch`'s intent, then dispatch its step with `act`, giving
    it up to `MARGIN_SECONDS` past its wait, settle it, and record its
    completion with the locator that found its target."""
    seq, index = dispatch.seq, dispatch.locator_index
    record.step_intent(
        seq,
        dispatch.action,
        side_effect=dispatch.side_effect,
        target_used=dispatch.target,
    )
    seconds = dispatch.wait + MARGIN_SECONDS
    limit = asyncio.timeout(seconds)
    try:
        async with limit:
            window = await act()
        settled = await session.settle(window)
    except (Error, PolicyEventError) as error:
        # Playwright's general error type (a failed navigation, an action
        # the page refused, a crashed page) or a document off the allowed
        # origins, raised between the intent and the completion: whether
        # the action took effect is unknown, so its intent stays unresolved
        # and the run stops.
        return StepResult(seq, "failed", error=_described(error))
    except TimeoutError:
        if not limit.expired():
            raise  # not the action's own limit
        return StepResult(
            seq, "failed", error=f"the action didn't finish within {seconds:g} s"
        )
    record.step_completed(seq, locator_used=index, settled=settled)
    return StepResult(seq, "completed", window, index, settled)


def _described(
    error: Error | PolicyEventError | DocumentChangedError | _UnansweredError,
) -> str:
    """What raised, as a failed step's or an unevaluated assertion's reason:
    the executor's or the session's own message (a policy event's names
    only an origin); or the first line of Playwright's, with
    what isn't printable escaped, at most `REASON_CHARS` in all. The rest of
    Playwright's message can hold what the page chose (#49 must keep it
    from a filled secret)."""
    if isinstance(error, PolicyEventError | DocumentChangedError | _UnansweredError):
        return str(error)
    line = str(error).split("\n", 1)[0]
    shown = "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in line
    )
    return f"{type(error).__name__}: {shown}"[:REASON_CHARS]
