"""The vendored price map is checked against its pin when it is loaded
(ADR-0007 amendment, TECH_STACK §7)."""

import hashlib
import json
import re
import subprocess
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
