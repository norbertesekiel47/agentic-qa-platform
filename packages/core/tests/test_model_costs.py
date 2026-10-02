"""A model call's cost record: the llm_calls fields of DATA_MODEL §2, priced from
the pinned price map or the config's own rates (ADR-0007 amendment; #40)."""

from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from aqa_core.model_costs import (
    AppliedPrices,
    CostRecord,
    Mode,
    Status,
    Usage,
    cost_record,
)
from aqa_core.model_roles import RoutedModel, resolve_roles
from aqa_core.price_map import ModelInfo, PriceSource, vendored
from aqa_core.project import load_config
from pydantic import ValidationError

VERSION = "6a8e0a270a8a119874c41fa2f479d3dfc965fd9f"
USAGE = Usage(input_tokens=1000, cached_input_tokens=400, output_tokens=200)


def routed(
    source: PriceSource, input_rate: str, output_rate: str, cached_rate: str
) -> RoutedModel:
    info = ModelInfo(
        capabilities=frozenset({"tools"}),
        input_usd_per_mtok=Decimal(input_rate),
        output_usd_per_mtok=Decimal(output_rate),
        cached_input_usd_per_mtok=Decimal(cached_rate),
        source=source,
    )
    return RoutedModel(
        provider="anthropic", name="model-x", info=info, price_map_version=VERSION
    )


def record_of(
    model: RoutedModel, usage: Usage = USAGE, status: Status = "ok"
) -> CostRecord:
    return cost_record(
        role="navigator",
        mode="explore",
        model=model,
        usage=usage,
        latency_ms=1234,
        status=status,
    )


def test_map_prices_charge_cached_input_at_the_cache_read_rate() -> None:
    record = record_of(routed("map", "2", "10", "0.2"))

    # (600 uncached x $2 + 400 cached x $0.20 + 200 output x $10) / 1,000,000
    assert record.cost_usd == Decimal("0.00328")
    assert str(record.cost_usd) == "0.00328"


def test_config_prices_charge_cached_input_at_the_input_rate() -> None:
    record = record_of(routed("config", "0.5", "1.5", "0.5"))

    # (600 x $0.50 + 400 x $0.50 + 200 x $1.50) / 1,000,000
    assert record.cost_usd == Decimal("0.0008")


def test_a_record_cites_the_pinned_commit_and_the_map_rates_it_used() -> None:
    record = record_of(routed("map", "2", "10", "0.2"))

    assert record.price_source == "map"
    assert record.price_map_version == VERSION
    assert record.applied_prices == AppliedPrices(
        input_usd_per_mtok=Decimal(2),
        output_usd_per_mtok=Decimal(10),
        cached_input_usd_per_mtok=Decimal("0.2"),
    )


def test_a_record_cites_the_config_rates_it_used() -> None:
    record = record_of(routed("config", "0.5", "1.5", "0.5"))

    assert record.price_source == "config"
    assert record.applied_prices == AppliedPrices(
        input_usd_per_mtok=Decimal("0.5"),
        output_usd_per_mtok=Decimal("1.5"),
        cached_input_usd_per_mtok=Decimal("0.5"),
    )


@pytest.mark.parametrize("status", ["refusal", "invalid"])
def test_a_response_that_was_refused_or_unusable_is_billed_like_any_other(
    status: Status,
) -> None:
    model = routed("map", "2", "10", "0.2")

    record = record_of(model, status=status)

    assert record.status == status
    assert record.cost_usd == record_of(model).cost_usd


def test_a_call_with_no_tokens_costs_nothing() -> None:
    usage = Usage(input_tokens=0, cached_input_tokens=0, output_tokens=0)

    assert str(record_of(routed("map", "2", "10", "0.2"), usage).cost_usd) == "0"


def test_a_record_carries_what_the_call_was() -> None:
    record = record_of(routed("map", "2", "10", "0.2"))

    assert (record.role, record.mode, record.provider, record.model) == (
        "navigator",
        "explore",
        "anthropic",
        "model-x",
    )
    assert (record.input_tokens, record.cached_input_tokens, record.output_tokens) == (
        1000,
        400,
        200,
    )
    assert record.latency_ms == 1234


def test_a_record_has_the_llm_calls_columns_the_api_does_not_assign() -> None:
    record = record_of(routed("map", "2", "10", "0.2"))

    # DATA_MODEL §2's llm_calls, less id, org_id and run_id.
    assert set(record.model_dump(mode="json")) == {
        "role",
        "mode",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "latency_ms",
        "price_map_version",
        "price_source",
        "applied_prices",
        "cost_usd",
        "status",
    }
    assert record.model_dump(mode="json")["cost_usd"] == "0.00328"


@pytest.mark.parametrize(
    "tokens",
    [
        {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 0},
        {"input_tokens": -1, "cached_input_tokens": 0, "output_tokens": 0},
        {"input_tokens": 1, "cached_input_tokens": -1, "output_tokens": 0},
        {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": -1},
    ],
    ids=["cached-above-input", "negative-input", "negative-cached", "negative-output"],
)
def test_token_counts_that_cannot_be_true_are_rejected(tokens: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Usage(**tokens)


def test_the_default_model_costs_what_tech_stack_says(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("")
    model = resolve_roles(load_config(path), vendored())["navigator"].model
    usage = Usage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)

    assert record_of(model, usage).cost_usd == Decimal(2)
    assert record_of(
        model, Usage(input_tokens=0, cached_input_tokens=0, output_tokens=1_000_000)
    ).cost_usd == Decimal(10)
    assert model.price_map_version == vendored().version


@pytest.mark.parametrize(
    "change",
    [
        {"input_tokens": 1, "cached_input_tokens": 5},
        {"cost_usd": Decimal(-1)},
        {
            "applied_prices": {
                "input_usd_per_mtok": Decimal(-1),
                "output_usd_per_mtok": Decimal(1),
                "cached_input_usd_per_mtok": Decimal(1),
            }
        },
        {"latency_ms": -1},
    ],
    ids=["cached-above-input", "negative-cost", "negative-rate", "negative-latency"],
)
def test_a_record_that_cannot_be_true_is_rejected(change: dict[str, object]) -> None:
    good = record_of(routed("map", "2", "10", "0.2"))

    with pytest.raises(ValidationError):
        CostRecord.model_validate({**good.model_dump(), **change})


@pytest.mark.parametrize("mode", ["explore", "heal", "verified"])
def test_a_record_accepts_the_modes_of_data_model_2(mode: Mode) -> None:
    model = routed("map", "2", "10", "0.2")

    record = cost_record(
        role="healer",
        mode=mode,
        model=model,
        usage=USAGE,
        latency_ms=1,
        status="ok",
    )

    assert record.mode == mode


def test_a_record_refuses_strict_mode_which_makes_no_model_calls() -> None:
    good = record_of(routed("map", "2", "10", "0.2"))

    with pytest.raises(ValidationError):
        CostRecord.model_validate({**good.model_dump(), "mode": "strict"})


def test_cost_stays_exact_past_28_significant_digits() -> None:
    usage = Usage(
        input_tokens=123456789012345678, cached_input_tokens=0, output_tokens=0
    )
    model = routed("config", "7.123456789012345", "1", "7.123456789012345")

    record = record_of(model, usage)

    # The product as integers: 123456789012345678 tokens at 7.123456789012345
    # dollars per million, to the last digit.
    assert Fraction(record.cost_usd) == Fraction(
        123456789012345678 * 7123456789012345, 10**21
    )
