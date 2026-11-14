"""A healer's turns on one drift (ADR-0024's #244 amendment; ADR-0006): a
LangGraph graph per drift, whose state is plain data, with what the turns use in
its context, never in state. So no snapshot enters a checkpoint: the only text
that does is what the last rebind did, scanned, which can quote a page's words
when a look's error does.

Each turn looks at the page once (`session.snapshot`), sends the model the
target's meaning, its locators and that snapshot (`healer.heal_request`), and
takes back one element's ref. Deterministic code checks that element
(`repairs.rebind_refusal`, the gate's early copy; the seam reads it again before
any dispatch), then the generator writes its locators inside the target's
contract. The model never writes a locator, a step or an assertion.

A tree turn whose reply calls no tool falls back to vision once (ADR-0002's #252
amendment): one screenshot goes to the `vision_fallback` role, which names a
point, and the element there, read without a click, goes through the same checks
and generator. A canvas can't be confirmed, and the heal ends after that turn.

Each priced response is on disk in `costs.jsonl` before the next call, and each
request sent leaves `heals/<key>/<n>.json`, or `vision.json`, whatever came
back, unless its cost line isn't on disk or the call is cancelled. `Healing`
holds one run's budget, so a run's drifts share it."""

import hashlib
import json
import struct
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, TypedDict

from aqa_core.compiled import Locator, NotVisible, TextInTarget, VisibleUnoccluded
from aqa_core.config import ModelRoleName
from aqa_core.heal_patch import literal_claim_spelled, text_claims
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from playwright.async_api import ElementHandle, Error, Page

from aqa_runner import reasons
from aqa_runner.anthropic_client import ProviderError
from aqa_runner.binding import is_among
from aqa_runner.browser_session import BrowserSession, RefError
from aqa_runner.document_origins import DocumentChangedError, PolicyEventError
from aqa_runner.evaluation import bounded, looked
from aqa_runner.evidence import capture_evidence
from aqa_runner.healer import Rebind, Unfit, decided, heal_request, healer_tools
from aqa_runner.locator_generation import (
    BindingRefusedError,
    LocatorError,
    Seen,
    TargetUses,
    seen_element,
)
from aqa_runner.model_router import ModelCallError, ModelRouter, Routed
from aqa_runner.model_turns import (
    OVER,
    UNRECORDED,
    UNSENT,
    Spending,
    UnsentRequestError,
    no_response,
    unspelled,
)
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import REASON_CHARS, Redacted, Redactor
from aqa_runner.repairs import (
    Basis,
    Drift,
    ModelReported,
    Offer,
    StepAt,
    cut_off,
    rebind_refusal,
)
from aqa_runner.run_record import RefusedWriteError, UnrecordedCostError, require_clean
from aqa_runner.screenshots import (
    Screenshot,
    ScreenshotWithheldError,
    take_screenshot,
)
from aqa_runner.vision_fallback import (
    VisionClick,
    clicked,
    vision_request,
    vision_tools,
)

# Why a heal stopped, in fixed words.
_FOUND_NONE: Final = "the healer found no element for the meaning"
_REFUSED: Final = "the model refused"
_NO_SPEND: Final = "the spend budget ran out"
_UNWRITTEN: Final = "the run record took no more lines, so the heal stopped"
_INTERRUPTED: Final = "the run was interrupted, so the heal stopped"
# Why vision fallback stopped, in fixed words (ADR-0002's #252 amendment). A
# render after a fill has no canvas (ADR-0034), so it can't show what the tree
# missed.
_VISION: Final = "vision fallback: "
_FILLED: Final = (
    f"{_VISION}a secret was filled, so a screenshot would be a render without "
    "the page's canvas, and none was taken"
)
_OUTSIDE: Final = (
    f"{_VISION}the point is outside the viewport, so nothing was read there"
)
_NOTHING_THERE: Final = f"{_VISION}no element is at the point"
_A_CANVAS: Final = (
    f"{_VISION}the element at the point is a canvas, and no locator names a point "
    "inside one, so the rebind can't be confirmed"
)
# The element at a point of the main frame's viewport, read in the page's
# world, which dispatches no event.
_AT_POINT: Final = "([x, y]) => document.elementFromPoint(x, y)"
# The refusals that say the page has no place for this subject, whichever
# element the model picks (#229). `outside_region` and the rest are about the
# pick, so another turn may find a better one.
_PAGE_LEVEL: Final = frozenset(
    {"region_absent", "region_ambiguous", "part_absent", "part_ambiguous"}
)
# What a heal that gave up adds once a pick was outside the subject's region. It
# may be a subject that moved or only a wrong pick, and telling them apart would
# take another read of the page, so the words say `if` (#229).
_OUTSIDE_ITS_REGION: Final = (
    "the subject contract refused a pick outside its region, "
    "so if the subject moved, a reviewed config edit is needed"
)
# A locator string past this is no stable name: a page can write any id.
_LONGEST_STRING: Final = 100


class _Turns(TypedDict):
    """The graph's state, plain data a checkpoint keeps: this drift's turns
    taken, what the last rebind did, scanned, and whether the turns are over."""

    turn: int
    results: str
    done: bool


@dataclass(frozen=True)
class _Stop:
    """Why the heal stopped, and the stop of the whole run it hit, if any."""

    why: str
    run: Literal["no_response", "record_error"] | None = None


@dataclass(frozen=True)
class _Call:
    """A turn's call: the role it asks, the tools it offers, the drift's turn
    it is, and the record it leaves, `heals/<key>/<name>.json`, which also
    holds `holds`."""

    role: ModelRoleName
    tools: tuple[BaseTool, ...]
    turn: int
    name: str
    holds: Mapping[str, object]


@dataclass
class Healing:
    """One run's healer: its router, its spend, its turns and the bound on each
    look at the page. `repair` is the `Repair` a replay hands each drift to.
    Spend and turns are the run's, shared by all its drifts: a turn is one
    `router.call`, a refusal's fallback included, and spend is checked after
    each priced response, so a run can spend up to its budget and one call's
    responses."""

    router: ModelRouter
    spending: Spending
    turns: int
    seconds: float
    taken: int = 0

    async def repair(self, drift: Drift) -> Offer:
        """What the turns on `drift` offer (module docstring): the script it was
        handed, with only the drifted target's locators replaced, or that
        script, which gives up. Only defects and cancellation raise."""
        context = _Heal(self, drift)
        graph = StateGraph(_Turns, context_schema=_Heal)
        graph.add_node("turn", _turn)
        graph.add_edge(START, "turn")
        graph.add_conditional_edges("turn", _next)
        await graph.compile(checkpointer=InMemorySaver()).ainvoke(
            _Turns(turn=0, results="", done=False),
            # One superstep per turn, and one to stop: the budget stops the
            # turns, never LangGraph's limit, which an environment can lower.
            {
                "configurable": {
                    "thread_id": f"{self.spending.record.run_id}/{drift.at.key}"
                },
                "recursion_limit": self.turns - self.taken + 2,
            },
            context=context,
        )
        return context.offer


@dataclass
class _Heal:
    """The graph's context: what the turns on one drift use, never in state, and
    what they offer, the script handed until a turn decides, which gives up. It
    remembers whether a pick was outside the subject's region, for the reason."""

    healing: Healing
    drift: Drift
    offer: Offer = field(init=False)
    observed: bool = field(init=False, default=False)
    outside_region: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.offer = Offer(self.drift.script)

    @property
    def session(self) -> BrowserSession:
        return self.drift.session

    @property
    def redactor(self) -> Redactor:
        return self.drift.session.redactor

    @property
    def folder(self) -> str:
        return f"heals/{self.drift.at.key}"

    def line(self, text: str) -> Redacted:
        """`text`, which the page or the model chose, scanned, on one line and
        cut, as a record keeps it."""
        scanned = self.redactor.redact(text)
        return self.redactor.redact(" ".join(scanned.split()), limit=REASON_CHARS)

    def gave_up(self, stop: _Stop) -> Offer:
        # A stop of the whole run is the model's or the record's: no config edit.
        adds = self.outside_region and stop.run is None
        said = self.redactor.redact(
            f"{stop.why}; {_OUTSIDE_ITS_REGION}" if adds else stop.why
        )
        record = self.folder if self.observed else None
        return Offer(self.drift.script, Basis(said, None, stop.run, record))


def _next(state: _Turns) -> str:
    return END if state["done"] else "turn"


async def _turn(state: _Turns, runtime: Runtime[_Heal]) -> _Turns:
    context = runtime.context
    taken = await _taken(context, state)
    if isinstance(taken, _Stop):
        context.offer = context.gave_up(taken)
    elif isinstance(taken, Offer):
        context.offer = taken
    else:
        return taken
    return state | {"done": True}


async def _taken(context: _Heal, state: _Turns) -> _Turns | Offer | _Stop:
    """The turn's new state, or what ends the turns."""
    if (stop := _spent(context)) is not None:
        return stop
    snapshot = await _observed(context)
    if isinstance(snapshot, _Stop):
        return snapshot
    reply = await _asked(context, state, snapshot)
    if isinstance(reply, _Stop):
        return reply
    call = decided(reply)
    if call is None:
        return await _looked_at(context, state["turn"] + 2)
    results = (
        call.why if isinstance(call, Unfit) else await _picked(context, call, snapshot)
    )
    if isinstance(results, str):
        return _Turns(turn=state["turn"] + 1, results=results, done=False)
    return results


def _spent(context: _Heal) -> _Stop | None:
    """Why no further turn may start: the run was interrupted, its turns are
    all taken, or no spend is left."""
    healing = context.healing
    if context.drift.interrupted():
        return _Stop(_INTERRUPTED)
    if healing.taken >= healing.turns:
        return _Stop(f"the turn budget ({healing.turns}) ran out")
    if healing.spending.left <= 0:
        return _Stop(_NO_SPEND)
    return None


async def _observed(context: _Heal) -> Redacted | _Stop:
    """The page's snapshot, looked at again while the page changes under the
    look, or why it couldn't be looked at; the first is also the heal's
    evidence, whose logs are empty, since a drift has no settle window."""
    session, seconds = context.session, context.healing.seconds
    try:
        seen = await bounded(looked(session.snapshot, seconds), seconds)
    except (Error, PolicyEventError, DocumentChangedError, UnansweredError) as error:
        return _Stop(_unlooked(context, error))
    if not context.observed:
        record = context.healing.spending.record
        try:
            await capture_evidence(record, session, context.folder, None, seen)
        except OSError:
            return _Stop(_UNWRITTEN, "record_error")
        context.observed = True
    return seen


def _unlooked(
    context: _Heal,
    error: Error | PolicyEventError | DocumentChangedError | UnansweredError,
) -> str:
    return f"the page couldn't be looked at: {reasons.reason(error, context.redactor)}"


async def _asked(context: _Heal, state: _Turns, snapshot: Redacted) -> Routed | _Stop:
    """What this turn's request got, or what stopped the turns."""
    target = context.drift.script.targets[context.drift.at.target]
    messages = heal_request(
        target.semantic,
        target.locators,
        state["results"],
        snapshot,
        redactor=context.redactor,
    )
    try:
        unspelled(context.redactor)(messages)
    except UnsentRequestError:
        return _Stop(UNSENT)
    if context.drift.interrupted():
        return _Stop(_INTERRUPTED)
    number = state["turn"] + 1
    return await _called(
        context, _Call("healer", healer_tools(), number, str(number), {}), messages
    )


async def _called(
    context: _Heal, asked: _Call, messages: list[BaseMessage]
) -> Routed | _Stop:
    """What the request got, on disk in its record (`_Call`), or the stop it
    makes: no response, a refusal no fallback answered, a response that cost
    more than was left, or a record that took no more. A stop the reply makes
    is carried through its record, so only a cost line not on disk, after which
    no call is made, leaves none. Counted as one turn, whatever came of it."""
    healing = context.healing
    priced = len(healing.spending.calls)
    healing.taken += 1
    try:
        routed = await healing.router.call(
            asked.role,
            "heal",
            messages,
            tools=asked.tools,
            spend=healing.spending.spend(),
        )
    except UnrecordedCostError:
        return _Stop(UNRECORDED, "record_error")
    except (ModelCallError, ProviderError) as error:
        return _kept(context, asked, messages, None, priced) or _Stop(
            no_response(error), "no_response"
        )
    stop = None
    if routed.outcome == "refusal":
        stop = _Stop(_REFUSED)
    elif healing.spending.left < 0:
        stop = _Stop(OVER)
    return _kept(context, asked, messages, routed, priced) or stop or routed


def _kept(
    context: _Heal,
    asked: _Call,
    messages: list[BaseMessage],
    routed: Routed | None,
    priced: int,
) -> _Stop | None:
    """Write the turn's record: the request as sent, what the call holds, how
    the call ended, the reply's first tool call, scanned, and the lines of the
    cost records it added to `costs.jsonl`, past the `priced` before it. The
    stop it makes when the record takes no more, which decides over any other."""
    redactor = context.redactor
    spending = context.healing.spending
    calls = [] if routed is None else routed.message.tool_calls
    first = None if not calls else {"name": calls[0]["name"], "args": calls[0]["args"]}
    name = f"{context.folder}/{asked.name}.json"
    try:
        spending.record.write(
            name,
            {
                "turn": asked.turn,
                "sent": messages[-1].text,
                **asked.holds,
                "outcome": "no_response" if routed is None else routed.outcome,
                "call": None if first is None else redactor.redact(json.dumps(first)),
                "costs": list(range(priced + 1, len(spending.calls) + 1)),
            },
            check=require_clean(redactor, name),
        )
    except OSError, RefusedWriteError:
        return _Stop(_UNWRITTEN, "record_error")
    return None


async def _picked(
    context: _Heal, call: Rebind, snapshot: Redacted
) -> Offer | _Stop | str:
    """The offer of the element `call` picked by its ref (`_bound`), or what
    ends the turns, a run interrupted by now among them, or why not it."""
    if context.drift.interrupted():
        return _Stop(_INTERRUPTED)
    session, seconds = context.session, context.healing.seconds
    try:
        seen = await bounded(seen_element(session, snapshot, call.ref), seconds)
    except (PolicyEventError, UnansweredError) as error:
        return _Stop(_unlooked(context, error))
    except (RefError, DocumentChangedError, Error) as error:
        return f"rebind did not run: {reasons.reason(error, context.redactor)}"
    reported = ModelReported(call.confidence, context.line(call.rationale))
    return await _bound(context, seen, reported)


async def _bound(
    context: _Heal, seen: Seen, reported: ModelReported | None
) -> Offer | _Stop | str:
    """The offer of `seen`, with what a model `reported` of it; what ends the
    turns: a page that won't answer or is off the allowed origins, or a subject
    contract that refuses this page; or, for the next turn's request, why not
    this element. Every read here is bounded. Only defects and cancellation
    raise."""
    session, seconds = context.session, context.healing.seconds
    at = context.drift.at
    target = context.drift.script.targets[at.target]
    try:
        refused = await rebind_refusal(
            session,
            at.step if isinstance(at, StepAt) else None,
            seen.element,
            target,
            secret=None,
            budget=seconds,
        )
        if refused is not None:
            return f"rebind did not run: {refused}"
        locators = await _generated(context, seen)
    except BindingRefusedError as error:
        return _contract_refused(context, error)
    except (PolicyEventError, UnansweredError) as error:
        return _Stop(_unlooked(context, error))
    except (RefError, LocatorError, DocumentChangedError, Error) as error:
        return f"rebind did not run: {reasons.reason(error, context.redactor)}"
    return _offered(context, seen, reported, locators)


async def _looked_at(context: _Heal, number: int) -> Offer | _Stop:
    """Vision fallback's one turn, turn `number`, once a tree turn found no
    element (ADR-0002's #252 amendment): a screenshot of the page sent with
    the target to `vision_fallback`, whose point is read as the element
    there. Whatever comes of it, the heal ends. Only defects and cancellation
    raise."""
    shot = await _shot(context)
    if isinstance(shot, _Stop):
        return shot
    reply = await _shown(context, number, shot)
    if isinstance(reply, _Stop):
        return reply
    point = clicked(reply)
    if point is None:
        return _Stop(_FOUND_NONE)
    if isinstance(point, Unfit):
        return _Stop(f"{_VISION}{point.why}")
    return await _pointed(context, point)


async def _shot(context: _Heal) -> Screenshot | _Stop:
    """One screenshot of the live page, taken once a turn may start, never
    after a fill, or what stopped it."""
    if (stop := _spent(context)) is not None:
        return stop
    session = context.session
    if session.secret_filled:
        return _Stop(_FILLED)
    try:
        return await take_screenshot(session)
    except ScreenshotWithheldError as withheld:
        return _Stop(f"{_VISION}the screenshot was withheld: {withheld}")
    except (PolicyEventError, DocumentChangedError) as error:
        return _Stop(_unlooked(context, error))


async def _shown(context: _Heal, number: int, shot: Screenshot) -> Routed | _Stop:
    """What the vision request with `shot` got, or what stopped the turns."""
    target = context.drift.script.targets[context.drift.at.target]
    messages = vision_request(
        target.semantic,
        target.locators,
        shot,
        context.session.settings.viewport,
        redactor=context.redactor,
    )
    try:
        unspelled(context.redactor)(messages)
    except UnsentRequestError:
        return _Stop(UNSENT)
    if context.drift.interrupted():
        return _Stop(_INTERRUPTED)
    held = {"image": _kept_of(shot)}
    asked = _Call("vision_fallback", vision_tools(), number, "vision", held)
    return await _called(context, asked, messages)


def _kept_of(shot: Screenshot) -> dict[str, object]:
    """What a vision turn's record keeps of the image it sent: its width and
    height, the first fields of its PNG header, and its sha256."""
    width, height = struct.unpack(">II", shot.png[16:24])
    return {
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(shot.png).hexdigest(),
    }


async def _pointed(context: _Heal, point: VisionClick) -> Offer | _Stop:
    """The offer of the element at `point`, checked as a tree pick is, with
    nothing a model reported, or why the heal ends with none. A point off the
    viewport reads nothing, and a canvas is refused before any check. The read
    is bounded, and it clicks, hovers, focuses and scrolls nothing."""
    width, height = context.session.settings.viewport
    if not (0 <= point.x < width and 0 <= point.y < height):
        return _Stop(_OUTSIDE)
    if context.drift.interrupted():
        return _Stop(_INTERRUPTED)
    session, seconds = context.session, context.healing.seconds
    try:
        async with session.observing_page() as page:
            # Through `cut_off`: `is_among` lets go of the element with a
            # read a busy page never answers.
            found = await cut_off(_at(page, point), seconds)
    except (PolicyEventError, UnansweredError) as error:
        return _Stop(_unlooked(context, error))
    except (DocumentChangedError, Error) as error:
        return _Stop(f"{_VISION}{reasons.reason(error, context.redactor)}")
    if isinstance(found, str):
        return _Stop(found)
    bound = await _bound(context, Seen(found), None)
    return _Stop(f"{_VISION}{bound}") if isinstance(bound, str) else bound


async def _at(page: Page, point: VisionClick) -> ElementHandle | str:
    """The element at `point` in the main frame's document, or why there is
    none to check, in fixed words: no element, or a canvas, which the engine
    tells in the utility world."""
    found = await page.evaluate_handle(_AT_POINT, [point.x, point.y])
    element = found.as_element()
    if element is None:
        return _NOTHING_THERE
    if await is_among(page.locator("css=canvas"), element):
        return _A_CANVAS
    return element


def _contract_refused(context: _Heal, error: BindingRefusedError) -> _Stop | str:
    """A refused pick's fixed reason code, never the subject check's words,
    which name the navigator's tools. `context` remembers a pick outside the
    region until a page-level stop, whose own words ask for the config edit."""
    if error.reason in _PAGE_LEVEL:
        context.outside_region = False
        return _Stop(
            f"the subject contract refuses this page ({error.reason}); "
            "heal never changes a contract, so a reviewed config edit is needed"
        )
    if error.reason == "outside_region":
        context.outside_region = True
    return (
        f"rebind did not run: the subject check refused this element ({error.reason})"
    )


def _offered(
    context: _Heal,
    seen: Seen,
    reported: ModelReported | None,
    locators: tuple[Locator, ...],
) -> Offer:
    """The script handed, with only the drifted target's locators replaced."""
    script, name = context.drift.script, context.drift.at.target
    rebound = script.targets[name].model_copy(update={"locators": locators})
    offered = script.model_copy(update={"targets": {**script.targets, name: rebound}})
    named = f' "{seen.name}"' if seen.name else ""
    bound_as = context.line(f"{seen.role or 'element'}{named}")
    return Offer(offered, Basis(bound_as, reported, None, context.folder))


async def _generated(context: _Heal, seen: Seen) -> tuple[Locator, ...]:
    """The locators the generator writes for `seen`, inside the target's
    contract, of the kinds the validator holds the target's uses to, each found
    alone (`_dropping` says which it never tries). The page changing under it
    means another try, within the bound. Its reads go through `cut_off`: they
    hold the element, and let go of it with a read a busy page never answers.
    `cut_off` wraps no session call, so it leaves no session lock held, and
    `observing_page`'s own checks read only what Chromium reports."""
    session, seconds = context.session, context.healing.seconds
    script, name = context.drift.script, context.drift.at.target
    target = script.targets[name]
    claims = text_claims(script).get(target.semantic, [])
    dropped = _dropping(context.redactor, claims)
    asserted = any(
        isinstance(check, TextInTarget | NotVisible | VisibleUnoccluded)
        and check.target == name
        for check in script.assertions
    )

    async def once() -> tuple[Locator, ...]:
        uses = TargetUses(
            target.semantic,
            checks_text=bool(claims),
            contract=target.contract,
            drop=dropped,
        )
        async with session.observing_page() as page:
            index = await cut_off(
                uses.add(page, seen, "assertion" if asserted else "action"), seconds
            )
        return uses.targets[index].locators

    return await looked(once, seconds)


def _dropping(
    redactor: Redactor, claims: list[TextInTarget]
) -> Callable[[Locator], bool]:
    """What a heal's generation never tries: a locator that spells a bound
    value, or a literal text claim on the target's meaning, which the validator
    refuses (a pattern claim stays with its bounded search, in `heal_patch`), or
    one holding a string past `_LONGEST_STRING`."""

    def dropped(locator: Locator) -> bool:
        written = locator.model_dump(mode="json", exclude_none=True)
        return (
            redactor.finds(json.dumps(written, ensure_ascii=False))
            or literal_claim_spelled(locator, claims)
            or any(len(text) > _LONGEST_STRING for text in _texts(written))
        )

    return dropped


def _texts(value: object) -> Iterator[str]:
    """Every value in `value`, a locator as JSON, as text, its scope's and held
    value's included."""
    if isinstance(value, dict):
        for each in value.values():
            yield from _texts(each)
    else:
        yield str(value)
