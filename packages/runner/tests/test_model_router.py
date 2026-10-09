"""The model router: one call per role, a cost record for every response, and
the role's fallback when the first model refuses (ADR-0007 amendments; #40)."""

import asyncio
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from aqa_core.config import Effort, ModelRoleName
from aqa_core.model_costs import Mode, Usage
from aqa_core.model_roles import RoutedModel
from aqa_core.project import load_config
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.chat_client import ChatClient, Reply
from aqa_runner.model_router import ModelCallError, ModelRouter, Routed
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool, tool
from langchain_core.tracers.langchain import wait_for_all_tracers
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
    complete: bool = True,
    parsed: BaseModel | None = None,
    usage: Usage = USAGE,
) -> Reply:
    return Reply(
        message=AIMessage(content=text),
        usage=usage,
        refused=refused,
        complete=complete,
        parsed=parsed,
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
    model_router: ModelRouter,
    *,
    role: ModelRoleName = "navigator",
    mode: Mode = "explore",
    tools: Sequence[BaseTool] = (),
    schema: type[BaseModel] | None = None,
) -> Routed:
    return asyncio.run(
        model_router.call(
            role, mode, [HumanMessage(content="go")], tools=tools, schema=schema
        )
    )


def test_a_router_builds_no_client_until_a_call(tmp_path: Path) -> None:
    factory = Factory(**{"claude-sonnet-5-5": FakeClient(reply())})

    model_router = router(tmp_path, "", factory)

    assert factory.built == []
    call(model_router)
    assert factory.built == [("anthropic", "claude-sonnet-5-5", None)]


def test_a_response_yields_a_cost_record_with_the_llm_calls_fields(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(reply("hello"), delay=0.05)
    model_router = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    result = call(model_router, role="healer", mode="heal")

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
    model_router = router(
        tmp_path,
        FALLBACK,
        Factory(**{"claude-sonnet-5-5": sonnet, "claude-opus-5-5": opus}),
    )

    result = call(model_router, role="healer", mode="heal")

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
    model_router = router(
        tmp_path, "", Factory(**{"claude-sonnet-5-5": FakeClient(refusal)})
    )

    result = call(model_router)

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
    model_router = router(tmp_path, FALLBACK, factory)

    result = call(model_router, role="healer")

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
    model_router = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    result = call(model_router, schema=Verdict)

    assert (result.outcome, result.parsed) == (
        "ok",
        Verdict(ok=True, reason="cart empty"),
    )
    assert sonnet.calls[0][2] is Verdict


def test_a_call_that_gets_no_response_records_nothing_and_raises(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(ConnectionError("the network is down"))
    model_router = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    with pytest.raises(ConnectionError, match="the network is down"):
        call(model_router)


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
    model_router = router(tmp_path, "roles: { verifier: { effort: high } }\n", factory)

    call(model_router, role="navigator")
    call(model_router, role="navigator")
    call(model_router, role="verifier")

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


def test_constructing_a_router_switches_ambient_tracing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING", "true")
    assert tracing_is_enabled()

    router(tmp_path, "", Factory())

    assert not tracing_is_enabled()
    assert "LANGCHAIN_TRACING" not in os.environ


# The cassettes are keyed by this prompt and by `Verdict`'s schema, which
# test_anthropic_client.py also builds: a test module can't import another's
# (ADR-0027), so each keeps its own copy.
VERDICT_PROMPT = "Is the cart empty? Answer with ok and a reason."


def refusal_then_fallback_through_the_adapter(tmp_path: Path) -> Routed:
    """A healer call through the real Anthropic adapter, whose model refuses and
    whose fallback answers. The cassette `refusal_then_fallback` replays it."""
    path = tmp_path / "config.yaml"
    path.write_text(FALLBACK)
    model_router = ModelRouter.from_config(load_config(path), AnthropicClient)
    return asyncio.run(
        model_router.call(
            "healer", "heal", [HumanMessage(content=VERDICT_PROMPT)], schema=Verdict
        )
    )


def test_a_refusal_through_the_real_adapter_is_recorded_and_the_fallback_answers(
    tmp_path: Path, cassette: Callable[..., AbstractContextManager[Any]]
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


# Keyed like VERDICT_PROMPT: test_anthropic_client.py builds the same request.
CLICK_PROMPT = "Click the Sign in button (ref e12)."


@tool
def click(ref: str) -> str:
    """Click the element with this ref."""
    return ref


def healer_through_the_adapter(
    tmp_path: Path,
    prompt: str,
    *,
    tools: Sequence[BaseTool] = (),
    schema: type[BaseModel] | None = None,
) -> tuple[ModelRouter, Routed]:
    """A healer call through the real Anthropic adapter, whose fallback model
    is called only if the first refuses."""
    path = tmp_path / "config.yaml"
    path.write_text(FALLBACK)
    model_router = ModelRouter.from_config(load_config(path), AnthropicClient)
    routed = asyncio.run(
        model_router.call(
            "healer",
            "heal",
            [HumanMessage(content=prompt)],
            tools=tools,
            schema=schema,
        )
    )
    return model_router, routed


def test_a_tool_reply_cut_off_at_the_output_bound_is_invalid_and_billed(
    tmp_path: Path, cassette: Callable[..., AbstractContextManager[Any]]
) -> None:
    # Half a call that still validates: e1, where the page's ref is e12.
    with cassette("tools_truncated") as recording:
        model_router, result = healer_through_the_adapter(
            tmp_path, CLICK_PROMPT, tools=[click]
        )

    # One request: an invalid reply is neither retried nor sent to the fallback.
    assert [body["model"] for body in recording.sent] == ["claude-sonnet-5-5"]
    assert result.outcome == "invalid"
    assert [(call["name"], call["args"]) for call in result.message.tool_calls] == [
        ("click", {"ref": "e1"})
    ]
    assert [
        (r.model, r.status, r.input_tokens, r.cached_input_tokens, r.output_tokens)
        for r in result.calls
    ] == [("claude-sonnet-5-5", "invalid", 388, 0, 4096)]
    # 388 in at $2 and 4096 out at $10, per million tokens.
    assert result.calls[0].cost_usd == Decimal("0.041736")
    assert model_router.completed_calls == result.calls


def test_a_tool_reply_that_finished_is_ok(
    tmp_path: Path, cassette: Callable[..., AbstractContextManager[Any]]
) -> None:
    # A recorded answer: its stop reason is `tool_use`, and a re-recording
    # passes too, since the usage is read from the response played.
    with cassette("tools") as recording:
        model_router, result = healer_through_the_adapter(
            tmp_path, CLICK_PROMPT, tools=[click]
        )

    (response,) = recording.responses
    assert response["stop_reason"] == "tool_use"
    assert result.outcome == "ok"
    (record,) = result.calls
    assert (record.status, record.output_tokens) == (
        "ok",
        response["usage"]["output_tokens"],
    )
    assert model_router.completed_calls == result.calls


def test_a_schema_answer_cut_off_stays_invalid_and_unparsed(
    tmp_path: Path, cassette: Callable[..., AbstractContextManager[Any]]
) -> None:
    # LangChain repairs this answer into a Verdict that validates; the router
    # hands back none.
    with cassette("structured_truncated") as recording:
        model_router, result = healer_through_the_adapter(
            tmp_path, VERDICT_PROMPT, schema=Verdict
        )

    assert [body["model"] for body in recording.sent] == ["claude-sonnet-5-5"]
    assert (result.outcome, result.parsed) == ("invalid", None)
    assert [
        (r.status, r.input_tokens, r.cached_input_tokens, r.output_tokens)
        for r in result.calls
    ] == [("invalid", 205, 0, 4096)]
    # 205 in at $2 and 4096 out at $10, per million tokens.
    assert result.calls[0].cost_usd == Decimal("0.04137")
    assert model_router.completed_calls == result.calls


def test_a_refusal_stays_a_refusal_though_it_is_not_complete(tmp_path: Path) -> None:
    opus = FakeClient(reply("here you go"))
    factory = Factory(
        **{
            "claude-sonnet-5-5": FakeClient(
                reply("I can't help", refused=True, complete=False)
            ),
            "claude-opus-5-5": opus,
        }
    )

    result = call(router(tmp_path, FALLBACK, factory), role="healer")

    assert [record.status for record in result.calls] == ["refusal", "ok"]
    assert result.outcome == "ok"
    assert len(opus.calls) == 1


@pytest.mark.parametrize("tools", [[click], []], ids=["tools", "plain"])
def test_an_answer_the_model_did_not_finish_is_invalid_and_not_retried(
    tmp_path: Path, tools: Sequence[BaseTool]
) -> None:
    opus = FakeClient()
    cut_off = reply("half an answer", complete=False)
    factory = Factory(
        **{"claude-sonnet-5-5": FakeClient(cut_off), "claude-opus-5-5": opus}
    )
    model_router = router(tmp_path, FALLBACK, factory)

    result = call(model_router, role="healer", tools=tools)

    assert (result.outcome, [record.status for record in result.calls]) == (
        "invalid",
        ["invalid"],
    )
    assert opus.calls == []
    assert model_router.completed_calls == result.calls


def test_a_reply_the_model_did_not_finish_carries_no_parse() -> None:
    # A client can't hand the router a repaired parse of half an answer:
    # `Routed.parsed` is what a caller such as the coverage plan reads.
    with pytest.raises(ValueError, match="didn't finish carries no parse"):
        reply(
            "half an answer",
            complete=False,
            parsed=Verdict(ok=True, reason="cart empty"),
        )


def ask_a_verdict_through(client: ChatClient) -> Reply:
    return asyncio.run(client.call([HumanMessage(content=VERDICT_PROMPT)], [], Verdict))


@pytest.mark.parametrize("switch", ["LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"])
def test_a_call_through_the_real_adapter_exports_no_trace_from_a_router(
    tmp_path: Path,
    customers_environment: Callable[[str], None],
    exported_to_langsmith: Callable[[float], bool],
    cassette: Callable[..., AbstractContextManager[Any]],
    switch: str,
) -> None:
    customers_environment(switch)
    path = tmp_path / "config.yaml"
    path.write_text("")
    model_router = ModelRouter.from_config(load_config(path), AnthropicClient)

    with cassette("structured_output", replay_only=True):
        result = asyncio.run(
            model_router.call(
                "navigator",
                "explore",
                [HumanMessage(content=VERDICT_PROMPT)],
                schema=Verdict,
            )
        )
    wait_for_all_tracers()

    assert result.outcome == "ok"
    assert not exported_to_langsmith(1)


def test_the_same_call_without_a_router_does_export(
    customers_environment: Callable[[str], None],
    exported_to_langsmith: Callable[[float], bool],
    cassette: Callable[..., AbstractContextManager[Any]],
    sonnet: RoutedModel,
) -> None:
    # The control: the stand-in is listening, and only the router's guard keeps
    # the run from reaching it.
    customers_environment("LANGSMITH_TRACING")

    with cassette("structured_output", replay_only=True):
        ask_a_verdict_through(AnthropicClient(sonnet, None))
    wait_for_all_tracers()

    assert exported_to_langsmith(5)


def test_a_fallback_that_gets_no_response_loses_no_billed_record(
    tmp_path: Path,
) -> None:
    # The refusal was billed; the failure after it must not erase it.
    factory = Factory(
        **{
            "claude-sonnet-5-5": FakeClient(reply(refused=True)),
            "claude-opus-5-5": FakeClient(ConnectionError("the network is down")),
        }
    )

    with pytest.raises(ModelCallError) as raised:
        call(router(tmp_path, FALLBACK, factory), role="healer", mode="heal")

    (record,) = raised.value.records
    assert (record.model, record.status, record.mode) == (
        "claude-sonnet-5-5",
        "refusal",
        "heal",
    )
    assert isinstance(raised.value.__cause__, ConnectionError)


def test_a_strict_call_is_refused_before_any_client_is_built(tmp_path: Path) -> None:
    # Strict replay makes zero model calls (AGENTS.md §6): `strict` is no call
    # mode, and asking anyway builds nothing and bills nothing.
    factory = Factory(**{"claude-sonnet-5-5": FakeClient(reply())})
    model_router = router(tmp_path, "", factory)

    with pytest.raises(ValueError, match="'strict' is not a call mode"):
        call(model_router, mode=cast(Mode, "strict"))

    assert factory.built == []


def test_tools_reach_the_client(tmp_path: Path) -> None:
    @tool
    def click(ref: str) -> str:
        """Click the element with this ref."""
        return ref

    sonnet = FakeClient(reply())
    model_router = router(tmp_path, "", Factory(**{"claude-sonnet-5-5": sonnet}))

    call(model_router, tools=[click])

    assert sonnet.calls[0][1] == [click]


def test_each_attempt_is_timed_on_its_own(tmp_path: Path) -> None:
    factory = Factory(
        **{
            "claude-sonnet-5-5": FakeClient(reply(refused=True), delay=0.05),
            "claude-opus-5-5": FakeClient(reply()),
        }
    )

    result = call(router(tmp_path, FALLBACK, factory), role="healer")

    refused, answered = result.calls
    assert refused.latency_ms >= 40
    assert answered.latency_ms < refused.latency_ms


def test_a_role_with_its_own_model_is_routed_to_it_and_the_others_are_not(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(reply(), reply(), reply())
    opus = FakeClient(reply())
    config = "roles: { verifier: { model: claude-opus-5-5 } }\n"
    model_router = router(
        tmp_path,
        config,
        Factory(**{"claude-sonnet-5-5": sonnet, "claude-opus-5-5": opus}),
    )

    models = {
        role: call(model_router, role=role).calls[0].model
        for role in ("navigator", "verifier", "healer", "vision_fallback")
    }

    assert models == {
        "navigator": "claude-sonnet-5-5",
        "verifier": "claude-opus-5-5",
        "healer": "claude-sonnet-5-5",
        "vision_fallback": "claude-sonnet-5-5",
    }
