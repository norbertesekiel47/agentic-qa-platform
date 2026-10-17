"""Exploring a spec (ADR-0024's #53 P15b and P15c amendments): preflight,
the plan, the attempt's phase, and how the run ended.

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

from asyncio import get_running_loop, timeout_at
from collections.abc import Callable, Iterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from aqa_core.compiled import CompiledScript
from aqa_core.compiler import CompileError, contracts_by_meaning, unsupported_features
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan, plan_hash, planned_checks
from aqa_core.project import SpecError, effective_browser, start_url, subject_contracts
from aqa_core.schema import Contract
from aqa_core.spec import Spec
from playwright.async_api import Playwright

from aqa_runner import executor
from aqa_runner.anthropic_client import ProviderError
from aqa_runner.binding import UnregisteredEngineError
from aqa_runner.bound_secrets import BoundSecret, bound_secrets
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.coverage_plan import Planned, judge, make_plan
from aqa_runner.egress import EgressGate, egress_policy
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
from aqa_runner.navigator import account_problems, navigator_prefix, spec_view
from aqa_runner.navigator_tools import Attempt
from aqa_runner.redaction import Redactor
from aqa_runner.reset import ResetFailedError, reset
from aqa_runner.run_record import (
    PARTS,
    RefusedWriteError,
    RunRecord,
    UnrecordedCostError,
    require_clean,
)
from aqa_runner.sandbox import SandboxUnavailableError

# How a run ends: as `explore_result.final_outcome` says once the egress
# proxy opened, or a spec error before it (#53 P15c).
type Ended = Outcome | Literal["spec_error"]

# Why the run stopped, in fixed words.
_UNMADE: Final = "the run record could not be created; nothing was planned"
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
    with `router` and `playwright`: bind and admit (12, 5), make the record (15),
    plan (3, 5, 11, 15; `plan.json`), then `run_attempt`, checking what it returns."""
    secrets = bound_secrets(spec, start)
    redactor = Redactor(secrets.values())
    _admit(spec, config, secrets, redactor)
    made = _made(root, redactor)
    if isinstance(made, Explored):
        return made
    record, attempt = made
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
    )
    planned = await _planned(run)
    if isinstance(planned, Explored):
        return _shown(run, planned)
    return _shown(run, await run_attempt(run, planned))


def _shown(run: Run, explored: Explored) -> Explored:
    """`explored`, or fixed words for a document still spelling a bound value."""
    if not run.redactor.spells(explored.document):
        return explored
    document = {"outcome": explored.outcome, "reasons": [_UNSHOWN]}
    return replace(explored, document=document)


def _admit(
    spec: Spec,
    config: ProjectConfig,
    secrets: Mapping[str, BoundSecret],
    redactor: Redactor,
) -> None:
    """`SpecError` naming each place, never its text, where a bound value
    could reach the model, the browser or the record (ADR-0026, #53 P15c)."""
    spec_id = spec.frontmatter.id
    problems = account_problems(spec, redactor)
    problems.extend(
        f"{spec.path}: {place}: a bound value's spelling is in it"
        for place, value in _places(spec)
        if redactor.spells(value)
    )
    problems.extend(
        f"subjects ({spec_id}, expect {expect}): a bound value's spelling is in "
        "its region or part"
        for expect, contract in subject_contracts(config, spec_id).items()
        if redactor.spells(contract.model_dump(mode="json"))
    )
    problems.extend(
        f"test secret {name}: a bound value's spelling is in its field's name"
        for name, secret in secrets.items()
        if secret.destination.field != "password"
        and redactor.spells(secret.destination.field.name)
    )
    if redactor.markers_spell_a_value:
        problems.append(
            "test secrets: a marker, [SECRET:<name>], spells a bound value, so no "
            "scan could take it out: rename the secret or change its value"
        )
    if problems:
        raise SpecError(problems)


def _places(spec: Spec) -> Iterator[tuple[str, object]]:
    """Each place of the spec as the model reads it (`navigator.spec_view`),
    by name, keys beside values (a probe's name); the start path whole, as
    the browser and step 0's intent take it. The account is checked apart."""
    view = spec_view(spec)
    preconditions = view.pop("preconditions")
    preconditions.pop("account", None)
    preconditions["start_url"] = spec.frontmatter.preconditions.start_url
    for name, value in preconditions.items():
        view[f"preconditions.{name}"] = value
    for place, value in view.items():
        yield place, _keyed(value)


def _keyed(value: object) -> object:
    """`value`, a JSON value, with each mapping's keys among its values."""
    if isinstance(value, dict):
        return [[key, _keyed(item)] for key, item in value.items()]
    if isinstance(value, list):
        return [_keyed(item) for item in value]
    return value


def _made(root: Path, redactor: Redactor) -> tuple[RunRecord, RunRecord] | Explored:
    """The run's record, scanning with `redactor`, and its `attempt-1` part,
    or how the run ended when either can't be made (15). A link at `.aqa` is
    a spec error; a part not made from the record, a runner defect."""
    try:
        record = RunRecord.create(root).redacting(redactor)
    except OSError:
        return _unmade(None)
    except ValueError as error:  # a .aqa or .aqa/runs that is a link
        raise SpecError([str(error)]) from None
    try:
        attempt = record.part(PARTS[0])
    except OSError, ValueError:  # in a directory just made: no spec's link
        return _unmade(record)
    if attempt.redactor is not redactor or not record.shares_journals(attempt):
        raise ValueError(
            "the run's attempt-1 must be a part made from its record, scanning "
            "with the run's redactor"
        )
    return record, attempt


def _unmade(record: RunRecord | None) -> Explored:
    return Explored(
        "record_error",
        _exit("record_error"),
        {"outcome": "record_error", "reasons": [_UNMADE]},
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
    # Once its session has opened, the browser closes after the budget's
    # span has ended, so the budget never cancels that close and never
    # takes the place of an error the phase raised as it closes.
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
        end.record.write(
            "explore.json", document, check=require_clean(run.redactor, "explore.json")
        )
        if (egress := document["egress"]) is not None:
            check = require_clean(run.redactor, "egress.json")
            end.record.write("egress.json", egress, check=check)
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
    )
