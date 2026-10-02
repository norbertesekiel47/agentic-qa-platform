"""An ambient LangChain tracing variable exports nothing (SECURITY §10; ADR-0007
amendment): only an aqa-specific setting may turn export on, and nothing does
in M1."""

import os
from collections.abc import Callable

import pytest
from aqa_runner.tracing import ignore_ambient_tracing
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tracers.langchain import wait_for_all_tracers

pytestmark = pytest.mark.usefixtures("reset_tracing")


# Every spelling of "tracing on" that LangChain and LangSmith read.
SWITCHES = [
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
]


def call_a_model() -> str:
    """Call a chat model and let any trace it queued go out."""
    model = GenericFakeChatModel(messages=iter([AIMessage(content="hello")]))
    answer = model.invoke("hi").content
    wait_for_all_tracers()
    assert isinstance(answer, str)
    return answer


@pytest.mark.parametrize("switch", SWITCHES)
def test_without_the_guard_an_ambient_tracing_variable_exports(
    customers_environment: Callable[[str], None],
    exported_to_langsmith: Callable[[float], bool],
    switch: str,
) -> None:
    # The control: this setup does reach the stubbed endpoint, so the test below
    # can't pass because the stub was never listening.
    customers_environment(switch)

    call_a_model()

    assert exported_to_langsmith(5)


@pytest.mark.parametrize("switch", SWITCHES)
def test_an_ambient_tracing_variable_exports_nothing_once_the_guard_ran(
    customers_environment: Callable[[str], None],
    exported_to_langsmith: Callable[[float], bool],
    switch: str,
) -> None:
    customers_environment(switch)
    ignore_ambient_tracing()

    answer = call_a_model()

    assert answer == "hello"
    assert not exported_to_langsmith(1)


@pytest.mark.parametrize("variable", ["LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"])
def test_the_version_1_variables_are_dropped_so_calls_still_work(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    # With tracing disabled, langchain_core raises on every call while either is
    # set (callbacks/manager.py: "Tracing using LangChainTracerV1 is no longer
    # supported").
    monkeypatch.setenv(variable, "true")

    ignore_ambient_tracing()

    assert variable not in os.environ
    model = GenericFakeChatModel(messages=iter([AIMessage(content="hello")]))
    assert model.invoke("hi").content == "hello"
