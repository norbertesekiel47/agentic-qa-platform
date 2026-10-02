"""The pinned price map: a vendored copy of LiteLLM's
`model_prices_and_context_window.json` (ADR-0007 amendment)."""

import hashlib
import json
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


@dataclass(frozen=True)
class ModelInfo:
    """What a model can do and what it costs, in US dollars per million tokens."""

    capabilities: frozenset[Capability]
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal
    source: PriceSource


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


def _rate(
    source: str, name: str, entry: Mapping[str, object], field: str
) -> Decimal | None:
    """`entry`'s `field`, a price per token, as dollars per million tokens; None
    if the entry has no such field. LiteLLM writes a price as a float such as
    2e-06 (or 0), read here as an exact decimal."""
    if field not in entry:
        return None
    value = entry[field]
    number = (
        Decimal(value)
        if isinstance(value, int | Decimal) and not isinstance(value, bool)
        else None
    )
    if number is None or not number.is_finite() or number < 0:
        raise PriceMapError(
            f"{source}: '{name}': {field} must be a finite number, 0 or more"
        )
    return plain(number.scaleb(6))


def _capabilities(
    source: str, name: str, entry: Mapping[str, object]
) -> frozenset[Capability]:
    capabilities: set[Capability] = set()
    for flag, capability in _FLAGS.items():
        if flag not in entry:
            continue
        if not isinstance(entry[flag], bool):
            raise PriceMapError(f"{source}: '{name}': {flag} must be true or false")
        if entry[flag]:
            capabilities.add(capability)
    return frozenset(capabilities)


def _model(source: str, name: str, entry: Mapping[str, object]) -> ModelInfo | None:
    """The model `entry` describes, or None if it has no token prices: an
    image, audio or embedding model is not one we can charge per token."""
    capabilities = _capabilities(source, name, entry)
    input_rate, output_rate, cache_rate = (
        _rate(source, name, entry, field) for field in _PRICES
    )
    if input_rate is None or output_rate is None:
        return None
    return ModelInfo(
        capabilities,
        input_rate,
        output_rate,
        input_rate if cache_rate is None else cache_rate,
        "map",
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
        if (model := _model(source, name, entry)) is not None:
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
