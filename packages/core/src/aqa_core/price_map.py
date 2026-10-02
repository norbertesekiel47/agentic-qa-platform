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

from aqa_core.schema import StrictModel

UPSTREAM: Final = "BerriAI/litellm"
MAP_FILE: Final = "model_prices_and_context_window.json"
PIN_FILE: Final = "pin.json"
VENDORED: Final = Path(__file__).resolve().parent / "price_map_data"
# A full commit ID. Pydantic's `pattern` is a search, not a full match, so a
# pattern built from this one is anchored (LAB_NOTES, 2026-10-01).
COMMIT: Final = r"[0-9a-f]{40}"


class PriceMapError(Exception):
    """The vendored price map can't be trusted."""


class Pin(StrictModel):
    """Which upstream commit the vendored map was copied from, and the sha256
    of the copy."""

    upstream: Literal["BerriAI/litellm"]
    commit: Annotated[str, Field(pattern=f"^{COMMIT}$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


@dataclass(frozen=True)
class PriceMap:
    """The map, as pinned: `version` is the upstream commit it was copied from."""

    version: str
    models: Mapping[str, Mapping[str, object]]


def load_price_map(directory: Path = VENDORED) -> PriceMap:
    """The map in `directory`, once its sha256 matches the pin beside it."""
    try:
        pin = Pin.model_validate_json((directory / PIN_FILE).read_bytes())
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, detail['loc']))}: {detail['msg']}"
            for detail in error.errors()
        )
        raise PriceMapError(f"{directory / PIN_FILE}: {problems}") from None
    data = (directory / MAP_FILE).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != pin.sha256:
        raise PriceMapError(
            f"{directory / MAP_FILE} has sha256 {actual}, but {PIN_FILE} pins "
            f"{pin.sha256} (upstream commit {pin.commit}). Don't edit the map by "
            "hand: refresh it with `uv run python -m aqa_core.price_map_refresh`"
        )
    # Prices stay exact: LiteLLM writes them as floats such as 2e-06.
    try:
        entries = json.loads(data, parse_float=Decimal)
    except ValueError as error:
        raise PriceMapError(f"{directory / MAP_FILE} is not JSON: {error}") from None
    if not isinstance(entries, dict):
        raise PriceMapError(f"{directory / MAP_FILE} is not a JSON object")
    # sample_spec documents the file's keys; it is not a model.
    models = {name: entry for name, entry in entries.items() if name != "sample_spec"}
    return PriceMap(version=pin.commit, models=models)
