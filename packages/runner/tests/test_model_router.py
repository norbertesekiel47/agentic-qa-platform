"""The model router: one call per role, a cost record for every response, and
the role's fallback when the first model refuses (ADR-0007 amendments; #40)."""

import asyncio
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import Effort, ModelRoleName
from aqa_core.model_costs import Mode, Usage
from aqa_core.model_roles import RoutedModel
from aqa_core.project import load_config
from aqa_runner.anthropic_client import build_client
from aqa_runner.chat_client import ChatClient, Reply
from aqa_runner.model_router import ModelRouter, Routed
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langsmith.utils import tracing_is_enabled
from pydantic import BaseModel

pytestmark = pytest.mark.usefixtures("reset_tracing")

USAGE = Usage(input_tokens=1000, cached_input_tokens=400, output_tokens=200)


class Verdict(BaseModel):
    ok: bool
    reason: str


def reply(
    text: str = "done",
    *,
    refused: bool = False,
    parsed: BaseModel | None = None,
    usage: Usage = USAGE,
) -> Reply:
    return Reply(
        message=AIMessage(content=text), usage=usage, refused=refused, parsed=parsed
    )


class FakeClient:
    """Answers each call with the next scripted reply, or raises it."""

    def __init__(self, *replies: Reply | Exception, delay: float = 0) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.calls: list[tuple[Sequence[BaseMessage], Sequence[BaseTool], object]] = []

    async def call(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        schema: type[BaseModel] | None,
    ) -> Reply:
        self.calls.append((messages, tools, schema))
        await asyncio.sleep(self.delay)
        answer = self.replies.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Factory:
    """Builds the client scripted for a model, and remembers every build."""

    def __init__(self, **clients: FakeClient) -> None:
        self.clients = clients
        self.built: list[tuple[str, str, Effort | None]] = []

    def __call__(self, model: RoutedModel, effort: Effort | None) -> ChatClient:
        self.built.append((model.provider, model.name, effort))
        return self.clients[model.name]


def router(tmp_path: Path, config: str, factory: Factory) -> ModelRouter:
    path = tmp_path / "config.yaml"
    path.write_text(config)
    return ModelRouter.from_config(load_config(path), factory)


def call(
    routed: ModelRouter,
    *,
    role: ModelRoleName = "navigator",
    mode: Mode = "explore",
    schema: type[BaseModel] | None = None,
) -> Routed:
    return asyncio.run(
        routed.call(role, mode, [HumanMessage(content="go")], schema=schema)
    )


def test_a_router_builds_no_client_until_a_call(tmp_path: Path) -> None:
    factory = Factory(**{"claude-sonnet-5-5": FakeClient(reply())})

    routed = router(tmp_path, "", factory)

    assert factory.built == []
    call(routed)
    assert factory.built == [("anthropic", "claude-sonnet-5-5", None)]


def test_a_response_yields_a_cost_record_with_the_llm_calls_fields(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(reply("hello"), delay=0.05)
    routed = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    result = call(routed, role="healer", mode="heal")

    assert (result.outcome, result.message.content) == ("ok", "hello")
    (record,) = result.calls
    assert (record.role, record.mode, record.provider, record.model) == (
        "healer",
        "heal",
        "anthropic",
        "claude-sonnet-5-5",
    )
    assert (record.input_tokens, record.cached_input_tokens, record.output_tokens) == (
        1000,
        400,
        200,
    )
    assert record.status == "ok"
    # (600 x $2 + 400 x $0.20 + 200 x $10) / 1,000,000, at the pinned map's rates.
    assert record.cost_usd == Decimal("0.00328")
    assert record.price_source == "map"
    assert record.latency_ms >= 40


FALLBACK = "roles: { healer: { fallback: claude-opus-5-5 } }\n"


def test_a_refusal_is_recorded_as_a_refusal_and_the_fallback_model_is_called(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(reply("I can't help", refused=True))
    opus = FakeClient(reply("here you go"))
    routed = router(
        tmp_path,
        FALLBACK,
        Factory(**{"claude-sonnet-5-5": sonnet, "claude-opus-5-5": opus}),
    )

    result = call(routed, role="healer", mode="heal")

    assert (result.outcome, result.message.content) == ("ok", "here you go")
    refused, answered = result.calls
    assert (refused.model, refused.status) == ("claude-sonnet-5-5", "refusal")
    assert (answered.model, answered.status) == ("claude-opus-5-5", "ok")
    # Each record is priced at its own model's rates: the refused response was
    # billed too. Opus 5.5 is $4 in, $20 out, $0.20 cached.
    assert refused.cost_usd == Decimal("0.00328")
    assert answered.cost_usd == Decimal("0.00648")
    assert len(sonnet.calls) == len(opus.calls) == 1


def test_a_refusal_with_no_fallback_is_the_outcome(tmp_path: Path) -> None:
    refusal = reply("I can't help", refused=True)
    routed = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": FakeClient(refusal)}))

    result = call(routed)

    assert result.outcome == "refusal"
    assert result.message.content == "I can't help"
    (record,) = result.calls
    assert record.status == "refusal"


def test_a_fallback_that_refuses_too_ends_as_a_refusal_with_both_recorded(
    tmp_path: Path,
) -> None:
    factory = Factory(
        **{
            "claude-sonnet-5-5": FakeClient(reply(refused=True)),
            "claude-opus-5-5": FakeClient(reply("no, really", refused=True)),
        }
    )

    result = call(router(tmp_path, FALLBACK, factory), role="healer")

    assert result.outcome == "refusal"
    assert result.message.content == "no, really"
    assert [record.status for record in result.calls] == ["refusal", "refusal"]


def test_the_fallback_is_left_alone_when_the_first_model_answers(
    tmp_path: Path,
) -> None:
    opus = FakeClient()
    factory = Factory(
        **{"claude-sonnet-5-5": FakeClient(reply()), "claude-opus-5-5": opus}
    )
    routed = router(tmp_path, FALLBACK, factory)

    result = call(routed, role="healer")

    assert [record.model for record in result.calls] == ["claude-sonnet-5-5"]
    assert opus.calls == []
    assert [name for _, name, _ in factory.built] == ["claude-sonnet-5-5"]


def test_a_schema_answer_that_does_not_parse_is_recorded_as_invalid(
    tmp_path: Path,
) -> None:
    # It was billed, so it is recorded; the caller decides what to do (a retry,
    # a fallback is only for refusals).
    opus = FakeClient()
    factory = Factory(
        **{"claude-sonnet-5-5": FakeClient(reply("not json")), "claude-opus-5-5": opus}
    )

    result = call(router(tmp_path, FALLBACK, factory), role="healer", schema=Verdict)

    assert (result.outcome, result.parsed) == ("invalid", None)
    (record,) = result.calls
    assert (record.status, record.cost_usd) == ("invalid", Decimal("0.00328"))
    assert opus.calls == []


def test_a_schema_answer_that_parses_is_returned_parsed(tmp_path: Path) -> None:
    sonnet = FakeClient(
        reply('{"ok": true}', parsed=Verdict(ok=True, reason="cart empty"))
    )
    routed = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    result = call(routed, schema=Verdict)

    assert (result.outcome, result.parsed) == (
        "ok",
        Verdict(ok=True, reason="cart empty"),
    )
    assert sonnet.calls[0][2] is Verdict


def test_a_call_that_gets_no_response_records_nothing_and_raises(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(ConnectionError("the network is down"))
    routed = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    with pytest.raises(ConnectionError, match="the network is down"):
        call(routed)


@pytest.mark.parametrize("mode", ["explore", "heal", "verified"])
def test_the_calls_mode_is_on_every_record(tmp_path: Path, mode: Mode) -> None:
    factory = Factory(
        **{
            "claude-sonnet-5-5": FakeClient(reply(refused=True)),
            "claude-opus-5-5": FakeClient(reply()),
        }
    )

    result = call(router(tmp_path, FALLBACK, factory), role="healer", mode=mode)

    assert [record.mode for record in result.calls] == [mode, mode]


def test_a_client_is_built_once_per_model_and_effort(tmp_path: Path) -> None:
    sonnet = FakeClient(reply(), reply(), reply())
    factory = Factory(**{"claude-sonnet-5-5": sonnet})
    routed = router(tmp_path, "roles: { verifier: { effort: high } }\n", factory)

    call(routed, role="navigator")
    call(routed, role="navigator")
    call(routed, role="verifier")

    assert factory.built == [
        ("anthropic", "claude-sonnet-5-5", None),
        ("anthropic", "claude-sonnet-5-5", "high"),
    ]


def test_a_roles_effort_reaches_its_client_and_its_fallbacks(tmp_path: Path) -> None:
    factory = Factory(
        **{
            "claude-sonnet-5-5": FakeClient(reply(refused=True)),
            "claude-opus-5-5": FakeClient(reply()),
        }
    )
    config = "roles: { healer: { effort: xhigh, fallback: claude-opus-5-5 } }\n"

    call(router(tmp_path, config, factory), role="healer")

    assert factory.built == [
        ("anthropic", "claude-sonnet-5-5", "xhigh"),
        ("anthropic", "claude-opus-5-5", "xhigh"),
    ]


def test_every_role_routes_to_the_model_its_config_names(tmp_path: Path) -> None:
    sonnet = FakeClient(reply(), reply(), reply(), reply())
    routed = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    roles: list[ModelRoleName] = ["navigator", "verifier", "healer", "vision_fallback"]
    results = [call(routed, role=role) for role in roles]

    assert [r.calls[0].role for r in results] == roles


def test_constructing_a_router_switches_ambient_tracing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING", "true")
    assert tracing_is_enabled()

    router(tmp_path, "", Factory())

    assert not tracing_is_enabled()
    assert "LANGCHAIN_TRACING" not in os.environ


VERDICT_PROMPT = "Is the cart empty? Answer with ok and a reason."


def refusal_then_fallback_through_the_adapter(tmp_path: Path) -> Routed:
    """A healer call through the real Anthropic adapter, whose model refuses and
    whose fallback answers. The cassette `refusal_then_fallback` replays it."""
    path = tmp_path / "config.yaml"
    path.write_text(FALLBACK)
    routed = ModelRouter.from_config(load_config(path), build_client)
    return asyncio.run(
        routed.call(
            "healer", "heal", [HumanMessage(content=VERDICT_PROMPT)], schema=Verdict
        )
    )


def test_a_refusal_through_the_real_adapter_is_recorded_and_the_fallback_answers(
    tmp_path: Path, cassette: Callable[[str], AbstractContextManager[Any]]
) -> None:
    with cassette("refusal_then_fallback") as recording:
        result = refusal_then_fallback_through_the_adapter(tmp_path)

    # The two requests the adapter sent: the role's model, then its fallback.
    assert [body["model"] for body in recording.sent] == [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
    ]
    assert not any(
        body.get("tool_choice", {}).get("type") in {"any", "tool"}
        for body in recording.sent
    )
    assert result.outcome == "ok"
    assert result.parsed == Verdict(ok=True, reason="The cart shows no items.")
    refused, answered = result.calls
    assert (refused.model, refused.status, refused.output_tokens) == (
        "claude-sonnet-5-5",
        "refusal",
        0,
    )
    assert (answered.model, answered.status, answered.output_tokens) == (
        "claude-opus-5-5",
        "ok",
        33,
    )
    # Priced at each model's own rates: 205 in and nothing out at $2; 205 in and
    # 33 out at Opus 5.5's $4 and $20, per million tokens.
    assert refused.cost_usd == Decimal("0.00041")
    assert answered.cost_usd == Decimal("0.00148")
