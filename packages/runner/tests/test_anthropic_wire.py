"""What the Anthropic adapter puts on the wire (ADR-0007 amendment, TESTING §4):
tools are strict and `tool_choice` is `auto`, a schema is the response format,
and no request forces a tool, because Sonnet 5.5 and Opus 5.5 answer a forced
`tool_choice` of `any` or `tool` with a 400. The cassettes hold the requests the
adapter really sent (the responses are hand-written; see each cassette's header
and ADR-0007)."""

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from aqa_core.model_roles import RoutedModel
from aqa_runner.anthropic_client import AnthropicClient, build_client
from aqa_runner.chat_client import Reply
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
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


def forced(body: dict[str, Any]) -> bool:
    """Whether a request body forces the model to call a tool."""
    return body.get("tool_choice", {}).get("type") in {"any", "tool"}


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
    # claude-sonnet-5-5 (it exempts only claude-opus-5-5 and claude-fable-5-1),
    # so a request like that would be rejected with a 400. This is why the
    # adapter asks for `json_schema`. If this fails, the library changed: update
    # the ADR-0007 amendment.
    chat = ChatAnthropic.model_validate({"model": sonnet.name})

    default = cast(Any, chat.with_structured_output(Verdict))

    assert forced({"tool_choice": default.first.kwargs["tool_choice"]})


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
