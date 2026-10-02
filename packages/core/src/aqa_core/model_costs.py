"""A model call's cost record (DATA_MODEL §2, `llm_calls`)."""

from decimal import Context, Decimal, Inexact, localcontext
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from aqa_core.config import ModelRoleName
from aqa_core.model_roles import RoutedModel
from aqa_core.price_map import ModelInfo, PriceSource, plain
from aqa_core.schema import StrictModel

_Count = Annotated[int, Field(ge=0)]
_Money = Annotated[Decimal, Field(ge=0)]
# Cost is exact: 100 digits hold any real call's tokens times its rates, and a
# call that would need more raises rather than rounds.
_EXACT = Context(prec=100, traps=[Inexact])

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
    """The rates a call was priced at, in US dollars per million tokens."""

    input_usd_per_mtok: _Money
    output_usd_per_mtok: _Money
    cached_input_usd_per_mtok: _Money


class CostRecord(Usage):
    """What one model call leaves behind: the `llm_calls` columns of DATA_MODEL
    §2 that the runner knows. Ingestion adds `id`, `org_id` and `run_id`."""

    role: ModelRoleName
    mode: Mode
    provider: str
    model: str
    latency_ms: _Count
    price_map_version: str
    price_source: PriceSource
    applied_prices: AppliedPrices
    cost_usd: _Money
    status: Status


def _cost(info: ModelInfo, usage: Usage) -> Decimal:
    """What `usage` costs at `info`'s rates, exactly."""
    uncached = usage.input_tokens - usage.cached_input_tokens
    with localcontext(_EXACT):
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
        **usage.model_dump(),
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
