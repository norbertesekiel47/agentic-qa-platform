"""What a run was billed, kept by its router (ADR-0007's #161 amendment):
every response's cost record, once each and in order, so an interruption that
skips `Routed` and `ModelCallError` loses none of them."""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from aqa_core.model_costs import Usage
from aqa_runner.model_router import ModelCallError, ModelRouter
from langchain_core.messages import HumanMessage

from packages.runner.tests.test_model_router import (
    FALLBACK,
    Factory,
    FakeClient,
    call,
    reply,
    router,
)

pytestmark = pytest.mark.usefixtures("reset_tracing")

SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"


def tokens(input_tokens: int) -> Usage:
    return Usage(input_tokens=input_tokens, cached_input_tokens=0, output_tokens=7)


def billed(model_router: ModelRouter) -> list[tuple[str, str, Decimal]]:
    return [(c.model, c.status, c.cost_usd) for c in model_router.completed_calls]


def test_completed_calls_are_ordered_once_across_success_and_fallback(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(
        reply(refused=True, usage=tokens(100)), reply(usage=tokens(200))
    )
    opus = FakeClient(reply(usage=tokens(100)))
    model_router = router(tmp_path, FALLBACK, Factory(**{SONNET: sonnet, OPUS: opus}))

    first = call(model_router, role="healer", mode="heal")
    second = call(model_router, role="healer", mode="heal")

    # Sonnet: $2 in and $10 out per million tokens; Opus: $4 and $20.
    assert billed(model_router) == [
        (SONNET, "refusal", Decimal("0.00027")),
        (OPUS, "ok", Decimal("0.00054")),
        (SONNET, "ok", Decimal("0.00047")),
    ]
    assert model_router.completed_calls == (*first.calls, *second.calls)


def test_equal_billed_records_are_not_deduplicated(tmp_path: Path) -> None:
    # Two responses with the same usage are two charges.
    sonnet = FakeClient(reply(usage=tokens(100)), reply(usage=tokens(100)))
    model_router = router(tmp_path, "", Factory(**{SONNET: sonnet}))

    call(model_router)
    call(model_router)

    assert billed(model_router) == [
        (SONNET, "ok", Decimal("0.00027")),
        (SONNET, "ok", Decimal("0.00027")),
    ]


def test_completed_calls_belong_to_one_router_run(tmp_path: Path) -> None:
    factory = Factory(**{SONNET: FakeClient(reply(usage=tokens(100)))})
    used = router(tmp_path, "", factory)
    fresh = router(tmp_path, "", factory)

    assert used.completed_calls == ()
    assert factory.built == []
    call(used)

    assert billed(used) == [(SONNET, "ok", Decimal("0.00027"))]
    assert fresh.completed_calls == ()


def test_a_refusal_survives_its_fallback_being_cancelled(tmp_path: Path) -> None:
    sonnet = FakeClient(reply(refused=True, usage=tokens(100)))
    opus = FakeClient(reply(usage=tokens(100)), delay=60)
    model_router = router(tmp_path, FALLBACK, Factory(**{SONNET: sonnet, OPUS: opus}))
    asked = model_router.call("healer", "heal", [HumanMessage(content="go")])

    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(asked, timeout=1))

    assert billed(model_router) == [(SONNET, "refusal", Decimal("0.00027"))]


def test_accounting_snapshot_keeps_normal_and_model_failure_contracts(
    tmp_path: Path,
) -> None:
    sonnet = FakeClient(
        ConnectionError("no response"), reply(refused=True, usage=tokens(100))
    )
    opus = FakeClient(ConnectionError("no response either"))
    model_router = router(tmp_path, FALLBACK, Factory(**{SONNET: sonnet, OPUS: opus}))

    # A first attempt with no response bills nothing and raises as it is.
    with pytest.raises(ConnectionError):
        call(model_router, role="healer", mode="heal")
    assert model_router.completed_calls == ()
    with pytest.raises(ModelCallError) as raised:
        call(model_router, role="healer", mode="heal")

    assert raised.value.records == model_router.completed_calls
    assert billed(model_router) == [(SONNET, "refusal", Decimal("0.00027"))]
