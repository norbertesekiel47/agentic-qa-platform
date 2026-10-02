"""A model call's cost record (DATA_MODEL §2, `llm_calls`)."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from aqa_core.config import ModelRoleName
from aqa_core.model_roles import RoutedModel
from aqa_core.price_map import ModelInfo, PriceSource, plain
from aqa_core.schema import StrictModel

_Count = Annotated[int, Field(ge=0)]

Mode = Literal["explore", "heal", "verified"]
Status = Literal["ok", "refusal", "invalid"]


class Usage(StrictModel):
    """The tokens a call used. `input_tokens` counts every input token, the
    cached ones included, as LangChain's `usage_metadata` does."""

    input_tokens: _Count
    cached_input_tokens: _Count
    output_tokens: _Count

    @model_validator(mode="after")
    def _cached_is_part_of_the_input(self) -> Self:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens are part of the input tokens")
        return self


class AppliedPrices(StrictModel):
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal


class CostRecord(StrictModel):
    role: ModelRoleName
    mode: Mode
    provider: str
    model: str
    input_tokens: _Count
    output_tokens: _Count
    cached_input_tokens: _Count
    latency_ms: _Count
    price_map_version: str
    price_source: PriceSource
    applied_prices: AppliedPrices
    cost_usd: Decimal
    status: Status


def _cost(info: ModelInfo, usage: Usage) -> Decimal:
    """What `usage` costs at `info`'s rates, exactly."""
    uncached = usage.input_tokens - usage.cached_input_tokens
    total = (
        uncached * info.input_usd_per_mtok
        + usage.cached_input_tokens * info.cached_input_usd_per_mtok
        + usage.output_tokens * info.output_usd_per_mtok
    )
    return plain(total.scaleb(-6))


def cost_record(
    *,
    role: ModelRoleName,
    mode: Mode,
    model: RoutedModel,
    usage: Usage,
    latency_ms: int,
    status: Status,
) -> CostRecord:
    return CostRecord(
        role=role,
        mode=mode,
        provider=model.provider,
        model=model.name,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        latency_ms=latency_ms,
        price_map_version=model.price_map_version,
        price_source=model.info.source,
        applied_prices=AppliedPrices(
            input_usd_per_mtok=model.info.input_usd_per_mtok,
            output_usd_per_mtok=model.info.output_usd_per_mtok,
            cached_input_usd_per_mtok=model.info.cached_input_usd_per_mtok,
        ),
        cost_usd=_cost(model.info, usage),
        status=status,
    )
