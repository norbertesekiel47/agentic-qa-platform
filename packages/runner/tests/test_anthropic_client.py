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
from aqa_runner.anthropic_client import AnthropicClient, build_client
from aqa_runner.chat_client import Reply
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

CASSETTES = Path(__file__).parent / "cassettes"
Cassette = Callable[[str], AbstractContextManager[Any]]


class Verdict(BaseModel):
    ok: bool
    reason: str


@tool
def click(ref: str) -> str:
    """Click the element with this ref."""
    return ref


CLICK_PROMPT = "Click the Sign in button (ref e12)."
VERDICT_PROMPT = "Is the cart empty? Answer with ok and a reason."


def ask_with_tools(client: AnthropicClient) -> Reply:
    return asyncio.run(client.call([HumanMessage(content=CLICK_PROMPT)], [click], None))


def ask_for_a_verdict(client: AnthropicClient) -> Reply:
    return asyncio.run(client.call([HumanMessage(content=VERDICT_PROMPT)], [], Verdict))


def adapter(model: RoutedModel) -> AnthropicClient:
    client = build_client(model, None)
    assert isinstance(client, AnthropicClient)
    return client


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


@pytest.fixture(autouse=True)
def a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")


# The client: its provider, its model, its effort.


def test_the_client_is_built_from_the_routed_models_provider_and_name(
    sonnet: RoutedModel,
) -> None:
    client = adapter(named(sonnet, name="claude-sonnet-5-5"))

    assert client.chat.model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    "name", ["openai:gpt-4o", "bedrock/anthropic.claude-sonnet-5-5"]
)
def test_a_provider_prefix_in_a_model_name_changes_nothing(
    sonnet: RoutedModel, name: str
) -> None:
    # A model a project declares can be called anything. The provider is the
    # role's, never read from the name: this client, not another provider's.
    client = adapter(named(sonnet, name=name))

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


def test_a_response_with_no_usage_is_refused_because_it_cannot_be_priced(
    sonnet: RoutedModel,
) -> None:
    client = adapter(sonnet)
    # A stand-in for the one Anthropic call, with no usage_metadata.
    client.chat = cast(
        ChatAnthropic,
        GenericFakeChatModel(messages=iter([AIMessage(content="no usage here")])),
    )

    with pytest.raises(ValueError, match="carries no usage"):
        asyncio.run(client.call([HumanMessage(content="go")], [], None))


# What goes on the wire.


def test_a_tools_call_sends_strict_tools_and_lets_the_model_choose(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("tools") as recording:
        ask_with_tools(adapter(sonnet))

    (body,) = recording.sent
    assert body["model"] == "claude-sonnet-5-5"
    assert body["tool_choice"] == {"type": "auto"}
    (click_tool,) = body["tools"]
    assert click_tool["name"] == "click"
    assert click_tool["strict"] is True


def test_a_schema_call_sends_a_response_format_and_no_tool(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("structured_output") as recording:
        ask_for_a_verdict(adapter(sonnet))

    (body,) = recording.sent
    response_format = body["output_config"]["format"]
    assert response_format["type"] == "json_schema"
    assert set(response_format["schema"]["properties"]) == {"ok", "reason"}
    assert "tools" not in body
    assert "tool_choice" not in body


@pytest.mark.parametrize(
    ("name", "ask"),
    [
        ("tools", ask_with_tools),
        ("structured_output", ask_for_a_verdict),
        ("structured_invalid", ask_for_a_verdict),
        ("refusal", ask_for_a_verdict),
    ],
)
def test_no_request_forces_a_tool_choice(
    sonnet: RoutedModel,
    cassette: Cassette,
    name: str,
    ask: Callable[[AnthropicClient], Reply],
) -> None:
    with cassette(name) as recording:
        ask(adapter(sonnet))

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


# The cassettes themselves.


def test_a_changed_prompt_fails_the_cassette_loudly(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with (
        pytest.raises(AssertionError, match="a prompt changed"),
        cassette("structured_output"),
    ):
        asyncio.run(
            adapter(sonnet).call([HumanMessage(content="another prompt")], [], Verdict)
        )


def test_a_recorded_response_that_is_never_played_fails_loudly(
    cassette: Cassette,
) -> None:
    with (
        pytest.raises(AssertionError, match="never used"),
        cassette("structured_output"),
    ):
        pass


def test_re_recording_needs_a_provider_key(
    monkeypatch: pytest.MonkeyPatch, cassette: Cassette
) -> None:
    monkeypatch.setenv("AQA_RECORD_CASSETTES", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with (
        pytest.raises(RuntimeError, match="needs ANTHROPIC_API_KEY"),
        cassette("tools"),
    ):
        pass

    assert (CASSETTES / "tools.yaml").exists()


def test_a_scenario_a_live_api_cannot_produce_is_never_re_recorded(
    monkeypatch: pytest.MonkeyPatch, sonnet: RoutedModel, cassette: Cassette
) -> None:
    # No key is needed and none is used: the hand-written refusal replays.
    monkeypatch.setenv("AQA_RECORD_CASSETTES", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with cassette("refusal"):
        reply = ask_for_a_verdict(adapter(sonnet))

    assert reply.refused


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
# (tools, structured_output) are checked against their own recorded response, so
# a real re-recording passes too; the others are hand-written by design.


def test_a_tool_call_comes_back_with_its_usage_counting_cached_input(
    sonnet: RoutedModel, cassette: Cassette
) -> None:
    with cassette("tools") as recording:
        reply = ask_with_tools(adapter(sonnet))

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
        reply = ask_for_a_verdict(adapter(sonnet))

    (response,) = recording.responses
    (text,) = [
        block["text"] for block in response["content"] if block["type"] == "text"
    ]
    assert reply.parsed == Verdict(**json.loads(text))
    assert not reply.refused
    usage = response["usage"]
    assert reply.usage.output_tokens == usage["output_tokens"]


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
