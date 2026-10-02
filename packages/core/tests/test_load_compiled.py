"""Loading a compiled script from its file: repeated keys refused, the text
validated in JSON mode, and the script's parts checked against each other
(DATA_MODEL §7, "Reading a compiled script"; ADR-0025's 2026-10-02
amendment; #46)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_compiled

from packages.core.tests.test_compiled import data_models_example

# The project config the example script's fill_secret step needs.
DECLARES_TEST_PASSWORD = ProjectConfig.model_validate(
    {"secrets": {"TEST_PASSWORD": {"origins": ["start"], "field": "password"}}}
)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "qa" / ".compiled" / "checkout-expired-card.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def example() -> dict[str, Any]:
    """DATA_MODEL §7's example, as JSON values a test can edit."""
    parsed = json.loads(data_models_example())
    assert isinstance(parsed, dict)
    return parsed


def problems(tmp_path: Path, text: str, config: ProjectConfig) -> tuple[str, ...]:
    """The problems loading `text` reports, each without the file's path."""
    path = write(tmp_path, text)
    with pytest.raises(SpecError) as refused:
        load_compiled(path, config)
    prefix = f"{path}: "
    assert all(line.startswith(prefix) for line in refused.value.problems), (
        refused.value.problems
    )
    return tuple(line.removeprefix(prefix) for line in refused.value.problems)


def test_the_data_model_example_loads_with_its_compiled_at_string(
    tmp_path: Path,
) -> None:
    script = load_compiled(
        write(tmp_path, data_models_example()), DECLARES_TEST_PASSWORD
    )

    assert script == CompiledScript.model_validate_json(data_models_example())
    # A string, which the strict model accepts only in JSON mode.
    assert script.compiled_at == datetime(2026, 10, 12, 14, 3, 22, tzinfo=UTC)


# Step 4 of the example, the sign-in click, as written there.
SIGN_IN_STEP = (
    '{ "seq": 4, "action": "click", "target": "sign_in", "side_effect": true, '
    '"side_effect_basis": "network: POST /api/users/login" }'
)


def test_a_repeated_side_effect_key_cannot_lower_the_flag(tmp_path: Path) -> None:
    # The flag reads true, but a reader keeps a key's last value: false, with
    # the basis nulled, which the format alone accepts.
    hidden = SIGN_IN_STEP.replace(
        " }", ', "side_effect": false, "side_effect_basis": null }'
    )
    text = data_models_example().replace(SIGN_IN_STEP, hidden)
    assert text != data_models_example()
    # The control: the format alone reads the step as replay-safe.
    lowered = CompiledScript.model_validate_json(text).steps[3]
    assert (lowered.seq, lowered.side_effect) == (4, False)

    found = problems(tmp_path, text, DECLARES_TEST_PASSWORD)

    assert [line.split(": ", 1)[0] for line in found] == [
        "steps[3].side_effect",
        "steps[3].side_effect_basis",
    ]
    assert all("more than once" in line for line in found), found


@pytest.mark.parametrize(
    ("original", "repeated", "key"),
    [
        (
            '"spec_id": "checkout-expired-card"',
            '"spec_id": "checkout-expired-card", "spec_id": "other"',
            "spec_id",
        ),
        (
            '"semantic": "the login form\'s email field"',
            '"semantic": "the login form\'s email field", "semantic": "x"',
            "targets.email_input.semantic",
        ),
        (
            '{ "role": "textbox", "name": "Email" }',
            '{ "role": "textbox", "name": "Email", "name": "Mail" }',
            "targets.email_input.locators[0].name",
        ),
        (
            '"scope": { "css": "app-cart-summary" }',
            '"scope": { "css": "app-cart-summary", "css": "body" }',
            "targets.cart_items.locators[1].scope.css",
        ),
        (
            '"pay_button":     {',
            (
                '"pay_button": { "semantic": "x", "locators": [ { "testid": "x" } ] },\n'
                '    "pay_button":     {'
            ),
            "targets.pay_button",
        ),
        (
            '"check": "url_matches", "pattern": "/checkout/payment"',
            '"check": "url_matches", "pattern": "/checkout/payment", "pattern": "/"',
            "assertions[4].pattern",
        ),
    ],
    ids=["top level", "target", "locator", "scope", "target name", "assertion"],
)
def test_a_repeated_key_is_named_wherever_it_is(
    tmp_path: Path, original: str, repeated: str, key: str
) -> None:
    text = data_models_example().replace(original, repeated, 1)
    assert text != data_models_example()

    found = problems(tmp_path, text, DECLARES_TEST_PASSWORD)

    assert [line for line in found if line.startswith(f"{key}: ")] == [
        (
            f"{key}: the key appears more than once, and a reader keeps only "
            "its last value, so write it once"
        )
    ], found


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('{\n  "schema_version": 1,\n', "line 3: not JSON: "),
        # Too deep for json to read, and deep enough for it to read.
        ("[" * 10**6 + "]" * 10**6, "nested more than 256 levels deep"),
        ('{"targets": ' + "[" * 10**5 + "]" * 10**5 + "}", "nested more than 256"),
        # The deepest the walk reads, which pydantic then refuses, and one
        # level more.
        ("[" * 256 + "]" * 256, "Invalid JSON: "),
        ("[" * 257 + "]" * 257, "nested more than 256 levels deep"),
        # Longer than Python reads an integer by default (sys.int_info).
        ('{"schema_version": ' + "1" * 5000 + "}", "not JSON: "),
    ],
    ids=[
        "cut short",
        "too deep to read",
        "too deep",
        "deepest",
        "one deeper",
        "a number too long",
    ],
)
def test_text_that_isnt_a_json_object_is_one_problem_naming_the_file(
    tmp_path: Path, text: str, problem: str
) -> None:
    [found] = problems(tmp_path, text, DECLARES_TEST_PASSWORD)

    assert found.startswith(problem), found


def edited(change: Callable[[dict[str, Any]], object]) -> str:
    """DATA_MODEL §7's example after `change` edits its JSON values."""
    script = example()
    change(script)
    return json.dumps(script, indent=2)


def assertion(script: dict[str, Any], check: str) -> dict[str, Any]:
    """The example's one assertion of kind `check`."""
    [found] = [each for each in script["assertions"] if each["check"] == check]
    assert isinstance(found, dict)
    return found


@pytest.mark.parametrize(
    ("change", "key", "problem"),
    [
        (
            lambda s: s["steps"][1].update(target="nowhere"),
            "steps[1].target",
            "'nowhere' names no target in targets",
        ),
        (
            lambda s: s["steps"][2].update(target="nowhere"),
            "steps[2].target",
            "'nowhere' names no target in targets",
        ),
        (
            lambda s: s["steps"][3].update(target="nowhere"),
            "steps[3].target",
            "'nowhere' names no target in targets",
        ),
        (
            lambda s: s["steps"].append(
                {
                    "seq": 10,
                    "action": "select",
                    "target": "nowhere",
                    "option": "M",
                    "side_effect": False,
                }
            ),
            "steps[5].target",
            "'nowhere' names no target in targets",
        ),
        (
            lambda s: assertion(s, "text_in_target").update(target="nowhere"),
            "assertions[3].target",
            "'nowhere' names no target in targets",
        ),
        (
            lambda s: assertion(s, "visible_unoccluded").update(target="nowhere"),
            "assertions[5].target",
            "'nowhere' names no target in targets",
        ),
        (
            lambda s: s["assertions"].append(
                {
                    "id": "a7",
                    "expect_index": 0,
                    "check": "not_visible",
                    "target": "nowhere",
                }
            ),
            "assertions[6].target",
            "'nowhere' names no target in targets",
        ),
        (
            lambda s: assertion(s, "url_matches").update(expect_index=7),
            "assertions[4].expect_index",
            "7 names no expectation in coverage.expectations",
        ),
        (
            lambda s: s["coverage"]["expectations"][0].update(assertions=["a9"]),
            "coverage.expectations[0].assertions[0]",
            "'a9' names no assertion in assertions",
        ),
        (
            lambda s: s["steps"][0].update(satisfies=["c9"]),
            "steps[0].satisfies[0]",
            "'c9' names no condition in coverage.requires",
        ),
        (
            lambda s: assertion(s, "probe_equals_baseline").update(probe="nowhere"),
            "assertions[2].probe",
            "'nowhere' names no probe in probe_baselines",
        ),
        (
            lambda s: s["probe_baselines"]["orders_count"].update(capture_before_seq=7),
            "probe_baselines.orders_count.capture_before_seq",
            "7 names no step's seq",
        ),
    ],
    ids=[
        "fill target",
        "fill_secret target",
        "click target",
        "select target",
        "text_in_target target",
        "visible_unoccluded target",
        "not_visible target",
        "expect_index",
        "coverage assertion",
        "satisfies",
        "probe",
        "capture_before_seq",
    ],
)
def test_a_name_the_script_uses_must_exist(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], object],
    key: str,
    problem: str,
) -> None:
    found = problems(tmp_path, edited(change), DECLARES_TEST_PASSWORD)

    assert found == (f"{key}: {problem}",)


def test_a_fill_secret_step_names_a_secret_the_project_config_declares(
    tmp_path: Path,
) -> None:
    found = problems(tmp_path, data_models_example(), ProjectConfig())

    assert found == (
        (
            "steps[2].secret: secret TEST_PASSWORD is not declared in the "
            "project config's secrets"
        ),
    )


@pytest.mark.parametrize(
    ("change", "key", "problem"),
    [
        (
            # a3 renamed a2, and expectation 1 naming only a2, so nothing
            # names an assertion that no longer exists.
            lambda s: (
                s["assertions"][2].update(id="a2"),
                s["coverage"]["expectations"][1].update(assertions=["a2"]),
            ),
            "assertions[2].id",
            "'a2' is already the id of assertions[1]",
        ),
        (
            lambda s: s["coverage"]["requires"].extend(
                [{"id": "c1", "condition": "x"}, {"id": "c1", "condition": "y"}]
            ),
            "coverage.requires[1].id",
            "'c1' is already the id of coverage.requires[0]",
        ),
        (
            lambda s: s["steps"][1].update(seq=1),
            "steps[1].seq",
            "1 is already the seq of steps[0]",
        ),
    ],
    ids=["assertion id", "condition id", "seq"],
)
def test_a_name_must_be_unique_where_it_is_defined(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], object],
    key: str,
    problem: str,
) -> None:
    found = problems(tmp_path, edited(change), DECLARES_TEST_PASSWORD)

    assert found == (f"{key}: {problem}",)


def test_a_name_defined_three_times_names_its_first_definition(
    tmp_path: Path,
) -> None:
    def change(script: dict[str, Any]) -> None:
        script["steps"][1]["seq"] = 1
        script["steps"][2]["seq"] = 1

    found = problems(tmp_path, edited(change), DECLARES_TEST_PASSWORD)

    assert found == (
        "steps[1].seq: 1 is already the seq of steps[0]",
        "steps[2].seq: 1 is already the seq of steps[0]",
    )


def test_an_expect_index_is_defined_once(tmp_path: Path) -> None:
    def change(script: dict[str, Any]) -> None:
        script["coverage"]["expectations"][4]["expect_index"] = 3

    found = problems(tmp_path, edited(change), DECLARES_TEST_PASSWORD)

    # Assertion a6 now names an expectation nothing defines.
    assert found == (
        (
            "coverage.expectations[4].expect_index: 3 is already the "
            "expect_index of coverage.expectations[3]"
        ),
        "assertions[5].expect_index: 4 names no expectation in coverage.expectations",
    )


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    def change(script: dict[str, Any]) -> None:
        script["steps"][1]["seq"] = 1
        script["steps"][1]["target"] = "nowhere"

    text = edited(change)
    repeated = text.replace('"spec_id": ', '"spec_id": "x", "spec_id": ', 1)
    unknown = repeated.replace('"spec_id": "x"', '"spec_ids": "x", "spec_id": "x"', 1)

    names = problems(tmp_path, text, DECLARES_TEST_PASSWORD)
    with_a_repeat = problems(tmp_path, repeated, DECLARES_TEST_PASSWORD)
    with_an_unknown_key = problems(tmp_path, unknown, DECLARES_TEST_PASSWORD)

    assert [line.split(": ", 1)[0] for line in names] == [
        "steps[1].seq",
        "steps[1].target",
    ]
    assert [line.split(": ", 1)[0] for line in with_a_repeat] == [
        "spec_id",
        "steps[1].seq",
        "steps[1].target",
    ]
    # Names are checked only in a script the format accepts.
    assert [line.split(": ", 1)[0] for line in with_an_unknown_key] == [
        "spec_id",
        "spec_ids",
    ]


def test_every_locator_of_a_not_visible_target_may_be_scoped(tmp_path: Path) -> None:
    def change(script: dict[str, Any]) -> None:
        in_payment_step = {"css": "app-payment-step"}
        script["targets"]["payment_error"] = {
            "semantic": "the payment step's error message",
            "locators": [
                {"testid": "payment-error", "scope": in_payment_step},
                {"css": ".error", "scope": in_payment_step},
            ],
        }
        script["assertions"].append(
            {
                "id": "a7",
                "expect_index": 3,
                "check": "not_visible",
                "target": "payment_error",
            }
        )

    script = load_compiled(write(tmp_path, edited(change)), DECLARES_TEST_PASSWORD)

    assert [each.id for each in script.assertions if each.check == "not_visible"] == [
        "a7"
    ]


def test_a_not_visible_target_with_an_unscoped_locator_is_refused(
    tmp_path: Path,
) -> None:
    # cart_items finds its element by a test ID with no scope, then by css
    # inside app-cart-summary.
    def change(script: dict[str, Any]) -> None:
        script["assertions"].append(
            {
                "id": "a7",
                "expect_index": 2,
                "check": "not_visible",
                "target": "cart_items",
            }
        )

    found = problems(tmp_path, edited(change), DECLARES_TEST_PASSWORD)

    assert found == (
        (
            "targets.cart_items.locators[0]: has no scope, but assertions[6] "
            "(a7) checks that this target is not visible: without a scope, its "
            "absence passes on any page that lacks a match, such as a wrong page "
            "or a 404"
        ),
    )


def test_an_unscoped_locator_after_a_scoped_one_is_refused(tmp_path: Path) -> None:
    # pay_button finds its element by role inside app-payment-step, then by
    # css with no scope.
    def change(script: dict[str, Any]) -> None:
        script["assertions"].append(
            {
                "id": "a7",
                "expect_index": 4,
                "check": "not_visible",
                "target": "pay_button",
            }
        )

    found = problems(tmp_path, edited(change), DECLARES_TEST_PASSWORD)

    assert [line.split(": ", 1)[0] for line in found] == [
        "targets.pay_button.locators[1]"
    ]


def test_repeated_keys_are_reported_in_the_order_the_text_writes_them(
    tmp_path: Path,
) -> None:
    text = (
        data_models_example()
        .replace('"Email" }', '"Email", "name": "Mail" }', 1)
        .replace('"/checkout/payment" }', '"/checkout/payment", "pattern": "/" }', 1)
        .replace('"confirmed": true', '"confirmed": true, "confirmed": false', 1)
    )

    found = problems(tmp_path, text, DECLARES_TEST_PASSWORD)

    assert [line.split(": ", 1)[0] for line in found] == [
        "confirmed",
        "targets.email_input.locators[0].name",
        "assertions[4].pattern",
    ]
