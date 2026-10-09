"""What a run was billed, kept by its router (ADR-0007's #161 amendment):
every response's cost record, once each and in order, so an interruption that
skips `Routed` and `ModelCallError` loses none of them; and handed to the
caller as it is priced, with a ceiling on fallbacks (the #53 P3 amendment)."""

import asyncio
import functools
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from aqa_core.config import ModelRoleName
from aqa_core.model_costs import CostRecord, Usage
from aqa_runner.chat_client import Reply
from aqa_runner.model_router import ModelCallError, ModelRouter, Routed, Spend
from aqa_runner.run_record import COSTS, LineCheck, RunRecord, UnrecordedCostError
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

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


def ask(
    model_router: ModelRouter,
    spend: Spend | None,
    *,
    role: ModelRoleName = "healer",
    schema: type[BaseModel] | None = None,
) -> Routed:
    messages = [HumanMessage(content="go")]
    return asyncio.run(
        model_router.call(role, "heal", messages, schema=schema, spend=spend)
    )


def priced(records: Sequence[CostRecord]) -> list[tuple[str, str, Decimal]]:
    return [(c.model, c.status, c.cost_usd) for c in records]


def refused_then(
    tmp_path: Path, opus: FakeClient, first: Reply | None = None
) -> tuple[ModelRouter, Factory]:
    """A healer: Sonnet gives `first` (a refusal by default), `opus` falls back."""
    sonnet = FakeClient(first or reply(refused=True, usage=tokens(100)))
    factory = Factory(**{SONNET: sonnet, OPUS: opus})
    return router(tmp_path, FALLBACK, factory), factory


REFUSAL = (SONNET, "refusal", Decimal("0.00027"))
ANSWER = (OPUS, "ok", Decimal("0.00054"))


def test_each_response_reaches_on_priced_before_the_next_call(
    tmp_path: Path,
) -> None:
    opus = FakeClient(reply(usage=tokens(100)))
    model_router, _ = refused_then(tmp_path, opus)
    delivered: list[CostRecord] = []
    fallbacks_asked: list[int] = []

    def on_priced(record: CostRecord) -> None:
        delivered.append(record)
        fallbacks_asked.append(len(opus.calls))

    routed = ask(model_router, Spend(on_priced))

    assert priced(delivered) == [REFUSAL, ANSWER]
    # The refusal was delivered before the fallback was asked.
    assert fallbacks_asked == [0, 1]
    assert list(map(id, delivered)) == list(map(id, routed.calls))
    assert list(map(id, delivered)) == list(map(id, model_router.completed_calls))


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [("0.00001", [REFUSAL]), ("0.00027", [REFUSAL]), ("0.00028", [REFUSAL, ANSWER])],
)
def test_a_refusal_at_the_ceiling_makes_no_fallback_call(
    tmp_path: Path, ceiling: str, expected: list[tuple[str, str, Decimal]]
) -> None:
    # Under the ceiling the fallback is asked, and may end over it.
    opus = FakeClient(reply(usage=tokens(100)))
    model_router, factory = refused_then(tmp_path, opus)
    delivered: list[CostRecord] = []

    routed = ask(model_router, Spend(delivered.append, Decimal(ceiling)))

    assert priced(delivered) == priced(routed.calls) == expected
    assert routed.outcome == expected[-1][1]
    assert [model for _, model, _ in factory.built] == [m for m, _, _ in expected]


def test_the_ceiling_counts_only_this_calls_records(tmp_path: Path) -> None:
    # A caller passes what is left of its budget, so an earlier call's charge
    # isn't counted twice.
    sonnet = FakeClient(
        reply(usage=tokens(200)), reply(refused=True, usage=tokens(100))
    )
    opus = FakeClient(reply(usage=tokens(100)))
    model_router = router(tmp_path, FALLBACK, Factory(**{SONNET: sonnet, OPUS: opus}))
    ask(model_router, None)

    routed = ask(model_router, Spend(lambda _record: None, Decimal("0.00028")))

    assert priced(routed.calls) == [REFUSAL, ANSWER]


def test_cancellation_during_a_fallback_keeps_the_refusals_record_through_on_priced(
    tmp_path: Path,
) -> None:
    # A working router is cancelled at once; the delay bounds a broken one.
    opus = FakeClient(reply(usage=tokens(100)), delay=1)
    model_router, _ = refused_then(tmp_path, opus)
    delivered: list[CostRecord] = []

    async def cancelled_during_the_fallback() -> None:
        task = asyncio.current_task()
        assert task is not None

        def on_priced(record: CostRecord) -> None:
            delivered.append(record)
            # The next await is the fallback's, which the router asks next.
            asyncio.get_running_loop().call_soon(task.cancel)

        messages = [HumanMessage(content="go")]
        await model_router.call("healer", "heal", messages, spend=Spend(on_priced))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_during_the_fallback())

    assert len(opus.calls) == 1
    assert priced(delivered) == billed(model_router) == [REFUSAL]


def test_a_call_with_neither_behaves_as_before(tmp_path: Path) -> None:
    opus = FakeClient(reply(usage=tokens(100)))
    model_router, factory = refused_then(tmp_path, opus)

    routed = ask(model_router, None)

    assert priced(routed.calls) == billed(model_router) == [REFUSAL, ANSWER]
    assert len(factory.clients[SONNET].calls) == len(opus.calls) == 1


class Verdict(BaseModel):
    ok: bool


@pytest.mark.parametrize(
    ("first", "schema"),
    [
        (reply(usage=tokens(100)), Verdict),
        (reply(complete=False, usage=tokens(100)), None),
    ],
    ids=["unparsed", "cut_off"],
)
def test_an_invalid_response_reaches_on_priced_once_and_gets_no_fallback(
    tmp_path: Path, first: Reply, schema: type[BaseModel] | None
) -> None:
    opus = FakeClient(reply(usage=tokens(100)))
    model_router, _ = refused_then(tmp_path, opus, first)
    delivered: list[CostRecord] = []

    routed = ask(model_router, Spend(delivered.append), schema=schema)

    assert priced(delivered) == [(SONNET, "invalid", Decimal("0.00027"))]
    assert list(map(id, delivered)) == list(map(id, routed.calls))
    assert list(map(id, delivered)) == list(map(id, model_router.completed_calls))
    assert opus.calls == []


def refusing(model: str) -> LineCheck:
    """A cost-line check that refuses `model`'s lines."""

    def check(line: str) -> None:
        if model in line:
            raise ValueError("refused")

    return check


@pytest.mark.parametrize(
    ("first", "schema", "refused", "fallbacks"),
    [
        (reply(refused=True, usage=tokens(100)), None, SONNET, 0),
        (reply(refused=True, usage=tokens(100)), None, OPUS, 1),
        (reply(usage=tokens(100)), Verdict, SONNET, 0),
    ],
    ids=["refusal", "fallback", "invalid"],
)
def test_an_on_priced_failure_propagates_and_no_later_call_is_made(
    tmp_path: Path,
    first: Reply,
    schema: type[BaseModel] | None,
    refused: str,
    fallbacks: int,
) -> None:
    # The run record's own refusal of a cost line, as explore will meet it.
    opus = FakeClient(reply(usage=tokens(100)))
    model_router, _ = refused_then(tmp_path, opus, first)
    record = RunRecord.create(tmp_path)
    spend = Spend(functools.partial(record.cost, check=refusing(refused)))

    with pytest.raises(UnrecordedCostError) as raised:
        ask(model_router, spend, schema=schema)

    assert raised.value.call is model_router.completed_calls[-1]
    assert raised.value.call.model == refused
    assert len(opus.calls) == fallbacks
    costs = record.path / COSTS
    lines = costs.read_text().splitlines() if costs.exists() else []
    assert [json.loads(line)["model"] for line in lines] == [SONNET] * fallbacks


EXACT = "acme/exact"
EXACT_CONFIG = (
    f"roles: {{ navigator: {{ model: {EXACT}, fallback: {OPUS} }} }}\n"
    f"models: {{ {EXACT}: {{ capabilities: [tools, structured_output], "
    "input_usd_per_mtok: 123456789012345, output_usd_per_mtok: 1.0e-15 } }\n"
)
# What `tokens(100)` costs on EXACT: 32 significant digits, so a sum in the
# default 28-digit context rounds it below itself.
EXACT_COST = Decimal("12345678901.234500000000000000007")


def test_a_refusal_at_a_ceiling_past_28_digits_makes_no_fallback_call(
    tmp_path: Path,
) -> None:
    opus = FakeClient(reply(usage=tokens(100)))
    refusal = FakeClient(reply(refused=True, usage=tokens(100)))
    model_router = router(
        tmp_path, EXACT_CONFIG, Factory(**{EXACT: refusal, OPUS: opus})
    )

    routed = ask(
        model_router, Spend(lambda _record: None, EXACT_COST), role="navigator"
    )

    assert priced(routed.calls) == [(EXACT, "refusal", EXACT_COST)]
    assert opus.calls == []
    left = Spend(lambda _record: None, Decimal(10**20)).after(routed.calls)
    assert left.ceiling_usd == Decimal("99999999987654321098.765499999999999999993")
