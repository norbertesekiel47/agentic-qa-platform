"""The vendored price map is checked against its pin when it is loaded
(ADR-0007 amendment, TECH_STACK §7)."""

import hashlib
import json
import re
import subprocess
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from aqa_core.price_map import (
    MAP_FILE,
    PIN_FILE,
    VENDORED,
    ModelInfo,
    PriceMapError,
    load_price_map,
    vendored,
)

COMMIT = "6a8e0a270a8a119874c41fa2f479d3dfc965fd9f"


def write_map(directory: Path, content: str | bytes) -> None:
    """A price map and the pin that matches it."""
    directory.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    (directory / MAP_FILE).write_bytes(data)
    pin = {
        "upstream": "BerriAI/litellm",
        "commit": COMMIT,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (directory / PIN_FILE).write_text(json.dumps(pin))


SMALL_MAP = """{
  "sample_spec": {
    "input_cost_per_token": 0.0, "output_cost_per_token": 0.0,
    "supports_function_calling": true, "litellm_provider": "one of ..."
  },
  "model-a": {
    "input_cost_per_token": 2e-06, "output_cost_per_token": 1e-05,
    "cache_read_input_token_cost": 2e-07,
    "supports_vision": true, "supports_function_calling": true
  },
  "image-model": {"mode": "image_generation", "input_cost_per_token": 1e-06}
}"""


def entry(**fields: object) -> str:
    """A map of one model, `model-a`, with these fields, as JSON."""
    return json.dumps({"model-a": fields})


def test_a_map_that_matches_its_pin_loads_and_cites_the_pinned_commit(
    tmp_path: Path,
) -> None:
    write_map(tmp_path, SMALL_MAP)

    price_map = load_price_map(tmp_path)

    assert price_map.version == COMMIT
    assert price_map.models["model-a"].capabilities == {"vision", "tools"}


def test_sample_spec_is_documentation_not_a_model(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)

    assert set(load_price_map(tmp_path).models) == {"model-a"}


def test_prices_load_as_exact_decimals_per_million_tokens(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)

    info = load_price_map(tmp_path).models["model-a"]

    assert info.input_usd_per_mtok == Decimal(2)
    assert info.output_usd_per_mtok == Decimal(10)
    # 2e-07 a token as a float times a million is 0.19999999999999998.
    assert info.cached_input_usd_per_mtok == Decimal("0.2")
    assert str(info.cached_input_usd_per_mtok) == "0.2"
    assert info.source == "map"


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
    # What to do about it: restore the pinned copy, or move to a newer commit.
    assert f"price_map_refresh {COMMIT}" in message
    assert "git restore" in message


@pytest.mark.parametrize("index", [0, 31, 63])
def test_a_pin_that_differs_from_the_maps_hash_in_one_character_is_rejected(
    tmp_path: Path, index: int
) -> None:
    write_map(tmp_path, SMALL_MAP)
    pin = json.loads((tmp_path / PIN_FILE).read_text())
    digit = "1" if pin["sha256"][index] == "0" else "0"
    pin["sha256"] = pin["sha256"][:index] + digit + pin["sha256"][index + 1 :]
    (tmp_path / PIN_FILE).write_text(json.dumps(pin))

    with pytest.raises(PriceMapError, match=r"but pin\.json pins"):
        load_price_map(tmp_path)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"commit": "main"}, "commit"),
        ({"commit": COMMIT.upper()}, "commit"),
        ({"commit": COMMIT + "0"}, "commit"),
        ({"commit": "zz" + COMMIT}, "commit"),
        ({"commit": COMMIT + "zz"}, "commit"),
        ({"commit": COMMIT + "\n--- injected"}, "commit"),
        ({"sha256": "abc"}, "sha256"),
        ({"sha256": "0" * 65}, "sha256"),
        ({"sha256": "x" + "0" * 64}, "sha256"),
        ({"sha256": "0" * 64 + "\n"}, "sha256"),
        ({"upstream": "someone-else/litellm"}, "upstream"),
        ({"extra": "key"}, "extra"),
    ],
    ids=[
        "branch-name",
        "uppercase-hex",
        "41-characters",
        "text-before-the-commit",
        "text-after-the-commit",
        "text-after-a-newline",
        "short-hash",
        "65-characters",
        "text-before-the-hash",
        "newline-after-the-hash",
        "other-repo",
        "unknown-key",
    ],
)
def test_a_malformed_pin_is_rejected_by_field(
    tmp_path: Path, change: dict[str, str], field: str
) -> None:
    write_map(tmp_path, SMALL_MAP)
    pin = json.loads((tmp_path / PIN_FILE).read_text()) | change
    (tmp_path / PIN_FILE).write_text(json.dumps(pin))

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    assert f"{PIN_FILE}: {field}: " in str(raised.value)


@pytest.mark.parametrize(
    "pin",
    [b"", b"[]", b"not json", b'{"commit": "\xff\xfe"}'],
    ids=["empty", "not-an-object", "not-json", "not-utf8"],
)
def test_a_pin_that_is_not_a_json_object_is_rejected(
    tmp_path: Path, pin: bytes
) -> None:
    write_map(tmp_path, SMALL_MAP)
    (tmp_path / PIN_FILE).write_bytes(pin)

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    # A problem with the whole file has no key to name.
    assert re.search(rf"{re.escape(PIN_FILE)}: [A-Z]", str(raised.value))


@pytest.mark.parametrize("missing", [PIN_FILE, MAP_FILE])
def test_a_missing_file_is_rejected_by_name(tmp_path: Path, missing: str) -> None:
    write_map(tmp_path, SMALL_MAP)
    (tmp_path / missing).unlink()

    with pytest.raises(PriceMapError, match=rf"cannot read .*{re.escape(missing)}"):
        load_price_map(tmp_path)


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("not json", "is not JSON"),
        (b'{"model-a": "\xff"}', "is not JSON"),
        ("[1, 2]", "is not a JSON object"),
        ('{"model-a": 1}', "'model-a' is not an object"),
        ('{"model-a": null}', "'model-a' is not an object"),
        ('{"model-a": [1]}', "'model-a' is not an object"),
    ],
    ids=[
        "not-json",
        "not-utf8",
        "not-an-object",
        "entry-is-a-number",
        "entry-is-null",
        "entry-is-a-list",
    ],
)
def test_a_pinned_file_that_is_not_a_map_of_objects_is_rejected(
    tmp_path: Path, content: str | bytes, problem: str
) -> None:
    write_map(tmp_path, content)

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    assert MAP_FILE in str(raised.value)
    assert problem in str(raised.value)


def test_the_vendored_price_map_loads_at_its_pinned_commit() -> None:
    pin = json.loads((VENDORED / PIN_FILE).read_text())

    price_map = load_price_map()

    assert price_map.version == pin["commit"]
    # The default model of every role (TECH_STACK §3). A refresh that drops or
    # renames it fails here, before role validation does.
    assert "claude-sonnet-5-5" in price_map.models


def test_git_checks_the_vendored_map_out_byte_for_byte() -> None:
    # With core.autocrlf=true, git rewrites a file's line endings on checkout,
    # and the rewritten map no longer matches its pin.
    run = subprocess.run(
        ["git", "check-attr", "text", "--", MAP_FILE],
        cwd=VENDORED,
        capture_output=True,
        text=True,
        check=True,
    )

    assert run.stdout.strip().endswith("text: unset")


def test_a_model_with_a_price_but_no_other_is_not_a_priced_model(
    tmp_path: Path,
) -> None:
    write_map(tmp_path, SMALL_MAP)

    assert "image-model" not in load_price_map(tmp_path).models


def test_a_model_with_no_cache_read_price_charges_cached_input_at_the_input_rate(
    tmp_path: Path,
) -> None:
    write_map(
        tmp_path,
        entry(input_cost_per_token=3e-06, output_cost_per_token=1.5e-05),
    )

    info = load_price_map(tmp_path).models["model-a"]

    assert info.cached_input_usd_per_mtok == info.input_usd_per_mtok == Decimal(3)


@pytest.mark.parametrize("zero", [0, 0.0, -0.0], ids=["int", "float", "negative"])
def test_a_price_of_zero_is_a_price_written_as_a_plain_zero(
    tmp_path: Path, zero: float
) -> None:
    write_map(tmp_path, entry(input_cost_per_token=zero, output_cost_per_token=zero))

    info = load_price_map(tmp_path).models["model-a"]

    assert (info.input_usd_per_mtok, info.output_usd_per_mtok) == (
        Decimal(0),
        Decimal(0),
    )
    # Written out: not 0E+6, and not -0.
    assert str(info.input_usd_per_mtok) == str(info.output_usd_per_mtok) == "0"


@pytest.mark.parametrize(
    ("flags", "capabilities"),
    [
        ({}, set()),
        ({"supports_function_calling": True}, {"tools"}),
        ({"supports_response_schema": True}, {"structured_output"}),
        ({"supports_vision": True}, {"vision"}),
        ({"supports_vision": False, "supports_function_calling": True}, {"tools"}),
        (
            {
                "supports_function_calling": True,
                "supports_response_schema": True,
                "supports_vision": True,
            },
            {"tools", "structured_output", "vision"},
        ),
    ],
    ids=["none", "tools", "structured-output", "vision", "false-flag", "all"],
)
def test_the_maps_flags_become_capabilities(
    tmp_path: Path, flags: dict[str, bool], capabilities: set[str]
) -> None:
    write_map(
        tmp_path,
        entry(input_cost_per_token=1e-06, output_cost_per_token=2e-06, **flags),
    )

    assert load_price_map(tmp_path).models["model-a"].capabilities == capabilities


@pytest.mark.parametrize(
    "price",
    [-1e-06, float("nan"), float("inf"), "0.000002", True, None],
    ids=["negative", "nan", "infinity", "string", "boolean", "null"],
)
@pytest.mark.parametrize(
    "field",
    ["input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost"],
)
def test_a_price_that_is_not_a_finite_number_of_zero_or_more_is_rejected_by_name(
    tmp_path: Path, field: str, price: object
) -> None:
    fields: dict[str, object] = {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 2e-06,
        "cache_read_input_token_cost": 1e-07,
    }
    write_map(tmp_path, entry(**{**fields, field: price}))

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    assert f"'model-a': {field}" in str(raised.value)


@pytest.mark.parametrize("flag", ["supports_vision", "supports_function_calling"])
@pytest.mark.parametrize("value", ["yes", 1, None], ids=["string", "number", "null"])
def test_a_capability_flag_that_is_not_true_or_false_is_rejected_by_name(
    tmp_path: Path, flag: str, value: object
) -> None:
    write_map(
        tmp_path,
        entry(input_cost_per_token=1e-06, output_cost_per_token=2e-06, **{flag: value}),
    )

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    assert f"'model-a': {flag}" in str(raised.value)


def test_a_loaded_map_cannot_be_changed(tmp_path: Path) -> None:
    write_map(tmp_path, SMALL_MAP)
    models = load_price_map(tmp_path).models
    info = models["model-a"]
    rate = "input_usd_per_mtok"

    with pytest.raises(TypeError):
        cast(dict[str, ModelInfo], models)["model-b"] = info
    with pytest.raises(FrozenInstanceError):
        setattr(info, rate, Decimal(0))


def test_the_vendored_map_is_loaded_once() -> None:
    assert vendored() is vendored()
    assert vendored().version == load_price_map().version


def test_a_rate_is_written_without_an_exponent(tmp_path: Path) -> None:
    # $0.001 a token is $1000 a million: never 1E+3.
    write_map(tmp_path, entry(input_cost_per_token=1e-03, output_cost_per_token=2e-03))

    info = load_price_map(tmp_path).models["model-a"]

    assert (str(info.input_usd_per_mtok), str(info.output_usd_per_mtok)) == (
        "1000",
        "2000",
    )


def test_a_cache_read_price_of_zero_is_a_free_cache_read(tmp_path: Path) -> None:
    write_map(
        tmp_path,
        entry(
            input_cost_per_token=3e-06,
            output_cost_per_token=1.5e-05,
            cache_read_input_token_cost=0,
        ),
    )

    info = load_price_map(tmp_path).models["model-a"]

    assert info.input_usd_per_mtok == Decimal(3)
    assert info.cached_input_usd_per_mtok == Decimal(0)


@pytest.mark.parametrize("field", ["input_cost_per_token", "output_cost_per_token"])
def test_a_model_with_only_one_token_price_is_not_a_priced_model(
    tmp_path: Path, field: str
) -> None:
    write_map(tmp_path, entry(**{field: 1e-06}))

    assert "model-a" not in load_price_map(tmp_path).models


def test_a_flag_that_is_not_true_or_false_is_rejected_on_a_model_with_no_prices(
    tmp_path: Path,
) -> None:
    write_map(tmp_path, entry(supports_vision="yes"))

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    assert "'model-a': supports_vision" in str(raised.value)


@pytest.mark.parametrize(
    ("fields", "provider"),
    [({"litellm_provider": "anthropic"}, "anthropic"), ({}, None)],
    ids=["named", "absent"],
)
def test_a_models_provider_is_the_maps_litellm_provider(
    tmp_path: Path, fields: dict[str, str], provider: str | None
) -> None:
    write_map(
        tmp_path,
        entry(input_cost_per_token=1e-06, output_cost_per_token=2e-06, **fields),
    )

    assert load_price_map(tmp_path).models["model-a"].provider == provider


@pytest.mark.parametrize("provider", [7, None, ["anthropic"]])
def test_a_provider_that_is_not_text_is_rejected_by_name(
    tmp_path: Path, provider: object
) -> None:
    write_map(
        tmp_path,
        entry(
            input_cost_per_token=1e-06,
            output_cost_per_token=2e-06,
            litellm_provider=provider,
        ),
    )

    with pytest.raises(PriceMapError) as raised:
        load_price_map(tmp_path)

    assert "'model-a': litellm_provider" in str(raised.value)


@pytest.mark.parametrize(
    ("field", "tiered"),
    [
        ("input_cost_per_token_above_200k_tokens", True),
        ("output_cost_per_token_above_32k_tokens", True),
        ("cache_read_input_token_cost_above_128k_tokens", True),
        # A cache lifetime, not a token threshold.
        ("cache_creation_input_token_cost_above_1hr", False),
        ("input_cost_per_token_batches", False),
        # Any price key that says "above" is a tier, so a new spelling fails closed.
        ("input_cost_per_token_above_272k_tokens_priority", True),
        ("output_cost_per_token_above_1m_tokens", True),
        ("input_cost_per_token_above_1hr", True),
    ],
)
def test_a_model_with_token_threshold_prices_is_marked_tiered(
    tmp_path: Path, field: str, tiered: bool
) -> None:
    write_map(
        tmp_path,
        entry(
            input_cost_per_token=1e-06, output_cost_per_token=2e-06, **{field: 3e-06}
        ),
    )

    assert load_price_map(tmp_path).models["model-a"].tiered is tiered


def test_the_default_models_are_neither_tiered_nor_from_another_provider() -> None:
    for name in ("claude-sonnet-5-5", "claude-opus-5-5"):
        info = vendored().models[name]
        assert (info.provider, info.tiered) == ("anthropic", False)
