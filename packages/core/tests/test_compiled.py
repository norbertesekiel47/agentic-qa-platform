"""A compiled script is read strictly (DATA_MODEL §7; ADR-0025; ADR-0006
amendment)."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from aqa_core import strict_yaml
from aqa_core.compiled import CompiledScript
from aqa_core.spec import spec_hash
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


def test_data_models_example_is_not_stale() -> None:
    # §7's example compiles §6's example spec, so it carries that spec's hash;
    # any other hash would make it stale (DATA_MODEL §7, spec_hash).
    section_6 = DATA_MODEL.read_text().split("\n## 6. ", 1)[1]
    found = re.search(r"```markdown\n---\n(.*?\n)---\n", section_6, re.DOTALL)
    assert found is not None, "DATA_MODEL §6 has no example spec"
    frontmatter = strict_yaml.parse(found[1])
    assert isinstance(frontmatter, dict)

    script = CompiledScript.model_validate_json(data_models_example())

    assert script.spec_hash == spec_hash(frontmatter)


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
        ({"role": "button", "name": "\uf218\xa0"}, "compares as empty"),
        ({"css": "button.pay", "scope": {}}, "names no kind"),
    ],
)
def test_a_malformed_locator_is_rejected(locator: dict[str, Any], problem: str) -> None:
    script = example()
    script["targets"]["pay_button"]["locators"][0] = locator

    [(location, _, message)] = errors(script)

    assert location[:4] == ("targets", "pay_button", "locators", 0)
    assert problem in message


BASIS_ON_A_FALSE_FLAG = (
    "Value error, side_effect_basis says why a step's side_effect is true; "
    "this step's is false, so remove it"
)
TRUE_FLAG_WITHOUT_BASIS = (
    "Value error, a step whose side_effect is true says why in side_effect_basis"
)


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


def test_browser_records_every_setting() -> None:
    # Replay uses the settings the script was explored under, so a missing
    # one must not become the pinned default (DATA_MODEL §7, ADR-0025).
    script = example()
    script["browser"] = {}

    assert [(location, kind) for location, kind, _ in errors(script)] == [
        (("browser", setting), "missing")
        for setting in (
            "timezone",
            "locale",
            "viewport",
            "device_scale_factor",
            "color_scheme",
        )
    ]


def put(script: dict[str, Any], path: tuple[str | int, ...], value: object) -> None:
    parent: Any = script
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


def test_every_action_and_check_validates() -> None:
    # The actions and checks DATA_MODEL §7's example doesn't use, with the
    # optional fields it leaves out.
    script = example()
    script["coverage"]["requires"] = [{"id": "c1", "condition": "after a reload"}]
    script["targets"]["size"] = {
        "semantic": "the size picker on the product page",
        "locators": [{"role": "combobox"}],
    }
    script["steps"] += [
        {"seq": 10, "action": "reload", "side_effect": False, "satisfies": ["c1"]},
        {
            "seq": 11,
            "action": "select",
            "target": "size",
            "option": "M",
            "side_effect": False,
        },
        {
            "seq": 12,
            "action": "fill",
            "target": "email_input",
            "value": "",
            "side_effect": False,
        },
        {"seq": 13, "action": "press", "key": "Escape", "side_effect": False},
    ]
    script["assertions"] += [
        {"id": "a7", "expect_index": 3, "check": "not_visible", "target": "pay_button"},
        {
            "id": "a8",
            "expect_index": 1,
            "check": "network_seen",
            "method": "GET",
            "url_pattern": "/api/cart",
            "status_class": "2xx",
        },
    ]

    compiled = CompiledScript.model_validate_json(json.dumps(script))

    assert [step.action for step in compiled.steps[5:]] == [
        "reload",
        "select",
        "fill",
        "press",
    ]
    assert compiled.steps[5].satisfies == ("c1",)
    assert [assertion.check for assertion in compiled.assertions[6:]] == [
        "not_visible",
        "network_seen",
    ]
    assert compiled.targets["size"].locators[0].name is None


# A field that breaks its rule, and where validation reports it: a step's or
# check's kind follows its index, and a list item's index follows its field.
FIELD_RULES: list[tuple[tuple[str | int, ...], object, tuple[str | int, ...]]] = [
    (("spec_hash",), "sha256:1bb957ce", ("spec_hash",)),
    (("coverage", "plan_hash"), "sha256:" + "D8" * 32, ("coverage", "plan_hash")),
    (("compiled_at",), "2026-10-12T14:03:22", ("compiled_at",)),
    (
        ("coverage", "expectations", 0, "assertions"),
        [],
        ("coverage", "expectations", 0, "assertions"),
    ),
    (("targets", "pay_button", "locators"), [], ("targets", "pay_button", "locators")),
    (
        ("targets", "pay_button", "locators"),
        [{"css": "#pay"}, {"css": "#pay"}],
        ("targets", "pay_button", "locators"),
    ),
    (("steps", 0, "seq"), 0, ("steps", 0, "navigate", "seq")),
    (("assertions", 1, "method"), "PSOT", ("assertions", 1, "network_none", "method")),
    (
        ("assertions", 1, "status_class"),
        "2XX",
        ("assertions", 1, "network_none", "status_class"),
    ),
    (
        ("assertions", 5, "min_size_px"),
        [0, 24],
        ("assertions", 5, "visible_unoccluded", "min_size_px", 0),
    ),
    (
        ("assertions", 5, "min_size_px"),
        ["44", 24],
        ("assertions", 5, "visible_unoccluded", "min_size_px", 0),
    ),
    (("browser", "viewport"), [1440], ("browser", "viewport", 1)),
]


@pytest.mark.parametrize(("path", "value", "location"), FIELD_RULES)
def test_a_field_breaking_its_rule_is_rejected(
    path: tuple[str | int, ...], value: object, location: tuple[str | int, ...]
) -> None:
    script = example()
    put(script, path, value)

    [(found, _, _)] = errors(script)

    assert found == location


def test_press_takes_no_target() -> None:
    # The agent's press(key) tool acts on the focused element (ARCHITECTURE
    # §3.4), so a press step names no target.
    script = example()
    script["steps"].append(
        {
            "seq": 10,
            "action": "press",
            "key": "Enter",
            "target": "pay_button",
            "side_effect": False,
        }
    )

    [(location, kind, _)] = errors(script)

    assert (location, kind) == (("steps", 5, "press", "target"), "extra_forbidden")
