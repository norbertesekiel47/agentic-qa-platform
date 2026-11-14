"""Evaluating a compiled script's assertions on the page as it stands
(DATA_MODEL §7, Replay outcomes; ADR-0024's #48 amendment), a `model_verify`
check by the verifier only a verified replay gives (`Evaluation.verify`,
ADR-0024's #256 amendment), and the bounded looks the executor shares with
it: a target looked for within the run's `resolve_seconds`, a look cut off
when the page stops answering."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, assert_never, overload

from aqa_core.compiled import (
    Assertion,
    ModelVerify,
    NetworkNone,
    NetworkSeen,
    NotVisible,
    ProbeBaseline,
    ProbeEquals,
    ProbeEqualsBaseline,
    Target,
    TextInTarget,
    TextVisible,
    UrlMatches,
    VisibleUnoccluded,
)
from aqa_core.project import probe_url
from aqa_core.spec import Spec
from aqa_core.text import SearchTimeoutError
from playwright.async_api import ElementHandle, Error

from aqa_runner import network, probes, reasons
from aqa_runner.browser_session import BrowserSession, UnsupportedVisualFrameError
from aqa_runner.document_origins import DocumentChangedError, PolicyEventError
from aqa_runner.egress import EgressGate, EgressRefusedError, EgressUpstreamError
from aqa_runner.evidence import Shot
from aqa_runner.locators import Absent, Miss, Resolved, Shown, Unresolved, Use
from aqa_runner.reasons import Raised, UnansweredError
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.text_search import text_matches, url_matches

# How often resolution looks again for a target that hasn't resolved.
LOOK_SECONDS = 0.1

# How long past an action's or a look's bound the executor waits for it
# before it stops waiting: Playwright's own error, which says what it waited
# for, comes first, and this catches what Playwright doesn't time, such as a
# page script that never returns while a field is filled.
MARGIN_SECONDS = 1

# What became of an assertion: its check held, or it didn't; no locator gave
# its target the match its use needs within `resolve_seconds`; its text
# search ran out of time, which establishes neither (DATA_MODEL §7); or it
# wasn't evaluated, since a step stopped the run first or a look at the page
# raised, or, for a `model_verify` check, the verifier wasn't asked or its
# reply decided nothing (`AssertionResult.verified` says why).
type AssertionOutcome = Literal[
    "pass", "failed", "binding_unresolved", "check_timed_out", "not_evaluated"
]

# Why a `model_verify` check reached with no verifier isn't evaluated: only
# `aqa replay --mode verified` has one (ADR-0024's #256 amendment).
NO_VERIFIER: Final = "model_verify needs a verified replay"


@dataclass(frozen=True)
class VerifierSaid:
    """A verifier's reply that parsed, each text scanned and on one line: its
    verdict, the references it cites, and its own report, which decides
    nothing (CONTEXT.md, "Verdict")."""

    verdict: str
    evidence_refs: tuple[Redacted, ...]
    confidence: float
    rationale: Redacted


@dataclass(frozen=True)
class Verified:
    """What a `model_verify` check came to (ADR-0024's #256 amendment): its
    verdict, and the fixed reason of one the verifier didn't give; the
    verifier's reply, when it parsed; and the folder of the run record its
    evidence was saved in, with what became of that folder's screenshot,
    when it was saved."""

    verdict: Literal["pass", "expectation_violated", "inconclusive"]
    reason: str | None = None
    said: VerifierSaid | None = None
    folder: str | None = None
    shot: Shot | None = None


# What judges a `model_verify` check whose target resolved, in a verified
# replay (`aqa_runner.verification`): given the session, the check and the
# element its target resolved to, which it doesn't dispose of.
type Verify = Callable[
    [BrowserSession, ModelVerify, ElementHandle], Awaitable[Verified]
]

# The outcome each of a verifier's verdicts gives its check.
_VERIFIED: Final[Mapping[str, Literal["pass", "failed", "not_evaluated"]]] = {
    "pass": "pass",
    "expectation_violated": "failed",
    "inconclusive": "not_evaluated",
}


@dataclass(frozen=True)
class Evaluation:
    """What evaluating an assertion needs (`evaluate`): the script's targets;
    the spec and the run's start origin its probes are read from, through
    the run's egress gate; the run's `resolve_seconds`; the scan of every
    value the run binds; the probe baselines, captured into
    `baseline_values` as the run goes; whether every locator of a target
    must resolve at each use; and, in a verified replay, the verifier a
    `model_verify` check goes to. Captured baselines stay in memory; they
    never enter a result or the run record."""

    targets: Mapping[str, Target]
    spec: Spec
    start: str
    gate: EgressGate
    budget: float
    redactor: Redactor
    baseline_definitions: Mapping[str, ProbeBaseline] = field(default_factory=dict)
    baseline_values: dict[str, probes.JsonValue] = field(default_factory=dict)
    every_locator: bool = False
    verify: Verify | None = None

    @property
    def withheld(self) -> bool:
        """Whether reasons and invariants keep no page text and name no
        origin of ours: the run binds a test secret, filled or not. A
        result's other fields keep what ADR-0026's #50 D amendment lists."""
        return not self.redactor.empty

    def reason(self, error: Raised) -> Redacted:
        """`error` as a failed step's or an unevaluated assertion's reason
        (`aqa_runner.reasons`: scanned for every bound value, the ones the
        script never fills too). When the run binds a test secret,
        Playwright's message and every origin of ours are withheld for the
        whole run: a page can throw a value back split into pieces no scan
        finds, or choose the origin a refusal would name."""
        return reasons.reason(error, self.redactor)


@dataclass(frozen=True)
class AssertionResult:
    """One assertion. A `binding_unresolved` one has each locator's miss
    (none when no look finished). A `not_evaluated` one has `stopped_at`,
    the step that stopped the run before any assertion was evaluated, or
    `error`, what a look at the page raised (the rest after it aren't
    evaluated either). One whose target resolved, then passed, failed or
    timed out, has the index of the locator that resolved it. A
    `model_verify` check that was evaluated has what it came to,
    `verified`."""

    id: str
    outcome: AssertionOutcome
    misses: tuple[Miss, ...] = ()
    stopped_at: int | None = None
    error: str | None = None
    locator_index: int | None = None
    verified: Verified | None = None


async def evaluate(
    session: BrowserSession, check: Assertion, evaluation: Evaluation
) -> AssertionResult:
    """`check`'s outcome on the page as it stands (DATA_MODEL §7, Replay
    outcomes), evaluated once within `evaluation.budget`. First the page
    must answer one look (its visible text): a URL, a target's state,
    recorded traffic or a probe can be read from a page whose renderer has
    stopped, or that is on an origin the run doesn't allow, and none is
    evaluated then. A look that raises leaves `check` not evaluated, with
    the reason.

    `evaluation` must scan with `session`'s redactor, the run's, or this
    raises `ValueError`: its reasons withhold by that scan."""
    if evaluation.redactor is not session.redactor:
        raise ValueError("an evaluation scans with its session's redactor")
    budget = evaluation.budget
    try:
        await bounded(looked(session.visible_text, budget), budget)
        held = await _held(session, check, evaluation, budget)
    except SearchTimeoutError, probes.ProbeUnstableError:
        return AssertionResult(check.id, "check_timed_out")
    except (
        Error,
        PolicyEventError,
        DocumentChangedError,
        UnansweredError,
        probes.ProbeError,
        EgressRefusedError,
        EgressUpstreamError,
        network.WindowsOverflowError,
        network.UnrecordedRequestError,
        UnsupportedVisualFrameError,
    ) as error:
        return AssertionResult(
            check.id, "not_evaluated", error=evaluation.reason(error)
        )
    if isinstance(held, Unresolved):
        return AssertionResult(check.id, "binding_unresolved", held.misses)
    if isinstance(held, Verified):
        return AssertionResult(check.id, "not_evaluated", verified=held)
    if isinstance(held, _Located):
        return AssertionResult(
            check.id,
            held.outcome,
            locator_index=held.locator_index,
            verified=held.verified,
        )
    return AssertionResult(check.id, "pass" if held else "failed")


async def evaluate_each(
    session: BrowserSession, checks: list[Assertion], evaluation: Evaluation
) -> tuple[AssertionResult, ...]:
    """Every assertion, in order, each evaluated once (`evaluate`), as the
    last step left the page; each gets the run's `resolve_seconds` of its
    own. Once an assertion's look at the page raises, what remains isn't
    evaluated, and each says which assertion's look it was."""
    results: list[AssertionResult] = []
    raised: str | None = None
    for check in checks:
        if raised is not None:
            reason = evaluation.redactor.redact(
                f"not evaluated after {raised}'s look at the page raised"
            )
            results.append(AssertionResult(check.id, "not_evaluated", error=reason))
            continue
        result = await evaluate(session, check, evaluation)
        if result.error is not None:
            raised = check.id
        results.append(result)
    return tuple(results)


@dataclass(frozen=True)
class _Located:
    """A target's check once a locator resolved the target: its outcome,
    that locator's index, and, for a `model_verify` check, what the verifier
    came to."""

    outcome: Literal["pass", "failed", "check_timed_out", "not_evaluated"]
    locator_index: int
    verified: Verified | None = None


async def _held(
    session: BrowserSession,
    check: Assertion,
    evaluation: Evaluation,
    budget: float,
) -> bool | _Located | Unresolved | Verified:
    """Whether `check` holds, through the session's observations and the
    bounded text search, or the drift that kept its target from being found.
    What it looks at on the page takes at most `budget` seconds and
    `MARGIN_SECONDS` past them, but for the verifier's own look and call,
    which bound themselves (`aqa_runner.verification`)."""
    match check:
        case TextVisible() | TextInTarget():
            return await _text_held(session, check, evaluation)
        case UrlMatches(pattern=pattern):
            return await url_matches(pattern, await bounded(session.url(), budget))
        case NotVisible(target=name):
            return await bounded(_absent(session, evaluation, name), budget)
        case VisibleUnoccluded() | ModelVerify():
            return await _seen(session, evaluation, check)
        case NetworkNone() | NetworkSeen():
            return await session.network_held(check)
        case ProbeEquals() | ProbeEqualsBaseline():
            expected: probes.JsonValue
            if isinstance(check, ProbeEquals):
                path, expected = check.json_path, check.value
            else:
                path = evaluation.baseline_definitions[check.probe].json_path
                expected = evaluation.baseline_values[check.probe]
            value = await _read_probe(evaluation, check.probe, path)
            return probes.same(value, expected)
        case _:
            assert_never(check)


async def _seen(
    session: BrowserSession,
    evaluation: Evaluation,
    check: VisibleUnoccluded | ModelVerify,
) -> _Located | Unresolved | Verified:
    """A visual check of a target: deterministic, bounded by the run's
    `resolve_seconds`, or the verifier's (`_verified`)."""
    if isinstance(check, ModelVerify):
        return await _verified(session, evaluation, check)
    return await bounded(_visual(session, evaluation, check), evaluation.budget)


async def _verified(
    session: BrowserSession, evaluation: Evaluation, check: ModelVerify
) -> _Located | Unresolved | Verified:
    """What the verifier came to on `check`'s target, resolved as a
    `text_in_target` target is; the drift that kept it from being found,
    with no evidence and no call; or, with no verifier, why it isn't
    evaluated, never a pass."""
    if evaluation.verify is None:
        return Verified("inconclusive", NO_VERIFIER)
    budget = evaluation.budget
    found = _answered(
        await bounded(look_for(session, evaluation, check.target, "assertion"), budget),
        budget,
    )
    if not isinstance(found, Resolved):
        return found
    try:
        verified = await evaluation.verify(session, check, found.element)
    finally:
        await found.element.dispose()
    return _Located(_VERIFIED[verified.verdict], found.locator_index, verified)


async def _text_held(
    session: BrowserSession, check: TextVisible | TextInTarget, evaluation: Evaluation
) -> bool | _Located | Unresolved:
    budget = evaluation.budget
    if isinstance(check, TextVisible):
        seen = await bounded(looked(session.visible_text, budget), budget)
        return await text_matches(check, seen)
    found = await bounded(_target_text(session, evaluation, check.target), budget)
    if isinstance(found, Unresolved):
        return found
    text, index = found
    try:
        held = await text_matches(check, text)
    except SearchTimeoutError:  # after resolution: the timeout keeps the index
        return _Located("check_timed_out", index)
    return _Located("pass" if held else "failed", index)


async def _visual(
    session: BrowserSession, evaluation: Evaluation, check: VisibleUnoccluded
) -> _Located | Unresolved:
    found = _answered(
        await look_for(session, evaluation, check.target, "assertion"),
        evaluation.budget,
    )
    if not isinstance(found, Resolved):
        return found
    try:
        held = await session.unoccluded(
            found.element, check.min_size_px, check.in_viewport
        )
    finally:
        await found.element.dispose()
    return _Located("pass" if held else "failed", found.locator_index)


async def _read_probe(evaluation: Evaluation, name: str, path: str) -> probes.JsonValue:
    try:
        return await probes.read_stable(
            evaluation.gate, probe_url(evaluation.spec, name, evaluation.start), path
        )
    except ValueError:
        raise probes.ProbeError("the probe could not be read") from None


async def capture_baselines(evaluation: Evaluation, seq: int) -> None:
    for name, definition in evaluation.baseline_definitions.items():
        if definition.capture_before_seq == seq:
            evaluation.baseline_values[name] = await _read_probe(
                evaluation, name, definition.json_path
            )


async def bounded[T](look: Awaitable[T], budget: float) -> T:
    """`look`'s result, or `UnansweredError` when the page hasn't answered
    within `budget` seconds and `MARGIN_SECONDS` past them."""
    seconds = budget + MARGIN_SECONDS
    limit = asyncio.timeout(seconds)
    try:
        async with limit:
            return await look
    except TimeoutError:
        if not limit.expired():
            raise  # not the look's own limit
        raise UnansweredError(seconds) from None


async def looked[T](look: Callable[[], Awaitable[T]], budget: float) -> T:
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
    session: BrowserSession, evaluation: Evaluation, name: str
) -> tuple[str, int] | Unresolved:
    """The rendered text of the target called `name` and the index of the
    locator that found it, or why it wasn't found within the evaluation's
    budget."""
    found = _answered(
        await look_for(session, evaluation, name, "assertion"), evaluation.budget
    )
    if not isinstance(found, Resolved):
        return found
    try:
        return await session.text_of(found.element), found.locator_index
    finally:
        await found.element.dispose()


def _answered[R: Resolved | Shown | Absent | Unresolved](found: R, budget: float) -> R:
    """`found`, unless no look at the page finished within `budget`: that
    is a page that didn't answer, not drift (every target has a locator, so
    a finished look leaves a miss)."""
    if isinstance(found, Unresolved) and not found.misses:
        raise UnansweredError(budget)
    return found


async def _absent(
    session: BrowserSession, evaluation: Evaluation, name: str
) -> bool | _Located | Unresolved:
    """Whether the target called `name` is absent: looked at once, so a
    target that is there fails at once (ADR-0024's #46 amendment), and only
    drift is waited out, within the evaluation's budget."""
    found = _answered(
        await look_for(session, evaluation, name, "negative_check"), evaluation.budget
    )
    if isinstance(found, Resolved):
        await found.element.dispose()
    if isinstance(found, Resolved | Shown):
        return _Located("failed", found.locator_index)
    return found if isinstance(found, Unresolved) else True


@overload
async def look_for(
    session: BrowserSession,
    evaluation: Evaluation,
    name: str,
    use: Literal["action", "assertion"],
) -> Resolved | Unresolved: ...


@overload
async def look_for(
    session: BrowserSession,
    evaluation: Evaluation,
    name: str,
    use: Literal["negative_check"],
) -> Resolved | Shown | Absent | Unresolved: ...


async def look_for(
    session: BrowserSession, evaluation: Evaluation, name: str, use: Use
) -> Resolved | Shown | Absent | Unresolved:
    """The element for `use` of the target called `name`, from all its
    locators when `evaluation.every_locator`, looked for every
    `LOOK_SECONDS` while it doesn't resolve, until the evaluation's budget
    has passed: then the last look's misses, or none when no look finished.

    Each look is cut off when the budget runs out, so a look that waits for
    good, as `is_enabled` can on an element moved into another document
    (ADR-0025's #52 amendment), can't hold the run. A look the page changed
    under (`DocumentChangedError`) is made again."""
    target, budget = evaluation.targets[name], evaluation.budget
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    last = Unresolved(())
    while loop.time() < deadline:
        limit = asyncio.timeout_at(deadline)
        try:
            async with limit:
                found = await (
                    session.resolve(target, use, every=True)
                    if evaluation.every_locator
                    else session.resolve(target, use)
                )
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
