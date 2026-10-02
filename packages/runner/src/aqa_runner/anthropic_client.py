"""The Anthropic adapter (ADR-0007 amendments).

It is the only module that imports langchain-anthropic. What it sends is pinned
by cassettes (tests/cassettes): no request forces a tool choice, because Sonnet
5.5 and Opus 5.5 answer a forced `tool_choice` with a 400."""

import os
from collections.abc import Sequence
from typing import Any, cast

from aqa_core.config import Effort
from aqa_core.model_costs import Usage
from aqa_core.model_roles import RoutedModel
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from aqa_runner.chat_client import Reply

API_URL = "https://api.anthropic.com"
# The one stop reason that ends a complete answer. Anything else (`max_tokens`,
# `pause_turn`, a stop sequence, ...) can leave a half-written one, which
# LangChain would repair into something that parses.
_COMPLETE = "end_turn"
_COUNTS = ("input_tokens", "output_tokens")
# Bounds on every request, stated here rather than left to langchain-anthropic
# 1.7.4: it takes max_tokens from its model profiles, 128,000 for
# claude-opus-5-5 and a 4096 fallback for claude-sonnet-5-5 (no profile), and
# passes timeout=None, which the SDK reads as never timing out. 120 s covers
# 4096 tokens at 40 a second, per try; the SDK still retries twice (LAB_NOTES).
MAX_OUTPUT_TOKENS = 4096
REQUEST_TIMEOUT_SECONDS = 120.0


def _usage(message: AIMessage) -> Usage:
    """The tokens a response used. LangChain's `usage_metadata` counts every
    input token, the cached ones included, which is what `Usage` means. It is
    zero for a response whose own `usage` lacks the counts, so such a response
    is refused."""
    metadata = message.usage_metadata
    own = message.response_metadata.get("usage") or {}
    counted = all(isinstance(own.get(name), int) for name in _COUNTS)
    if metadata is None or not counted:
        raise ValueError("the response carries no usage counts, so it can't be priced")
    cached = (metadata.get("input_token_details") or {}).get("cache_read") or 0
    return Usage(
        input_tokens=metadata["input_tokens"],
        cached_input_tokens=cached,
        output_tokens=metadata["output_tokens"],
    )


class AnthropicClient:
    """One Anthropic model, called with the Messages API."""

    def __init__(self, model: RoutedModel, effort: Effort | None) -> None:
        # The provider is the role's, never read from the model's name: a model
        # a project declares may be called `openai:gpt-4o`.
        if model.provider != "anthropic":
            raise ValueError(f"no adapter for provider '{model.provider}'")
        settings: dict[str, Any] = {
            "model": model.name,
            # Explicit, from Anthropic's own variables only. Left to
            # langchain-anthropic, an ambient LANGSMITH_GATEWAY would send the
            # call, and the key, to LangSmith's gateway (SECURITY section 10).
            "base_url": os.environ.get("ANTHROPIC_API_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or API_URL,
            "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "timeout": REQUEST_TIMEOUT_SECONDS,
        }
        if effort is not None:
            settings["effort"] = effort
        # model_validate, not the constructor: the keywords are Pydantic aliases,
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
        raw: AIMessage
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
        stop_reason = raw.response_metadata.get("stop_reason")
        if stop_reason != _COMPLETE:
            parsed = None
        return Reply(
            message=raw,
            usage=_usage(raw),
            refused=stop_reason == "refusal",
            parsed=parsed,
        )
