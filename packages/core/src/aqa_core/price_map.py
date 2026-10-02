"""The pinned price map: a vendored copy of LiteLLM's
`model_prices_and_context_window.json` (ADR-0007 amendment)."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
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


@dataclass(frozen=True)
class PriceMap:
    """The map, as pinned: `version` is the upstream commit it was copied from."""

    version: str
    models: Mapping[str, Mapping[str, object]]


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise PriceMapError(f"cannot read {path}: {error.strerror}") from None


def _problem(detail: ErrorDetails) -> str:
    key = ".".join(map(str, detail["loc"]))
    return f"{key}: {detail['msg']}" if key else detail["msg"]


def parse_models(data: bytes, source: str) -> dict[str, dict[str, object]]:
    """The models in `data`, a price map read from `source`: a JSON object of
    objects, without its `sample_spec` entry. Prices are exact decimals."""
    try:
        # LiteLLM writes prices as floats such as 2e-06.
        entries = json.loads(data, parse_float=Decimal)
    except ValueError as error:
        raise PriceMapError(f"{source} is not JSON: {error}") from None
    if not isinstance(entries, dict):
        raise PriceMapError(f"{source} is not a JSON object")
    models: dict[str, dict[str, object]] = {}
    for name, entry in entries.items():
        # sample_spec documents the file's keys; it is not a model.
        if name == "sample_spec":
            continue
        if not isinstance(entry, dict):
            raise PriceMapError(f"{source}: '{name}' is not an object")
        models[name] = entry
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
    return PriceMap(version=pin.commit, models=parse_models(data, str(map_path)))
