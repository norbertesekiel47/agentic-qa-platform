import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import ByCss, CompiledScript, HeldValue
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_compiled, parse_compiled

from packages.core.tests.test_compiled import data_models_example
from packages.core.tests.test_load_compiled import DECLARES_TEST_PASSWORD


def test_parsing_json_reads_no_file_and_keeps_the_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "missing.json"
    script = parse_compiled(
        data_models_example(), DECLARES_TEST_PASSWORD, source=source
    )
    assert script.spec_id == "checkout-expired-card"
    assert script.compiled_at == datetime(2026, 10, 12, 14, 3, 22, tzinfo=UTC)
    assert not source.exists()


@pytest.mark.parametrize(
    ("old", "new", "config", "problem"),
    [
        (
            '"confirmed": true',
            '"confirmed": true, "confirmed": false',
            DECLARES_TEST_PASSWORD,
            "confirmed: the key appears more than once, and a reader keeps only its last value, so write it once",
        ),
        (
            '"target": "sign_in"',
            '"target": "missing"',
            DECLARES_TEST_PASSWORD,
            "steps[3].target: 'missing' names no target in targets",
        ),
        (
            '"confirmed": true',
            '"confirmed": true',
            ProjectConfig(),
            "steps[2].secret: secret TEST_PASSWORD is not declared in the project config's secrets",
        ),
    ],
)
def test_both_entry_points_report_literal_problems(
    tmp_path: Path, old: str, new: str, config: ProjectConfig, problem: str
) -> None:
    source = tmp_path / "script.json"
    text = data_models_example().replace(old, new)
    source.write_text(text)
    with pytest.raises(SpecError) as parsed:
        parse_compiled(text, config, source=source)
    with pytest.raises(SpecError) as loaded:
        load_compiled(source, config)
    assert parsed.value.problems == (f"{source}: {problem}",)
    assert loaded.value.problems == (f"{source}: {problem}",)


@pytest.mark.parametrize(("target", "index"), [("cart_items", 0), ("pay_button", 1)])
def test_parser_and_loader_refuse_each_unscoped_negative_locator(
    tmp_path: Path, target: str, index: int
) -> None:
    data = json.loads(data_models_example())
    data["assertions"].append(
        {"id": "a7", "expect_index": 2, "check": "not_visible", "target": target}
    )
    text, source = json.dumps(data), tmp_path / "script.json"
    source.write_text(text)
    readers: tuple[Callable[[], CompiledScript], ...] = (
        lambda: parse_compiled(text, DECLARES_TEST_PASSWORD, source=source),
        lambda: load_compiled(source, DECLARES_TEST_PASSWORD),
    )
    for read in readers:
        with pytest.raises(SpecError) as refused:
            read()
        assert refused.value.problems == (
            (
                f"{source}: targets.{target}.locators[{index}]: has no scope, but "
                "assertions[6] (a7) checks that this target is not visible: without "
                "a scope, its absence passes on any page that lacks a match, such "
                "as a wrong page or a 404"
            ),
        )


# A scope by the value step 2 fills (ADR-0025's #58 amendment).
EMAIL = "returning@example.test"


def holding(
    target: str, position: int, held: dict[str, object], *, scoped: bool = True
) -> dict[str, Any]:
    """The DATA_MODEL example with the target's locator at `position`, or
    its scope, holding `held`."""
    data: dict[str, Any] = json.loads(data_models_example())
    locator = data["targets"][target]["locators"][position]
    (locator["scope"] if scoped else locator)["holds_value"] = held
    return data


def read(data: dict[str, Any], tmp_path: Path) -> CompiledScript:
    """`data` as parse_compiled and load_compiled read it, each the same."""
    text, source = json.dumps(data), tmp_path / "script.json"
    source.write_text(text)
    readers: tuple[Callable[[], CompiledScript], ...] = (
        lambda: parse_compiled(text, DECLARES_TEST_PASSWORD, source=source),
        lambda: load_compiled(source, DECLARES_TEST_PASSWORD),
    )
    scripts = [reader() for reader in readers]
    assert scripts[0] == scripts[1]
    return scripts[0]


@pytest.mark.parametrize(("target", "position"), [("pay_button", 0), ("cart_items", 1)])
def test_a_scope_may_hold_the_value_an_earlier_fill_step_typed(
    tmp_path: Path, target: str, position: int
) -> None:
    held = {"step": 2, "value": EMAIL}

    script = read(holding(target, position, held), tmp_path)

    scope = script.targets[target].locators[position].scope
    assert isinstance(scope, ByCss)
    assert scope.holds_value == HeldValue(step=2, value=EMAIL)


def refusals(data: dict[str, Any], tmp_path: Path) -> list[tuple[str, ...]]:
    """The problems parse_compiled and load_compiled each give `data`."""
    text, source = json.dumps(data), tmp_path / "script.json"
    source.write_text(text)
    found: list[tuple[str, ...]] = []
    readers: tuple[Callable[[], CompiledScript], ...] = (
        lambda: parse_compiled(text, DECLARES_TEST_PASSWORD, source=source),
        lambda: load_compiled(source, DECLARES_TEST_PASSWORD),
    )
    for reader in readers:
        with pytest.raises(SpecError) as refused:
            reader()
        found.append(
            tuple(each.removeprefix(f"{source}: ") for each in refused.value.problems)
        )
    return found


PAY = "targets.pay_button.locators[0].scope.holds_value"


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (
            holding("pay_button", 0, {"step": 7, "value": EMAIL}),
            f"{PAY}.step: 7 names no fill step",
        ),
        (
            holding("pay_button", 0, {"step": 3, "value": EMAIL}),
            f"{PAY}.step: 3 names no fill step",
        ),
        (
            holding("pay_button", 0, {"step": 4, "value": EMAIL}),
            f"{PAY}.step: 4 names no fill step",
        ),
        (
            holding("pay_button", 0, {"step": 2, "value": "someone@example.test"}),
            f"{PAY}.value: isn't the value step 2 fills",
        ),
        (
            holding("email_input", 2, {"step": 2, "value": EMAIL}, scoped=False),
            (
                "targets.email_input.locators[2].holds_value.step: steps[1] uses "
                "this target, and step 2, whose value it holds, doesn't come "
                "before it"
            ),
        ),
    ],
    ids=["no step", "a fill_secret", "a click", "another value", "not earlier"],
)
def test_a_held_value_names_an_earlier_fill_step_and_its_value(
    tmp_path: Path, data: dict[str, Any], problem: str
) -> None:
    assert refusals(data, tmp_path) == [(problem,), (problem,)]


HOODIE = "Classic Hoodie in M"


@pytest.mark.parametrize(
    ("check", "value"),
    [
        ({"pattern": "Classic Hoodie.*\\bM\\b"}, HOODIE),
        ({"text": "Classic Hoodie"}, HOODIE),
        ({"text": "Classic Hoodie in M, size medium"}, HOODIE),
    ],
    ids=[
        "its pattern matches the value",
        "the value holds its text",
        "its text holds the value",
    ],
)
def test_a_held_value_never_finds_the_text_its_targets_check_reads(
    tmp_path: Path, check: dict[str, str], value: str
) -> None:
    # Found by the text its check verifies, the target would be missing,
    # which is drift, wherever that text is missing, which is a failure.
    data = holding("cart_items", 1, {"step": 2, "value": value})
    data["steps"][1]["value"] = value
    assertion = data["assertions"][3]
    assertion.pop("pattern")
    assertion.update(check)

    problem = (
        "targets.cart_items.locators[1].scope.holds_value.value: assertions[3] "
        "(a4) checks this target's text, and the value it holds matches that "
        "text, so a failure there would read as drift"
    )
    assert refusals(data, tmp_path) == [(problem,), (problem,)]
