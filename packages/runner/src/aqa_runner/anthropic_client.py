"""The Anthropic adapter (ADR-0007 amendments).

It is the only module that imports langchain-anthropic. What it sends is pinned
by cassettes (tests/cassettes): no request forces a tool choice, because Sonnet
5.5 and Opus 5.5 answer a forced `tool_choice` with a 400."""

from collections.abc import Sequence
from typing import Any, cast

from aqa_core.config import Effort
from aqa_core.model_costs import Usage
from aqa_core.model_roles import RoutedModel
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from aqa_runner.chat_client import ChatClient, Reply


def _usage(message: AIMessage) -> Usage:
    """The tokens a response used. LangChain's `usage_metadata` counts every
    input token, the cached ones included, which is what `Usage` means."""
    metadata = message.usage_metadata
    if metadata is None:
        raise ValueError("the response carries no usage, so it can't be priced")
    cached = (metadata.get("input_token_details") or {}).get("cache_read") or 0
    return Usage(
        input_tokens=metadata["input_tokens"],
        cached_input_tokens=cached,
        output_tokens=metadata["output_tokens"],
    )


class AnthropicClient:
    """One Anthropic model, called with the Messages API."""

    def __init__(self, model: RoutedModel, effort: Effort | None) -> None:
        settings: dict[str, Any] = {"model": model.name}
        if effort is not None:
            settings["effort"] = effort
        # model_validate, not the constructor: the keyword is a Pydantic alias,
        # which mypy --strict can't see without the plugin.
        self.chat = ChatAnthropic.model_validate(settings)

    async def call(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        schema: type[BaseModel] | None,
    ) -> Reply:
        """Call the model with `tools` or with a `schema`. Tools are strict and
        `tool_choice` is `auto`. A schema is the response format
        (`output_config.format`), never a tool: langchain-anthropic's default
        structured output forces one on `claude-sonnet-5-5`."""
        if tools and schema is not None:
            raise ValueError("a call has tools or a schema, not both")
        parsed: BaseModel | None = None
        if schema is not None:
            structured = self.chat.with_structured_output(
                schema, method="json_schema", include_raw=True
            )
            # include_raw: a refusal or a bad answer leaves `parsed` None
            # instead of raising, so the response can still be recorded.
            result = cast(dict[str, Any], await structured.ainvoke(list(messages)))
            raw, parsed = result["raw"], result["parsed"]
        elif tools:
            raw = await self.chat.bind_tools(
                list(tools), tool_choice="auto", strict=True
            ).ainvoke(list(messages))
        else:
            raw = await self.chat.ainvoke(list(messages))
        return Reply(
            message=raw,
            usage=_usage(raw),
            refused=raw.response_metadata.get("stop_reason") == "refusal",
            parsed=parsed,
        )


def build_client(model: RoutedModel, effort: Effort | None) -> ChatClient:
    """The client for `model`, chosen by its provider and never by parsing its
    name: a model a project declares may be called `openai:gpt-4o`."""
    if model.provider != "anthropic":
        raise ValueError(f"no adapter for provider '{model.provider}'")
    return AnthropicClient(model, effort)
