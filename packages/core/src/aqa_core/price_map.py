"""The pinned price map: a vendored copy of LiteLLM's
`model_prices_and_context_window.json` (ADR-0007 amendment)."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import Field, ValidationError
from pydantic_core import ErrorDetails

from aqa_core.schema import StrictModel

UPSTREAM: Final = "BerriAI/litellm"
MAP_FILE: Final = "model_prices_and_context_window.json"
PIN_FILE: Final = "pin.json"
VENDORED: Final = Path(__file__).resolve().parent / "price_map_data"
# A full commit ID, not anchored: use `re.fullmatch`. Pydantic's `pattern` is a
# search, so a field's pattern is this one between `^` and `$` (LAB_NOTES,
# 2026-10-01).
COMMIT_PATTERN: Final = r"[0-9a-f]{40}"


class PriceMapError(Exception):
    """The vendored price map can't be trusted."""


class Pin(StrictModel):
    """Which upstream commit the vendored map was copied from, and the sha256
    of the copy."""

    upstream: Literal["BerriAI/litellm"]
    commit: Annotated[str, Field(pattern=f"^{COMMIT_PATTERN}$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


Capability = Literal["tools", "structured_output", "vision"]
PriceSource = Literal["map", "config"]


@dataclass(frozen=True, kw_only=True)
class ModelInfo:
    """What a model can do and what it costs, in US dollars per million tokens.
    `provider` is the map's `litellm_provider` (None for a model a project
    declares, or an entry that names none), and `tiered` says the map also
    prices the model per token above a token threshold, which cost records don't
    apply."""

    capabilities: frozenset[Capability]
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal
    source: PriceSource
    provider: str | None = None
    tiered: bool = False


@dataclass(frozen=True)
class PriceMap:
    """The map, as pinned: `version` is the upstream commit it was copied from."""

    version: str
    models: Mapping[str, ModelInfo]


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise PriceMapError(f"cannot read {path}: {error.strerror}") from None


def _problem(detail: ErrorDetails) -> str:
    key = ".".join(map(str, detail["loc"]))
    return f"{key}: {detail['msg']}" if key else detail["msg"]


def plain(number: Decimal) -> Decimal:
    """`number` with no trailing zeros and no positive exponent (1000, not 1E+3),
    and a zero without a sign. A tiny number still prints as 2E-7: only the
    value is exact."""
    if not number:
        return Decimal(0)
    return Decimal(format(number.normalize(), "f"))


_PRICES = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
)
_FLAGS: Mapping[str, Capability] = {
    "supports_function_calling": "tools",
    "supports_response_schema": "structured_output",
    "supports_vision": "vision",
}
# A price per token that changes above a token threshold, such as
# input_cost_per_token_above_200k_tokens. "above_1hr" is a cache lifetime.
_TIERS = re.compile(
    r"(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost)"
    r"_above_\d+k_tokens"
)


class _FieldError(ValueError):
    """A price or flag in an entry is not what LiteLLM documents."""


def _rate(entry: Mapping[str, object], field: str) -> Decimal | None:
    """`entry`'s `field`, a price per token, as dollars per million tokens; None
    if the entry has no such field. LiteLLM writes a price as a float such as
    2e-06 (or 0), read here as an exact decimal."""
    if field not in entry:
        return None
    value = entry[field]
    if isinstance(value, bool) or not isinstance(value, int | Decimal) or value < 0:
        raise _FieldError(f"{field} must be a finite number, 0 or more")
    return plain(Decimal(value).scaleb(6))


def _capabilities(entry: Mapping[str, object]) -> frozenset[Capability]:
    capabilities: set[Capability] = set()
    for flag, capability in _FLAGS.items():
        value = entry.get(flag, False)
        if not isinstance(value, bool):
            raise _FieldError(f"{flag} must be true or false")
        if value:
            capabilities.add(capability)
    return frozenset(capabilities)


def _provider(entry: Mapping[str, object]) -> str | None:
    if "litellm_provider" not in entry:
        return None
    value = entry["litellm_provider"]
    if not isinstance(value, str):
        raise _FieldError("litellm_provider must be text")
    return value


def _model(entry: Mapping[str, object]) -> ModelInfo | None:
    """The model `entry` describes, or None if it has no token prices: an
    image, audio or embedding model is not one we can charge per token."""
    capabilities = _capabilities(entry)
    input_rate, output_rate, cache_rate = (_rate(entry, field) for field in _PRICES)
    if input_rate is None or output_rate is None:
        return None
    return ModelInfo(
        capabilities=capabilities,
        input_usd_per_mtok=input_rate,
        output_usd_per_mtok=output_rate,
        cached_input_usd_per_mtok=input_rate if cache_rate is None else cache_rate,
        source="map",
        provider=_provider(entry),
        tiered=any(_TIERS.fullmatch(key) for key in entry),
    )


def parse_models(data: bytes, source: str) -> dict[str, ModelInfo]:
    """The priced models in `data`, a price map read from `source`: a JSON
    object of objects, without its `sample_spec` entry. A price or capability
    flag that is not what LiteLLM documents is an error, never skipped."""
    try:
        entries = json.loads(data, parse_float=Decimal)
    except ValueError as error:
        raise PriceMapError(f"{source} is not JSON: {error}") from None
    if not isinstance(entries, dict):
        raise PriceMapError(f"{source} is not a JSON object")
    models: dict[str, ModelInfo] = {}
    for name, entry in entries.items():
        # sample_spec documents the file's keys; it is not a model.
        if name == "sample_spec":
            continue
        if not isinstance(entry, dict):
            raise PriceMapError(f"{source}: '{name}' is not an object")
        try:
            model = _model(entry)
        except _FieldError as error:
            raise PriceMapError(f"{source}: '{name}': {error}") from None
        if model is not None:
            models[name] = model
    return models


def load_price_map(directory: Path = VENDORED) -> PriceMap:
    """The map in `directory`, once its sha256 matches the pin beside it."""
    pin_path, map_path = directory / PIN_FILE, directory / MAP_FILE
    try:
        pin = Pin.model_validate_json(_read(pin_path))
    except ValidationError as error:
        problems = "; ".join(map(_problem, error.errors()))
        raise PriceMapError(f"{pin_path}: {problems}") from None
    data = _read(map_path)
    actual = hashlib.sha256(data).hexdigest()
    if actual != pin.sha256:
        raise PriceMapError(
            f"{map_path} has sha256 {actual}, but {PIN_FILE} pins {pin.sha256} "
            f"(upstream commit {pin.commit}). The map changes only through its "
            f"script: `git restore {map_path}` undoes an edit, "
            f"`uv run python -m aqa_core.price_map_refresh {pin.commit}` fetches "
            "the pinned commit again, and the same command with a newer ref, or "
            "none for upstream's main, pins another"
        )
    models = parse_models(data, str(map_path))
    return PriceMap(version=pin.commit, models=MappingProxyType(models))


@cache
def vendored() -> PriceMap:
    """The vendored map, loaded and checked once."""
    return load_price_map()
