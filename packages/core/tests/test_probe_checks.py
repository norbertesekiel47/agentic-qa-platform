"""The compiled format's probe checks: probe_equals's fields and the JSON path
a probe's value is selected by, in probe_equals and probe_baselines
(DATA_MODEL §7; ADR-0025's #48 amendment)."""

import json
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript, ProbeEquals, json_path_steps

from packages.core.tests.test_compiled import errors, example

# A probe_equals check on §7's example probe (#48).
PROBE_EQUALS: dict[str, Any] = {
    "id": "a7",
    "expect_index": 1,
    "check": "probe_equals",
    "probe": "orders_count",
    "json_path": "$.count",
    "value": 0,
}


def test_probe_equals_takes_a_probe_a_json_path_and_a_value() -> None:
    script = example()
    script["assertions"] += [
        PROBE_EQUALS | {"value": 2},
        PROBE_EQUALS | {"id": "a8", "json_path": "$.orders[0].state", "value": "paid"},
    ]

    compiled = CompiledScript.model_validate_json(json.dumps(script))

    count, state = compiled.assertions[-2:]
    assert isinstance(count, ProbeEquals)
    assert (count.probe, count.json_path, count.value) == ("orders_count", "$.count", 2)
    assert isinstance(state, ProbeEquals)
    assert (state.json_path, state.value) == ("$.orders[0].state", "paid")


@pytest.mark.parametrize("field", ["probe", "json_path", "value"])
def test_probe_equals_needs_each_of_its_fields(field: str) -> None:
    script = example()
    script["assertions"].append(PROBE_EQUALS.copy())
    del script["assertions"][-1][field]

    [(location, kind, _)] = errors(script)

    assert (location, kind) == (("assertions", 6, "probe_equals", field), "missing")


# Neither true, 2.0 nor "2" reads as 2, and an empty string compares as nothing.
@pytest.mark.parametrize("value", [True, 2.0, "", None, [2]])
def test_probe_equals_value_is_an_integer_or_a_string(value: object) -> None:
    script = example()
    script["assertions"].append(PROBE_EQUALS | {"value": value})

    problems = errors(script)

    assert problems
    assert {location[:4] for location, _, _ in problems} == {
        ("assertions", 6, "probe_equals", "value")
    }


@pytest.mark.parametrize(
    "json_path",
    [
        "$",
        "$.count",
        "$.orders[0].state",
        "$[2]",
        "$[123456789]",
        "$.comment-count",
        "$._id9",
    ],
)
def test_a_json_path_selects_by_names_and_indexes(json_path: str) -> None:
    script = example()
    script["probe_baselines"]["orders_count"]["json_path"] = json_path
    script["assertions"].append(PROBE_EQUALS | {"json_path": json_path})

    compiled = CompiledScript.model_validate_json(json.dumps(script))

    assert compiled.probe_baselines["orders_count"].json_path == json_path
    last = compiled.assertions[-1]
    assert isinstance(last, ProbeEquals)
    assert last.json_path == json_path


@pytest.mark.parametrize(
    "json_path",
    [
        "count",
        "$.",
        "$..count",
        "$['count']",
        "$.orders[*]",
        "$.orders[-1]",
        "$.orders[01]",
        # Past nine digits, an index no array has, and Python's int() can refuse.
        "$[1234567890]",
        "$.orders[0",
        "$ .count",
        "$.count ",
        "$.café",
    ],
)
def test_a_json_path_outside_the_syntax_is_refused(json_path: str) -> None:
    script = example()
    script["probe_baselines"]["orders_count"]["json_path"] = json_path
    script["assertions"].append(PROBE_EQUALS | {"json_path": json_path})

    problems = errors(script)

    assert [location for location, _, _ in problems] == [
        ("probe_baselines", "orders_count", "json_path"),
        ("assertions", 6, "probe_equals", "json_path"),
    ]
    assert all(
        message
        == (
            f"{json_path!r} is not a JSON path this format reads: write $, then "
            ".name (ASCII letters, digits, _ or -) or [index] for each step, such "
            "as $.count or $.orders[0].total"
        )
        for _, _, message in problems
    )


def test_json_path_steps_reads_keys_and_indexes_in_order() -> None:
    assert json_path_steps("$.orders[0].lines[12].qty") == (
        "orders",
        0,
        "lines",
        12,
        "qty",
    )
    assert json_path_steps("$") == ()


def test_json_path_steps_refuses_any_other_text() -> None:
    # Never the steps it can find in it: "xx.a b[0]" isn't $.a[0].
    with pytest.raises(ValueError, match="is not a JSON path this format reads"):
        json_path_steps("xx.a b[0]")
