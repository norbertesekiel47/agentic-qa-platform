"""The seam between a replay and its repair (ADR-0024's #242 and #244
amendments): what a drift hands over, every check of the script a repair
offers, and the record of each handoff. The executor decides when something
drifted; `Handing` does the rest."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from aqa_core.compiled import (
    ByRole,
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    Select,
    Step,
    Target,
    TextInTarget,
    VisibleUnoccluded,
)
from aqa_core.config import ProjectConfig
from aqa_core.heal_patch import heal_patch_problems, pointer
from aqa_core.project import SpecError, parse_compiled
from aqa_core.spec import Spec
from playwright.async_api import ElementHandle, Error, Page
from pydantic_core import PydanticSerializationError

from aqa_runner import reasons
from aqa_runner.admission import Targeted, accepted
from aqa_runner.binding import is_among, same_element
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import DocumentChangedError, PolicyEventError
from aqa_runner.evaluation import MARGIN_SECONDS, bounded
from aqa_runner.locators import Miss, Resolved, resolve
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.run_record import RefusedWriteError, RunRecord, require_clean
from aqa_runner.secret_fields import SecretNotFilledError, SecretRefusedError
from aqa_runner.settling import Settled, Window

# Why a step through a rebound target didn't run, in fixed words (ADR-0024's
# #244 amendment): the page chooses what its elements say, never these.
_OTHER_ROLE: Final = (
    "a repair rebound this step's target to an element without its committed "
    "role, so the step did not run"
)
_OTHER_ROLE_CHECKED: Final = (
    "a repair may bind this check's target only to an element with its committed role"
)
_TAKES_NO_TEXT: Final = (
    "a repair rebound this step's target to an element that takes no text, so "
    "the step did not run"
)
_NOT_A_SELECT: Final = (
    "a repair rebound this step's target to an element that is no select "
    "control, so the step did not run"
)
_UNANCHORED: Final = (
    "a repair rebound this step's target to an element no committed locator "
    "finds alone, so the step did not run"
)
_BROKE: Final = (
    "the page broke the checks on this step's rebound target, so it did not run"
)
# Why a step a repair inserted failed once it settled, in fixed words
# (ADR-0024's #246 amendment), each missing piece of evidence in this order.
# Three are explore's words for the same evidence (`aqa_core.compiler`).
_UNSHOWN: Final = "a repair inserted this step, and it didn't show it changed nothing: "
_BUSY: Final = "settle: the page was still busy when settling's wait ran out"
_UNHEARD: Final = (
    "network: the egress proxy didn't hear every write from the step's dispatch on"
)
_WROTE: Final = "network: a write request"
_TUNNEL: Final = (
    "network: an encrypted or WebSocket connection was open, which the egress "
    "proxy can't read"
)
_NAVIGATED: Final = "page: the page's main frame navigated"
_UNKEPT: Final = "settle: the step kept no settle window or settling to read"


@dataclass(frozen=True)
class StepAt:
    """A drifted step: its target didn't resolve, so nothing was dispatched."""

    step: Targeted

    @property
    def key(self) -> str:
        """What its record is named by: `repairs/<seq>.json`."""
        return str(self.step.seq)

    @property
    def target(self) -> str:
        return self.step.target


@dataclass(frozen=True)
class AssertionAt:
    """A drifted positive check, and its index in the script's assertions."""

    index: int
    check: TextInTarget | VisibleUnoccluded

    @property
    def key(self) -> str:
        """What its record is named by, since a check's id may hold `/`:
        `repairs/assertion-<index>.json`."""
        return f"assertion-{self.index}"

    @property
    def target(self) -> str:
        return self.check.target


# Where a replay paused for its repair.
type At = StepAt | AssertionAt


@dataclass(frozen=True)
class Drift:
    """Where a replay paused, for its repair: a deep copy of what drifted;
    each locator's miss; a deep copy of the script in use, the committed one
    or an earlier repair's, so an edit to either reaches nothing replay holds;
    the live session, which the repair observes through and dispatches nothing
    with; and whether something has interrupted the run by now."""

    at: At
    misses: tuple[Miss, ...]
    script: CompiledScript
    session: BrowserSession
    interrupted: Callable[[], bool]


@dataclass(frozen=True)
class ModelReported:
    """What a model said of its own pick: not calibrated, and never what
    decides a verdict (CONTEXT.md, "Verdict")."""

    confidence: float
    rationale: Redacted


@dataclass(frozen=True)
class Basis:
    """A repair's account of what it offers, each text scanned by the repair:
    why it gave up, or what it bound; what its model said of that, if a model
    picked it; a stop of the whole run it hit, if any; and the folder of the
    run record its own files are in, if it wrote any."""

    said: Redacted
    model_reported: ModelReported | None = None
    stop: Literal["no_response", "record_error"] | None = None
    record: str | None = None


@dataclass(frozen=True)
class Offer:
    """What a repair offers for a drift: the whole script to go on with, and
    its account of it. The script it was handed gives up, and what drifted
    stays drifted."""

    script: CompiledScript
    basis: Basis | None = None


# What a replay hands a drift to.
type Repair = Callable[[Drift], Awaitable[Offer]]


@dataclass(frozen=True)
class Repaired:
    """One drift's repair, as `repairs/<at.key>.json` records it: what
    drifted and its misses; why replay refused the script offered, each
    problem scanned, none when it was taken; and the script the run went on
    with, as replay parsed it, None when refused. A repair that offers the
    script it was handed gives up: neither problems nor a script. `basis` is
    the repair's account of what it offered."""

    at: At
    misses: tuple[Miss, ...]
    problems: tuple[Redacted, ...]
    script: CompiledScript | None
    basis: Basis | None = None


@dataclass(frozen=True)
class Handing:
    """One run's handoffs to its repair: the repair; `committed`, replay's
    own copy of the script it was given, which nothing handed to a repair
    holds, since the session holds its browser settings; what the checks and
    the record need; whether something interrupted the run; and each handoff
    so far, in order."""

    repair: Repair
    committed: CompiledScript
    record: RunRecord
    config: ProjectConfig
    spec: Spec
    session: BrowserSession
    interrupted: Callable[[], bool]
    repaired: list[Repaired] = field(default_factory=list)

    def handed(self, at: At) -> bool:
        """Whether `at` has had its one repair."""
        return any(repaired.at.key == at.key for repaired in self.repaired)

    async def hand(
        self,
        at: At,
        misses: tuple[Miss, ...],
        in_use: CompiledScript,
        ran: Sequence[Step],
    ) -> Repaired:
        """Give the repair a `Drift` for `at`, with deep copies of it and of
        `in_use`, then check and record what it offers (`_checked`). What the
        repair raises leaves as any failure of the run does. A target a step in
        `ran` acted through goes to no repair and is recorded as refused: no
        offer could change it (`_ran_problems`), so nothing is spent asking."""
        if at.target in _acted_through(ran):
            problem = (
                f"/targets/{pointer(at.target)}: a step that already ran acted "
                "through this target, so it goes to no repair"
            )
            repaired = self._refused(at, misses, [problem])
        else:
            offered = await self.repair(
                Drift(
                    deepcopy(at),
                    misses,
                    in_use.model_copy(deep=True),
                    self.session,
                    self.interrupted,
                )
            )
            repaired = await self._checked(at, misses, offered, in_use, ran)
        self.repaired.append(repaired)
        return repaired

    async def _checked(
        self,
        at: At,
        misses: tuple[Miss, ...],
        offered: Offer,
        in_use: CompiledScript,
        ran: Sequence[Step],
    ) -> Repaired:
        """The repair of `at`, whose repair offered `offered`, recorded in
        `repairs/<at.key>.json`: taken, as the loader reads its JSON, once
        nothing refuses it, or given up, with neither problems nor a script,
        when the loader reads it as `in_use`, the script the repair was
        handed a copy of. The first of these to find problems refuses it, and
        each problem is scanned: its JSON, which must hold only values its
        fields' types allow (refused unquoted) and no bound value's spelling;
        the loader's rules, which a `model_copy` skips; the heal-patch
        validator, against `committed`; what ran stays as it ran
        (`_ran_problems`), since the run goes on after it; replay's own
        admission (`accepted`); and the record, which checks the document as
        written and refuses one that spells a bound value, so nothing runs
        that isn't recorded. The refusal is recorded through the same check,
        which raises `RefusedWriteError` when the document still spells a
        value once its problems are scanned: a miss or a scan's marker can.
        The validator searches text checks' patterns in a child process, so it
        runs off the loop, which keeps the proxy serving."""
        record = self.record
        name = f"repairs/{at.key}.json"
        clean = require_clean(record.redactor, name)
        problems: Sequence[str]
        try:
            # A field of a type `model_copy` let through makes the serializer
            # warn, quoting its value: raised here, and its text never kept.
            text = offered.script.model_dump_json(warnings="error")
            # Before any problem names a part of it: a validator's pointer
            # escapes `/` and `~` (RFC 6901), a spelling no scan finds.
            clean(text)
            patched = parse_compiled(text, self.config, source=Path(name))
            if patched.model_dump(mode="json") == in_use.model_dump(mode="json"):
                gave_up = Repaired(at, misses, (), None, offered.basis)
                record.write(name, _repair_record(gave_up), check=clean)
                return gave_up
            problems = await asyncio.to_thread(
                heal_patch_problems, self.committed, patched
            )
            if not problems:
                problems = _ran_problems(patched, in_use, ran, at)
            if not problems:
                accepted(patched, self.spec)
                taken = Repaired(at, misses, (), patched, offered.basis)
                record.write(name, _repair_record(taken), check=clean)
                return taken
        except PydanticSerializationError:
            problems = [
                f"{name}: a field holds a value its type doesn't allow, so it isn't read"
            ]
        except SpecError as error:
            problems = error.problems
        except RefusedWriteError as error:
            # What it said may be what the record refused, so none of it stays.
            return self._refused(at, misses, [str(error)])
        return self._refused(at, misses, problems, offered.basis)

    def _refused(
        self,
        at: At,
        misses: tuple[Miss, ...],
        problems: Sequence[str],
        basis: Basis | None = None,
    ) -> Repaired:
        """`at`'s repair refused, each of `problems` scanned, and recorded, with
        the repair's account of what it offered, if it gave one."""
        name = f"repairs/{at.key}.json"
        redactor = self.record.redactor
        scanned = tuple(redactor.redact(problem) for problem in problems)
        refused = Repaired(at, misses, scanned, None, basis)
        self.record.write(
            name, _repair_record(refused), check=require_clean(redactor, name)
        )
        return refused


async def rebind_refusal(
    session: BrowserSession,
    step: Targeted | None,
    element: ElementHandle,
    committed: Target,
    *,
    secret: BoundSecret | None,
    budget: float,
) -> Redacted | None:
    """Why `step` may not act on `element`, the element a target a repair
    rebound now resolves to, or None, read before its intent, so a refusal
    leaves nothing unresolved and the run stays resumable (ADR-0024's #244
    amendment): the role `committed`, the target as committed, says, when its
    locators name one; a `fill` or `fill_secret` needs an editable element, and
    a `select` a select control; a side-effect or `fill_secret` step needs the
    anchor (`_found_alone`); and a `fill_secret` needs its binding
    (`BrowserSession.secret_refusal`), last, since it holds the session's lock
    itself. It keeps a test secret from the wrong element and a repair from
    acting on one the committed locators can't vouch for. A look that raises
    or that the page doesn't answer refuses it too, with a reason from the
    run's closed vocabulary (`aqa_runner.reasons`) or fixed words. With no
    `step`, `element` is for a check's target, and only its role is read."""
    redactor = session.redactor
    refused: str | None = None
    try:
        async with session.observing_page() as page:
            refused = await cut_off(_refusal(page, step, element, committed), budget)
        if refused is None and secret is not None:
            # Safe under `bounded` only because this call lets go of what it
            # holds with `dispose`, which a busy page answers. A hold released
            # by a read would need `cut_off`, but `cut_off` would leave
            # `session._turn` held by the abandoned task.
            await bounded(session.secret_refusal(element, secret), budget)
    except (
        PolicyEventError,
        DocumentChangedError,
        UnansweredError,
        SecretRefusedError,
        SecretNotFilledError,
    ) as error:
        return reasons.reason(error, redactor)
    except Error:
        # Playwright's general error type: the page broke a read, or closed.
        refused = _BROKE
    return None if refused is None else redactor.redact(refused)


async def _refusal(
    page: Page, step: Targeted | None, element: ElementHandle, committed: Target
) -> str | None:
    """The checks that read the page: the element's role, its kind for
    `step`'s action, and the anchor. Each reads the element as it is now: a
    page can change it while a repair works, and any repair can offer it."""
    role = next(
        (each.role for each in committed.locators if isinstance(each, ByRole)), None
    )
    if role is not None and not await is_among(page.get_by_role(role), element):
        return _OTHER_ROLE_CHECKED if step is None else _OTHER_ROLE
    if step is None:
        return None
    if isinstance(step, Fill | FillSecret) and not await _editable(element):
        return _TAKES_NO_TEXT
    if isinstance(step, Select) and not await is_among(
        page.locator("css=select"), element
    ):
        return _NOT_A_SELECT
    if (step.side_effect or isinstance(step, FillSecret)) and not await _found_alone(
        page, element, committed
    ):
        return _UNANCHORED
    return None


async def _editable(element: ElementHandle) -> bool:
    """Whether `element` takes text now. Playwright raises for a kind that
    never can, which is the same answer."""
    try:
        return await element.is_editable()
    except Error:
        return False


async def _found_alone(page: Page, element: ElementHandle, committed: Target) -> bool:
    """The anchor: whether some committed locator, alone, still finds `element`
    for an action, inside the target's region when it has a contract."""
    for locator in committed.locators:
        alone = committed.model_copy(update={"locators": (locator,)})
        found = await resolve(page, alone, "action")
        if isinstance(found, Resolved):
            try:
                if await same_element(element, found.element):
                    return True
            finally:
                await found.element.dispose()
    return False


def inserted_failure(
    window: Window | None, settled: Settled | None, redactor: Redactor
) -> Redacted | None:
    """Why a step a repair inserted, whose action's settle window is `window`
    and whose settling ended `settled`, didn't show it changed nothing, or None
    when it did (ADR-0024's #246 amendment): settling ended `idle`, the egress
    proxy heard every write from the window's opening on and read none, no
    tunnel was open, and the page's main frame didn't navigate. Each that
    fails is named, scanned with `redactor`, the session's. A step that kept
    no window or no settling fails with that alone, since nothing else can be
    read: missing evidence never passes it. Only the step's own window
    counts: the one before belongs to the step that opened it, and later ones
    open after the next step ran."""
    if window is None or settled is None:
        return redactor.redact(_UNSHOWN + _UNKEPT)
    unshown = [
        phrase
        for phrase, holds in (
            (_BUSY, settled != "idle"),
            (_UNHEARD, not window.heard),
            (_WROTE, window.wrote),
            (_TUNNEL, window.tunnelled),
            (_NAVIGATED, window.navigated),
        )
        if holds
    ]
    return redactor.redact(_UNSHOWN + "; ".join(unshown)) if unshown else None


async def cut_off[T](look: Coroutine[Any, Any, T], budget: float) -> T:
    """`look`'s result, or `UnansweredError` when the page hasn't answered
    within `budget` seconds and `MARGIN_SECONDS` past them. `evaluation.bounded`
    waits for a look it cancels to finish, and a look that holds an element or
    a region lets go of it with one more read, which a page that stopped
    answering never answers (`binding.same_element`, `binding.held_region`). So
    `look` runs as a task of its own, which nothing waits for once it is cut
    off; it ends when the session's browser closes."""
    seconds = budget + MARGIN_SECONDS
    task = asyncio.ensure_future(look)
    # So a look cut off that ends in an error logs nothing when it ends.
    task.add_done_callback(lambda ended: ended.cancelled() or ended.exception())
    try:
        await asyncio.wait({task}, timeout=seconds)
    finally:
        task.cancel()
    if not task.done():
        raise UnansweredError(seconds)
    return task.result()


def _acted_through(ran: Sequence[Step]) -> set[str]:
    """The targets `ran`, the steps that ran, acted through."""
    return {
        step.target
        for step in ran
        if isinstance(step, Click | Fill | FillSecret | Select)
    }


def _ran_problems(
    patched: CompiledScript, in_use: CompiledScript, ran: Sequence[Step], at: At
) -> list[str]:
    """Why `patched` changes what ran, `ran` as `in_use` holds its steps. The
    validator reads only the committed script, so this also keeps an earlier
    repair's inserted steps that ran as they ran: what the record holds as run
    must be what ran. Every step has run by the time an assertion drifts, so
    its repair inserts none. A target a step acted through stays as that step
    found it, compared with `in_use`, so an earlier repair's binding may stay:
    otherwise the run's script would hold locators no step ran through."""
    kept = patched.steps[: len(ran)]
    if [step.seq for step in kept] != [step.seq for step in ran]:
        return ["/steps: a repair inserts no step before a step that already ran"]
    problems = [
        f"/steps/{index}: a repair rewrites no step that already ran"
        for index, (now, was) in enumerate(zip(kept, ran, strict=True))
        if now.model_dump(mode="json") != was.model_dump(mode="json")
    ]
    if isinstance(at, AssertionAt) and len(patched.steps) > len(ran):
        problems.append(
            "/steps: an assertion's repair inserts no step: every step has run"
        )
    problems.extend(
        (
            f"/targets/{pointer(name)}: a repair rebinds no target a step that "
            "already ran acted through"
        )
        for name in sorted(_acted_through(ran))
        if patched.targets[name] != in_use.targets[name]
    )
    return problems


def _repair_record(repaired: Repaired) -> dict[str, object]:
    at, script, basis = repaired.at, repaired.script, repaired.basis
    subject = (
        {"seq": at.step.seq} if isinstance(at, StepAt) else {"assertion": at.check.id}
    )
    return {
        **subject,
        "misses": list(repaired.misses),
        "problems": list(repaired.problems),
        "script": None
        if script is None
        else script.model_dump(mode="json", exclude_none=True),
        **({} if basis is None else {"basis": _basis_record(basis)}),
    }


def _basis_record(basis: Basis) -> dict[str, object]:
    reported = basis.model_reported
    return {
        "said": basis.said,
        "model_reported": None
        if reported is None
        else {"confidence": reported.confidence, "rationale": reported.rationale},
        "stop": basis.stop,
    }
