"""An attempt's navigator turns (ADR-0024's #53 P15a amendment): a LangGraph
graph whose state is plain data, checkpointed in memory; the attempt, the
router and the run's spend ride in its context, never in state.

Each turn takes the page's one observation, builds one fresh request
(`navigator.navigator_request`) and runs at most the reply's first tool
call. Each priced response is on disk in `costs.jsonl` before the next call,
and each request sent leaves `turns/<n>.json`, whatever came back."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import MAX_PREC, Decimal, localcontext
from typing import Final, TypedDict

from aqa_core.compiled import CompiledScript
from aqa_core.compiler import Provenance
from aqa_core.model_costs import CostRecord
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from playwright.async_api import Error

from aqa_runner import reasons
from aqa_runner.anthropic_client import ProviderError
from aqa_runner.document_origins import DocumentChangedError, PolicyEventError
from aqa_runner.executor import StepResult
from aqa_runner.explore_result import Stop
from aqa_runner.model_router import ModelCallError, ModelRouter, Routed, Spend
from aqa_runner.navigator import (
    AssertCheck,
    Decision,
    Finish,
    NoteForAbsence,
    Prefix,
    Turn,
    Unrunnable,
    decision,
    navigator_request,
    navigator_tools,
)
from aqa_runner.navigator_tools import TOOL_NAMES, Action, Attempt
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.run_record import (
    COSTS,
    RefusedWriteError,
    RunRecord,
    UnrecordedCostError,
    require_clean,
)

# Why a turn's call didn't run: its response cost more than was left.
_OVER: Final = "the spend budget ran out on this turn, so its call didn't run"

# Why the turns stopped, in fixed words.
UNRECORDED: Final = (
    "a priced model call's cost line isn't on disk, so the run's cost is incomplete"
)
UNWRITTEN: Final = "the run record took no more lines, so the attempt stopped"
UNSENT: Final = (
    "a request still spelled a bound value after its scans, so it wasn't sent"
)


class UnsentRequestError(Exception):
    """A model request in which the run's scan still finds a bound value:
    never sent (ADR-0026's #53 P15a amendment)."""

    def __init__(self) -> None:
        super().__init__(UNSENT)


def unspelled(redactor: Redactor) -> Callable[[Sequence[BaseMessage]], None]:
    """The check a model request of the run passes before it is sent:
    `UnsentRequestError` when any of its messages still spells a value
    `redactor` binds, such as one a join at a marker's edge completes."""

    def check(messages: Sequence[BaseMessage]) -> None:
        if any(redactor.finds(message.text) for message in messages):
            raise UnsentRequestError

    return check


class Turns(TypedDict):
    """The graph's state, plain data a checkpoint keeps: the turns taken,
    the attempt's log of steps, the model's last notes, what its last tool
    call did, and whether the turns are over."""

    turn: int
    log: tuple[str, ...]
    notes: str
    results: tuple[str, ...]
    done: bool


@dataclass
class Spending:
    """The run's model spend against its budget: each priced response,
    kept, then written to `costs.jsonl` as its exact line, checked, before
    any further call (ADR-0007's #53 P3 amendment). A line that isn't
    written raises `UnrecordedCostError` out of the call."""

    record: RunRecord
    budget: Decimal
    calls: list[CostRecord] = field(default_factory=list)

    def priced(self, call: CostRecord) -> None:
        self.calls.append(call)
        self.record.cost(call, check=require_clean(self.record.redactor, COSTS))

    @property
    def left(self) -> Decimal:
        """What is left of the budget, exactly; below zero once a response
        cost more than was left."""
        with localcontext(prec=MAX_PREC):
            return self.budget - sum((call.cost_usd for call in self.calls), Decimal(0))

    def spend(self) -> Spend:
        """What the next call may spend: what is left, and no fallback past it."""
        return Spend(self.priced, self.left)


@dataclass
class Turning:
    """The graph's context: what the turns use, never in state, and what
    they leave: each stop they made and the script `finish` compiled. The
    run's record is the spend's; every text the turns make is scanned with
    the attempt's session redactor."""

    attempt: Attempt
    router: ModelRouter
    prefix: Prefix
    spending: Spending
    turns: int
    stops: list[Stop]
    script: CompiledScript | None = None

    @property
    def redactor(self) -> Redactor:
        return self.attempt.session.redactor

    def said(self, text: str) -> Redacted:
        return self.redactor.redact(text)


@dataclass(frozen=True)
class _Reply:
    """What a request sent got: the reply, if one came; how many responses
    it priced; and the stop it makes, if any."""

    routed: Routed | None
    priced: int
    stop: Stop | None


async def take_turns(context: Turning) -> None:
    """Take the attempt's turns until one stops them (module docstring)."""
    graph = StateGraph(Turns, context_schema=Turning)
    graph.add_node("turn", _turn)
    graph.add_edge(START, "turn")
    graph.add_conditional_edges("turn", _next)
    await graph.compile(checkpointer=InMemorySaver()).ainvoke(
        Turns(turn=0, log=(), notes="", results=(), done=False),
        # One superstep per turn, and one to stop: the budget stops the
        # turns, never LangGraph's limit, which an environment can lower.
        {
            "configurable": {"thread_id": context.spending.record.run_id},
            "recursion_limit": context.turns + 2,
        },
        context=context,
    )


def _next(state: Turns) -> str:
    return END if state["done"] else "turn"


async def _turn(state: Turns, runtime: Runtime[Turning]) -> Turns:
    context = runtime.context
    taken = await _taken(context, state)
    if taken is None or isinstance(taken, Stop):
        if taken is not None:
            context.stops.append(taken)
        return state | {"done": True}
    return taken


async def _taken(context: Turning, state: Turns) -> Turns | Stop | None:
    """The turn's new state, the stop that ends the turns, or None when the
    run was interrupted (an egress block or an infrastructure event), whose
    facts then decide how it ends."""
    attempt = context.attempt
    if (stop := _spent(context, state["turn"])) is not None:
        return stop
    messages = await _request(context, state)
    if attempt.interrupted():
        # Its observation's included: no call is made.
        return None
    reply = await _called(context, messages)
    if isinstance(reply, Stop):
        return reply
    number = state["turn"] + 1
    try:
        chosen = _kept(context, number, messages, reply)
        if reply.stop is not None or chosen is None or attempt.interrupted():
            return reply.stop
        results, logged = await _ran(context, chosen)
    except OSError, RefusedWriteError:
        # A journal or a document the record couldn't take, a broken
        # journal's refusal included: nothing more may be dispatched.
        return Stop("record_error", context.said(UNWRITTEN))
    return Turns(
        turn=number,
        log=(*state["log"], *logged),
        notes=chosen.notes,
        results=results,
        done=bool(context.stops or context.script) or attempt.interrupted(),
    )


def _spent(context: Turning, taken: int) -> Stop | None:
    """Why no further turn may start: every turn taken, or no spend left."""
    if taken >= context.turns:
        return Stop(
            "gave_up", context.said(f"the attempt took all {taken} of its turns")
        )
    if context.spending.left <= 0:
        return Stop("gave_up", context.said("the spend budget ran out"))
    return None


async def _request(context: Turning, state: Turns) -> list[BaseMessage]:
    """This turn's request, with the page's one observation, or none and
    why, when the page couldn't be looked at."""
    looked: tuple[str, ...] = ()
    try:
        snapshot = await context.attempt.observe()
    except (PolicyEventError, DocumentChangedError, UnansweredError, Error) as error:
        why = reasons.reason(error, context.redactor)
        snapshot, looked = context.said(""), (f"the page couldn't be looked at: {why}",)
    results = (*state["results"], *looked)
    turn = Turn(tuple(state["log"]), state["notes"], results, snapshot)
    return navigator_request(context.prefix, turn, redactor=context.redactor)


async def _called(context: Turning, messages: list[BaseMessage]) -> _Reply | Stop:
    """What the request got, and the stop it makes: no response, a refusal
    no fallback answered, or a response that cost more than was left; or
    the stop that leaves no turn to keep: a request never sent, or a cost
    line not on disk, after which no call is made."""
    try:
        unspelled(context.redactor)(messages)
        routed = await context.router.call(
            "navigator",
            "explore",
            messages,
            tools=navigator_tools(),
            spend=context.spending.spend(),
        )
    except UnsentRequestError:
        return Stop("gave_up", context.said(UNSENT))
    except UnrecordedCostError:
        return Stop("record_error", context.said(UNRECORDED))
    except ModelCallError as error:
        if not isinstance(error.__cause__, ProviderError):
            raise
        return _Reply(None, len(error.records), _silent(context, error.__cause__))
    except ProviderError as error:
        return _Reply(None, 0, _silent(context, error))
    if routed.outcome == "refusal":
        stop: Stop | None = Stop("gave_up", context.said("the model refused its turn"))
    else:
        over = context.spending.left < 0
        stop = Stop("gave_up", context.said(_OVER)) if over else None
    return _Reply(routed, len(routed.calls), stop)


def _silent(context: Turning, error: ProviderError) -> Stop:
    """The stop a call with no response makes: fixed words and the error's
    class, never the provider's message."""
    why = f"the model gave no response ({type(error).__name__})"
    return Stop("no_response", context.said(why))


def _kept(
    context: Turning, number: int, messages: list[BaseMessage], reply: _Reply
) -> Decision | None:
    """Write `turns/<number>.json`: the turn as sent, the reply's first
    tool call and notes, and the lines of its cost records in `costs.jsonl`;
    then the reply's decision, if a reply came."""
    redactor = context.redactor
    routed = reply.routed
    chosen = None if routed is None else decision(routed)
    calls = [] if routed is None else routed.message.tool_calls
    first = None if not calls else {"name": calls[0]["name"], "args": calls[0]["args"]}
    name = f"turns/{number}.json"
    lines = len(context.spending.calls)
    context.spending.record.write(
        name,
        {
            "turn": number,
            "sent": messages[-1].text,
            "outcome": "no_response" if routed is None else routed.outcome,
            "call": None if first is None else redactor.redact(json.dumps(first)),
            "unrun": 0 if chosen is None else chosen.unrun,
            "notes": "" if chosen is None else redactor.redact(chosen.notes),
            "costs": list(range(lines - reply.priced + 1, lines + 1)),
        },
        check=require_clean(redactor, name),
    )
    return chosen


async def _ran(
    context: Turning, chosen: Decision
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Run the reply's first call on the attempt: what it did, and the log
    line of the step it dispatched, if it did."""
    attempt, call = context.attempt, chosen.call
    logged: tuple[str, ...] = ()
    if call is None:
        text: str = context.said("the reply called no tool, so nothing ran")
    elif isinstance(call, Unrunnable):
        text = call.problem
    elif isinstance(call, AssertCheck):
        text = await attempt.check(call)
    elif isinstance(call, NoteForAbsence):
        text = await attempt.note(call)
    elif isinstance(call, Finish):
        finished = await attempt.finish(call, _by(context))
        context.script, text = finished.script, finished.text
    else:
        acted = await attempt.act(call)
        text, logged = acted.text, _line(call, acted.step)
        if attempt.ended:
            context.stops.append(Stop("gave_up", acted.text))
    unrun = f"{chosen.unrun} more tool calls in the reply didn't run: a turn runs one"
    return ((text, unrun) if chosen.unrun else (text,)), logged


def _line(call: Action, step: StepResult | None) -> tuple[str, ...]:
    """The log line of the step `call` ran, by its number, if it ran: one
    that failed once dispatched ended the attempt, so no turn reads more."""
    if step is None or step.outcome != "completed":
        return ()
    said = json.dumps(call.model_dump(mode="json", exclude={"ref"}), sort_keys=True)
    return (f"step {step.seq}: {TOOL_NAMES[type(call)]} {said}",)


def _by(context: Turning) -> Provenance:
    """Who compiled the path: each role's model that answered last, the
    price map the run priced by, and now."""
    calls = context.spending.calls
    return Provenance(
        models={call.role: call.model for call in calls},
        price_map=calls[-1].price_map_version,
        at=datetime.now(UTC),
    )
