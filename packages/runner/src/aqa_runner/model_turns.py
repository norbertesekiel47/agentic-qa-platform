"""What each model-using mode's turns share (explore's and heal's, ADR-0024):
a labelled block of a request, the last check before it is sent, the spend
against the run's budget, and why a call got no response."""

import json
from base64 import b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import MAX_PREC, Decimal, localcontext
from typing import Any, Final

from aqa_core.model_costs import CostRecord
from langchain_core.messages import AIMessage, BaseMessage

from aqa_runner.anthropic_client import ProviderError
from aqa_runner.model_router import ModelCallError, Spend
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import COSTS, RunRecord, require_clean
from aqa_runner.screenshots import Screenshot

# Why a turn's call didn't run: its response cost more than was left.
OVER: Final = "the spend budget ran out on this turn, so its call didn't run"

# Why the turns stopped, in fixed words.
UNRECORDED: Final = (
    "a priced model call's cost line isn't on disk, so the run's cost is incomplete"
)
UNSENT: Final = (
    "a request still spelled a bound value after its scans, so it wasn't sent"
)


class UnsentRequestError(Exception):
    """A model request in which the run's scan still finds a bound value:
    never sent (ADR-0026's #53 P15a amendment)."""

    def __init__(self) -> None:
        super().__init__(UNSENT)


def unspelled(
    redactor: Redactor, screenshots: Sequence[Screenshot] = ()
) -> Callable[[Sequence[BaseMessage]], None]:
    """The check a model request of the run passes before it is sent:
    `UnsentRequestError` when any of its messages still spells a value
    `redactor` binds, such as one a join at a marker's edge completes: in
    its text, or in its content and tool calls as JSON, non-ASCII as
    written, so no other text's escape counts as a spelling. The one part it
    doesn't read is an image block's data that is exactly the PNG of one of
    `screenshots`, which `take_screenshot` checked under `redactor`
    (SECURITY §5): any other image block is read byte for byte. A
    screenshot that isn't a `Screenshot`, or one made under another scan,
    raises before anything is checked."""
    skipped = {_data(shot, redactor) for shot in screenshots}

    def check(messages: Sequence[BaseMessage]) -> None:
        for message in messages:
            calls = message.tool_calls if isinstance(message, AIMessage) else []
            content = _shown(message.content, skipped)
            sent = json.dumps([content, calls], ensure_ascii=False)
            if redactor.finds(message.text) or redactor.finds(sent):
                raise UnsentRequestError

    return check


def _data(shot: Screenshot, redactor: Redactor) -> str:
    """`shot`'s PNG as an image block's base64 data holds it."""
    if not isinstance(shot, Screenshot):
        raise TypeError("only a Screenshot take_screenshot made skips the check")
    if shot.redactor is not redactor:
        raise ValueError("a screenshot made under another scan skips no check")
    return b64encode(shot.png).decode("ascii")


def _shown(content: str | list[str | dict[Any, Any]], skipped: set[str]) -> object:
    """`content` with the base64 data of each image block that holds one of
    `skipped` left out."""
    if isinstance(content, str):
        return content
    return [
        {key: value for key, value in part.items() if key != "base64"}
        if isinstance(part, dict)
        and part.get("type") == "image"
        and part.get("base64") in skipped
        else part
        for part in content
    ]


def block(label: str, text: str, redactor: Redactor) -> str:
    """`text` in the labelled block a request holds it in, where no text can
    close it."""
    # Scanned before escaping, which could hide a value holding "<", and
    # after, since "&lt;" could complete one.
    escaped = redactor.redact(text).replace("<", "&lt;")
    return f"<{label}>\n{redactor.redact(escaped)}\n</{label}>"


def no_response(error: ModelCallError | ProviderError) -> str:
    """Why a call got no response, in the provider error's class, never its
    message. A `ModelCallError` caused by anything else is a defect behind a
    billed refusal, and goes on as it came."""
    failed = error.__cause__ if isinstance(error, ModelCallError) else error
    if not isinstance(failed, ProviderError):
        raise error
    return f"the model gave no response ({type(failed).__name__})"


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
