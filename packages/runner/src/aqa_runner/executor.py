"""The M1 strict executor (ADR-0024's Consequences and its #46 amendment): a
compiled script's steps, run in a fresh browser session with the script's
recorded settings, each step's intent on disk before its action and its
completion after (ARCHITECTURE §3.3). No model is involved: nothing here
imports or builds a model client.

Browser observations use the session methods, which check every document
they touch (ADR-0026). Probe reads use the same egress gate as the browser."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Literal, assert_never, overload

from aqa_core.compiled import (
    Assertion,
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    Navigate,
    NetworkNone,
    NetworkSeen,
    NotVisible,
    Press,
    ProbeBaseline,
    ProbeEquals,
    ProbeEqualsBaseline,
    Reload,
    Select,
    Target,
    TextInTarget,
    TextVisible,
    UrlMatches,
    VisibleUnoccluded,
)
from aqa_core.config import ProjectConfig
from aqa_core.project import (
    SpecError,
    contract_problems,
    path_on_origin,
    probe_url,
    start_url,
)
from aqa_core.spec import Spec, secret_references
from playwright.async_api import BrowserType, ElementHandle, Error

from aqa_runner import network, probes
from aqa_runner.bound_secrets import BoundSecret, bound_secrets
from aqa_runner.browser_session import (
    BrowserSession,
    UnsupportedVisualFrameError,
    one_key,
    open_browser_session,
)
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
)
from aqa_runner.egress import (
    EgressGate,
    EgressRefusedError,
    EgressUpstreamError,
    InfrastructureEvent,
)
from aqa_runner.egress_proxy import EgressBlocks, EgressProxy
from aqa_runner.evidence import capture_evidence
from aqa_runner.invariants import InvariantResult, invariant_results
from aqa_runner.locators import Absent, Miss, Resolved, Unresolved, Use
from aqa_runner.redaction import Redacted, Redactor, error_text
from aqa_runner.run_record import RefusedWriteError, RunRecord
from aqa_runner.secret_fields import SecretNotFilledError, SecretRefusedError
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

# The steps the executor runs: those that act on a target, and the rest.
type Targeted = Click | Fill | FillSecret | Select
type Untargeted = Navigate | Reload | Press

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
type ErrorCode = Literal["egress_blocked"]


@dataclass(frozen=True)
class RunSetup:
    """What a replay runs for: the spec and project config, the run's start
    origin (`aqa_core.project.start_origin`), and the run's record."""

    spec: Spec
    config: ProjectConfig
    start: str
    record: RunRecord


@dataclass(frozen=True)
class _Replay:
    """State belonging to one replay. Captured baselines stay in memory;
    they never enter the result or the run record."""

    setup: RunSetup
    targets: Mapping[str, Target]
    secrets: Mapping[str, BoundSecret]
    gate: EgressGate
    redactor: Redactor
    baseline_definitions: Mapping[str, ProbeBaseline]
    baseline_values: dict[str, probes.JsonValue] = field(default_factory=dict)

    @property
    def withheld(self) -> bool:
        return bool(self.secrets)

    def reason(self, error: _Raised) -> str:
        """`error` as a failed step's or an unevaluated assertion's reason
        (`_owned`, then `error_text`: scanned for every bound value, the
        ones the script never fills too). When the script fills a test
        secret, Playwright's message is withheld for the whole run, the steps before the first
        fill included: a page handed a value can throw it back in any later
        error, and the run never needs to know which steps came after it."""
        return error_text(
            error, self.redactor, ours=_owned(error), withheld=self.withheld
        )


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
    session's policy events (the first 100). Invariants are judged for every
    run, errored ones included. Egress blocks keep hosts in memory until
    #50 redacts what #53 prints or saves."""

    run_id: str
    outcome: RunOutcome
    steps: tuple[StepResult, ...]
    assertions: tuple[AssertionResult, ...]
    infrastructure_events: tuple[InfrastructureEvent, ...]
    policy_events: tuple[PolicyEvent, ...]
    invariants: tuple[InvariantResult, ...]
    egress_blocks: EgressBlocks
    error_code: ErrorCode | None


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
    settings (ADR-0025). Once its browser has closed, replay ends the
    proxy's open phase and only then reads the run's egress blocks and
    infrastructure events, so the proxy is between phases when it returns.

    Before accepting steps or binding secrets, the script must agree with the
    project's subject contracts. A mismatch raises `SpecError`.

    Before the browser starts, it binds the test secrets the spec references
    (`aqa_runner.bound_secrets.bound_secrets`): a missing value raises
    `MissingSecretError`, Playwright's protocol logging `SecretLoggedError`,
    and a binding the run doesn't allow `SpecError`. When the script fills a
    test secret, every reason keeps nothing of Playwright's messages, for the
    whole run (`_Replay.reason`).

    After every step it keeps, it saves the step's evidence in the record,
    which scans what it writes with the session's redactor
    (`aqa_runner.evidence`)."""
    # The script's own problems first, so a spec error wins over a missing
    # value (exit 5 before 12).
    if problems := contract_problems(script, setup.config):
        raise SpecError(problems)
    runnable, checks = _accepted(script, setup.spec)
    bound = bound_secrets(setup.spec, setup.start)
    redactor = Redactor(bound.values())
    setup = replace(setup, record=setup.record.redacting(redactor))
    run = _Replay(
        setup,
        script.targets,
        {
            step.secret: bound[step.secret]
            for step in runnable
            if isinstance(step, FillSecret)
        },
        gate,
        redactor,
        script.probe_baselines,
    )
    record = setup.record
    steps: list[StepResult] = []
    async with open_browser_session(
        chromium, egress=proxy, settings=script.browser, redactor=redactor
    ) as session:
        session.limit_waits(
            action_seconds=setup.config.budgets.resolve_seconds,
            navigation_seconds=NAVIGATION_SECONDS,
        )
        expected = setup.config.egress.expected_blocked

        def interrupted() -> bool:
            return (
                bool(gate.infrastructure_events)
                or proxy.egress_blocks(expected).blocked
            )

        # Seq 0 is no compiled step: a compiled script's steps start at 1.
        first = start_url(setup.spec, setup.start)
        path = setup.spec.frontmatter.preconditions.start_url
        await _keep_with_evidence(
            session,
            run,
            steps,
            await _dispatched(
                session,
                run,
                _Dispatch(
                    0, {"action": "navigate", "url": path}, False, NAVIGATION_SECONDS
                ),
                lambda: session.navigate(first),
            ),
        )
        for step in runnable:
            if steps[-1].outcome != "completed" or interrupted():
                break
            result = await _run(session, run, step, interrupted)
            if result is None:
                break
            await _keep_with_evidence(session, run, steps, result)
        if steps[-1].outcome != "completed" or interrupted():
            assertions = tuple(
                AssertionResult(check.id, "not_evaluated", stopped_at=steps[-1].seq)
                for check in checks
            )
        else:
            assertions = await _evaluate_each(session, checks, run)
        invariants = invariant_results(
            session.invariant_observers.seen, setup.spec.frontmatter.invariants
        )
        if run.withheld:
            invariants = tuple(replace(result, seen=()) for result in invariants)
        policy_events = tuple(session.policy_events.kept)
    # Ending the phase joins every connection the browser opened, so no
    # proxy task can record a block or an event after the verdict.
    await proxy.end_phase()
    blocks = proxy.egress_blocks(expected)
    errored = (
        blocks.blocked
        or bool(gate.infrastructure_events)
        or any(step.outcome == "failed" for step in steps)
        or any(assertion.error is not None for assertion in assertions)
    )
    passed = (
        all(step.outcome == "completed" for step in steps)
        and all(assertion.outcome == "pass" for assertion in assertions)
        and all(invariant.outcome != "violated" for invariant in invariants)
    )
    if blocks.blocked:
        _write_egress_record(record, blocks, withheld=run.withheld)
    return RunResult(
        record.run_id,
        "errored" if errored else "passed" if passed else "failed",
        tuple(steps),
        assertions,
        tuple(_scanned(event, redactor) for event in gate.infrastructure_events),
        policy_events,
        invariants,
        blocks,
        "egress_blocked" if blocks.blocked else None,
    )


async def _keep_with_evidence(
    session: BrowserSession, run: _Replay, steps: list[StepResult], step: StepResult
) -> None:
    """Keep `step`, then save its evidence with the page's snapshot as the
    step left it."""
    steps.append(step)
    budget = run.setup.config.budgets.resolve_seconds
    snapshot = await _observed(session, budget)
    await capture_evidence(run.setup.record, session, step.seq, step.window, snapshot)


async def _observed(session: BrowserSession, budget: float) -> Redacted | None:
    """The page's snapshot for evidence, looked at as `_looked` looks, or
    None for a page off the allowed origins, stopped, crashed or changing
    throughout. Its refusal records no policy event, so evidence leaves
    the result as it was."""
    try:
        return await _bounded(
            _looked(lambda: session.snapshot(record_refusal=False), budget), budget
        )
    except Error, PolicyEventError, DocumentChangedError, _UnansweredError:
        return None


def _accepted(
    script: CompiledScript, spec: Spec
) -> tuple[list[Targeted | Untargeted], list[Assertion]]:
    """`script`'s steps and assertions, or `SpecError` naming each step and
    check M1 can't run, before anything is opened (ADR-0024's #46
    amendment), and each `fill_secret` naming a secret `spec` doesn't
    reference, which has no binding in the run. Probe assertions and baseline
    definitions must name probes declared by the spec."""
    referenced = {name for _, name in secret_references(spec.frontmatter)}
    runnable: list[Targeted | Untargeted] = []
    checks: list[Assertion] = []
    problems: list[str] = []
    for index, step in enumerate(script.steps):
        where = f"steps[{index}] (seq {step.seq})"
        if isinstance(step, FillSecret) and step.secret not in referenced:
            problems.append(
                f"{where}: fill_secret names {step.secret}, which the spec doesn't "
                "reference, so this run has no binding for it"
            )
        elif isinstance(step, Press) and not one_key(step.key):
            problems.append(
                f"{where}: press takes one key, with only modifiers held before "
                f"it: {step.key[:40]!r}"
            )
        else:
            runnable.append(step)
    declared = spec.frontmatter.preconditions.probes
    for index, assertion in enumerate(script.assertions):
        if (
            isinstance(assertion, ProbeEquals | ProbeEqualsBaseline)
            and assertion.probe not in declared
        ):
            problems.append(
                f"assertions[{index}] ({assertion.id}): probe {assertion.probe} is not declared by the spec"
            )
        if isinstance(assertion, VisibleUnoccluded) and not assertion.in_viewport:
            problems.append(
                f"assertions[{index}] ({assertion.id}): "
                "visible_unoccluded requires in_viewport: true"
            )
        else:
            checks.append(assertion)
    problems.extend(
        f"probe_baselines[{name}]: probe {name} is not declared by the spec"
        for name in script.probe_baselines
        if name not in declared
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
    session: BrowserSession, checks: list[Assertion], run: _Replay
) -> tuple[AssertionResult, ...]:
    """Every assertion, in order, each evaluated once, as the last step left
    the page; each gets the run's `resolve_seconds` of its own. First the
    page must answer one look (its visible text): a URL or a target's state
    can be read from a page whose renderer has stopped. Once a look at the
    page raises, what remains isn't evaluated, and each says which
    assertion's look it was."""
    budget = run.setup.config.budgets.resolve_seconds
    try:
        await _bounded(_looked(session.visible_text, budget), budget)
    except (Error, PolicyEventError, DocumentChangedError, _UnansweredError) as error:
        # As below: the page couldn't be looked at, so nothing on it can be
        # evaluated.
        reason = run.reason(error)
        return tuple(
            AssertionResult(check.id, "not_evaluated", error=reason) for check in checks
        )
    results: list[AssertionResult] = []
    raised: str | None = None
    for check in checks:
        if raised is not None:
            reason = run.redactor.redact(
                f"not evaluated after {raised}'s look at the page raised"
            )
            results.append(AssertionResult(check.id, "not_evaluated", error=reason))
            continue
        try:
            results.append(await _evaluate(session, check, run, budget))
        except (
            Error,
            PolicyEventError,
            DocumentChangedError,
            _UnansweredError,
            probes.ProbeError,
            EgressRefusedError,
            EgressUpstreamError,
            network.WindowsOverflowError,
            UnsupportedVisualFrameError,
        ) as error:
            raised = check.id
            results.append(
                AssertionResult(
                    check.id,
                    "not_evaluated",
                    error=run.reason(error),
                )
            )
    return tuple(results)


async def _evaluate(
    session: BrowserSession,
    check: Assertion,
    run: _Replay,
    budget: float,
) -> AssertionResult:
    """`check`'s outcome (DATA_MODEL §7, Replay outcomes)."""
    try:
        held = await _held(session, check, run, budget)
    except SearchTimeoutError, probes.ProbeUnstableError:
        return AssertionResult(check.id, "check_timed_out")
    if isinstance(held, Unresolved):
        return AssertionResult(check.id, "binding_unresolved", held.misses)
    return AssertionResult(check.id, "pass" if held else "failed")


async def _held(
    session: BrowserSession,
    check: Assertion,
    run: _Replay,
    budget: float,
) -> bool | Unresolved:
    """Whether `check` holds, through the session's observations and the
    bounded text search, or the drift that kept its target from being found.
    What it looks at on the page takes at most `budget` seconds and
    `MARGIN_SECONDS` past them."""
    match check:
        case TextVisible() | TextInTarget():
            return await _text_held(session, check, run.targets, budget)
        case UrlMatches(pattern=pattern):
            return await url_matches(pattern, await _bounded(session.url(), budget))
        case NotVisible(target=name):
            return await _bounded(_absent(session, run.targets[name], budget), budget)
        case VisibleUnoccluded(target=name):
            return await _bounded(
                _visual(session, run.targets[name], check, budget), budget
            )
        case NetworkNone() | NetworkSeen():
            return await session.network_held(check)
        case ProbeEquals() | ProbeEqualsBaseline():
            expected: probes.JsonValue
            if isinstance(check, ProbeEquals):
                path, expected = check.json_path, check.value
            else:
                path = run.baseline_definitions[check.probe].json_path
                expected = run.baseline_values[check.probe]
            value = await _read_probe(run, check.probe, path)
            return probes.same(value, expected)
        case _:
            assert_never(check)


async def _text_held(
    session: BrowserSession,
    check: TextVisible | TextInTarget,
    targets: Mapping[str, Target],
    budget: float,
) -> bool | Unresolved:
    seen = await _bounded(
        _target_text(session, targets[check.target], budget)
        if isinstance(check, TextInTarget)
        else _looked(session.visible_text, budget),
        budget,
    )
    return seen if isinstance(seen, Unresolved) else await text_matches(check, seen)


async def _visual(
    session: BrowserSession, target: Target, check: VisibleUnoccluded, budget: float
) -> bool | Unresolved:
    found = _answered(await _resolve(session, target, "assertion", budget), budget)
    if not isinstance(found, Resolved):
        return found
    try:
        return await session.unoccluded(
            found.element, check.min_size_px, check.in_viewport
        )
    finally:
        await found.element.dispose()


async def _read_probe(run: _Replay, name: str, path: str) -> probes.JsonValue:
    try:
        return await probes.read_stable(
            run.gate, probe_url(run.setup.spec, name, run.setup.start), path
        )
    except ValueError:
        raise probes.ProbeError("the probe could not be read") from None


async def _capture_baselines(run: _Replay, seq: int) -> None:
    for name, definition in run.baseline_definitions.items():
        if definition.capture_before_seq == seq:
            run.baseline_values[name] = await _read_probe(
                run, name, definition.json_path
            )


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


async def _looked[T](look: Callable[[], Awaitable[T]], budget: float) -> T:
    """`look`'s result, looked again while the page changes under the look,
    up to `budget` seconds; then the last change raises."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while True:
        try:
            return await look()
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
    run: _Replay,
    step: Targeted | Untargeted,
    interrupted: Callable[[], bool],
) -> StepResult | None:
    """Capture baselines before resolving, recording or dispatching `step`.
    None when the run was interrupted during capture or resolution, so no
    next action is dispatched."""
    try:
        await _capture_baselines(run, step.seq)
    except (
        probes.ProbeError,
        probes.ProbeUnstableError,
        EgressRefusedError,
        EgressUpstreamError,
    ) as error:
        return StepResult(step.seq, "failed", error=run.reason(error))
    setup = run.setup
    budget = setup.config.budgets.resolve_seconds
    action = step.model_dump(
        mode="json", exclude={"seq", "side_effect", "side_effect_basis", "satisfies"}
    )
    if isinstance(step, Navigate | Reload | Press):
        wait = budget if isinstance(step, Press) else NAVIGATION_SECONDS
        return (
            None
            if interrupted()
            else await _dispatched(
                session,
                run,
                _Dispatch(step.seq, action, step.side_effect, wait),
                lambda: _untargeted(session, step, setup.start),
            )
        )
    try:
        found = await _resolve(session, run.targets[step.target], "action", budget)
    except (Error, PolicyEventError) as error:
        # A look the session refused (a page off the allowed origins), or
        # Playwright's general error type (a crashed page): nothing was
        # dispatched, so there is no intent, and the run stops.
        return StepResult(step.seq, "failed", error=run.reason(error))
    if not isinstance(found, Resolved):
        return StepResult(step.seq, "drifted", misses=found.misses)
    if interrupted():
        return None
    return await _dispatched(
        session,
        run,
        _Dispatch(
            step.seq, action, step.side_effect, budget, step.target, found.locator_index
        ),
        lambda: _targeted(session, step, found.element, run.secrets),
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
    session: BrowserSession,
    step: Targeted,
    element: ElementHandle,
    secrets: Mapping[str, BoundSecret],
) -> Awaitable[Window]:
    """`step`'s action on `element`, its target's, through the session;
    a `fill_secret` with the secret of that name, bound for the run."""
    if isinstance(step, Click):
        return session.click(element)
    if isinstance(step, Fill):
        return session.fill(element, step.value)
    if isinstance(step, FillSecret):
        return session.fill_secret(element, secrets[step.secret])
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
    run: _Replay,
    dispatch: _Dispatch,
    act: Callable[[], Awaitable[Window]],
) -> StepResult:
    """Record `dispatch`'s intent in `run`'s record, then dispatch its step
    with `act`, giving it up to `MARGIN_SECONDS` past its wait, settle it,
    and record its completion with the locator that found its target."""
    record = run.setup.record
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
    except (Error, PolicyEventError, SecretRefusedError) as error:
        # Playwright's general error type (a failed navigation, an action
        # the page refused, a crashed page) or a document off the allowed
        # origins, raised between the intent and the completion: whether the
        # action took effect is unknown, so its intent stays unresolved and
        # the run stops. A fill_secret its binding refused filled nothing,
        # but the refusal comes from inside the session's lock, after the
        # intent; its intent stays unresolved too, as a dispatch's that
        # failed (DATA_MODEL §7), so a side-effect fill_secret refused makes
        # the run non-resumable.
        return StepResult(seq, "failed", error=run.reason(error))
    except TimeoutError:
        if not limit.expired():
            raise  # not the action's own limit
        return StepResult(
            seq,
            "failed",
            error=run.redactor.redact(f"the action didn't finish within {seconds:g} s"),
        )
    record.step_completed(seq, locator_used=index, settled=settled)
    return StepResult(seq, "completed", window, index, settled)


# What a step or an assertion's look can raise that becomes its reason.
type _Raised = (
    Error
    | PolicyEventError
    | DocumentChangedError
    | _UnansweredError
    | SecretRefusedError
    | probes.ProbeError
    | probes.ProbeUnstableError
    | EgressRefusedError
    | EgressUpstreamError
    | network.WindowsOverflowError
    | UnsupportedVisualFrameError
)


def _owned(error: _Raised) -> str | None:
    """The executor's or the session's own message for an error it owns (a
    policy event's names only an origin, a fill_secret refusal only the
    secret, origins and field), or its fixed diagnostic for a probe's; None
    for Playwright's, which the page may have chosen. `error_text` presents
    either, and withholds the rest of Playwright's message in a run whose
    script fills a test secret: a page handed a value can throw it back from
    any later call, encoded as it likes (ADR-0026's fill_secret
    amendment)."""
    for kind, reason in (
        (probes.ProbeError, "the probe could not be read"),
        (
            UnsupportedVisualFrameError,
            "visible_unoccluded does not support elements inside frames",
        ),
        (
            probes.ProbeUnstableError,
            "the probe value did not stabilize within its read bound",
        ),
        (EgressRefusedError, "the probe request was refused by the egress policy"),
        (EgressUpstreamError, "the probe request could not reach its allowed origin"),
    ):
        if isinstance(error, kind):
            return reason
    if isinstance(
        error,
        PolicyEventError
        | DocumentChangedError
        | _UnansweredError
        | SecretRefusedError
        | SecretNotFilledError
        | network.WindowsOverflowError,
    ):
        return str(error)
    return None


def _scanned(event: InfrastructureEvent, redactor: Redactor) -> InfrastructureEvent:
    """A copy of the gate's event with its host and cause scanned: a cause
    can name the host, which can be a bound value. The gate's own list keeps
    the raw event."""
    return replace(
        event, host=redactor.redact(event.host), cause=redactor.redact(event.cause)
    )


def _write_egress_record(
    record: RunRecord, blocks: EgressBlocks, *, withheld: bool
) -> None:
    """Write `egress.json`. When the record refuses it (a bound value its
    scan can't remove), the counts-only form, which holds a subset of it;
    when that is refused too, nothing: the result still names the block."""
    try:
        record.write("egress.json", _egress_record(blocks, withheld=withheld))
    except RefusedWriteError:
        with suppress(RefusedWriteError):
            record.write("egress.json", _egress_record(blocks, withheld=True))


def _egress_record(blocks: EgressBlocks, *, withheld: bool) -> dict[str, object]:
    if withheld:
        return {
            "error_code": "egress_blocked",
            "refused_count": len(blocks.refused),
            "overflowed": blocks.overflowed,
            "hosts_and_ports_withheld": True,
        }
    return {
        "error_code": "egress_blocked",
        "refused": [
            {"host": refused.host, "port": refused.port} for refused in blocks.refused
        ],
        "overflowed": blocks.overflowed,
        "hosts_and_ports_withheld": False,
    }
