"""The Anthropic adapter builds its client from the routed model's provider and
name, and asks for what a role needs (ADR-0007 amendments; #40)."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, cast

import pytest
from aqa_core.config import Effort
from aqa_core.model_costs import Usage
from aqa_core.model_roles import RoutedModel
from aqa_runner.anthropic_client import AnthropicClient, build_client
from aqa_runner.chat_client import Reply
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel


class Verdict(BaseModel):
    ok: bool
    reason: str


@tool
def click(ref: str) -> str:
    """Click the element with this ref."""
    return ref


def named(model: RoutedModel, *, name: str, provider: str = "anthropic") -> RoutedModel:
    return RoutedModel(
        provider=provider,
        name=name,
        info=model.info,
        price_map_version=model.price_map_version,
    )


Cassette = Callable[[str], AbstractContextManager[Any]]


@pytest.fixture(autouse=True)
def a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")


def test_the_client_is_built_from_the_routed_models_provider_and_name(
    sonnet: RoutedModel,
) -> None:
    model = named(sonnet, name="claude-sonnet-5-5")

    client = build_client(model, None)

    assert isinstance(client, AnthropicClient)
    assert client.chat.model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    "name", ["openai:gpt-4o", "bedrock/anthropic.claude-sonnet-5-5"]
)
def test_a_provider_prefix_in_a_model_name_changes_nothing(
    sonnet: RoutedModel, name: str
) -> None:
    # A model a project declares can be called anything. The provider is the
    # role's, never read from the name: this client, not another provider's.
    client = build_client(named(sonnet, name=name), None)

    assert isinstance(client, AnthropicClient)
    assert client.chat.model == name


def test_a_provider_with_no_adapter_is_refused(sonnet: RoutedModel) -> None:
    model = named(sonnet, name="gpt-4o", provider="openai")

    with pytest.raises(ValueError, match="no adapter for provider 'openai'"):
        build_client(model, None)


@pytest.mark.parametrize("effort", [None, "low", "high", "max"])
def test_a_roles_effort_is_asked_of_the_model(
    sonnet: RoutedModel, effort: Effort | None
) -> None:
    client = build_client(sonnet, effort)

    assert isinstance(client, AnthropicClient)
    assert client.chat.reasoning_effort == effort


def test_a_call_with_tools_and_a_schema_is_refused(sonnet: RoutedModel) -> None:
    client = build_client(sonnet, None)

    with pytest.raises(ValueError, match="tools or a schema, not both"):
        asyncio.run(client.call([HumanMessage(content="go")], [click], Verdict))


def ask_with_tools(client: AnthropicClient) -> Reply:
    return asyncio.run(
        client.call(
            [HumanMessage(content="Click the Sign in button (ref e12).")], [click], None
        )
    )


def ask_for_a_verdict(client: AnthropicClient) -> Reply:
    return asyncio.run(
        client.call(
            [HumanMessage(content="Is the cart empty? Answer with ok and a reason.")],
            [],
            Verdict,
        )
    )


def adapter(model: RoutedModel) -> AnthropicClient:
    client = build_client(model, None)
    assert isinstance(client, AnthropicClient)
    return client


def test_a_tool_call_comes_back_with_its_usage_counting_cached_input(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("tools"):
        reply = ask_with_tools(adapter(sonnet))

    (tool_call,) = reply.message.tool_calls
    assert (tool_call["name"], tool_call["args"]) == ("click", {"ref": "e12"})
    assert (reply.refused, reply.parsed) == (False, None)
    # The response says 312 input tokens and 100 read from the cache: Anthropic's
    # input_tokens leaves the cached ones out, ours counts them.
    assert reply.usage == Usage(
        input_tokens=412, cached_input_tokens=100, output_tokens=48
    )


def test_a_schema_answer_comes_back_parsed(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("structured_output"):
        reply = ask_for_a_verdict(adapter(sonnet))

    assert reply.parsed == Verdict(ok=True, reason="The cart shows no items.")
    assert not reply.refused
    assert reply.usage == Usage(
        input_tokens=205, cached_input_tokens=0, output_tokens=31
    )


def test_a_refusal_is_flagged_with_its_usage_and_nothing_parsed(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("refusal"):
        reply = ask_for_a_verdict(adapter(sonnet))

    assert reply.refused
    assert reply.parsed is None
    assert reply.usage == Usage(
        input_tokens=205, cached_input_tokens=0, output_tokens=0
    )


def test_a_schema_answer_that_does_not_validate_is_returned_unparsed(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    # The response arrived and was billed, so it comes back for a record rather
    # than raising.
    with cassette("structured_invalid"):
        reply = ask_for_a_verdict(adapter(sonnet))

    assert reply.parsed is None
    assert not reply.refused
    assert reply.usage == Usage(
        input_tokens=205, cached_input_tokens=0, output_tokens=29
    )


def test_a_response_with_no_usage_is_refused_because_it_cannot_be_priced(
    sonnet: RoutedModel,
) -> None:
    client = adapter(sonnet)
    client.chat = cast(
        ChatAnthropic,
        GenericFakeChatModel(messages=iter([AIMessage(content="no usage here")])),
    )

    with pytest.raises(ValueError, match="carries no usage"):
        asyncio.run(client.call([HumanMessage(content="go")], [], None))
