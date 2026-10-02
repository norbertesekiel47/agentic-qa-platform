"""The Anthropic adapter (ADR-0007 amendments; #40): the client comes from the
routed model's provider, tools are strict and `tool_choice` is `auto`, a schema
is the response format, and no request forces a tool, because Sonnet 5.5 and
Opus 5.5 answer a forced `tool_choice` of `any` or `tool` with a 400. The
cassettes hold the requests the adapter really sent; their responses are
hand-written (see each cassette's header and TESTING §4)."""

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from aqa_core.config import Effort
from aqa_core.model_costs import Usage
from aqa_core.model_roles import RoutedModel
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.chat_client import Reply
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

CASSETTES = Path(__file__).parent / "cassettes"
Cassette = Callable[..., AbstractContextManager[Any]]


class Verdict(BaseModel):
    ok: bool
    reason: str


@tool
def click(ref: str) -> str:
    """Click the element with this ref."""
    return ref


CLICK_PROMPT = "Click the Sign in button (ref e12)."
VERDICT_PROMPT = "Is the cart empty? Answer with ok and a reason."
PLAIN_PROMPT = "Say hello in one word."


def ask_with_tools(model: RoutedModel) -> Reply:
    client = AnthropicClient(model, None)
    return asyncio.run(client.call([HumanMessage(content=CLICK_PROMPT)], [click], None))


def ask_for_a_verdict(model: RoutedModel) -> Reply:
    client = AnthropicClient(model, None)
    return asyncio.run(client.call([HumanMessage(content=VERDICT_PROMPT)], [], Verdict))


def ask_plainly_at_high_effort(model: RoutedModel) -> Reply:
    client = AnthropicClient(model, "high")
    return asyncio.run(client.call([HumanMessage(content=PLAIN_PROMPT)], [], None))


def named(model: RoutedModel, *, name: str, provider: str = "anthropic") -> RoutedModel:
    return RoutedModel(
        provider=provider,
        name=name,
        info=model.info,
        price_map_version=model.price_map_version,
    )


def forced(body: dict[str, Any]) -> bool:
    """Whether a request body forces the model to call a tool."""
    return body.get("tool_choice", {}).get("type") in {"any", "tool"}


# The client: its provider, its model, its effort, its endpoint.


def test_the_client_is_built_from_the_routed_models_provider_and_name(
    sonnet: RoutedModel,
) -> None:
    client = AnthropicClient(named(sonnet, name="claude-sonnet-5-5"), None)

    assert client.chat.model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    "name", ["openai:gpt-4o", "bedrock/anthropic.claude-sonnet-5-5"]
)
def test_a_provider_prefix_in_a_model_name_changes_nothing(
    sonnet: RoutedModel, name: str
) -> None:
    # A model a project declares can be called anything. The provider is the
    # role's, never read from the name: this client, not another provider's.
    client = AnthropicClient(named(sonnet, name=name), None)

    assert client.chat.model == name


def test_a_provider_with_no_adapter_is_refused(sonnet: RoutedModel) -> None:
    model = named(sonnet, name="gpt-4o", provider="openai")

    with pytest.raises(ValueError, match="no adapter for provider 'openai'"):
        AnthropicClient(model, None)


@pytest.mark.parametrize("effort", [None, "low", "high", "max"])
def test_a_roles_effort_is_asked_of_the_model(
    sonnet: RoutedModel, effort: Effort | None
) -> None:
    client = AnthropicClient(sonnet, effort)

    assert client.chat.reasoning_effort == effort


GATEWAY = "https://gateway.example.invalid"


@pytest.fixture
def anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No endpoint or key from the developer's own environment, and a LangSmith
    gateway that, left to langchain-anthropic, would take the call."""
    for variable in ("ANTHROPIC_API_URL", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("LANGSMITH_GATEWAY", GATEWAY)
    monkeypatch.setenv("LANGSMITH_GATEWAY_API_KEY", "fake-gateway-key")


@pytest.mark.usefixtures("anthropic_environment")
def test_an_ambient_langsmith_gateway_does_not_take_the_call_or_the_key(
    monkeypatch: pytest.MonkeyPatch, sonnet: RoutedModel
) -> None:
    # The control first: left alone, langchain-anthropic would send the call to
    # the gateway, with the gateway's key.
    left_alone = ChatAnthropic.model_validate({"model": sonnet.name})
    assert (left_alone.anthropic_api_url or "").startswith(GATEWAY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-provider-key")

    client = AnthropicClient(sonnet, None)

    assert client.chat.anthropic_api_url == "https://api.anthropic.com"
    assert client.chat.anthropic_api_key.get_secret_value() == "fake-provider-key"


@pytest.mark.usefixtures("anthropic_environment")
def test_a_gateway_key_is_not_sent_to_anthropic_when_there_is_no_provider_key(
    sonnet: RoutedModel,
) -> None:
    client = AnthropicClient(sonnet, None)

    assert client.chat.anthropic_api_key.get_secret_value() == ""


@pytest.mark.usefixtures("anthropic_environment")
@pytest.mark.parametrize(
    ("variables", "url"),
    [
        (
            {"ANTHROPIC_BASE_URL": "http://proxy.example.invalid"},
            "http://proxy.example.invalid",
        ),
        (
            {"ANTHROPIC_API_URL": "http://proxy.example.invalid"},
            "http://proxy.example.invalid",
        ),
        (
            {
                "ANTHROPIC_API_URL": "http://first.example.invalid",
                "ANTHROPIC_BASE_URL": "http://second.example.invalid",
            },
            "http://first.example.invalid",
        ),
    ],
)
def test_the_anthropic_endpoint_variables_are_honoured(
    monkeypatch: pytest.MonkeyPatch,
    sonnet: RoutedModel,
    variables: dict[str, str],
    url: str,
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    assert AnthropicClient(sonnet, None).chat.anthropic_api_url == url


def test_a_replay_is_not_redirected_by_the_developers_own_endpoint(
    monkeypatch: pytest.MonkeyPatch, sonnet: RoutedModel, cassette: Cassette
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_URL", "http://redirect.example.invalid")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://redirect.example.invalid")

    with cassette("structured_output", replay_only=True):
        reply = ask_for_a_verdict(sonnet)

    assert reply.parsed is not None


def test_a_call_with_tools_and_a_schema_is_refused(sonnet: RoutedModel) -> None:
    client = AnthropicClient(sonnet, None)

    with pytest.raises(ValueError, match="tools or a schema, not both"):
        asyncio.run(client.call([HumanMessage(content="go")], [click], Verdict))


@pytest.mark.parametrize(
    "answer",
    [
        AIMessage(content="no usage here"),
        AIMessage(content="no usage_metadata", response_metadata={"usage": {}}),
    ],
    ids=["nothing", "response_metadata_only"],
)
def test_a_response_with_no_usage_is_refused_because_it_cannot_be_priced(
    sonnet: RoutedModel, answer: AIMessage
) -> None:
    client = AnthropicClient(sonnet, None)
    # A stand-in for the one Anthropic call, with no usage_metadata.
    client.chat = cast(ChatAnthropic, GenericFakeChatModel(messages=iter([answer])))

    with pytest.raises(ValueError, match="carries no usage"):
        asyncio.run(client.call([HumanMessage(content="go")], [], None))


def test_a_response_without_a_usage_field_is_refused_not_priced_at_zero(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    # langchain-anthropic turns an absent `usage` into zero tokens, which would
    # record a billed call as free.
    with (
        cassette("no_usage"),
        pytest.raises(ValueError, match="carries no usage"),
    ):
        ask_for_a_verdict(sonnet)


# What goes on the wire.


def test_a_tools_call_sends_strict_tools_and_lets_the_model_choose(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("tools") as recording:
        ask_with_tools(sonnet)

    (body,) = recording.sent
    assert body["model"] == "claude-sonnet-5-5"
    assert body["tool_choice"] == {"type": "auto"}
    (click_tool,) = body["tools"]
    assert click_tool["name"] == "click"
    assert click_tool["strict"] is True


def test_a_schema_call_sends_a_response_format_and_no_tool_and_no_effort(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("structured_output") as recording:
        ask_for_a_verdict(sonnet)

    (body,) = recording.sent
    response_format = body["output_config"]["format"]
    assert response_format["type"] == "json_schema"
    assert set(response_format["schema"]["properties"]) == {"ok", "reason"}
    assert "effort" not in body["output_config"]
    assert "tools" not in body
    assert "tool_choice" not in body


def test_a_plain_call_sends_the_roles_effort_and_nothing_else(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("plain_with_effort") as recording:
        ask_plainly_at_high_effort(sonnet)

    (body,) = recording.sent
    assert body["output_config"] == {"effort": "high"}
    assert "tools" not in body
    assert "tool_choice" not in body


ASKS: dict[str, Callable[[RoutedModel], Reply]] = {
    "tools": ask_with_tools,
    "structured_output": ask_for_a_verdict,
    "structured_invalid": ask_for_a_verdict,
    "structured_truncated": ask_for_a_verdict,
    "refusal": ask_for_a_verdict,
    "plain_with_effort": ask_plainly_at_high_effort,
}


@pytest.mark.parametrize("name", sorted(ASKS))
def test_no_request_forces_a_tool_choice(
    sonnet: RoutedModel, cassette: Cassette, name: str
) -> None:
    with cassette(name) as recording:
        ASKS[name](sonnet)

    assert recording.sent
    assert not any(forced(body) for body in recording.sent)


def test_every_request_in_every_cassette_leaves_the_tool_choice_free() -> None:
    # The cassettes as files: what was recorded is what the adapter sends, and
    # none of it forces a tool.
    cassettes = sorted(CASSETTES.glob("*.yaml"))
    assert cassettes
    for path in cassettes:
        interactions = yaml.safe_load(path.read_text())["interactions"]
        assert interactions, path.name
        for interaction in interactions:
            assert not forced(json.loads(interaction["request"]["body"])), path.name


def test_langchains_default_structured_output_would_force_a_tool(
    sonnet: RoutedModel,
) -> None:
    # The control for the tests above: langchain-anthropic 1.7.4's default
    # (`method="function_calling"`) binds a forced tool_choice for
    # claude-sonnet-5-5, which the pinned price map marks
    # `supports_forced_tool_use: false`, so a request like that would be
    # rejected with a 400. This is why the adapter asks for `json_schema`. If
    # this fails, the library changed: update the ADR-0007 amendment.
    chat = ChatAnthropic.model_validate({"model": sonnet.name})

    default = cast(Any, chat.with_structured_output(Verdict))

    assert forced({"tool_choice": default.first.kwargs["tool_choice"]})


def test_cassettes_carry_no_credentials_and_say_what_they_are() -> None:
    cassettes = sorted(CASSETTES.glob("*.yaml"))
    assert cassettes
    for path in cassettes:
        text = path.read_text()
        assert not re.search(r"x-api-key|authorization|sk-ant-", text, re.IGNORECASE)
        # Either hand-written and labelled so, or recorded from the API, whose
        # answers carry a request-id.
        assert text.startswith("# HAND-WRITTEN RESPONSES") or "request-id" in text, (
            path.name
        )


# How a response is read. The cassettes whose scenario a live API can produce
# (tools, structured_output, plain_with_effort) are checked against their own
# recorded response, so a real re-recording passes too; the others are
# hand-written by design, and their tests say the values written in them.


def test_a_tool_call_comes_back_with_its_usage_counting_cached_input(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("tools") as recording:
        reply = ask_with_tools(sonnet)

    (response,) = recording.responses
    usage = response["usage"]
    cached = usage.get("cache_read_input_tokens") or 0
    created = usage.get("cache_creation_input_tokens") or 0
    # Anthropic's input_tokens leaves the cached ones out; ours counts them.
    assert reply.usage == Usage(
        input_tokens=usage["input_tokens"] + cached + created,
        cached_input_tokens=cached,
        output_tokens=usage["output_tokens"],
    )
    called = [block for block in response["content"] if block["type"] == "tool_use"]
    assert [(call["name"], call["args"]) for call in reply.message.tool_calls] == [
        (block["name"], block["input"]) for block in called
    ]
    assert (reply.refused, reply.parsed) == (False, None)


def test_a_schema_answer_comes_back_parsed(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("structured_output") as recording:
        reply = ask_for_a_verdict(sonnet)

    (response,) = recording.responses
    (text,) = [
        block["text"] for block in response["content"] if block["type"] == "text"
    ]
    assert reply.parsed == Verdict(**json.loads(text))
    assert not reply.refused
    usage = response["usage"]
    assert reply.usage.output_tokens == usage["output_tokens"]


def test_a_plain_answer_comes_back_as_its_text_with_no_parse(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("plain_with_effort") as recording:
        reply = ask_plainly_at_high_effort(sonnet)

    (response,) = recording.responses
    (text,) = [
        block["text"] for block in response["content"] if block["type"] == "text"
    ]
    assert reply.message.content == text
    assert (reply.refused, reply.parsed) == (False, None)
    assert reply.usage.output_tokens == response["usage"]["output_tokens"]


def test_a_refusal_is_flagged_with_its_usage_and_nothing_parsed(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("refusal"):
        reply = ask_for_a_verdict(sonnet)

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
        reply = ask_for_a_verdict(sonnet)

    assert reply.parsed is None
    assert not reply.refused
    assert reply.usage == Usage(
        input_tokens=205, cached_input_tokens=0, output_tokens=29
    )


def test_langchain_repairs_a_cut_off_answer_into_one_that_parses(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    # The control for the test below: asked directly, langchain-anthropic turns
    # JSON that ended at `max_tokens` into a verdict that validates.
    with cassette("structured_truncated"):
        structured = AnthropicClient(sonnet, None).chat.with_structured_output(
            Verdict, method="json_schema", include_raw=True
        )
        result = cast(
            dict[str, Any],
            asyncio.run(structured.ainvoke([HumanMessage(content=VERDICT_PROMPT)])),
        )

    assert result["raw"].response_metadata["stop_reason"] == "max_tokens"
    assert isinstance(result["parsed"], Verdict)


def test_an_answer_cut_off_at_max_tokens_is_not_trusted_as_parsed(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    # Half a reason is not a verdict: it is billed, and comes back unparsed.
    # No cache fields in its usage: they count as zero.
    with cassette("structured_truncated"):
        reply = ask_for_a_verdict(sonnet)

    assert reply.parsed is None
    assert not reply.refused
    assert reply.usage == Usage(
        input_tokens=205, cached_input_tokens=0, output_tokens=4096
    )
