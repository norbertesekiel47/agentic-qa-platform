"""Fixtures the explore graph's tests share (#53 P15): a scripted navigator
whose moves read each request, and an attempt's turns taken through
`explore_turns.take_turns` against the executor's fixture app's shop page in
real Chromium, with the run's record and its spend. Imported by its path, as
pytest names the runner's test modules (TESTING.md §1, Shared egress
fixtures)."""

import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Unpack

import anthropic
import httpx2
from aqa_core.config import Effort
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.model_costs import Usage
from aqa_core.model_roles import RoutedModel
from aqa_runner.chat_client import ChatClient, Reply
from aqa_runner.explore_turns import Spending, Turning, take_turns
from aqa_runner.model_router import ModelRouter
from aqa_runner.navigator import navigator_prefix
from aqa_runner.navigator_tools import Attempt
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from packages.runner.tests.executor_fixtures import App
from packages.runner.tests.navigator_fixtures import Run, on_app

# The shop page's elements, by the meanings the turns act on them under.
NAME = "the shop's name field"
SAVE = "the shop's save button"
STATUS = "the shop's save status"

# The plan the turns explore for: the save status reads the saved name.
PLAN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": STATUS,
                "claim": "says the name Ada was saved",
                "checks": [
                    {
                        "check": "text_in_target",
                        "target_meaning": STATUS,
                        "text": "Saved Ada",
                    }
                ],
            }
        ],
        "requires": [],
    }
)

# What each scripted response used: $0.003 at the navigator's Sonnet prices.
USAGE = Usage(input_tokens=1000, cached_input_tokens=0, output_tokens=100)

type Move = (
    Reply | Exception | Callable[[Sequence[BaseMessage]], Reply | Awaitable[Reply]]
)


class Scripted:
    """A model that answers each call with its next move: a reply, an error
    it raises, or a function of the request, which may be a coroutine
    function. It is also its router's client factory, and keeps the model
    of each client built, every request it was sent, and the tools and
    schema each one offered."""

    def __init__(self, *moves: Move) -> None:
        self.moves = list(moves)
        self.models: list[str] = []
        self.requests: list[list[BaseMessage]] = []
        self.offered: list[tuple[tuple[str, ...], type[BaseModel] | None]] = []

    def __call__(self, model: RoutedModel, effort: Effort | None) -> ChatClient:
        self.models.append(f"{model.name} at effort {effort}")
        return self

    async def call(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        schema: type[BaseModel] | None,
    ) -> Reply:
        self.requests.append(list(messages))
        self.offered.append((tuple(each.name for each in tools), schema))
        move = self.moves.pop(0)
        if isinstance(move, Exception):
            raise move
        if isinstance(move, Reply):
            return move
        answered = move(messages)
        return await answered if inspect.isawaitable(answered) else answered


def answer(message: AIMessage, *, refused: bool = False) -> Reply:
    """A response of `message`, complete unless refused."""
    return Reply(
        message=message, usage=USAGE, refused=refused, complete=not refused, parsed=None
    )


def calling(name: str, args: dict[str, object]) -> Reply:
    """A reply that calls the tool `name` with `args`, after its notes."""
    return answer(
        AIMessage(
            content=f"notes before {name}",
            tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}],
        )
    )


def planned(plan: CoveragePlan | None = PLAN) -> Reply:
    """The plan call's answer: `plan`, parsed, at the usual usage."""
    return Reply(
        message=AIMessage(content="the plan"),
        usage=USAGE,
        refused=False,
        complete=True,
        parsed=plan,
    )


def tool(
    name: str, args: Callable[[Sequence[BaseMessage]], dict[str, object]]
) -> Callable[[Sequence[BaseMessage]], Reply]:
    """A move that calls the tool `name` with `args` of the request."""
    return lambda messages: calling(name, args(messages))


def ref(messages: Sequence[BaseMessage], role: str, name: str) -> str:
    """The ref the request's snapshot gives the first element with `role`
    and `name`."""
    text = messages[-1].text
    found = re.search(rf'- {role} "{re.escape(name)}"[^\n]*?\[ref=(e\d+)\]', text)
    found = found or re.search(rf"- {role} \[ref=(e\d+)\]: {re.escape(name)}", text)
    assert found is not None, text
    return found[1]


FILL = tool(
    "fill", lambda m: {"ref": ref(m, "textbox", "Name"), "meaning": NAME, "text": "Ada"}
)
CLICK = tool("click", lambda m: {"ref": ref(m, "button", "Save"), "meaning": SAVE})
REFUSED = answer(AIMessage(content="I can't help with that."), refused=True)
NO_RESPONSE = anthropic.APIConnectionError(
    request=httpx2.Request("POST", "http://127.0.0.1:9/fake")
)


def turning(
    attempt: Attempt, client: Scripted, *, turns: int = 40, usd: str = "1"
) -> Turning:
    """The turns of `attempt`, whose model is `client`, with `turns` turns
    and `usd` US dollars to spend, recorded in the record `attempt`'s
    part was made from."""
    record = replace(attempt.record, path=attempt.record.path.parent)
    redactor = attempt.session.redactor
    return Turning(
        attempt,
        ModelRouter.from_config(attempt.config, client),
        navigator_prefix(attempt.spec, attempt.plan, {}, redactor=redactor),
        Spending(record, Decimal(usd)),
        turns,
        [],
    )


def taking(
    client: Scripted, *, turns: int = 40, usd: str = "1"
) -> Callable[[Attempt], Awaitable[Turning]]:
    """A scenario that takes an attempt's turns (`turning`) and gives
    their context."""

    async def scenario(attempt: Attempt) -> Turning:
        context = turning(attempt, client, turns=turns, usd=usd)
        await take_turns(context)
        return context

    return scenario


def turned(
    app: App,
    tmp_path: Path,
    client: Scripted,
    *,
    turns: int = 40,
    usd: str = "1",
    **run: Unpack[Run],
) -> Turning:
    """The turns, once taken, of an attempt on the shop page for `PLAN`,
    unless `run` says otherwise."""
    run.setdefault("page", "/page/shop")
    run.setdefault("plan", PLAN)
    return on_app(app, tmp_path, taking(client, turns=turns, usd=usd), **run)


def record_of(context: Turning) -> Path:
    """The directory of the run record the turns wrote to."""
    return context.spending.record.path
