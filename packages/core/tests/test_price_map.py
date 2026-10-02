"""The vendored price map is checked against its pin when it is loaded
(ADR-0007 amendment, TECH_STACK §7)."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from aqa_core.price_map import (
    MAP_FILE,
    PIN_FILE,
    VENDORED,
    PriceMapError,
    load_price_map,
)

COMMIT = "6a8e0a270a8a119874c41fa2f479d3dfc965fd9f"


def write_map(directory: Path, text: str, *, commit: str = COMMIT) -> None:
    """A price map and the pin that matches it."""
    directory.mkdir(parents=True, exist_ok=True)
    data = text.encode()
    (directory / MAP_FILE).write_bytes(data)
    pin = {
        "upstream": "BerriAI/litellm",
        "commit": commit,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (directory / PIN_FILE).write_text(json.dumps(pin))


SMALL_MAP = """{
  "sample_spec": {"input_cost_per_token": 0.0, "litellm_provider": "one of ..."},
  "model-a": {"input_cost_per_token": 2e-06, "supports_vision": true}
}"""


def test_a_map_that_matches_its_pin_loads_and_cites_the_pinned_commit(
    tmp_path: Path,
) -> None:
    write_map(tmp_path, SMALL_MAP)

    price_map = load_price_map(tmp_path)

    assert price_map.version == COMMIT
    assert price_map.models["model-a"]["supports_vision"] is True


def test_sample_spec_is_documentation_not_a_model(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)

    assert set(load_price_map(tmp_path).models) == {"model-a"}


def test_prices_load_as_exact_decimals(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)

    price = load_price_map(tmp_path).models["model-a"]["input_cost_per_token"]

    assert price == Decimal("0.000002")
    assert isinstance(price, Decimal)


def test_a_modified_price_map_is_rejected(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)
    pinned = json.loads((tmp_path / PIN_FILE).read_text())["sha256"]
    # A price quietly lowered: still valid JSON.
    modified = SMALL_MAP.replace("2e-06", "2e-07")
    (tmp_path / MAP_FILE).write_text(modified)

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    message = str(raised.value)
    assert pinned in message
    assert hashlib.sha256(modified.encode()).hexdigest() in message
    assert COMMIT in message


def test_a_pin_that_names_another_sha256_is_rejected(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)
    pin = json.loads((tmp_path / PIN_FILE).read_text())
    pin["sha256"] = hashlib.sha256(b"another file").hexdigest()
    (tmp_path / PIN_FILE).write_text(json.dumps(pin))

    with pytest.raises(PriceMapError, match="sha256"):
        load_price_map(tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"commit": "main"},
        {"commit": COMMIT.upper()},
        {"commit": COMMIT + "0"},
        {"commit": "zz" + COMMIT + "zz"},
        {"commit": COMMIT + "\n--- injected"},
        {"sha256": "abc"},
        {"sha256": "0" * 65},
        {"sha256": "x" + "0" * 64},
        {"upstream": "someone-else/litellm"},
        {"extra": "key"},
    ],
    ids=[
        "branch-name",
        "uppercase-hex",
        "41-characters",
        "text-around-the-commit",
        "text-after-a-newline",
        "short-hash",
        "65-characters",
        "text-before-the-hash",
        "other-repo",
        "unknown-key",
    ],
)
def test_a_malformed_pin_is_rejected(tmp_path: Path, change: dict[str, str]) -> None:
    write_map(tmp_path, SMALL_MAP)
    pin = json.loads((tmp_path / PIN_FILE).read_text()) | change
    (tmp_path / PIN_FILE).write_text(json.dumps(pin))

    with pytest.raises(PriceMapError, match=PIN_FILE):
        load_price_map(tmp_path)


@pytest.mark.parametrize(
    "text", ["not json", "[1, 2]"], ids=["not-json", "not-an-object"]
)
def test_a_pinned_file_that_is_not_a_json_object_is_rejected(
    tmp_path: Path, text: str
) -> None:
    write_map(tmp_path, text)

    with pytest.raises(PriceMapError, match=MAP_FILE):
        load_price_map(tmp_path)


def test_the_vendored_price_map_matches_its_pin_and_prices_the_default_model() -> None:
    pin = json.loads((VENDORED / PIN_FILE).read_text())

    price_map = load_price_map()

    assert price_map.version == pin["commit"]
    assert (
        hashlib.sha256((VENDORED / MAP_FILE).read_bytes()).hexdigest() == pin["sha256"]
    )
    # TECH_STACK §3's default for every role.
    assert "claude-sonnet-5-5" in price_map.models


def test_a_pin_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)
    (tmp_path / PIN_FILE).write_bytes(b'{"commit": "\xff\xfe"}')

    with pytest.raises(PriceMapError, match=PIN_FILE):
        load_price_map(tmp_path)
