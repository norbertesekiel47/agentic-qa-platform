"""One pilot attempt (ADR-0023, #51): a fresh run record, egress gate, proxy
and Playwright driver, the spec's reset hook sent on that gate, then the
strict replay. Manifest answers never enter; the caller scores the result."""

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
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.run_record import RunRecord
from aqa_runner.runner_requests import runner_request
from pilot_inputs import PilotInput
from pilot_results import Observation, observe
from playwright.async_api import async_playwright

# What stopped an attempt first: a secret that can't be bound; the reset
# refused, unreachable, not a 2xx (redirects aren't followed) or unanswered
# within resolve_seconds; a script replay refuses; the attempt outlasting
# `minutes`; or, after a replay that returned, a resource failing to close.
type Failure = Literal[
    "reset_unreachable",
    "reset_rejected",
    "reset_timeout",
    "secret_unusable",
    "script_refused",
    "operation_timeout",
    "cleanup_failed",
]
# Whether every resource opened closed: each exit returned, one raised, or
# they outlasted resolve_seconds.
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
    """A replay that returned, every resource closed, receipts written."""

    run_id: str
    record: Path
    observation: Observation
    health: AttemptHealth


@dataclass(frozen=True)
class FailedAttempt:
    """An attempt without a usable replay. Resources are `closed` only when
    its browser never opened or replay returned, and every exit returned."""

    run_id: str
    record: Path
    failure: Failure
    cleanup: Cleanup
    resources: Resources


class _RefusedError(Exception):
    """The attempt stopped for the failure its state records."""


@dataclass
class _State:
    """One attempt's progress, shared by coordinator and task; first wins."""

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
        """Note the first unexpected `error` before the resources unwind, so a
        stalled cleanup can't stand in for it. After a stop, it is cleanup."""
        if self.failure is None and self.error is None and not self.interrupted:
            self.error = error
        else:
            self.cleanup_raised = True

    def exit_failed(self) -> None:
        """An exit raised: after a replay that returned and no earlier stop,
        the failure, which a later timeout can't replace."""
        self.cleanup_raised = True
        if self.browser == "closed" and not (
            self.failure or self.error or self.interrupted
        ):
            self.failure = "cleanup_failed"

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
    resources owned by a task of its own. At `minutes`, or on an interrupt,
    the task is cancelled once. If its cleanup outlasts `resolve_seconds`,
    `cleanup-incomplete.json` says so and this waits for it however long it
    takes: nothing may act on the app while its browser may be open. Later
    cancellations of this coroutine are recorded, never passed on (a second
    Ctrl-C under `asyncio.run` is not one). The final receipt is `attempt.json`."""
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
    _check_secrets(pilot, state)
    gate = EgressGate(egress_policy(pilot.spec, pilot.config, pilot.start))
    if pilot.reset is not None:
        await _reset(gate, pilot.reset.url, pilot.config.budgets.resolve_seconds, state)
    setup = RunSetup(pilot.spec, pilot.config, pilot.start, record)
    async with (
        _owned(async_playwright(), "driver", state) as playwright,
        _owned(EgressProxy(gate), "proxy", state) as proxy,
    ):
        try:
            await register_identity_engine(playwright)
            state.browser = "unknown"
            result = await replay(
                pilot.script,
                setup,
                chromium=playwright.chromium,
                proxy=proxy,
                gate=gate,
            )
        # Replay checks the script and binds secrets before its browser starts.
        except SpecError:
            state.browser = "unopened"
            raise state.refuse("script_refused") from None
        except (MissingSecretError, SecretLoggedError, UnicodeEncodeError):
            state.browser = "unopened"
            raise state.refuse("secret_unusable") from None
        except Exception as error:
            state.latch(error)
            raise
        state.browser = "closed"
        return result


def _check_secrets(pilot: PilotInput, state: _State) -> None:
    """Refuse before the reset what replay refuses before its browser. The
    error is dropped: a value with surrogates fails to encode with a
    ValueError quoting it."""
    try:
        bound_secrets(pilot.spec, pilot.start)
    except (MissingSecretError, SecretLoggedError, ValueError):
        raise state.refuse("secret_unusable") from None
    except SpecError:
        raise state.refuse("script_refused") from None


async def _reset(gate: EgressGate, url: str, seconds: float, state: _State) -> None:
    """POST the spec's reset hook through the attempt's `gate`, so reset and
    replay share its pins. Anything but a 2xx within `seconds` stops the
    attempt. A URL no request line carries is unreachable too; its error
    quotes the URL, so it is dropped."""
    try:
        async with asyncio.timeout(seconds):
            response = await runner_request(gate, "POST", url)
    except TimeoutError:
        raise state.refuse("reset_timeout") from None
    except (EgressRefusedError, EgressUpstreamError, ValueError):
        raise state.refuse("reset_unreachable") from None
    if not 200 <= response.status < 300:
        raise state.refuse("reset_rejected")
    state.reset_completed = True


@asynccontextmanager
async def _owned[T](
    resource: AbstractAsyncContextManager[T], name: _Owned, state: _State
) -> AsyncIterator[T]:
    """`resource`, entered, then exited however the body ends; closed only
    once its exit returns. A cancel during the enter waits for it, then
    exits: Playwright's enter starts a driver whose tasks it doesn't own,
    so cutting it short would leave the driver running. It is waited for
    with asyncio.wait: a cancelled shield would log a late error's text."""
    state.opened.add(name)
    entering = asyncio.create_task(resource.__aenter__())
    try:
        await asyncio.wait({entering})
    except asyncio.CancelledError:
        await asyncio.wait({entering})
        if entering.exception() is None:
            await _exit(resource, name, state)
        raise
    try:
        value = entering.result()
    except Exception as error:
        state.latch(error)
        raise
    try:
        yield value
    finally:
        await _exit(resource, name, state)


async def _exit(
    resource: AbstractAsyncContextManager[object], name: _Owned, state: _State
) -> None:
    # Exited as on success: the state records failures; neither reads them.
    try:
        await resource.__aexit__(None, None, None)
    except Exception:
        state.exit_failed()
        raise
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
    if state.interrupted:
        _receipt(record, state, "interrupted")
        raise asyncio.CancelledError
    if state.failure is not None:
        _receipt(record, state, "failed")
        return FailedAttempt(
            record.run_id, record.path, state.failure, state.cleanup, state.resources
        )
    if (error := state.error or error) is not None:
        _receipt(record, state, "unexpected")
        raise error
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
