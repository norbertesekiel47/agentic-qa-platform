"""What the router asks of a provider's client, whatever the provider."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aqa_core.config import Effort
from aqa_core.model_costs import Usage
from aqa_core.model_roles import RoutedModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel


@dataclass(frozen=True)
class Reply:
    """What a provider's client brings back from one call. `parsed` is the
    answer to a schema, or None when the call gave no schema or the output did
    not parse and validate."""

    message: AIMessage
    usage: Usage
    refused: bool
    parsed: BaseModel | None


class ChatClient(Protocol):
    """One provider's way of calling one model. A call has tools or a schema,
    not both."""

    async def call(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        schema: type[BaseModel] | None,
    ) -> Reply: ...


ClientFactory = Callable[[RoutedModel, Effort | None], ChatClient]
