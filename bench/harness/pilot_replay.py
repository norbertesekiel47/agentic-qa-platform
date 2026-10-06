"""One pilot attempt (ADR-0023, #51): a fresh run record, egress gate, proxy,
Playwright driver and browser, the spec's reset hook sent on that gate, then
the strict replay. Manifest answers never enter; the caller scores what this
returns."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aqa_core.project import SpecError
from aqa_runner.bound_secrets import (
    MissingSecretError,
    SecretLoggedError,
    bound_secrets,
)
from aqa_runner.egress import (
    EgressGate,
    EgressRefusedError,
    EgressUpstreamError,
    egress_policy,
)
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import RunResult, RunSetup, replay
from aqa_runner.run_record import RunRecord
from aqa_runner.runner_requests import runner_request
from pilot_inputs import PilotInput
from pilot_results import Observation, observe
from playwright.async_api import async_playwright

# What stopped an attempt first: a test secret that can't be bound; its reset
# refused, unreachable, answered with anything but a 2xx (a redirect is never
# followed) or unanswered within resolve_seconds; replay refusing the script
# before its browser; the attempt outlasting `minutes`; or, after a replay
# that returned, a resource that failed to close.
type Failure = Literal[
    "reset_unreachable",
    "reset_rejected",
    "reset_timeout",
    "secret_unusable",
    "script_refused",
    "operation_timeout",
    "cleanup_failed",
]
# Whether every resource opened was closed: each exit returned, one raised,
# or they weren't done within resolve_seconds of cancellation.
type Cleanup = Literal["completed", "failed", "incomplete"]
type Resources = Literal["closed", "unknown"]
type _Owned = Literal["driver", "proxy"]


@dataclass(frozen=True)
class AttemptHealth:
    """What the attempt's machinery saw, beside the scored observation."""

    reset_completed: bool
    infrastructure: bool
    egress_blocked: bool


@dataclass(frozen=True)
class ObservedAttempt:
    """A replay that returned, with every resource closed and its receipts
    written."""

    run_id: str
    record: Path
    observation: Observation
    health: AttemptHealth


@dataclass(frozen=True)
class FailedAttempt:
    """An attempt that ended without a usable replay. Its resources are
    `closed` only when its browser never opened or replay returned, and
    every exit returned."""

    run_id: str
    record: Path
    failure: Failure
    cleanup: Cleanup
    resources: Resources


class _RefusedError(Exception):
    """The attempt stopped for the failure its state records."""


@dataclass
class _State:
    """One attempt's progress, shared by its coordinator and its task. The
    first failure stays first."""

    failure: Failure | None = None
    error: BaseException | None = None
    interrupted: bool = False
    reset_completed: bool = False
    browser: Literal["unopened", "unknown", "closed"] = "unopened"
    opened: set[_Owned] = field(default_factory=set)
    closed: set[_Owned] = field(default_factory=set)
    cleanup_raised: bool = False
    incomplete: bool = False

    def refuse(self, failure: Failure) -> _RefusedError:
        self.failure = self.failure or failure
        return _RefusedError()

    def latch(self, error: Exception) -> None:
        """Note the first `error` nothing expected, before the resources
        unwind, so a stalled cleanup can't stand in for it. Once the attempt
        was stopped, it is its cleanup failing."""
        if self.failure is None and self.error is None and not self.interrupted:
            self.error = error
        else:
            self.cleanup_raised = True

    @property
    def cleanup(self) -> Cleanup:
        if self.incomplete:
            return "incomplete"
        closed = self.closed == self.opened and not self.cleanup_raised
        return "completed" if closed else "failed"

    @property
    def resources(self) -> Resources:
        known = self.cleanup == "completed" and self.browser != "unknown"
        return "closed" if known else "unknown"


async def replay_pilot(
    pilot: PilotInput, record_root: Path
) -> ObservedAttempt | FailedAttempt:
    """One attempt at `pilot`, in a new run record under `record_root`, its
    resources owned by a task of its own. The attempt may take the config's
    `minutes`; then it, or an interrupt, cancels the task once. If the
    task's cleanup outlasts `resolve_seconds`, `cleanup-incomplete.json`
    says so, and this waits for the task however long it takes: nothing may
    act on the app while its browser may still be open. Later cancellations
    of this coroutine are recorded, never passed on; a second Ctrl-C under
    `asyncio.run`'s own SIGINT handling is not one of them. The final
    receipt is `attempt.json`."""
    record = RunRecord.create(record_root)
    record.write("attempt-start.json", {"spec_id": pilot.spec.frontmatter.id})
    state = _State()
    attempt = asyncio.create_task(_attempt(pilot, record, state))
    budgets = pilot.config.budgets
    try:
        await asyncio.wait({attempt}, timeout=budgets.minutes * 60)
    except asyncio.CancelledError:
        state.interrupted = True
    if not attempt.done():
        if not state.interrupted and state.error is None:
            state.failure = state.failure or "operation_timeout"
        attempt.cancel()
        if not await _waited(attempt, budgets.resolve_seconds, state):
            await _held(attempt, record, state)
    return _conclude(pilot, record, attempt, state)


async def _held(
    attempt: asyncio.Task[RunResult], record: RunRecord, state: _State
) -> None:
    """Record that cleanup outlasted its window; wait for it even if that fails."""
    state.incomplete = True
    incomplete = {
        "failure": state.failure,
        "interrupted": state.interrupted,
        "resources": "unknown",
        "reservation_release": "forbidden",
    }
    try:
        record.write("cleanup-incomplete.json", incomplete)
    finally:
        await _waited(attempt, None, state)


async def _waited(
    task: asyncio.Task[RunResult], seconds: float | None, state: _State
) -> bool:
    """Whether `task` ended within `seconds`, or at all when None. An
    interrupt meanwhile is recorded, never passed on: cancelling the task
    again could cut its cleanup short."""
    loop = asyncio.get_running_loop()
    deadline = None if seconds is None else loop.time() + seconds
    while not task.done():
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            return False
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            state.interrupted = True
    return True


async def _attempt(pilot: PilotInput, record: RunRecord, state: _State) -> RunResult:
    """The attempt's own task, which opens, uses and closes its resources."""
    if (refusal := _unbound(pilot)) is not None:
        raise state.refuse(refusal)
    gate = EgressGate(egress_policy(pilot.spec, pilot.config, pilot.start))
    if pilot.reset is not None:
        await _reset(gate, pilot.reset.url, pilot.config.budgets.resolve_seconds, state)
    setup = RunSetup(pilot.spec, pilot.config, pilot.start, record)
    async with (
        _owned(async_playwright(), "driver", state) as playwright,
        _owned(EgressProxy(gate), "proxy", state) as proxy,
    ):
        state.browser = "unknown"
        try:
            result = await replay(
                pilot.script,
                setup,
                chromium=playwright.chromium,
                proxy=proxy,
                gate=gate,
            )
        # Replay checks the script before its browser starts (its docstring).
        except SpecError:
            state.browser = "unopened"
            raise state.refuse("script_refused") from None
        except Exception as error:
            state.latch(error)
            raise
        state.browser = "closed"
        return result


def _unbound(pilot: PilotInput) -> Failure | None:
    """Why the spec's test secrets can't be bound, checked before the reset
    as replay checks them before its browser. The error is dropped: a value
    with surrogates fails to encode with a ValueError quoting it."""
    try:
        bound_secrets(pilot.spec, pilot.start)
    except (MissingSecretError, SecretLoggedError, ValueError):
        return "secret_unusable"
    except SpecError:
        return "script_refused"
    return None


async def _reset(gate: EgressGate, url: str, seconds: float, state: _State) -> None:
    """POST the spec's reset hook through `gate`, the attempt's, so the reset
    and the replay share its pins. Anything but a 2xx within `seconds` stops
    the attempt before its browser starts."""
    try:
        async with asyncio.timeout(seconds):
            response = await runner_request(gate, "POST", url)
    except TimeoutError:
        raise state.refuse("reset_timeout") from None
    except (EgressRefusedError, EgressUpstreamError):
        raise state.refuse("reset_unreachable") from None
    if not 200 <= response.status < 300:
        raise state.refuse("reset_rejected")
    state.reset_completed = True


@asynccontextmanager
async def _owned[T](
    resource: AbstractAsyncContextManager[T], name: _Owned, state: _State
) -> AsyncIterator[T]:
    """`resource`, entered, then exited however the body ends. It counts as
    closed only once its exit returns."""
    state.opened.add(name)
    try:
        value = await resource.__aenter__()
    except Exception as error:
        state.latch(error)
        raise
    try:
        yield value
    finally:
        # Exited as on success: the state records failures; neither reads them.
        await resource.__aexit__(None, None, None)
        state.closed.add(name)


def _conclude(
    pilot: PilotInput,
    record: RunRecord,
    attempt: asyncio.Task[RunResult],
    state: _State,
) -> ObservedAttempt | FailedAttempt:
    """The ended attempt, once `attempt.json` records it. An interrupt wins
    over any failure and is raised again, as is an unexpected error."""
    error = None if attempt.cancelled() else attempt.exception()
    if error is not None and state.failure is None and state.error is None:
        if state.browser == "closed":
            state.failure = "cleanup_failed"
        else:
            state.error = error
    if state.interrupted:
        _receipt(record, state, "interrupted")
        raise asyncio.CancelledError
    if state.failure is not None:
        _receipt(record, state, "failed")
        return FailedAttempt(
            record.run_id, record.path, state.failure, state.cleanup, state.resources
        )
    if state.error is not None:
        _receipt(record, state, "unexpected")
        raise state.error
    result = attempt.result()
    observation = observe(
        pilot.script, result, invariants=pilot.spec.frontmatter.invariants
    )
    health = AttemptHealth(
        state.reset_completed,
        bool(result.infrastructure_events),
        result.egress_blocks.blocked,
    )
    _receipt(record, state, "observed")
    return ObservedAttempt(record.run_id, record.path, observation, health)


def _receipt(
    record: RunRecord,
    state: _State,
    outcome: Literal["observed", "failed", "interrupted", "unexpected"],
) -> None:
    receipt = {
        "outcome": outcome,
        "failure": state.failure,
        "interrupted": state.interrupted,
        "reset_completed": state.reset_completed,
        "cleanup": state.cleanup,
        "resources": state.resources,
    }
    record.write("attempt.json", receipt)
