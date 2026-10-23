"""Exploring a spec (ADR-0024's #53 P15b, P15c and P16 amendments):
preflight, the plan, the attempt's phase, the confirmation's, publication,
and how the run ended.

`run_attempt` takes a `Run` that preflight made, with its record and that
record's `attempt-1` part, and the frozen plan it explores by. In order,
each stop with its exit code (API.md §7):

1. Open the egress proxy. Within the minutes budget: send the spec's reset
   hook (14), register the selector engines, open the browser (10, and 10
   for an engine left unregistered), load the start URL as step 0, and,
   unless something stopped or interrupted the run, take the navigator's
   turns (`explore_turns`). Between the reset and the engines, each probe a
   `probe_equals` check reads is read once for its options (3 for none).
2. Close the browser and end the phase (14 for uncertain traffic), then
   infer the flags from its evidence (ADR-0025's #54 amendment). When the
   attempt compiled a path and nothing stopped or interrupted it, drop
   every locator that spells a bound value (3 for a target left with
   none). A path with a side-effect step, or a start URL whose load may
   change the app, whose spec has no reset hook is published unconfirmed,
   never replayed (AGENTS.md §6). Any other is
   confirmed within the minutes budget: the reset hook, when the spec has
   one (14), then, in the record's `confirmation` part (15 when it can't be
   made), in a phase of its own, the path replayed with every locator in a
   fresh browser and no model (`executor.replay`), which ends that phase
   when it returns; otherwise the phase ends once the browser has closed
   (10, 15, 3, 14). Once it passed, its runs can raise a flag, and one
   raised with no reset before them is the run's warning.
   A failed confirmation starts the next attempt in its own part (#56,
   `_again`, 15 when it can't be made), after its reset, while attempts
   are left and the spec has a hook; without one the run stops (3). Its
   failure, with the page it left, leads each request of the next. A
   pass of a script whose confirmation failed before is flaky, and
   publishes nothing (3).
3. Read the egress blocks and the infrastructure events once, after the
   last phase's end. With the proxy closed, publish the path when nothing
   stopped the run, confirmed when its confirmation passed, under the
   spec's lock (`publication.publish`; 3, or 15 for a file that failed).
4. Write `explore.json`, and `egress.json` when blocked, from
   `explore_result.present`; its outcome decides. A record that doesn't
   scan with the run's redactor is refused there, whether or not a browser
   opened."""

from asyncio import get_running_loop, timeout_at
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from aqa_core.compiled import CompiledScript
from aqa_core.compiler import CompileError, contracts_by_meaning, unsupported_features
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan, plan_hash, planned_checks
from aqa_core.project import (
    SpecError,
    effective_browser,
    probe_url,
    start_url,
    subject_contracts,
)
from aqa_core.schema import Contract
from aqa_core.spec import Spec
from playwright.async_api import Playwright

from aqa_runner import egress, executor, navigator, probes, reasons
from aqa_runner.anthropic_client import ProviderError
from aqa_runner.binding import UnregisteredEngineError
from aqa_runner.bound_secrets import BoundSecret, bound_secrets
from aqa_runner.browser_session import (
    BrowserSession,
    launched_browser,
    prepared_session,
)
from aqa_runner.coverage_plan import Planned, judge, make_plan
from aqa_runner.egress import EgressGate, egress_policy
from aqa_runner.egress_proxy import EgressProxy, PhaseTraffic
from aqa_runner.evaluation import AssertionResult
from aqa_runner.explore_admission import UNSCANNABLE, admit
from aqa_runner.explore_attempts import FLAKY, UNRESET, Attempts
from aqa_runner.explore_result import (
    EXIT_CODES,
    Confirmation,
    ExploreEnd,
    Outcome,
    Phase,
    Published,
    Stop,
    Stoppage,
    final_outcome,
    present,
)
from aqa_runner.explore_turns import (
    UNRECORDED,
    UNSENT,
    UNWRITTEN,
    Spending,
    Turning,
    UnsentRequestError,
    take_turns,
    unspelled,
)
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.model_router import ModelCallError, ModelRouter
from aqa_runner.navigator import navigator_prefix
from aqa_runner.navigator_tools import Attempt
from aqa_runner.publication import (
    Publication,
    PublishError,
    PublishWriteError,
    publication,
    publish,
)
from aqa_runner.redaction import Redactor
from aqa_runner.reset import ResetFailedError, reset
from aqa_runner.run_record import (
    RefusedWriteError,
    RunRecord,
    UnrecordedCostError,
    phase_parts,
    require_clean,
)
from aqa_runner.sandbox import SandboxUnavailableError

# How a run ends: as `explore_result.final_outcome` says once the egress
# proxy opened, or a spec error before it (#53 P15c).
type Ended = Outcome | Literal["spec_error"]
# Why the run stopped, in fixed words.
_UNMADE: Final = "the run record could not be created; nothing was planned"
_UNREAD: Final = "the compiled script could not be read; nothing was planned"
_MINUTES: Final = "the minutes budget ran out"
_UNSHOWN: Final = "the document still spelled a bound value, so it isn't shown"
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
    """How the run ended: its outcome and exit code; its last document,
    `plan.json` or `explore.json`, as written or as the record couldn't take it, whose
    `outcome` and `reasons` say why; its record, None when none could be
    made; what its model calls cost; the script its attempt compiled, its
    flags inferred (unconfirmed, every locator kept), when it compiled
    one, which `explore` returns only when it spells no bound value; and
    whether the run's `egress.json` was written."""

    outcome: Ended
    exit_code: int
    document: Mapping[str, object]
    record: Path | None
    cost: Decimal
    script: CompiledScript | None = None
    egress_saved: bool = False


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
    the authority every scan of the run answers to; the record and the part
    of the attempt it is on, `attempt-1` first, made from it; the secrets
    the attempt may fill; the router; the Playwright whose Chromium it
    browses with; the minutes budget's deadline, on the event loop's clock;
    the egress gate; and where it publishes, as it found the compiled file
    at its start. Its spend is recorded in its record, and it keeps each
    stop it made, the attempt whose `finish` compiled, that script once its
    flags are inferred (ADR-0025's #54 amendment), and its attempts so far
    (#56)."""

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
    publication: Publication
    spending: Spending = field(init=False)
    stops: list[Stop] = field(init=False, default_factory=list)
    script: CompiledScript | None = field(init=False, default=None)
    attempted: Attempt | None = field(init=False, default=None)
    attempts: Attempts = field(init=False)

    def __post_init__(self) -> None:
        budget = Decimal(str(self.config.budgets.model_usd))
        self.spending = Spending(self.record, budget)
        hook = self.spec.frontmatter.preconditions.reset
        self.attempts = Attempts(self.config.budgets.attempts, hook is not None)

    def stopped(self, outcome: Stoppage, why: str) -> None:
        """Keep a stop, its text scanned with the run's redactor."""
        self.stops.append(Stop(outcome, self.redactor.redact(why)))

    @property
    def cost(self) -> Decimal:
        """What the run's priced model calls cost, each counted once."""
        return sum((call.cost_usd for call in self.spending.calls), Decimal(0))


async def explore(
    spec: Spec,
    config: ProjectConfig,
    root: Path,
    start: str,
    *,
    router: ModelRouter,
    playwright: Playwright,
) -> Explored:
    """Explore `spec` (`config`, spec root `root`) from the start origin `start`
    with `router` and `playwright`: bind and admit (12, 5), read the compiled
    file and make the record (5, 15), plan (3, 5, 11, 15; `plan.json`), then
    `run_attempt`, checking what it returns."""
    secrets = bound_secrets(spec, start)
    redactor = Redactor(secrets.values())
    admit(spec, config, secrets, redactor)
    made = _made(root, spec, redactor)
    if isinstance(made, Explored):
        return made
    at, record, attempt = made
    run = Run(
        spec,
        config,
        start,
        redactor,
        record,
        attempt,
        secrets,
        router,
        playwright,
        get_running_loop().time() + config.budgets.minutes * 60,
        EgressGate(egress_policy(spec, config, start)),
        at,
    )
    planned = await _planned(run)
    if isinstance(planned, Explored):
        return _shown(run, planned)
    return _shown(run, await run_attempt(run, planned))


def _shown(run: Run, explored: Explored) -> Explored:
    """`explored`, with no script when its script spells a bound value, as
    one `finish` compiled with a locator the attempt's own drop missed
    does, and fixed words for a document still spelling one."""
    script = explored.script
    if script is not None and run.redactor.spells(script.model_dump(mode="json")):
        explored = replace(explored, script=None)
    if not run.redactor.spells(explored.document):
        return explored
    document = {"outcome": explored.outcome, "reasons": [_UNSHOWN]}
    return replace(explored, document=document)


def _made(
    root: Path, spec: Spec, redactor: Redactor
) -> tuple[Publication, RunRecord, RunRecord] | Explored:
    """The run's record, scanning with `redactor`, its `attempt-1` part,
    and where `spec` publishes, with the compiled file as found now, or how
    the run ended when either can't be made or the file can't be read (15).
    A link at `.aqa`, or where the script is published, is a spec error; a
    part not made from the record, a runner defect."""
    try:
        record = RunRecord.create(root).redacting(redactor)
    except OSError:
        return _unmade(None, _UNMADE)
    except ValueError as error:  # a .aqa or .aqa/runs that is a link
        raise _linked(str(error), redactor) from None
    try:
        attempt = record.part(phase_parts(1)[0])
    except OSError, ValueError:  # in a directory just made: no spec's link
        return _unmade(record, _UNMADE)
    if attempt.redactor is not redactor or not record.shares_journals(attempt):
        raise ValueError(
            "the run's attempt-1 must be a part made from its record, scanning "
            "with the run's redactor"
        )
    try:
        at = publication(root, spec)
    except OSError:
        return _unmade(record, _UNREAD)
    except SpecError as error:  # a link where the script is published
        raise _linked(str(error), redactor) from None
    return at, record, attempt


def _linked(problem: str, redactor: Redactor) -> SpecError:
    """`problem`, a link's, which names its path, scanned with `redactor`."""
    shown = redactor.redact(problem)
    return SpecError([UNSCANNABLE if redactor.finds(shown) else shown])


def _unmade(record: RunRecord | None, why: str) -> Explored:
    return Explored(
        "record_error",
        _exit("record_error"),
        {"outcome": "record_error", "reasons": [why]},
        None if record is None else record.path,
        Decimal(0),
    )


@dataclass(frozen=True)
class _Unplanned:
    """Why planning stopped the run: its outcome, why, and the reasons."""

    outcome: Ended
    why: str
    reasons: tuple[str, ...] = ()


async def _planned(run: Run) -> Explorable | Explored:
    """The plan the run explores by, or how planning ended; `plan.json`,
    written either way and checked as written, says which."""
    planned = await _planning(run)
    plan = None if isinstance(planned, _Unplanned) else planned.plan
    judged = planned if isinstance(planned, _Unplanned) else _explorable(run, planned)
    stop = judged if isinstance(judged, _Unplanned) else None
    try:
        run.record.write(
            "plan.json",
            _plan_document(run, plan, stop),
            check=require_clean(run.redactor, "plan.json"),
        )
    except OSError, RefusedWriteError:
        return _unplanned(run, plan, _Unplanned("record_error", UNWRITTEN))
    if isinstance(judged, _Unplanned):
        return _unplanned(run, plan, judged)
    return judged


async def _planning(run: Run) -> Planned | _Unplanned:
    """The plan call's answer within the minutes budget, or why there is
    none: time, a request never sent, a cost line lost, or no response."""
    limit = timeout_at(run.deadline)
    try:
        async with limit:
            return await make_plan(
                run.router,
                run.spec,
                spend=run.spending.spend(),
                check=unspelled(run.redactor),
            )
    except TimeoutError:
        if not limit.expired():
            raise  # not the minutes budget's own
        return _Unplanned("gave_up", _MINUTES)
    except UnsentRequestError:
        return _Unplanned("gave_up", UNSENT)
    except UnrecordedCostError:
        return _Unplanned("record_error", UNRECORDED)
    except (ModelCallError, ProviderError) as error:
        failed = error.__cause__ if isinstance(error, ModelCallError) else error
        if not isinstance(failed, ProviderError):
            raise
        # The error's class, never the provider's message.
        return _Unplanned(
            "no_response", f"the model gave no response ({type(failed).__name__})"
        )


def _explorable(run: Run, planned: Planned) -> Explorable | _Unplanned:
    """The plan, or why exploring can't keep it (D35): `judge`'s refusal, a
    check it can't compile yet, a subject contract, or the spend gone."""
    judged = judge(planned, run.spec)
    if not isinstance(judged, CoveragePlan):
        return _Unplanned(*judged)
    if lines := unsupported_features(judged):
        why = "the plan has checks exploring can't keep"
        return _Unplanned("spec_error", why, lines)
    try:
        rows = subject_contracts(run.config, run.spec.frontmatter.id)
        contracts = contracts_by_meaning(judged, rows)
    except CompileError as error:
        why = "the plan contradicts a subject contract"
        return _Unplanned("spec_error", why, error.problems)
    if run.spending.left <= 0:
        return _Unplanned("gave_up", "the spend budget ran out with the plan")
    return Explorable(judged, contracts)


def _plan_document(
    run: Run, plan: CoveragePlan | None, stop: _Unplanned | None
) -> dict[str, object]:
    """`plan.json`: how planning ended and why, the plan the model wrote,
    if any, and how many priced responses the run kept."""
    said = run.redactor.redact
    return {
        "run_id": run.record.run_id,
        "spec_id": run.spec.frontmatter.id,
        "spec_hash": run.spec.spec_hash,
        "outcome": "planned" if stop is None else stop.outcome,
        "reasons": [] if stop is None else [said(stop.why), *map(said, stop.reasons)],
        "plan": None
        if plan is None
        else plan.model_dump(mode="json", exclude_none=True),
        "plan_hash": None if plan is None else plan_hash(plan),
        "costs": len(run.spending.calls),
    }


def _unplanned(run: Run, plan: CoveragePlan | None, stop: _Unplanned) -> Explored:
    return Explored(
        stop.outcome,
        _exit(stop.outcome),
        _plan_document(run, plan, stop),
        run.record.path,
        run.cost,
    )


async def run_attempt(run: Run, explorable: Explorable) -> Explored:
    """Run the attempts by `explorable`, each attempt's phase, then its
    confirmation's, from the egress proxy's opening to its end, publish, and
    say how the run ended (module docstring)."""
    return _ended(run, await _attempted(run, explorable))


@dataclass(frozen=True)
class _Tried:
    """How one attempt ended: each planned check's last evaluation, its
    phase and its confirmation's, the path it may publish and how, and
    whether it warns that its confirmation may have changed the app."""

    checks: Mapping[str, AssertionResult | None]
    phase: Phase
    confirmation: Confirmation | None = None
    script: CompiledScript | None = None
    intended: Published | None = None
    repeated: bool = False

    @property
    def phases(self) -> tuple[Phase, ...]:
        last = () if self.confirmation is None else (self.confirmation,)
        return (self.phase, *last)


async def _attempted(run: Run, explorable: Explorable) -> ExploreEnd:
    """Each attempt's phase and, for a path it may publish, the
    confirmation's, from the egress proxy's opening to its end, until one
    publishes or no new attempt may start (`_again`); then the path
    published, if it may be."""
    expected = run.config.egress.expected_blocked
    earlier: list[Phase] = []
    async with EgressProxy(run.gate) as proxy:

        def interrupted() -> bool:
            return (
                bool(run.gate.infrastructure_events)
                or proxy.egress_blocks(expected).blocked
            )

        tried = await _tried(run, explorable, proxy, interrupted)
        while tried.intended is None and _again(run, tried, interrupted):
            try:
                part = run.record.part(phase_parts(run.attempts.made + 1)[0])
            except OSError, ValueError:  # in a directory just made: no spec's link
                run.stopped("record_error", UNWRITTEN)
                break
            # Only once the next part exists: the last attempt's phases stay
            # `tried`'s until another attempt takes its place.
            earlier.extend(tried.phases)
            run.attempt = part
            run.attempts.made += 1
            await proxy.begin_phase()
            tried = await _tried(run, explorable, proxy, interrupted)
        blocks = proxy.egress_blocks(expected)
    published = None
    if tried.script is not None and tried.intended is not None:
        published = await _published(run, tried.script, tried.intended)
    return ExploreEnd(
        run.record,
        tried.phase,
        confirmation=tried.confirmation,
        blocks=blocks,
        infrastructure_events=tuple(run.gate.infrastructure_events),
        checks=tried.checks,
        stops=tuple(run.stops),
        published=published,
        earlier=tuple(earlier),
        repeated=tried.repeated,
    )


async def _tried(
    run: Run,
    explorable: Explorable,
    proxy: EgressProxy,
    interrupted: Callable[[], bool],
) -> _Tried:
    """One attempt in the run's attempt part, and its confirmation when its
    path may be confirmed: unconfirmed, a path that would repeat a side
    effect with no reset before it (AGENTS.md §6); confirmed, one whose
    confirmation passed, unless an earlier one of the same script failed
    (flaky)."""
    run.attempted = None
    checks, events = await _phase(run, explorable, proxy, interrupted)
    # The browser has closed. Ending the phase joins every connection, and
    # the confirmation's has ended once `_confirming` returns, so nothing
    # records a block or an event after the snapshot the run ends with, and
    # no write reaches the app.
    traffic = await proxy.end_phase()
    start = _infer_flags(run)
    _uncertain(run, traffic)
    phase = Phase(run.attempt, traffic, events)
    script = _publishable(run, interrupted)
    if script is None:
        return _Tried(checks, phase)
    if _unconfirmable(run.spec, script, start):
        return _Tried(checks, phase, script=script, intended="unconfirmed")
    confirmation = await _confirming(run, proxy, script, interrupted)
    if (replayed := _passing(run, confirmation)) is None:
        if confirmation is not None and confirmation.result is not None:
            run.attempts.failed_with(script, confirmation.result, run.redactor)
        return _Tried(checks, phase, confirmation)
    if run.attempts.flaky(script):
        run.stopped("gave_up", FLAKY)
        return _Tried(checks, phase, confirmation)
    # Its runs can only raise a flag; one raised with no reset before them
    # says the replay may have changed the app.
    start = _infer_flags(run, replayed.steps)
    script = _publishable(run, interrupted)
    repeated = script is not None and _unconfirmable(run.spec, script, start)
    return _Tried(checks, phase, confirmation, script, "confirmed", repeated)


def _again(run: Run, tried: _Tried, interrupted: Callable[[], bool]) -> bool:
    """Whether a new attempt starts after `tried`: its confirmation failed,
    with nothing stopping or interrupting the run, no intent of its left
    unresolved, whatever its flag, an attempt left, and a reset hook
    (`explore_attempts`)."""
    if run.stops or interrupted() or tried.confirmation is None:
        return False
    if any(phase.record.unresolved() for phase in tried.phases):
        return False
    if run.attempts.made >= run.attempts.budget:
        return False
    if not run.attempts.resets:
        run.stopped("gave_up", f"no new attempt started: {UNRESET}")
        return False
    return True


def _uncertain(run: Run, traffic: PhaseTraffic) -> None:
    """Stop for each kind of exchange `traffic` left uncertain (D29)."""
    uncertain = (traffic.plain_uncertain, traffic.tunnel_uncertain)
    for why, count in zip(_UNCERTAIN, uncertain, strict=True):
        if count:
            run.stopped("reset_unsafe", why)


def _publishable(run: Run, interrupted: Callable[[], bool]) -> CompiledScript | None:
    """The path `finish` compiled, when nothing stopped or interrupted the
    attempt, with every locator that spells a bound value dropped, never
    redacted; None, with a stop naming it, when a target has none left."""
    if run.script is None or run.stops or interrupted():
        return None
    targets = {}
    for name, target in run.script.targets.items():
        kept = tuple(
            locator
            for locator in target.locators
            if not run.redactor.spells(locator.model_dump(mode="json"))
        )
        if not kept:
            run.stopped(
                "gave_up", f"target {name} has no locator that spells no bound value"
            )
            return None
        targets[name] = target.model_copy(update={"locators": kept})
    return run.script.model_copy(update={"targets": targets})


def _infer_flags(
    run: Run, confirmation: Sequence[executor.StepResult] = ()
) -> str | None:
    """Infer the flags of the script the attempt's `finish` compiled, from
    every run of its steps, the confirmation's included when given, into
    `run.script` (`Attempt.inferred`); why its start may change the app,
    None when nothing says so. Only once the phases have ended."""
    inferred = None if run.attempted is None else run.attempted.inferred(confirmation)
    run.script = None if inferred is None else inferred.script
    return None if inferred is None else inferred.start


def _unconfirmable(spec: Spec, script: CompiledScript, start: str | None) -> bool:
    """Whether confirming `script` would repeat, with no reset before it,
    which no replay may (AGENTS.md §6), a side-effect step or a start URL
    whose load may change the app (`start`, why)."""
    hook = spec.frontmatter.preconditions.reset
    effect = start is not None or any(step.side_effect for step in script.steps)
    return hook is None and effect


async def _confirming(
    run: Run,
    proxy: EgressProxy,
    script: CompiledScript,
    interrupted: Callable[[], bool],
) -> Confirmation | None:
    """Within the minutes budget, reset the app, when the spec has a hook,
    then open a phase and replay `script` with every locator in a fresh
    browser, into the record's `confirmation` part, with no model: the
    confirmation's phase, None when it never began. The browser closes
    after the budget's span has ended, as the attempt's does. Replay ends
    the phase when it returns; otherwise this ends it once the browser has
    closed, outside the span, so its traffic is the proxy's to give."""
    began: RunRecord | None = None
    result: executor.RunResult | None = None
    limit = timeout_at(run.deadline)
    try:
        async with AsyncExitStack() as browser, limit:
            if not await _reset(run) or interrupted():
                return None
            try:
                part = run.record.part(phase_parts(run.attempts.made)[1])
            except OSError, ValueError:  # in a directory just made: no spec's link
                run.stopped("record_error", UNWRITTEN)
                return None
            await proxy.begin_phase()
            began = part
            launched = await browser.enter_async_context(
                launched_browser(run.playwright.chromium)
            )
            result = await executor.replay(
                script,
                executor.RunSetup(run.spec, run.config, run.start, part),
                chromium=launched,
                proxy=proxy,
                gate=run.gate,
                every_locator=True,
            )
    # First: a TimeoutError is an OSError.
    except TimeoutError:
        if not limit.expired():
            raise  # not the minutes budget's own
        run.stopped("gave_up", _MINUTES)
    except (SandboxUnavailableError, UnregisteredEngineError) as error:
        run.stopped("no_sandbox", str(error))
    except OSError, RefusedWriteError:
        if began is None:
            raise  # the phase's listener, which propagates as the attempt's does
        # A journal line the record couldn't take: nothing more may go.
        run.stopped("record_error", UNWRITTEN)
    finally:
        if began is not None and result is None:
            # Replay didn't return, so the phase is this function's to end,
            # once the browser that held its context has closed; unless
            # replay's own end had ended it as a cancellation reached it.
            with suppress(RuntimeError):
                await proxy.end_phase()
    if began is None:
        return None
    traffic = proxy.ended_traffic()
    _uncertain(run, traffic)
    total = None if result is None else result.policy_events_total
    return Confirmation(began, traffic, total, result)


def _passing(run: Run, confirmation: Confirmation | None) -> executor.RunResult | None:
    """The confirmation's replay when it passed with no stop held."""
    replayed = None if confirmation is None else confirmation.result
    passed = replayed is not None and replayed.outcome == "passed" and not run.stops
    return replayed if passed else None


async def _published(
    run: Run, script: CompiledScript, kind: Published
) -> Published | None:
    """Publish `script` as `kind` before the run's deadline, its text
    checked as written: `kind` once it is installed, with a stop when a
    failure came after; None, with a stop, when it isn't."""
    at = run.publication
    place = str(at.target.relative_to(at.root))
    try:
        await publish(
            at,
            script.model_copy(update={"confirmed": kind == "confirmed"}),
            check=require_clean(run.redactor, place),
            deadline=run.deadline,
        )
    except PublishError as error:
        run.stopped("gave_up", str(error))
    except (RefusedWriteError, PublishWriteError) as error:
        run.stopped("record_error", str(error))
        if isinstance(error, PublishWriteError) and error.installed:
            return kind
    else:
        return kind
    return None


async def _phase(
    run: Run,
    explorable: Explorable,
    proxy: EgressProxy,
    interrupted: Callable[[], bool],
) -> tuple[Mapping[str, AssertionResult | None], int]:
    """Reset, open the browser, load the start URL and take the turns, all
    within the minutes budget: each planned check's last evaluation, None
    while never bound, kept however the phase ended, and how many policy
    events the session recorded. The probes are read after the reset."""
    session: BrowserSession | None = None
    attempt: Attempt | None = None
    limit = timeout_at(run.deadline)
    # Once Chromium has launched, the browser closes after the budget's span
    # has ended, so the budget never cancels that close and never takes the
    # place of an error the phase raised as it closes; and that error
    # reaches the close before it becomes a stop (#204).
    try:
        async with AsyncExitStack() as browser, limit:
            if await _reset(run) and (offered := await _offered(run, explorable)):
                if run.attempts.made == 1:
                    # Once per Playwright: a second registration is refused.
                    await register_identity_engine(run.playwright)
                launched = await browser.enter_async_context(
                    launched_browser(run.playwright.chromium)
                )
                session = await browser.enter_async_context(
                    prepared_session(
                        launched,
                        egress=proxy,
                        settings=effective_browser(run.spec, run.config),
                        redactor=run.redactor,
                    )
                )
                if session.redactor is not run.redactor:
                    raise ValueError(
                        "the browser session must scan with the run's redactor"
                    )
                attempt = _attempt(run, session, explorable, interrupted, offered)
                await _started(run, attempt)
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


async def _offered(run: Run, explorable: Explorable) -> navigator.Offered | None:
    """What the probes `probe_equals` checks read offer: their values' JSON
    paths, each read once now, after the reset and before the browser,
    numbered o1, o2, ... in the plan's order (ADR-0025's #53 P20
    amendment). None, with the run's stop, when a probe offers none."""
    options: dict[str, navigator.ProbeOption] = {}
    cut: list[str] = []
    for name in dict.fromkeys(
        check.probe
        for _, check in planned_checks(explorable.plan).values()
        if check.check == "probe_equals" and check.probe is not None
    ):
        leaves = await _leaves(run, name)
        if isinstance(leaves, str):
            run.stopped("gave_up", f"probe {name} offers no option: {leaves}")
            return None
        options |= {
            f"o{n}": navigator.ProbeOption(name, path)
            for n, path in enumerate(leaves.paths, start=len(options) + 1)
        }
        if leaves.cut:
            cut.append(name)
    return navigator.Offered(options, tuple(cut))


async def _leaves(run: Run, name: str) -> probes.Leaves | str:
    """The JSON paths probe `name` offers, from one read now, or why none."""
    url = probe_url(run.spec, name, run.start)
    seconds = run.config.budgets.resolve_seconds
    try:
        body = await probes.read_once(run.gate, url, seconds=seconds)
    except (
        probes.ProbeError,
        egress.EgressRefusedError,
        egress.EgressUpstreamError,
    ) as error:
        return reasons.reason(error, run.redactor)
    leaves = probes.leaf_paths(body)
    if not 0 < len(leaves.paths) <= probes.OFFERED_PATHS:
        return (
            f"its response has {len(leaves.paths)} values that a JSON path of at "
            f"most {probes.OFFERED_PATH_CHARS} characters names, and an attempt "
            f"offers 1 to {probes.OFFERED_PATHS}"
        )
    return leaves


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
    offered: navigator.Offered,
) -> Attempt:
    """The attempt the navigator acts through, interrupted as `interrupted`
    says. The project's `resolve_seconds` bound its looks and the session's
    actions, and navigation keeps the executor's bound, as replay's does."""
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
        offered=offered,
        pinned=run.attempts.pinned,
    )


async def _started(run: Run, attempt: Attempt) -> None:
    """Load the start URL as step 0, no side-effect step (DATA_MODEL §7),
    as replay does, but through `Attempt.dispatch`, so step 0 owes its
    evidence to the first observation and keeps its window for inferring the
    flags."""
    path = run.spec.frontmatter.preconditions.start_url
    try:
        step = await attempt.dispatch(
            executor.Dispatch(
                0,
                {"action": "navigate", "url": path},
                False,
                executor.NAVIGATION_SECONDS,
            ),
            lambda: attempt.session.navigate(start_url(run.spec, run.start)),
        )
    except OSError:
        # A journal line the record couldn't take: nothing more may go.
        run.stopped("record_error", UNWRITTEN)
        return
    if step.outcome != "completed":
        run.stopped("gave_up", f"the start URL didn't load: {step.error}")


async def _turns(run: Run, attempt: Attempt, plan: CoveragePlan) -> None:
    """Take the navigator's turns (`explore_turns.take_turns`) with the
    run's spend, turn budget and stops, and keep the script `finish`
    compiled, if any."""
    rows = subject_contracts(run.config, run.spec.frontmatter.id)
    turning = Turning(
        attempt,
        run.router,
        navigator_prefix(run.spec, plan, rows, redactor=run.redactor),
        run.spending,
        run.config.budgets.actions_per_attempt,
        run.stops,
        attempts=run.attempts,
    )
    await take_turns(turning)
    if turning.script is not None:
        run.attempted = attempt


def _ended(run: Run, end: ExploreEnd) -> Explored:
    """Write the run's document, and its egress record when blocked, each
    checked as written, and say how the run ended. A record that doesn't
    scan with the run's redactor is refused before anything is written,
    with or without a browser (`ValueError`). One the record refuses or
    can't write adds a record error, which decides unless a more decisive
    outcome holds. Whether `egress.json` was written is the result's
    `egress_saved`."""
    if end.record.redactor is not run.redactor:
        raise ValueError("the run's record must scan with the run's redactor")
    document = present(end)
    saved = False
    try:
        end.record.write(
            "explore.json", document, check=require_clean(run.redactor, "explore.json")
        )
        if (egress := document["egress"]) is not None:
            check = require_clean(run.redactor, "egress.json")
            end.record.write("egress.json", egress, check=check)
            saved = True
    except OSError, RefusedWriteError:
        run.stopped("record_error", UNWRITTEN)
        end = replace(end, stops=tuple(run.stops))
        document = present(end)
    outcome = final_outcome(end)
    return Explored(
        outcome,
        _exit(outcome),
        document,
        end.record.path,
        run.cost,
        run.script,
        egress_saved=saved,
    )
