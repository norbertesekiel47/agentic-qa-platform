"""A compiled script is read strictly (DATA_MODEL §7; ADR-0025; ADR-0006
amendment)."""

import json
import re
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aqa_core import strict_yaml
from aqa_core.browser import BrowserSettings
from aqa_core.compiled import (
    ByCss,
    ByRole,
    CompiledScript,
    NetworkNone,
    Target,
    TextInTarget,
    TextVisible,
    VisibleUnoccluded,
)
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
    assert isinstance(pay_button, ByRole)
    assert (pay_button.role, pay_button.name) == ("button", "Pay")
    assert isinstance(pay_button.scope, ByCss)
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
    # Our own messages without pydantic's "Value error, " prefix.
    return [
        (error["loc"], error["type"], error["msg"].removeprefix("Value error, "))
        for error in raised.value.errors()
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
    ("locator", "kind"),
    [
        ({}, "locator_kind"),
        # Not an object at all.
        ("#pay", "locator_kind"),
        (None, "locator_kind"),
        ({"role": "button", "label": "Pay"}, "locator_kind"),
        ({"name": "Pay"}, "locator_kind"),
        # A name goes only with a role.
        ({"label": "Pay", "name": "Pay"}, "extra_forbidden"),
        ({"role": "buton", "name": "Pay"}, "literal_error"),
        ({"css": ""}, "string_too_short"),
        ({"label": ""}, "string_too_short"),
        ({"placeholder": ""}, "string_too_short"),
        ({"testid": ""}, "string_too_short"),
        ({"css": " \t"}, "value_error"),
        ({"css": "button.pay", "scope": {}}, "locator_kind"),
        (
            {"testid": "pay", "scope": {"label": "Payment", "scope": {"css": 7}}},
            "string_type",
        ),
    ],
)
def test_a_malformed_locator_is_rejected(locator: object, kind: str) -> None:
    script = example()
    script["targets"]["pay_button"]["locators"][0] = locator

    [(location, found, _)] = errors(script)

    assert location[:4] == ("targets", "pay_button", "locators", 0)
    assert found == kind


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        # A name with a glyph or extra whitespace could never match, because
        # a name is compared after normalizing.
        ("\uf218 Pay", "is not normalized"),
        ("Pay  now", "is not normalized"),
        ("Pay\xadment", "is not normalized"),
        (" Pay ", "is not normalized"),
        ("\uf218\xa0", "compares as empty"),
    ],
)
def test_a_role_name_is_written_normalized(name: str, problem: str) -> None:
    script = example()
    script["targets"]["pay_button"]["locators"][0] = {"role": "button", "name": name}

    [(location, _, message)] = errors(script)

    assert location == ("targets", "pay_button", "locators", 0, "role", "name")
    assert problem in message


BASIS_ON_A_FALSE_FLAG = (
    "side_effect_basis says why a step's side_effect is true; "
    "this step's is false, so remove it"
)
TRUE_FLAG_WITHOUT_BASIS = (
    "a step whose side_effect is true says why in side_effect_basis"
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


TAKES_ONE = "a text check takes text or pattern, exactly one"


@pytest.mark.parametrize(
    ("index", "check", "add", "remove"),
    [
        # a1 is a text_visible check with a text, a4 a text_in_target check
        # with a pattern: each gets both, or loses its only one.
        (0, "text_visible", {"pattern": "card has expired"}, ()),
        (0, "text_visible", {}, ("text",)),
        (3, "text_in_target", {"text": "Classic Hoodie"}, ()),
        (3, "text_in_target", {}, ("pattern",)),
    ],
)
def test_a_text_check_takes_text_or_pattern(
    index: int, check: str, add: dict[str, str], remove: tuple[str, ...]
) -> None:
    script = example()
    assertion = script["assertions"][index]
    assertion.update(add)
    for key in remove:
        del assertion[key]

    [(location, _, message)] = errors(script)

    assert (location, message) == (("assertions", index, check), TAKES_ONE)


def test_a_text_is_written_normalized() -> None:
    script = example()
    script["assertions"][0]["text"] = "card  has expired"

    [(location, _, message)] = errors(script)

    assert location == ("assertions", 0, "text_visible", "text")
    assert "is not normalized" in message


@pytest.mark.parametrize(
    "pattern",
    [
        "Classic Hoodie (size M",
        # re.compile raises OverflowError and RecursionError for these, not
        # re.error, and each must still be a validation error.
        "a{4294967296}",
        "(" * 1000 + ")" * 1000,
        # A pattern whose meaning Python says it will change: re.compile only
        # warns, and this repo turns warnings into errors.
        "[[:digit:]]+",
    ],
    ids=["unclosed", "huge repeat", "deep nesting", "nested set"],
)
@pytest.mark.parametrize(
    ("index", "check"), [(3, "text_in_target"), (4, "url_matches")]
)
def test_a_pattern_must_compile(pattern: str, index: int, check: str) -> None:
    script = example()
    script["assertions"][index]["pattern"] = pattern

    [(location, _, message)] = errors(script)

    assert location == ("assertions", index, check, "pattern")
    assert message.startswith(f"{pattern!r} is not a Python regex: ")


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
    # Worded for a compiled step too, which has no start_url.
    assert message == (
        f"'{url}' is not a path: write a path such as /login, with no empty, . "
        "or .. segment; the origin comes from the run (ADR-0026)"
    )


@pytest.mark.parametrize(
    ("part", "index", "field", "value"),
    [
        ("steps", 0, "action", "hover"),
        # Checks whose fields DATA_MODEL §7 doesn't give yet: probe_equals
        # gets them in #48; pixel_diff, contrast_min and model_verify in M2.
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

    [(location, kind, message)] = errors(script)

    assert (location, kind) == ((part, index), "union_tag_invalid")
    assert repr(value) in message


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, 0])
def test_schema_version_is_the_integer_1(version: object) -> None:
    script = example()
    script["schema_version"] = version

    [(location, _, _)] = errors(script)

    assert location == ("schema_version",)


@pytest.mark.parametrize(
    "missing",
    [list(BrowserSettings.model_fields), ["locale"]],
    ids=["all", "one"],
)
def test_browser_records_every_setting(missing: list[str]) -> None:
    # Replay uses the settings the script was explored under, so a missing
    # one must not become the pinned default (DATA_MODEL §7, ADR-0025). The
    # settings come from BrowserSettings, so one added later is required too.
    script = example()
    for setting in missing:
        del script["browser"][setting]

    [(location, _, message)] = errors(script)

    assert location == ("browser",)
    assert message.startswith(f"records no {', '.join(missing)}:")


def test_every_action_and_check_validates() -> None:
    # The actions and checks DATA_MODEL §7's example doesn't use, with the
    # optional fields it leaves out.
    script = example()
    steps, assertions = len(script["steps"]), len(script["assertions"])
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

    assert [step.action for step in compiled.steps[steps:]] == [
        "reload",
        "select",
        "fill",
        "press",
    ]
    assert compiled.steps[steps].satisfies == ("c1",)
    assert [assertion.check for assertion in compiled.assertions[assertions:]] == [
        "not_visible",
        "network_seen",
    ]
    assert compiled.targets["size"].locators == (ByRole(role="combobox"),)


# A field that breaks its rule, and where validation reports it: a step's or
# check's kind follows its index, and a list item's index follows its field.
FIELD_RULES: list[tuple[tuple[str | int, ...], object, tuple[str | int, ...]]] = [
    (("spec_hash",), "sha256:1bb957ce", ("spec_hash",)),
    (("spec_hash",), "sha256:" + "d8" * 32 + "zz", ("spec_hash",)),
    (("confirmed",), 1, ("confirmed",)),
    (
        ("assertions", 5, "in_viewport"),
        "true",
        ("assertions", 5, "visible_unoccluded", "in_viewport"),
    ),
    (("targets", "pay_button", "semantic"), "", ("targets", "pay_button", "semantic")),
    (
        ("targets", ""),
        {"semantic": "the page's heading", "locators": [{"role": "heading"}]},
        ("targets", "", "[key]"),
    ),
    (
        ("probe_baselines", ""),
        {"capture_before_seq": 9, "json_path": "$.count"},
        ("probe_baselines", "", "[key]"),
    ),
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
    (("browser",), None, ("browser",)),
    (("browser", "timezone"), "Mars/Phobos", ("browser", "timezone")),
    (("browser", "locale"), "english", ("browser", "locale")),
    (("steps", 3, "side_effect_basis"), "", ("steps", 3, "click", "side_effect_basis")),
    (("steps", 3, "target"), "", ("steps", 3, "click", "target")),
    (("steps", 3, "satisfies"), ["c1", "c1"], ("steps", 3, "click", "satisfies")),
    (("steps", 2, "secret"), "test_password", ("steps", 2, "fill_secret", "secret")),
    (("coverage", "expectations"), [], ("coverage", "expectations")),
    (("assertions",), [], ("assertions",)),
    (
        ("coverage", "expectations", 1, "assertions"),
        ["a2", "a2"],
        ("coverage", "expectations", 1, "assertions"),
    ),
    (
        ("coverage", "requires"),
        [{"id": "c1", "condition": "after a reload", "replay_safe": True}],
        ("coverage", "requires", 0, "replay_safe"),
    ),
    (
        ("probe_baselines", "orders_count", "capture_before_seq"),
        0,
        ("probe_baselines", "orders_count", "capture_before_seq"),
    ),
    (
        ("assertions", 0, "expect_index"),
        -1,
        ("assertions", 0, "text_visible", "expect_index"),
    ),
    (("compiled_by", "mode"), "heal", ("compiled_by", "mode")),
    (
        ("compiled_by", "models"),
        {"pilot": "claude-sonnet-5-5"},
        ("compiled_by", "models", "pilot", "[key]"),
    ),
]


@pytest.mark.parametrize(("path", "value", "location"), FIELD_RULES)
def test_a_field_breaking_its_rule_is_rejected(
    path: tuple[str | int, ...], value: object, location: tuple[str | int, ...]
) -> None:
    script = example()
    at(script, path[:-1])[str(path[-1])] = value

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


def test_locators_built_in_python_keep_their_kind() -> None:
    # The compiler builds locators as objects (#52), not as JSON.
    locators = (
        ByRole(role="button", name="Pay", scope=ByCss(css="app-payment-step")),
        ByCss(css="app-payment-step button[type=submit]"),
    )

    target = Target(semantic="the payment step's submit button", locators=locators)

    assert target.locators == locators
    # A locator prints as the script writes it, without empty fields.
    assert json.loads(str(target.locators[0])) == {
        "role": "button",
        "name": "Pay",
        "scope": {"css": "app-payment-step"},
    }


@pytest.mark.parametrize(
    "css",
    [
        "iframe >> internal:control=enter-frame >> input[type=password]",
        "body >> xpath=//button",
        "button>>nth=0",
    ],
)
def test_a_css_locator_is_one_css_selector(css: str) -> None:
    # Playwright reads >> as a chain into other selector engines, even after
    # css=, which would reach into frames or match by position (ADR-0025,
    # 2026-10-02 amendment).
    script = example()
    script["targets"]["pay_button"]["locators"][1] = {"css": css}

    [(location, _, message)] = errors(script)

    assert location == ("targets", "pay_button", "locators", 1, "css", "css")
    assert "isn't one CSS selector" in message
    # A >> inside an attribute value is written escaped instead.
    assert r"write \>\>" in message


@pytest.mark.parametrize("value", [0, 1, "false", "no", None])
def test_side_effect_is_a_boolean(value: object) -> None:
    # A lax reading would take 0 or "no" as false, and false lets a
    # continuation repeat the step (ADR-0006 amendment).
    script = example()
    script["steps"][3]["side_effect"] = value

    [(location, kind, _)] = errors(script)

    assert (location, kind) == (("steps", 3, "click", "side_effect"), "bool_type")


@pytest.mark.parametrize(
    "path",
    [
        ("spec_id",),
        ("confirmed",),
        ("compiled_by", "price_map"),
        ("coverage", "requires"),
        ("targets", "pay_button", "semantic"),
        ("probe_baselines", "orders_count", "json_path"),
        ("steps", 0, "url"),
        ("steps", 3, "target"),
        ("assertions", 0, "id"),
        ("assertions", 1, "url_pattern"),
    ],
    ids=lambda path: ".".join(map(str, path)),
)
def test_a_required_field_is_required(path: tuple[str | int, ...]) -> None:
    script = example()
    del at(script, path[:-1])[str(path[-1])]

    [(location, kind, _)] = errors(script)

    assert kind == "missing"
    assert location[-1] == path[-1]


def test_a_script_built_in_python_validates() -> None:
    # The compiler builds a script from objects (#52, #53): the browser
    # settings it explored under, and lists where JSON has arrays.
    script: dict[str, Any] = example()
    script["compiled_at"] = datetime(2026, 10, 12, 14, 3, 22, tzinfo=UTC)
    script["browser"] = BrowserSettings()

    compiled = CompiledScript.model_validate(script)

    assert compiled.browser == BrowserSettings()
    visible = compiled.assertions[5]
    assert isinstance(visible, VisibleUnoccluded)
    assert visible.min_size_px == (44, 24)


def test_a_locator_naming_no_kind_says_what_to_write() -> None:
    script = example()
    script["targets"]["pay_button"]["locators"][0] = {"name": "Pay"}

    [(_, _, message)] = errors(script)

    assert message == (
        "a locator names exactly one kind: role, label, placeholder, testid or css"
    )


METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx")


@pytest.mark.parametrize(
    ("method", "status_class"), list(zip(METHODS, STATUS_CLASSES * 2, strict=False))
)
def test_every_method_and_status_class_validates(
    method: str, status_class: str
) -> None:
    script = example()
    script["assertions"][1].update(method=method, status_class=status_class)

    network = CompiledScript.model_validate_json(json.dumps(script)).assertions[1]

    assert isinstance(network, NetworkNone)
    assert (network.method, network.status_class) == (method, status_class)


def test_a_pattern_python_warns_about_is_refused_even_when_cached() -> None:
    # re.compile returns a cached pattern without warning again, so an earlier
    # compile elsewhere in the process must not let it through.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        re.compile("[[:alpha:]]+")
    script = example()
    script["assertions"][4]["pattern"] = "[[:alpha:]]+"

    [(location, _, message)] = errors(script)

    assert location == ("assertions", 4, "url_matches", "pattern")
    assert message.startswith("'[[:alpha:]]+' is not a Python regex: ")


@pytest.mark.parametrize(
    ("index", "rendered", "found"),
    [
        # a1 checks the literal "card has expired".
        (0, "Sorry, your card has expired.", True),
        (0, "Your card expired", False),
        # A literal ignores case, which a pattern without (?i) doesn't.
        (0, "CARD HAS EXPIRED", True),
        # a4 checks the pattern Classic Hoodie.*\bM\b, case-sensitively.
        (3, "Classic Hoodie\n  size M", True),
        (3, "classic hoodie size M", False),
    ],
)
def test_a_text_check_matches_rendered_text(
    index: int, rendered: str, found: bool
) -> None:
    check = CompiledScript.model_validate_json(data_models_example()).assertions[index]
    assert isinstance(check, TextVisible | TextInTarget)

    assert check.matches(rendered) is found


@pytest.mark.parametrize(
    "css",
    [
        # A quote CSS ignores, inside a comment, still opens one for
        # Playwright's selector splitter, which would then read whatever is
        # chained after this value as part of it.
        'iframe /* "',
        "iframe /* '",
        "iframe /* `",
        # A last backslash takes the first character of whatever Playwright
        # chains after the value.
        "button\\",
    ],
)
def test_a_css_value_leaves_no_quote_open(css: str) -> None:
    script = example()
    script["targets"]["pay_button"]["locators"][1] = {"css": css}

    [(location, _, message)] = errors(script)

    assert location == ("targets", "pay_button", "locators", 1, "css", "css")
    assert "leaves a quote or escape open" in message


@pytest.mark.parametrize(
    "css", ['[href^="/*"]', "a[title='it\\'s']", '[data-x="a\\"b"]', "a::after"]
)
def test_a_css_value_with_closed_quotes_is_accepted(css: str) -> None:
    script = example()
    script["targets"]["pay_button"]["locators"][1] = {"css": css}

    assert CompiledScript.model_validate_json(json.dumps(script))
