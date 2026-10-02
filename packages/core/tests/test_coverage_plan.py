"""The coverage plan (ADR-0024; DATA_MODEL §7, "Coverage plan first"; #41):
each expectation's subject and the checks that establish its claim, or why no
check can, the conditions the plan requires, and the hash that freezes it."""

import hashlib
from typing import Any

import pytest
from aqa_core.coverage_plan import (
    CoveragePlan,
    PlannedCheck,
    PlannedExpectation,
    misfits,
    plan_hash,
    uncovered,
)
from aqa_core.spec import SpecContext, SpecFrontmatter
from pydantic import ValidationError

# One planned check of each of M1's nine types, with exactly the fields its
# type takes (DATA_MODEL §7's compiled fields, less what compiling adds).
OF_EACH_TYPE: dict[str, dict[str, Any]] = {
    "text_visible": {"text": "card has expired"},
    "text_in_target": {
        "target_meaning": "the line items in the cart summary",
        "pattern": r"Classic Hoodie.*\bM\b",
    },
    "not_visible": {"target_meaning": "the header's sign-in link"},
    "url_matches": {"pattern": "/checkout/payment"},
    "network_none": {
        "method": "POST",
        "url_pattern": "/api/orders",
        "status_class": "2xx",
    },
    "network_seen": {
        "method": "GET",
        "url_pattern": "/api/cart",
        "status_class": "2xx",
    },
    "probe_equals": {"probe": "orders_count", "value": 0},
    "probe_equals_baseline": {"probe": "orders_count"},
    "visible_unoccluded": {"target_meaning": "the payment step's submit button"},
}

# A valid value for every field a check may take.
ANY_VALUE: dict[str, Any] = {
    "target_meaning": "the order summary",
    "text": "Pay",
    "pattern": "Pay",
    "probe": "orders_count",
    "value": "open",
    "method": "DELETE",
    "url_pattern": "/api/x",
    "status_class": "4xx",
}

TEXT_OR_PATTERN = {"text", "pattern"}


def planned(check: str, **fields: Any) -> PlannedCheck:
    return PlannedCheck.model_validate({"check": check, **fields})


@pytest.mark.parametrize("check", sorted(OF_EACH_TYPE))
def test_each_check_type_takes_its_own_fields(check: str) -> None:
    made = planned(check, **OF_EACH_TYPE[check])

    assert made.check == check
    assert made.model_dump(exclude_none=True) == {"check": check, **OF_EACH_TYPE[check]}


@pytest.mark.parametrize(
    ("check", "field"),
    [
        (check, field)
        for check, fields in sorted(OF_EACH_TYPE.items())
        for field in sorted(fields)
        if field not in TEXT_OR_PATTERN
    ],
)
def test_a_check_without_a_field_its_type_needs_is_refused(
    check: str, field: str
) -> None:
    fields = {name: v for name, v in OF_EACH_TYPE[check].items() if name != field}

    with pytest.raises(ValidationError, match=f"{check} check needs {field}"):
        planned(check, **fields)


@pytest.mark.parametrize(
    ("check", "field"),
    [
        (check, field)
        for check, fields in sorted(OF_EACH_TYPE.items())
        for field in sorted(ANY_VALUE)
        if field not in fields
        and not (field in TEXT_OR_PATTERN and fields.keys() & TEXT_OR_PATTERN)
    ],
)
def test_a_check_with_a_field_its_type_does_not_take_is_refused(
    check: str, field: str
) -> None:
    with pytest.raises(ValidationError, match=f"{check} check takes no {field}"):
        planned(check, **OF_EACH_TYPE[check], **{field: ANY_VALUE[field]})


@pytest.mark.parametrize("check", ["text_visible", "text_in_target"])
@pytest.mark.parametrize("fields", [{}, {"text": "Pay", "pattern": "Pay"}])
def test_a_text_check_takes_text_or_pattern_exactly_one(
    check: str, fields: dict[str, str]
) -> None:
    others = {
        name: value
        for name, value in OF_EACH_TYPE[check].items()
        if name not in TEXT_OR_PATTERN
    }

    with pytest.raises(
        ValidationError, match=f"a {check} check takes text or pattern, exactly one"
    ):
        planned(check, **others, **fields)


@pytest.mark.parametrize(
    "check", ["pixel_diff", "contrast_min", "model_verify", "text"]
)
def test_only_m1s_nine_check_types_can_be_planned(check: str) -> None:
    # pixel_diff, contrast_min and model_verify arrive in M2: an expectation
    # that needs one is planned as unsupported instead.
    with pytest.raises(ValidationError) as error:
        planned(check, text="Pay")

    assert [detail["loc"] for detail in error.value.errors()] == [("check",)]


@pytest.mark.parametrize(
    ("check", "fields", "refused"),
    [
        # Written as the compiled script writes them, so a planned check
        # always fits its assertion (DATA_MODEL §7, "Reading a compiled script").
        ("text_visible", {"text": "card  has expired"}, "text"),
        ("text_visible", {"text": "\uf218 New Article"}, "text"),
        ("text_visible", {"pattern": "card (has"}, "pattern"),
        ("url_matches", {"pattern": "[[:digit:]]"}, "pattern"),
        ("network_seen", {**OF_EACH_TYPE["network_seen"], "method": "FETCH"}, "method"),
        (
            "network_seen",
            {**OF_EACH_TYPE["network_seen"], "status_class": "2XX"},
            "status_class",
        ),
    ],
)
def test_planned_values_are_held_to_the_compiled_scripts_rules(
    check: str, fields: dict[str, str], refused: str
) -> None:
    with pytest.raises(ValidationError) as error:
        planned(check, **fields)

    assert [detail["loc"] for detail in error.value.errors()] == [(refused,)]


PAY_BUTTON = {
    "check": "visible_unoccluded",
    "target_meaning": "the payment step's submit button",
}


def expectation(**fields: Any) -> PlannedExpectation:
    return PlannedExpectation.model_validate(
        {
            "expect_index": 4,
            "subject": "the payment step's submit button",
            "claim": "visible and not covered",
            **fields,
        }
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"checks": []},
        {"checks": [PAY_BUTTON], "unsupported": {"reason": "covered twice"}},
    ],
)
def test_an_expectation_has_checks_or_is_unsupported_never_both_nor_neither(
    fields: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="checks or is unsupported"):
        expectation(**fields)


def test_an_expectation_lists_each_check_once() -> None:
    with pytest.raises(ValidationError) as error:
        expectation(checks=[PAY_BUTTON, PAY_BUTTON])

    assert [detail["loc"] for detail in error.value.errors()] == [("checks",)]


def test_an_expectation_index_is_never_negative() -> None:
    with pytest.raises(ValidationError) as error:
        expectation(expect_index=-1, checks=[PAY_BUTTON])

    assert [detail["loc"] for detail in error.value.errors()] == [("expect_index",)]


@pytest.mark.parametrize("needs", ["pixel_diff", "contrast_min", "model_verify", None])
def test_an_unsupported_expectation_says_what_it_needs(needs: str | None) -> None:
    unsupported = {"reason": "the claim is about how the button looks", "needs": needs}

    made = expectation(unsupported=unsupported)

    assert made.unsupported is not None
    assert made.unsupported.needs == needs
    assert made.checks == ()


@pytest.mark.parametrize("needs", ["visible_unoccluded", "a model", ""])
def test_an_unsupported_expectation_needs_an_m2_check_or_nothing_named(
    needs: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        expectation(unsupported={"reason": "no check", "needs": needs})

    assert [detail["loc"] for detail in error.value.errors()] == [
        ("unsupported", "needs")
    ]


def plan(**fields: Any) -> CoveragePlan:
    return CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": "the payment step's submit button",
                    "claim": "visible and not covered",
                    "checks": [PAY_BUTTON],
                }
            ],
            "requires": [],
            **fields,
        }
    )


def test_a_plan_covers_at_least_one_expectation() -> None:
    with pytest.raises(ValidationError) as error:
        plan(expectations=[])

    assert [detail["loc"] for detail in error.value.errors()] == [("expectations",)]


def test_a_plans_conditions_have_distinct_ids() -> None:
    reload = {"id": "c1", "condition": "checked after reloading the article page"}
    signed_in = {"id": "c1", "condition": "signed in as reader"}

    with pytest.raises(ValidationError, match="c1"):
        plan(requires=[reload, signed_in])


def test_plan_hash_is_the_sha256_of_the_plans_canonical_json() -> None:
    # Written out by hand: sorted keys, no whitespace, ASCII escapes, and no
    # field the plan leaves out (spec_hash's rules, DATA_MODEL §7).
    canonical = (
        '{"expectations":[{"checks":[{"check":"visible_unoccluded",'
        '"target_meaning":"the payment step\'s submit button"}],'
        '"claim":"visible and not covered","expect_index":0,'
        '"subject":"the payment step\'s submit button"}],"requires":[]}'
    )

    assert (
        plan_hash(plan()) == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    )


def test_plan_hash_escapes_non_ascii_as_spec_hash_does() -> None:
    accented = plan(requires=[{"id": "c1", "condition": "signed in as Zoë"}])
    canonical = (
        '{"expectations":[{"checks":[{"check":"visible_unoccluded",'
        '"target_meaning":"the payment step\'s submit button"}],'
        '"claim":"visible and not covered","expect_index":0,'
        '"subject":"the payment step\'s submit button"}],'
        '"requires":[{"condition":"signed in as Zo\\u00eb","id":"c1"}]}'
    )

    assert (
        plan_hash(accented)
        == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    )


# Each changes one thing the plan_hash covers: a subject, a claim, a check, an
# unsupported expectation's reason and need, and a required condition.
CHANGES: dict[str, dict[str, Any]] = {
    "subject": {"subject": "the payment step's pay button"},
    "claim": {"claim": "visible"},
    "check": {"checks": [{**PAY_BUTTON, "target_meaning": "the pay button"}]},
    "unsupported": {"checks": [], "unsupported": {"reason": "looks"}},
    "needs": {"checks": [], "unsupported": {"reason": "looks", "needs": "pixel_diff"}},
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_plan_hash_changes_with_anything_the_plan_says(change: str) -> None:
    original = plan()
    entry = {
        **original.expectations[0].model_dump(exclude_none=True),
        **CHANGES[change],
    }

    changed = plan(expectations=[entry])

    assert plan_hash(changed) != plan_hash(original)


def test_plan_hash_changes_with_a_required_condition() -> None:
    reload = {"id": "c1", "condition": "checked after reloading the article page"}

    assert plan_hash(plan(requires=[reload])) != plan_hash(plan())


def test_a_plan_cannot_change_once_made() -> None:
    # The plan is frozen for the explore run (ADR-0024): what was hashed is
    # what the run uses.
    made = plan()

    with pytest.raises(ValidationError) as error:
        made.expectations[0].checks[0].target_meaning = "the pay button"

    assert [detail["type"] for detail in error.value.errors()] == ["frozen_instance"]


def test_a_plan_read_back_from_its_json_has_the_same_hash() -> None:
    original = plan(requires=[{"id": "c1", "condition": "after a reload"}])

    read_back = CoveragePlan.model_validate_json(original.model_dump_json())

    assert plan_hash(read_back) == plan_hash(original)


# A spec's frontmatter with three expectations and one probe.
CHECKOUT = SpecFrontmatter.model_validate(
    {
        "id": "checkout",
        "goal": "A returning user pays with an expired card and is told why it failed.",
        "preconditions": {
            "start_url": "/",
            "probes": {"orders_count": "GET /test-api/orders/count"},
        },
        "expect": [
            "An error message says the card has expired",
            "No order is created for this user",
            {
                "text": "The Pay button keeps its brand colour",
                "visual": "deterministic",
            },
        ],
    },
    context=SpecContext(file_id="checkout", declared_secrets=frozenset()),
)

ERROR_SHOWN = {"check": "text_visible", "text": "card has expired"}
NO_NEW_ORDER = {"check": "probe_equals_baseline", "probe": "orders_count"}


def entry(index: int, **fields: Any) -> dict[str, Any]:
    return {
        "expect_index": index,
        "subject": f"expectation {index}'s subject",
        "claim": f"expectation {index}'s claim",
        **fields,
    }


def checkout_plan(*entries: dict[str, Any]) -> CoveragePlan:
    return CoveragePlan.model_validate({"expectations": list(entries), "requires": []})


COVERED = (
    entry(0, checks=[ERROR_SHOWN]),
    entry(1, checks=[NO_NEW_ORDER]),
    entry(2, unsupported={"reason": "a colour", "needs": "pixel_diff"}),
)


def test_a_plan_with_one_entry_per_expectation_in_order_fits_its_spec() -> None:
    assert misfits(checkout_plan(*COVERED), CHECKOUT) == ()


@pytest.mark.parametrize(
    ("order", "said"),
    [
        ((0, 1), "[0, 1]"),
        ((0, 1, 2, 2), "[0, 1, 2, 2]"),
        ((0, 2, 1), "[0, 2, 1]"),
        ((1, 2, 3), "[1, 2, 3]"),
    ],
)
def test_a_plan_without_one_entry_per_expectation_in_order_does_not_fit(
    order: tuple[int, ...], said: str
) -> None:
    entries = [{**COVERED[min(i, 2)], "expect_index": i} for i in order]

    found = misfits(checkout_plan(*entries), CHECKOUT)

    assert len(found) == 1
    assert said in found[0]
    assert "3 expectations" in found[0]


def test_a_probe_check_names_a_probe_the_spec_declares() -> None:
    undeclared = {"check": "probe_equals", "probe": "order_count", "value": 0}
    entries = (COVERED[0], entry(1, checks=[undeclared]), COVERED[2])

    found = misfits(checkout_plan(*entries), CHECKOUT)

    assert len(found) == 1
    assert "expect[1]" in found[0]
    assert "order_count" in found[0]
    assert "orders_count" in found[0]


def test_a_fully_covered_plan_has_nothing_uncovered() -> None:
    covered = (COVERED[0], COVERED[1], entry(2, checks=[ERROR_SHOWN]))

    assert uncovered(checkout_plan(*covered), CHECKOUT) == ()


@pytest.mark.parametrize(
    ("needs", "named"),
    [
        (None, "no M1 check can establish it"),
        ("pixel_diff", "pixel_diff"),
        ("contrast_min", "contrast_min"),
        ("model_verify", "model_verify"),
    ],
)
def test_each_uncovered_expectation_is_named_with_its_reason_and_need(
    needs: str | None, named: str
) -> None:
    unsupported = {"reason": "the claim is about a colour", "needs": needs}
    entries = (COVERED[0], COVERED[1], entry(2, unsupported=unsupported))

    lines = uncovered(checkout_plan(*entries), CHECKOUT)

    assert len(lines) == 1
    assert lines[0].startswith('expect[2] "The Pay button keeps its brand colour"')
    assert named in lines[0]
    assert "the claim is about a colour" in lines[0]


def test_uncovered_refuses_a_plan_that_does_not_fit_its_spec() -> None:
    # An entry past the spec's last expectation names no expectation text.
    beyond = entry(3, unsupported={"reason": "a colour"})

    with pytest.raises(ValueError, match="doesn't fit its spec"):
        uncovered(checkout_plan(*COVERED, beyond), CHECKOUT)
