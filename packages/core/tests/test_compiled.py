"""A compiled script is read strictly (DATA_MODEL §7; ADR-0025; ADR-0006
amendment)."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from pydantic import ValidationError

DATA_MODEL = Path(__file__).parents[3] / "DATA_MODEL.md"


def data_models_example() -> str:
    """The JSON example in DATA_MODEL §7, read from the doc so the two can't
    drift apart."""
    section = DATA_MODEL.read_text().split("\n## 7. ", 1)[1]
    found = re.search(r"```json\n(.*?)\n```", section, re.DOTALL)
    assert found is not None, "DATA_MODEL §7 has no JSON example"
    return found[1]


def test_data_models_example_validates() -> None:
    script = CompiledScript.model_validate_json(data_models_example())

    assert script.spec_id == "checkout-expired-card"
    assert [step.seq for step in script.steps] == [1, 2, 3, 4, 9]
    assert [step.side_effect for step in script.steps] == [
        False,
        False,
        False,
        True,
        True,
    ]
    assert [assertion.check for assertion in script.assertions] == [
        "text_visible",
        "network_none",
        "probe_equals_baseline",
        "text_in_target",
        "url_matches",
        "visible_unoccluded",
    ]
    pay_button = script.targets["pay_button"].locators[0]
    assert (pay_button.role, pay_button.name) == ("button", "Pay")
    assert pay_button.scope is not None
    assert pay_button.scope.css == "app-payment-step"
    assert script.browser.viewport == (1440, 900)


def example() -> dict[str, Any]:
    """DATA_MODEL §7's example, parsed, for a test to change one part of."""
    parsed: dict[str, Any] = json.loads(data_models_example())
    return parsed


def errors(script: dict[str, Any]) -> list[tuple[tuple[str | int, ...], str, str]]:
    """Each problem validation finds in `script`: its location, type and
    message."""
    with pytest.raises(ValidationError) as raised:
        CompiledScript.model_validate_json(json.dumps(script))
    return [
        (error["loc"], error["type"], error["msg"]) for error in raised.value.errors()
    ]


@pytest.mark.parametrize(("index", "action"), [(0, "navigate"), (3, "click")])
def test_a_step_without_side_effect_is_rejected(index: int, action: str) -> None:
    script = example()
    del script["steps"][index]["side_effect"]

    [(location, kind, _)] = errors(script)

    assert (location, kind) == (("steps", index, action, "side_effect"), "missing")


# Where an unknown field can hide: each place, and the path to the object.
PLACES: list[tuple[str | int, ...]] = [
    (),
    ("compiled_by",),
    ("browser",),
    ("coverage",),
    ("coverage", "expectations", 0),
    ("targets", "pay_button"),
    ("targets", "pay_button", "locators", 0),
    ("targets", "pay_button", "locators", 0, "scope"),
    ("probe_baselines", "orders_count"),
    ("steps", 3),
    ("assertions", 5),
]


def at(script: dict[str, Any], path: tuple[str | int, ...]) -> dict[str, Any]:
    found: Any = script
    for key in path:
        found = found[key]
    assert isinstance(found, dict)
    return found


@pytest.mark.parametrize("path", PLACES, ids=lambda path: ".".join(map(str, path)))
def test_an_unknown_field_is_rejected(path: tuple[str | int, ...]) -> None:
    script = example()
    at(script, path)["replay_safe"] = True

    [(location, kind, _)] = errors(script)

    assert kind == "extra_forbidden"
    assert location[-1] == "replay_safe"


@pytest.mark.parametrize(
    ("locator", "problem"),
    [
        ({}, "names no kind"),
        ({"role": "button", "label": "Pay"}, "names 2 kinds: role, label"),
        ({"name": "Pay"}, "a name goes with a role"),
        ({"label": "Pay", "name": "Pay"}, "a name goes with a role"),
        ({"role": "buton", "name": "Pay"}, "Input should be 'alert'"),
        ({"css": ""}, "at least 1 character"),
        # A name with a glyph or a doubled space could never match, because a
        # name is compared after normalizing.
        ({"role": "button", "name": "\uf218 Pay"}, "is not normalized"),
        ({"role": "button", "name": "Pay  now"}, "is not normalized"),
        ({"css": "button.pay", "scope": {}}, "names no kind"),
    ],
)
def test_a_malformed_locator_is_rejected(locator: dict[str, Any], problem: str) -> None:
    script = example()
    script["targets"]["pay_button"]["locators"][0] = locator

    [(location, _, message)] = errors(script)

    assert location[:4] == ("targets", "pay_button", "locators", 0)
    assert problem in message


def test_side_effect_basis_goes_with_a_true_flag() -> None:
    # Step 4 is a side-effect step, so it must say why; step 1 is replay-safe,
    # so a basis would contradict its flag (DATA_MODEL §7).
    script = example()
    del script["steps"][3]["side_effect_basis"]
    script["steps"][0]["side_effect_basis"] = "network: GET /login"

    assert [(location, message) for location, _, message in errors(script)] == [
        (("steps", 0, "navigate"), BASIS_ON_A_FALSE_FLAG),
        (("steps", 3, "click"), TRUE_FLAG_WITHOUT_BASIS),
    ]


BASIS_ON_A_FALSE_FLAG = (
    "Value error, side_effect_basis says why a step's side_effect is true; "
    "this step's is false, so remove it"
)
TRUE_FLAG_WITHOUT_BASIS = (
    "Value error, a step whose side_effect is true says why in side_effect_basis"
)


TAKES_ONE = "Value error, a text check takes text or pattern, exactly one"


@pytest.mark.parametrize(
    ("index", "check", "change", "problem"),
    [
        # a1 is a text_visible check with a text, a4 a text_in_target check
        # with a pattern.
        (0, "text_visible", {"pattern": "card has expired"}, TAKES_ONE),
        (0, "text_visible", {"text": None}, TAKES_ONE),
        (3, "text_in_target", {"text": "Classic Hoodie"}, TAKES_ONE),
        (3, "text_in_target", {"pattern": None}, TAKES_ONE),
    ],
)
def test_a_text_check_takes_text_or_pattern(
    index: int, check: str, change: dict[str, Any], problem: str
) -> None:
    script = example()
    assertion = script["assertions"][index]
    assertion.update(change)
    for key in [key for key, value in assertion.items() if value is None]:
        del assertion[key]

    [(location, _, message)] = errors(script)

    assert (location, message) == (("assertions", index, check), problem)


def test_a_text_is_written_normalized() -> None:
    script = example()
    script["assertions"][0]["text"] = "card  has expired"

    [(location, _, message)] = errors(script)

    assert location == ("assertions", 0, "text_visible", "text")
    assert "is not normalized" in message


@pytest.mark.parametrize(
    ("index", "check"), [(3, "text_in_target"), (4, "url_matches")]
)
def test_a_pattern_must_compile(index: int, check: str) -> None:
    script = example()
    script["assertions"][index]["pattern"] = "Classic Hoodie (size M"

    [(location, _, message)] = errors(script)

    assert location == ("assertions", index, check, "pattern")
    assert message == (
        "Value error, 'Classic Hoodie (size M' is not a Python regex: "
        "missing ), unterminated subpattern at position 15"
    )


@pytest.mark.parametrize(
    "url",
    ["//evil.test/login", "/..//evil.test", "https://shop.example.test/login", "login"],
)
def test_a_navigate_url_is_a_path_held_to_start_urls_rules(url: str) -> None:
    # A navigate to the start origin stores a path, which joins the start
    # origin as the spec's start_url does (DATA_MODEL §6, §7; #89).
    script = example()
    script["steps"][0]["url"] = url

    [(location, _, message)] = errors(script)

    assert location == ("steps", 0, "navigate", "url")
    assert message.startswith(f"Value error, '{url}' is not a path:")


@pytest.mark.parametrize(
    ("part", "index", "field", "value"),
    [
        ("steps", 0, "action", "hover"),
        # Checks whose fields DATA_MODEL §7 doesn't give yet (#48).
        ("assertions", 2, "check", "probe_equals"),
        ("assertions", 5, "check", "pixel_diff"),
        ("assertions", 5, "check", "contrast_min"),
        ("assertions", 0, "check", "model_verify"),
    ],
)
def test_an_unknown_action_or_check_is_refused_by_name(
    part: str, index: int, field: str, value: str
) -> None:
    script = example()
    script[part][index][field] = value

    [(location, _, message)] = errors(script)

    assert location == (part, index)
    assert message.startswith(
        f"Input tag '{value}' found using '{field}' does not match"
    )


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_schema_version_is_the_integer_1(version: object) -> None:
    script = example()
    script["schema_version"] = version

    [(location, _, _)] = errors(script)

    assert location == ("schema_version",)
