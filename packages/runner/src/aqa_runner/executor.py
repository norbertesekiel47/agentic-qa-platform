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
from typing import Literal

from aqa_core.compiled import (
    Assertion,
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    Navigate,
    NotVisible,
    Press,
    Reload,
    TextInTarget,
    VisibleUnoccluded,
)
from aqa_core.config import ProjectConfig
from aqa_core.project import path_on_origin, start_url
from aqa_core.spec import Spec
from playwright.async_api import Browser, BrowserType, ElementHandle, Error

from aqa_runner import probes, reasons
from aqa_runner.admission import Targeted, Untargeted, admitted, intent_action
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import (
    BrowserSession,
    open_browser_session,
    prepared_session,
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
from aqa_runner.evaluation import (
    MARGIN_SECONDS,
    AssertionResult,
    Evaluation,
    bounded,
    capture_baselines,
    evaluate_each,
    look_for,
    looked,
)
from aqa_runner.evidence import Shot, capture_evidence
from aqa_runner.invariants import InvariantResult, invariant_results
from aqa_runner.locators import Miss, Resolved
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.repairs import (
    AssertionAt,
    Handing,
    Repair,
    Repaired,
    StepAt,
    inserted_failure,
    rebind_refusal,
)
from aqa_runner.run_record import STEPS, RefusedWriteError, RunRecord, require_clean
from aqa_runner.secret_fields import SecretRefusedError
from aqa_runner.settling import Settled, Window

# How long `navigate` and `reload` may wait for the page to load, holding the
# session's lock: Playwright's default, made explicit. An action's wait is
# bounded by the run's `resolve_seconds` instead (ADR-0024's #46 amendment).
NAVIGATION_SECONDS = 30

# How a step ended: dispatched and settled; its target never resolved, so
# nothing was dispatched; or dispatching or settling raised, so its outcome
# is unknown, or a step a repair inserted settled without showing it changed
# nothing.
type StepOutcome = Literal["completed", "drifted", "failed"]

# How the run ended (DATA_MODEL's `runs.status`).
type RunOutcome = Literal["passed", "failed", "errored"]
type ErrorCode = Literal["egress_blocked"]


@dataclass(frozen=True)
class RunSetup:
    """What a replay runs for: the spec and project config, the run's start
    origin (`aqa_core.project.start_origin`), the run's record, the repair a
    drift goes to, if any (#242, #244), and whether a step that drifts or
    fails saves a screenshot in its evidence, which only `strict_replay`
    asks for (#251)."""

    spec: Spec
    config: ProjectConfig
    start: str
    record: RunRecord
    repair: Repair | None = None
    screenshots: bool = False


@dataclass(frozen=True)
class _Replay:
    """State belonging to one replay: its setup, the secrets its script
    fills, what evaluating its assertions needs, and its own copy of the
    script it was given, which every check of a repair reads and nothing
    handed to a repair holds: the session holds `script.browser`."""

    setup: RunSetup
    secrets: Mapping[str, BoundSecret]
    evaluation: Evaluation
    committed: CompiledScript


@dataclass(frozen=True)
class Dispatch:
    """A step about to be dispatched: what its intent line records, how long
    its action may take, and the locator that found its target."""

    seq: int
    action: Mapping[str, object]
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
    raised, or, for a step a repair inserted that settled without showing it
    changed nothing, why, beside the window, index and settling it keeps."""

    seq: int
    outcome: StepOutcome
    window: Window | None = None
    locator_index: int | None = None
    settled: Settled | None = None
    misses: tuple[Miss, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class Evidence:
    """One step's evidence snapshot as a replay returns it: the step's seq,
    and the page's snapshot as its evidence keeps it, scanned, as plain
    text, None when none was kept."""

    seq: int
    snapshot: str | None


@dataclass(frozen=True)
class RunResult:
    """A replay: every step that ran and every assertion, each with its
    outcome, and the run's; the egress gate's infrastructure events, and the
    session's policy events (the first 100) and how many it recorded in
    all. Invariants are judged for every run, errored ones included; in a
    run that binds a test secret they keep no text. Egress blocks keep hosts
    and ports in memory: what #53 prints or saves of them is its
    preflight's. `evidence` is the page a failure shows explore's next
    attempt (#56): the step's where an enabled invariant first became
    violated, else the last step's. `repairs` holds each drift's repair, in
    order: a run that passed through one keeps its outcome. `shots` holds, by
    seq, what became of each screenshot a step's evidence asked for
    (`RunSetup.screenshots`)."""

    run_id: str
    outcome: RunOutcome
    steps: tuple[StepResult, ...]
    assertions: tuple[AssertionResult, ...]
    infrastructure_events: tuple[InfrastructureEvent, ...]
    policy_events: tuple[PolicyEvent, ...]
    invariants: tuple[InvariantResult, ...]
    egress_blocks: EgressBlocks
    error_code: ErrorCode | None
    policy_events_total: int
    evidence: Evidence | None = None
    repairs: tuple[Repaired, ...] = ()
    shots: Mapping[int, Shot] = field(default_factory=dict)


async def replay(
    script: CompiledScript,
    setup: RunSetup,
    *,
    chromium: BrowserType | Browser,
    proxy: EgressProxy,
    gate: EgressGate,
    every_locator: bool = False,
) -> RunResult:
    """Run `script` once for `setup`, through `proxy`, the run's egress
    proxy, whose gate is `gate`, recording each step in the setup's record.
    With `every_locator`, every target resolves from all its locators at
    each use (`aqa_runner.locators.resolve_every`), as a confirmation does.
    The session uses `script.browser`, never the project's or the spec's
    settings (ADR-0025), in a fresh browser launched from `chromium`, or in
    `chromium` itself: a browser its caller launched through
    `launched_browser` and closes after this returns or raises. It needs
    the proxy's phase open, as the browser's launch does. With a browser
    of its own it ends the phase however it leaves, once that browser has
    closed: a refusal before the session, a failed launch, a journal that
    takes no more lines and a cancellation included, so the proxy keeps the
    phase's traffic (`EgressProxy.ended_traffic`). With a caller's browser
    it ends the phase when it returns, once the session's context has
    closed, and when it raises leaves that end to its caller, since the
    context is still live until that browser closes: ending the phase frees
    the listener's port (#204). Only once the phase has ended does it read
    the run's egress blocks and infrastructure events.

    Before accepting steps or binding secrets, the script must be compiled
    from the spec as it is (its `spec_hash`) and agree with the project's
    subject contracts. A stale script or a mismatch raises `SpecError`.

    Before its session is prepared, so after a caller's launch but before
    its own, it binds the test secrets the spec references
    (`aqa_runner.bound_secrets.bound_secrets`): a missing value raises
    `MissingSecretError`, Playwright's protocol logging or a bound value
    elsewhere in the environment `SecretLoggedError`, and a binding the run
    doesn't allow `SpecError`. A bound value's
    spelling in the start path, a step's action or a target's name or
    locators raises `SpecError` too (`admission.admitted`). The run
    scans with the record's scan when that covers every secret it binds, as
    `aqa replay`'s covers every spec's, and with its own secrets' otherwise.
    When that scan holds a test secret, filled or not, every reason keeps
    nothing of Playwright's messages and names no origin of ours
    (`Evaluation.reason`), the invariants keep no text, and `egress.json`
    names no host or port (ADR-0026's #50 D and #53 P9+P10 amendments).

    After every step it keeps, it saves the step's evidence in the record,
    which scans what it writes with the session's redactor
    (`aqa_runner.evidence`). With `setup.screenshots`, a kept step that
    drifted or failed saves its masked screenshot there too, and no other
    step does; a capture that is withheld changes no outcome.

    With `setup.repair`, a step whose target drifted, in a run nothing has
    interrupted, goes to that repair (`repairs.Drift`), once. The script it
    offers is checked and recorded before the run goes on with it
    (`repairs.Handing`), the drifted step first, which keeps no evidence of
    its drift; a refused one, or the script it was handed, ends the run as
    drift. Once every step has completed, a positive check whose target
    drifted goes to it too (`_evaluated`). What the repair raises leaves as
    any failure here does. A step through a target a repair rebound passes a
    gate before its intent (`repairs.rebind_refusal`). A step a repair
    inserted, once it completed, must show from its own settle window that
    it changed nothing, or it fails, its completion on disk, and the run
    stops there (`repairs.inserted_failure`)."""
    try:
        replayed = await _replayed(
            script, setup, gate, every_locator, chromium=chromium, proxy=proxy
        )
    except BaseException:
        if isinstance(chromium, Browser):
            raise  # the session's context is live until the caller's close
        # The failure to report: a phase the caller left unopened, or that
        # another task is ending, never replaces it.
        with suppress(RuntimeError):
            await proxy.end_phase()
        raise
    # Ending the phase joins every connection the browser opened, so no
    # proxy task can record a block or an event after the verdict.
    await proxy.end_phase()
    return _run_result(replayed, proxy, gate)


@dataclass(frozen=True)
class _Replayed:
    """What a replay's browser left: its record and redactor, the hosts
    the project config expects blocked, and every step, assertion,
    invariant and kept policy event, how many it recorded, the evidence a
    failure shows (`RunResult.evidence`), each repair, and each step's
    screenshot (`RunResult.shots`)."""

    record: RunRecord
    redactor: Redactor
    expected: tuple[str, ...]
    steps: tuple[StepResult, ...]
    assertions: tuple[AssertionResult, ...]
    invariants: tuple[InvariantResult, ...]
    policy_events: tuple[PolicyEvent, ...]
    policy_events_total: int
    evidence: Evidence
    repairs: tuple[Repaired, ...]
    shots: Mapping[int, Shot]


async def _replayed(
    script: CompiledScript,
    setup: RunSetup,
    gate: EgressGate,
    every_locator: bool,
    *,
    chromium: BrowserType | Browser,
    proxy: EgressProxy,
) -> _Replayed:
    """Admit `script` for `setup`, then run it in a browser session whose
    context has closed when this returns, and whose browser has when it
    raises, unless its caller launched it."""
    runnable, checks, bound, redactor = admitted(
        script, setup.spec, setup.config, setup.start
    )
    if all(map(setup.record.redactor.covers, bound.values())):
        redactor = setup.record.redactor
    else:
        setup = replace(setup, record=setup.record.redacting(redactor))
    run = _Replay(
        setup,
        {
            step.secret: bound[step.secret]
            for step in runnable
            if isinstance(step, FillSecret)
        },
        Evaluation(
            script.targets,
            setup.spec,
            setup.start,
            gate,
            setup.config.budgets.resolve_seconds,
            redactor,
            script.probe_baselines,
            every_locator=every_locator,
        ),
        script.model_copy(deep=True),
    )
    record = setup.record
    steps: list[StepResult] = []
    shots: dict[int, Shot] = {}
    async with (
        prepared_session(
            chromium, egress=proxy, settings=script.browser, redactor=redactor
        )
        if isinstance(chromium, Browser)
        else open_browser_session(
            chromium, egress=proxy, settings=script.browser, redactor=redactor
        )
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
        evidence = await _keep_with_evidence(
            session,
            run,
            steps,
            shots,
            await dispatched(
                session,
                setup.record,
                Dispatch(
                    0, {"action": "navigate", "url": path}, False, NAVIGATION_SECONDS
                ),
                lambda: session.navigate(first),
            ),
        )
        # A violated invariant's evidence is the page that first showed it,
        # whatever the path did after; one the final checks first show
        # keeps the last step's, the page they ran on.
        violated = _violated(session, setup.spec)
        handing = (
            None
            if setup.repair is None
            else Handing(
                setup.repair,
                run.committed,
                record,
                setup.config,
                setup.spec,
                session,
                interrupted,
            )
        )
        pending, in_use = list(runnable), script
        while pending and steps[-1].outcome == "completed" and not interrupted():
            step = pending[0]
            result = _shown(run, await _run(session, run, step, interrupted), redactor)
            if result is None:
                break
            # One repair per drifted step: a step that drifts again after its
            # repair stops the run, so a repair is never asked twice. None
            # once the run is interrupted, which stops it anyway.
            if (
                handing is not None
                and result.outcome == "drifted"
                and not isinstance(step, Navigate | Reload | Press)
                and not handing.handed(at := StepAt(step))
                and not interrupted()
            ):
                ran = in_use.steps[: len(steps) - 1]
                repaired = await handing.hand(at, result.misses, in_use, ran)
                if (patched := repaired.script) is not None:
                    run = replace(
                        run, evaluation=replace(run.evaluation, targets=patched.targets)
                    )
                    pending, in_use = list(patched.steps[len(ran) :]), patched
                    continue
            pending.pop(0)
            kept = await _keep_with_evidence(session, run, steps, shots, result)
            if not violated:
                evidence, violated = kept, _violated(session, setup.spec)
        if steps[-1].outcome != "completed" or interrupted():
            assertions = tuple(
                AssertionResult(check.id, "not_evaluated", stopped_at=steps[-1].seq)
                for check in checks
            )
        else:
            assertions = await _evaluated(
                session, run, checks, handing, in_use, interrupted=interrupted
            )
        invariants = invariant_results(
            session.invariant_observers.seen, setup.spec.frontmatter.invariants
        )
        if run.evaluation.withheld:
            invariants = tuple(replace(result, seen=()) for result in invariants)
        events = session.policy_events
    return _Replayed(
        record,
        redactor,
        expected,
        tuple(steps),
        assertions,
        invariants,
        tuple(events.kept),
        events.total,
        evidence,
        () if handing is None else tuple(handing.repaired),
        shots,
    )


async def _evaluated(
    session: BrowserSession,
    run: _Replay,
    checks: list[Assertion],
    handing: Handing | None,
    in_use: CompiledScript,
    *,
    interrupted: Callable[[], bool],
) -> tuple[AssertionResult, ...]:
    """Every assertion's result, each evaluated once (`evaluate_each`), then,
    with a repair, each drift it takes (ADR-0024's #244 amendment). While
    nothing interrupted the run, every result is `pass` or
    `binding_unresolved`, since anything else keeps the run from passing
    whatever a repair does, and no invariant the spec keeps enabled is
    violated, the first positive check that drifted goes to the repair, once.
    Taken, only the checks on the targets whose locators it changed are
    evaluated again, since time can flip the others' results. Refused or
    given up, nothing more is handed, and that check stays drifted."""
    evaluation = run.evaluation
    results = list(await evaluate_each(session, checks, evaluation))
    if handing is None:
        return tuple(results)
    while (
        not interrupted()
        and all(result.outcome in ("pass", "binding_unresolved") for result in results)
        and not _violated(session, run.setup.spec)
        and (next_drift := _next_drift(handing, checks, results)) is not None
    ):
        repaired = await handing.hand(*next_drift, in_use, in_use.steps)
        if (patched := repaired.script) is None:
            break
        changed = {
            name
            for name, target in patched.targets.items()
            if target.locators != in_use.targets[name].locators
        }
        in_use, evaluation = patched, replace(evaluation, targets=patched.targets)
        again = [
            index
            for index, check in enumerate(checks)
            if isinstance(check, TextInTarget | NotVisible | VisibleUnoccluded)
            and check.target in changed
        ]
        fresh = await evaluate_each(
            session, [checks[index] for index in again], evaluation
        )
        for index, result in zip(again, fresh, strict=True):
            results[index] = result
    return tuple(results)


def _next_drift(
    handing: Handing, checks: list[Assertion], results: list[AssertionResult]
) -> tuple[AssertionAt, tuple[Miss, ...]] | None:
    """The first positive check whose target drifted and hasn't had its one
    repair, with its misses. A `not_visible` check's drift goes to none: its
    element may rightly be gone."""
    for index, (check, result) in enumerate(zip(checks, results, strict=True)):
        if result.outcome == "binding_unresolved" and isinstance(
            check, TextInTarget | VisibleUnoccluded
        ):
            at = AssertionAt(index, check)
            if not handing.handed(at):
                return at, result.misses
    return None


def _violated(session: BrowserSession, spec: Spec) -> bool:
    """Whether an invariant `spec` keeps enabled is violated yet."""
    results = invariant_results(
        session.invariant_observers.seen, spec.frontmatter.invariants
    )
    return any(result.outcome == "violated" for result in results)


def _run_result(replayed: _Replayed, proxy: EgressProxy, gate: EgressGate) -> RunResult:
    """The replay's result from what its browser left and, its phase
    ended, the run's egress blocks and infrastructure events."""
    blocks = proxy.egress_blocks(replayed.expected)
    errored = (
        blocks.blocked
        or bool(gate.infrastructure_events)
        or any(step.outcome == "failed" for step in replayed.steps)
        or any(assertion.error is not None for assertion in replayed.assertions)
    )
    passed = (
        all(step.outcome == "completed" for step in replayed.steps)
        and all(assertion.outcome == "pass" for assertion in replayed.assertions)
        and all(invariant.outcome != "violated" for invariant in replayed.invariants)
    )
    # A saved file, so its own predicate: whatever a caller may one day let
    # the returned result keep, a bound run's record names no host or port.
    secret_bound = not replayed.redactor.empty
    if blocks.blocked:
        _write_egress_record(replayed.record, blocks, withheld=secret_bound)
    return RunResult(
        replayed.record.run_id,
        "errored" if errored else "passed" if passed else "failed",
        replayed.steps,
        replayed.assertions,
        tuple(
            _scanned(event, replayed.redactor) for event in gate.infrastructure_events
        ),
        replayed.policy_events,
        replayed.invariants,
        blocks,
        "egress_blocked" if blocks.blocked else None,
        replayed.policy_events_total,
        replayed.evidence,
        replayed.repairs,
        replayed.shots,
    )


async def _keep_with_evidence(
    session: BrowserSession,
    run: _Replay,
    steps: list[StepResult],
    shots: dict[int, Shot],
    step: StepResult,
) -> Evidence:
    """Keep `step`, then save its evidence with the page's snapshot as the
    step left it, and, when the run asks for them and it drifted or failed,
    its screenshot, noted in `shots`; give that snapshot as plain text."""
    steps.append(step)
    budget = run.setup.config.budgets.resolve_seconds
    snapshot = await _observed(session, budget)
    shot = await capture_evidence(
        run.setup.record,
        session,
        f"evidence/{step.seq}",
        step.window,
        snapshot,
        screenshot=run.setup.screenshots and step.outcome != "completed",
    )
    if shot is not None:
        shots[step.seq] = shot
    return Evidence(step.seq, None if snapshot is None else str(snapshot))


async def _observed(session: BrowserSession, budget: float) -> Redacted | None:
    """The page's snapshot for evidence, looked at as `looked` looks, or
    None for a page off the allowed origins, stopped, crashed or changing
    throughout. Its refusal records no policy event, so evidence leaves
    the result as it was."""
    try:
        return await bounded(
            looked(lambda: session.snapshot(record_refusal=False), budget), budget
        )
    except Error, PolicyEventError, DocumentChangedError, UnansweredError:
        return None


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
        await capture_baselines(run.evaluation, step.seq)
    except (
        probes.ProbeError,
        probes.ProbeUnstableError,
        EgressRefusedError,
        EgressUpstreamError,
    ) as error:
        return StepResult(step.seq, "failed", error=run.evaluation.reason(error))
    if isinstance(step, Navigate | Reload | Press):
        wait = (
            run.setup.config.budgets.resolve_seconds
            if isinstance(step, Press)
            else NAVIGATION_SECONDS
        )
        return (
            None
            if interrupted()
            else await dispatched(
                session,
                run.setup.record,
                Dispatch(step.seq, intent_action(step), step.side_effect, wait),
                lambda: _untargeted(session, step, run.setup.start),
            )
        )
    return await _run_targeted(session, run, step, interrupted)


def _shown(
    run: _Replay, result: StepResult | None, redactor: Redactor
) -> StepResult | None:
    """`result`, unless it is a step a repair inserted that completed and whose
    own settle window doesn't show it changed nothing: then it failed, with why
    (`repairs.inserted_failure`), and keeps its window, locator and settling.
    One that kept no window or settling fails too, so nothing passes unread.
    Judged once its completion is on disk and before it is kept, so before its
    evidence and anything of the next step. A step is inserted when the
    committed script has no step of its seq."""
    if (
        result is None
        or result.outcome != "completed"
        or any(step.seq == result.seq for step in run.committed.steps)
    ):
        return result
    failure = inserted_failure(result.window, result.settled, redactor)
    if failure is None:
        return result
    return replace(result, outcome="failed", error=failure)


async def _run_targeted(
    session: BrowserSession,
    run: _Replay,
    step: Targeted,
    interrupted: Callable[[], bool],
) -> StepResult | None:
    """Find `step`'s target, gate it when a repair rebound it
    (`repairs.rebind_refusal`), then record and dispatch `step`."""
    budget = run.setup.config.budgets.resolve_seconds
    try:
        found = await look_for(session, run.evaluation, step.target, "action")
    except (Error, PolicyEventError) as error:
        # A look the session refused (a page off the allowed origins), or
        # Playwright's general error type (a crashed page): nothing was
        # dispatched, so there is no intent, and the run stops.
        return StepResult(step.seq, "failed", error=run.evaluation.reason(error))
    if not isinstance(found, Resolved):
        return StepResult(step.seq, "drifted", misses=found.misses)
    committed = run.committed.targets[step.target]
    if run.evaluation.targets[step.target] != committed:
        refused = await rebind_refusal(
            session,
            step,
            found.element,
            committed,
            secret=run.secrets[step.secret] if isinstance(step, FillSecret) else None,
            budget=budget,
        )
        if refused is not None:
            return StepResult(step.seq, "failed", error=refused)
    if interrupted():
        return None
    return await dispatched(
        session,
        run.setup.record,
        Dispatch(
            step.seq,
            intent_action(step),
            step.side_effect,
            budget,
            step.target,
            found.locator_index,
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


async def dispatched(
    session: BrowserSession,
    record: RunRecord,
    dispatch: Dispatch,
    act: Callable[[], Awaitable[Window]],
) -> StepResult:
    """Record `dispatch`'s intent in `record`, then dispatch its step with
    `act`, giving it up to `MARGIN_SECONDS` past its wait, settle it, and
    record its completion with the locator that found its target. Replay's
    steps and the navigator's actions (`aqa_runner.navigator_tools`) go
    through here, each with inputs its caller admitted first: replay's
    `admitted`, the tools' argument scan.

    `record` must scan with `session`'s redactor, the run's, or this raises
    `ValueError` before any line: its reasons withhold by that scan. Each
    line is checked as the exact text written, its own fields and time
    included: one that spells a bound value isn't written (the record's
    `RefusedWriteError`), and the step fails with that refusal. A refused
    intent dispatches nothing; after a refused completion, the intent stays
    unresolved, as a dispatch's that failed (DATA_MODEL §7). A line the
    record can't write raises, as does a cancellation: either can leave an
    intent on disk unresolved."""
    redactor = session.redactor
    if record.redactor is not redactor:
        raise ValueError(
            "a dispatch's record must be scanned by its session's redactor"
        )
    seq, index = dispatch.seq, dispatch.locator_index
    clean = require_clean(redactor, STEPS)
    try:
        record.step_intent(
            seq,
            dispatch.action,
            side_effect=dispatch.side_effect,
            target_used=dispatch.target,
            check=clean,
        )
    except RefusedWriteError as error:
        return StepResult(seq, "failed", error=reasons.reason(error, redactor))
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
        return StepResult(seq, "failed", error=reasons.reason(error, redactor))
    except TimeoutError:
        if not limit.expired():
            raise  # not the action's own limit
        return StepResult(
            seq,
            "failed",
            error=redactor.redact(f"the action didn't finish within {seconds:g} s"),
        )
    try:
        record.step_completed(seq, locator_used=index, settled=settled, check=clean)
    except RefusedWriteError as error:
        return StepResult(seq, "failed", error=reasons.reason(error, redactor))
    return StepResult(seq, "completed", window, index, settled)


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
    """Write `egress.json`, or nothing when the record refuses it (a bound
    value its scan can't remove): the result still names the block."""
    with suppress(RefusedWriteError):
        record.write("egress.json", _egress_record(blocks, withheld=withheld))


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
