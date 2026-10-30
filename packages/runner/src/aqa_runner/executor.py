"""The M1 strict executor (ADR-0024's Consequences and its #46 amendment): a
compiled script's steps, run in a fresh browser session with the script's
recorded settings, each step's intent on disk before its action and its
completion after (ARCHITECTURE §3.3). No model is involved: nothing here
imports or builds a model client.

Browser observations use the session methods, which check every document
they touch (ADR-0026). Probe reads use the same egress gate as the browser."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from aqa_core.compiled import (
    Assertion,
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    Navigate,
    Press,
    ProbeEquals,
    ProbeEqualsBaseline,
    Reload,
    Select,
    Target,
    VisibleUnoccluded,
)
from aqa_core.config import ProjectConfig
from aqa_core.heal_patch import heal_patch_problems
from aqa_core.project import (
    SpecError,
    contract_problems,
    parse_compiled,
    path_on_origin,
    start_url,
)
from aqa_core.spec import Spec, secret_references
from playwright.async_api import Browser, BrowserType, ElementHandle, Error

from aqa_runner import probes, reasons
from aqa_runner.bound_secrets import BoundSecret, bound_secrets
from aqa_runner.browser_session import (
    BrowserSession,
    one_key,
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
from aqa_runner.evidence import capture_evidence
from aqa_runner.invariants import InvariantResult, invariant_results
from aqa_runner.locators import Miss, Resolved
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.run_record import STEPS, RefusedWriteError, RunRecord, require_clean
from aqa_runner.secret_fields import SecretRefusedError
from aqa_runner.settling import Settled, Window

# How long `navigate` and `reload` may wait for the page to load, holding the
# session's lock: Playwright's default, made explicit. An action's wait is
# bounded by the run's `resolve_seconds` instead (ADR-0024's #46 amendment).
NAVIGATION_SECONDS = 30


# The steps the executor runs: those that act on a target, and the rest.
type Targeted = Click | Fill | FillSecret | Select
type Untargeted = Navigate | Reload | Press

# How a step ended: dispatched and settled; its target never resolved, so
# nothing was dispatched; or dispatching or settling raised, so its outcome
# is unknown.
type StepOutcome = Literal["completed", "drifted", "failed"]

# How the run ended (DATA_MODEL's `runs.status`).
type RunOutcome = Literal["passed", "failed", "errored"]
type ErrorCode = Literal["egress_blocked"]


@dataclass(frozen=True)
class RunSetup:
    """What a replay runs for: the spec and project config, the run's start
    origin (`aqa_core.project.start_origin`), the run's record, and the
    repair a drifted step goes to, if any (#242)."""

    spec: Spec
    config: ProjectConfig
    start: str
    record: RunRecord
    repair: Repair | None = None


@dataclass(frozen=True)
class _Replay:
    """State belonging to one replay: its setup, the secrets its script
    fills, and what evaluating its assertions needs."""

    setup: RunSetup
    secrets: Mapping[str, BoundSecret]
    evaluation: Evaluation


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
    raised."""

    seq: int
    outcome: StepOutcome
    window: Window | None = None
    locator_index: int | None = None
    settled: Settled | None = None
    misses: tuple[Miss, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class Drift:
    """The step a replay paused at, for its repair: its target didn't
    resolve, so nothing was dispatched; each locator's miss; a deep copy of
    the script in use, the committed one or an earlier repair's, so an edit
    to it reaches nothing replay holds; and the live session, which the
    repair observes through and dispatches nothing with."""

    step: Targeted
    misses: tuple[Miss, ...]
    script: CompiledScript
    session: BrowserSession


# What a replay hands a drifted step to: the whole script to go on with.
# Returning the script it was handed gives up.
type Repair = Callable[[Drift], Awaitable[CompiledScript]]


@dataclass(frozen=True)
class Repaired:
    """One drifted step's repair, as `repairs/<seq>.json` records it: the
    step's seq and misses; why replay refused the script offered, each
    problem scanned, none when it was taken; and the script the run went on
    with, as replay parsed it, None when refused."""

    seq: int
    misses: tuple[Miss, ...]
    problems: tuple[Redacted, ...]
    script: CompiledScript | None


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
    violated, else the last step's. `repairs` holds each drifted step's
    repair, in order: a run that passed through one keeps its outcome."""

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
    locators raises `SpecError` too (`_spelling_bound_values`). The run
    scans with the record's scan when that covers every secret it binds, as
    `aqa replay`'s covers every spec's, and with its own secrets' otherwise.
    When that scan holds a test secret, filled or not, every reason keeps
    nothing of Playwright's messages and names no origin of ours
    (`Evaluation.reason`), the invariants keep no text, and `egress.json`
    names no host or port (ADR-0026's #50 D and #53 P9+P10 amendments).

    After every step it keeps, it saves the step's evidence in the record,
    which scans what it writes with the session's redactor
    (`aqa_runner.evidence`)."""
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
    failure shows (`RunResult.evidence`), and each repair."""

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
    )
    record = setup.record
    steps: list[StepResult] = []
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
        pending, in_use = list(runnable), script
        repairs: list[Repaired] = []
        while pending and steps[-1].outcome == "completed" and not interrupted():
            step = pending[0]
            result = await _run(session, run, step, interrupted)
            if result is None:
                break
            # One repair per drifted step: a step that drifts again after its
            # repair stops the run, so a repair is never asked twice. None
            # once the run is interrupted, which stops it anyway.
            if (
                setup.repair is not None
                and result.outcome == "drifted"
                and not isinstance(step, Navigate | Reload | Press)
                and all(repaired.seq != step.seq for repaired in repairs)
                and not interrupted()
            ):
                handed = in_use.model_copy(deep=True)
                drift = Drift(step, result.misses, handed, session)
                offered = await setup.repair(drift)
                ran = [done.seq for done in steps[1:]]
                repaired = await _repaired(run, script, result, offered, ran)
                repairs.append(repaired)
                if (patched := repaired.script) is not None:
                    run = replace(
                        run, evaluation=replace(run.evaluation, targets=patched.targets)
                    )
                    pending, in_use = list(patched.steps[len(ran) :]), patched
                    continue
            pending.pop(0)
            kept = await _keep_with_evidence(session, run, steps, result)
            if not violated:
                evidence, violated = kept, _violated(session, setup.spec)
        if steps[-1].outcome != "completed" or interrupted():
            assertions = tuple(
                AssertionResult(check.id, "not_evaluated", stopped_at=steps[-1].seq)
                for check in checks
            )
        else:
            assertions = await evaluate_each(session, checks, run.evaluation)
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
        tuple(repairs),
    )


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
    )


async def _keep_with_evidence(
    session: BrowserSession, run: _Replay, steps: list[StepResult], step: StepResult
) -> Evidence:
    """Keep `step`, then save its evidence with the page's snapshot as the
    step left it, and give that snapshot as plain text."""
    steps.append(step)
    budget = run.setup.config.budgets.resolve_seconds
    snapshot = await _observed(session, budget)
    await capture_evidence(run.setup.record, session, step.seq, step.window, snapshot)
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


class BoundValueSpelledError(SpecError):
    """A bound value is spelled where the run would write or use it
    (`_spelling_bound_values`): a spec error found once the spec's test
    secrets are bound, with that binding, so whoever prints the refusal
    can scan it with them."""

    def __init__(
        self, problems: Sequence[str], bound: Mapping[str, BoundSecret]
    ) -> None:
        super().__init__(problems)
        self.bound = bound


def admitted(
    script: CompiledScript, spec: Spec, config: ProjectConfig, start: str
) -> tuple[
    list[Targeted | Untargeted], list[Assertion], Mapping[str, BoundSecret], Redactor
]:
    """`script`'s steps and assertions, and the test secrets a run of `spec`
    from the start origin `start` binds with their scan, once nothing stops
    `script` before anything is opened: it is compiled from the spec as it
    is, agrees with `config`'s subject contracts and runs in M1
    (`accepted`); every secret the spec references has a value
    (`bound_secrets`); and no bound value is spelled where the run would
    write or use it (`_spelling_bound_values`), which raises
    `BoundValueSpelledError`. Otherwise `SpecError`, or `bound_secrets`'
    own error. The script's own problems come first, so a spec error wins
    over a missing value (exit 5 before 12), and a stale script's other
    problems are moot. Public so `aqa replay` admits every spec before any
    run's reset (#239)."""
    if script.spec_hash != spec.spec_hash:
        raise SpecError(["compiled from another version of the spec: explore it again"])
    if problems := contract_problems(script, config):
        raise SpecError(problems)
    runnable, checks = accepted(script, spec)
    bound = bound_secrets(spec, start)
    redactor = Redactor(bound.values())
    if problems := _spelling_bound_values(spec, runnable, script.targets, redactor):
        raise BoundValueSpelledError(problems, bound)
    return runnable, checks, bound, redactor


def _spelling_bound_values(
    spec: Spec,
    steps: list[Targeted | Untargeted],
    targets: Mapping[str, Target],
    redactor: Redactor,
) -> list[str]:
    """Each place, among what replay writes to the steps journal or hands
    the browser to find a target with, holding a spelling of a value
    `redactor` binds, as written or JSON-escaped, as the journal writes it:
    the start path, each step's action as its intent records it, and each
    target's name, every string of its locators, scopes included, and its
    subject contract's region and part (`Redactor.spells`). A place is named
    by its position, never by its text (SECURITY §5: keeping values out of
    `steps.jsonl` is the caller's preflight)."""
    places: list[tuple[str, object]] = [
        ("preconditions.start_url", spec.frontmatter.preconditions.start_url)
    ]
    places.extend(
        (f"steps[{index}] (seq {step.seq})", _action(step))
        for index, step in enumerate(steps)
    )
    places.extend(
        (
            f"targets[{index}]",
            [name, target.model_dump(mode="json", exclude={"semantic"})],
        )
        for index, (name, target) in enumerate(targets.items())
    )
    return [
        f"{place}: a bound value's spelling is in it, so the replay doesn't start"
        for place, value in places
        if redactor.spells(value)
    ]


def _action(step: Targeted | Untargeted) -> dict[str, object]:
    """`step` as its intent records it: as compiled, less its seq, which the
    line holds, its side-effect fields and its links to the coverage plan."""
    return step.model_dump(
        mode="json", exclude={"seq", "side_effect", "side_effect_basis", "satisfies"}
    )


def accepted(
    script: CompiledScript, spec: Spec
) -> tuple[list[Targeted | Untargeted], list[Assertion]]:
    """`script`'s steps and assertions, or `SpecError` naming each step and
    check M1 can't run, before anything is opened (ADR-0024's #46
    amendment), and each `fill_secret` naming a secret `spec` doesn't
    reference, which has no binding in the run. Probe assertions and baseline
    definitions must name probes declared by the spec. Public because a
    script replay would refuse is never up to date (`aqa explore`, #216)."""
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
    setup = run.setup
    budget = setup.config.budgets.resolve_seconds
    action = _action(step)
    if isinstance(step, Navigate | Reload | Press):
        wait = budget if isinstance(step, Press) else NAVIGATION_SECONDS
        return (
            None
            if interrupted()
            else await dispatched(
                session,
                setup.record,
                Dispatch(step.seq, action, step.side_effect, wait),
                lambda: _untargeted(session, step, setup.start),
            )
        )
    try:
        found = await look_for(session, run.evaluation, step.target, "action")
    except (Error, PolicyEventError) as error:
        # A look the session refused (a page off the allowed origins), or
        # Playwright's general error type (a crashed page): nothing was
        # dispatched, so there is no intent, and the run stops.
        return StepResult(step.seq, "failed", error=run.evaluation.reason(error))
    if not isinstance(found, Resolved):
        return StepResult(step.seq, "drifted", misses=found.misses)
    if interrupted():
        return None
    return await dispatched(
        session,
        setup.record,
        Dispatch(
            step.seq, action, step.side_effect, budget, step.target, found.locator_index
        ),
        lambda: _targeted(session, step, found.element, run.secrets),
    )


async def _repaired(
    run: _Replay,
    committed: CompiledScript,
    drifted: StepResult,
    offered: CompiledScript,
    ran: list[int],
) -> Repaired:
    """The repair of the step `drifted`, whose repair offered `offered`,
    recorded in `repairs/<seq>.json`: taken, as the loader reads its JSON,
    once nothing refuses it. The first of these to find problems refuses
    it, and each problem is scanned: the loader's rules, which a
    `model_copy` skips; the heal-patch validator, against `committed`, the
    script replay was given, never a copy it handed out; its first steps
    must be those that ran, by seq in `ran`, since the run goes on after
    them; and replay's own admission (`accepted`). The validator searches
    text checks' patterns in a child process, so it runs off the loop,
    which keeps the proxy serving."""
    seq, record = drifted.seq, run.setup.record
    name = f"repairs/{seq}.json"
    problems: Sequence[str]
    try:
        patched = parse_compiled(
            offered.model_dump_json(), run.setup.config, source=Path(name)
        )
        problems = await asyncio.to_thread(heal_patch_problems, committed, patched)
        if not problems and [step.seq for step in patched.steps[: len(ran)]] != ran:
            problems = [
                "/steps: a repair inserts no step before a step that already ran"
            ]
        if not problems:
            accepted(patched, run.setup.spec)
            taken = Repaired(seq, drifted.misses, (), patched)
            record.write(name, _repair_record(taken))
            return taken
    except SpecError as error:
        problems = error.problems
    scanned = tuple(record.redactor.redact(problem) for problem in problems)
    refused = Repaired(seq, drifted.misses, scanned, None)
    record.write(name, _repair_record(refused))
    return refused


def _repair_record(repaired: Repaired) -> dict[str, object]:
    script = repaired.script
    return {
        "seq": repaired.seq,
        "misses": list(repaired.misses),
        "problems": list(repaired.problems),
        "script": None
        if script is None
        else script.model_dump(mode="json", exclude_none=True),
    }


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
