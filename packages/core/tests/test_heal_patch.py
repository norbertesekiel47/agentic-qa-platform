import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.heal_patch import heal_patch_problems
from aqa_core.project import parse_compiled

from packages.core.tests.test_compiled import data_models_example
from packages.core.tests.test_load_compiled import DECLARES_TEST_PASSWORD

# A change to DATA_MODEL §7's example, made in place on its JSON.
type Change = Callable[[dict[str, Any]], object]


def unchanged(data: dict[str, Any]) -> None:
    del data


def script(change: Change) -> CompiledScript:
    """DATA_MODEL §7's example, changed, then read as the loader reads it."""
    data = json.loads(data_models_example())
    change(data)
    return parse_compiled(
        json.dumps(data), DECLARES_TEST_PASSWORD, source=Path("script.json")
    )


def heal(change: Change, base: Change = unchanged) -> list[str]:
    """The problems with healing `base`'s script into one `change` makes of it."""
    return heal_patch_problems(
        script(base), script(lambda data: (base(data), change(data)))
    )


# A replay-safe step's target, a side-effect step's, and an assertion's.
@pytest.mark.parametrize("target", ["email_input", "sign_in", "cart_items"])
def test_a_heal_may_rebind_any_targets_locators(target: str) -> None:
    assert heal(lambda d: d["targets"][target].update(locators=[{"css": "#x"}])) == []


def another_cart_view(data: dict[str, Any]) -> None:
    """Adds a target of the cart items' meaning, whose text a4 checks, that
    only a visibility check reads."""
    data["targets"]["cart_view"] = {
        "semantic": data["targets"]["cart_items"]["semantic"],
        "locators": [{"testid": "cart-view"}],
    }
    data["assertions"].append(
        {
            "id": "a7",
            "expect_index": 2,
            "check": "visible_unoccluded",
            "target": "cart_view",
            "min_size_px": [1, 1],
            "in_viewport": True,
        }
    )


# ADR-0025: an assertion's target is never found by what its claim says.
@pytest.mark.parametrize(
    ("base", "target", "locator", "path"),
    [
        # a4 checks the cart's text, which this name could hold.
        (unchanged, "cart_items", {"role": "list", "name": "Classic Hoodie M"}, ""),
        (unchanged, "pay_button", {"label": "Pay"}, ""),
        (unchanged, "pay_button", {"placeholder": "Pay"}, ""),
        (
            unchanged,
            "cart_items",
            {"testid": "cart-lines", "scope": {"role": "region", "name": "Cart"}},
            "/scope",
        ),
        (another_cart_view, "cart_view", {"role": "list", "name": "Cart"}, ""),
    ],
    ids=["named, text checked", "label", "placeholder", "named scope", "meaning"],
)
def test_a_heal_finds_no_assertion_target_by_what_it_could_claim(
    base: Change, target: str, locator: dict[str, Any], path: str
) -> None:
    assert heal(
        lambda d: d["targets"][target].update(locators=[locator]), base=base
    ) == [
        (
            f"/targets/{target}/locators/0{path}: a heal finds no assertion's "
            "target by a label or placeholder, or by a name when its text is checked"
        )
    ]


# The Pay button's name, where no check reads its text; an action's label.
@pytest.mark.parametrize(
    ("target", "locator"),
    [
        ("pay_button", {"role": "button", "name": "Pay now"}),
        ("email_input", {"label": "Email address"}),
    ],
    ids=["named, text unchecked", "action"],
)
def test_a_heal_may_find_a_target_by_what_its_uses_allow(
    target: str, locator: dict[str, Any]
) -> None:
    assert heal(lambda d: d["targets"][target].update(locators=[locator])) == []


def test_a_heal_never_changes_a_targets_meaning() -> None:
    assert heal(
        lambda d: d["targets"]["pay_button"].update(semantic="the cart's button")
    ) == ["/targets/pay_button/semantic: a heal never changes a target's meaning"]


def test_a_heal_never_changes_a_contract() -> None:
    contract = {"region": "app-cart-summary", "part": "ul.cart-lines"}
    assert heal(lambda d: d["targets"]["cart_items"].update(contract=contract)) == [
        (
            "/targets/cart_items/contract: a heal never changes a subject "
            "contract, which only the reviewed config gives"
        )
    ]


SPARE = {"semantic": "the cart's close button", "locators": [{"css": "#close"}]}


@pytest.mark.parametrize(
    ("base", "change", "problem"),
    [
        (
            lambda d: d["targets"].update(spare=SPARE),
            lambda d: d["targets"].pop("spare"),
            "/targets/spare: a heal removes no target",
        ),
        (
            unchanged,
            lambda d: d["targets"].update(spare=SPARE),
            "/targets/spare: a heal adds no target",
        ),
    ],
    ids=["removed", "added"],
)
def test_a_heal_neither_adds_nor_removes_a_target(
    base: Change, change: Change, problem: str
) -> None:
    assert heal(change, base) == [problem]


# Selectors Level 4, §13.3 and §13.4: every child-indexed pseudo-class.
@pytest.mark.parametrize(
    "css",
    [
        "li:nth-child(2)",
        "li:nth-last-child(2)",
        "li:first-child",
        "li:last-child",
        "li:only-child",
        "li:nth-of-type(2)",
        "li:nth-last-of-type(2)",
        "li:first-of-type",
        "li:last-of-type",
        "li:only-of-type",
    ],
)
def test_no_child_indexed_pseudo_class_finds_a_rebound_element(css: str) -> None:
    assert heal(
        lambda d: d["targets"]["cart_items"].update(locators=[{"css": css}])
    ) == [
        (
            "/targets/cart_items/locators/0/css: a heal finds no element by "
            "its position among its siblings"
        )
    ]


@pytest.mark.parametrize(
    ("locator", "path"),
    [
        (
            {"testid": "cart-lines", "scope": {"css": "app-cart-summary:FIRST-CHILD"}},
            "/targets/cart_items/locators/0/scope/css",
        ),
        # \2d is an escaped hyphen, which CSS reads as one.
        ({"css": r"ul:nth\2d of-type(1)"}, "/targets/cart_items/locators/0/css"),
    ],
    ids=["scope", "escaped"],
)
def test_a_heal_writes_no_positional_pseudo_class(
    locator: dict[str, Any], path: str
) -> None:
    assert heal(lambda d: d["targets"]["cart_items"].update(locators=[locator])) == [
        f"{path}: a heal finds no element by its position among its siblings"
    ]


@pytest.mark.parametrize(
    ("locator", "path"),
    [
        # :first-child and :last-child, spelled with a sibling combinator.
        ({"css": "li:not(* ~ *)"}, "/targets/cart_items/locators/0/css"),
        ({"css": "li:not(:has(~ *))"}, "/targets/cart_items/locators/0/css"),
        (
            {"testid": "cart-lines", "scope": {"css": "h2 + app-cart-summary"}},
            "/targets/cart_items/locators/0/scope/css",
        ),
    ],
    ids=["general sibling", "nested", "next sibling in a scope"],
)
def test_a_heal_finds_no_element_by_its_siblings(
    locator: dict[str, Any], path: str
) -> None:
    assert heal(lambda d: d["targets"]["cart_items"].update(locators=[locator])) == [
        f"{path}: a heal finds no element by its position among its siblings"
    ]


# An attribute's ~= operator, a quoted value and an escaped character.
@pytest.mark.parametrize(
    "css", ["ul[class~=cart-lines]", 'ul[title="a + b ~ c"]', r"ul.a\+b"]
)
def test_a_tilde_or_plus_that_combines_no_siblings_passes(css: str) -> None:
    assert (
        heal(lambda d: d["targets"]["cart_items"].update(locators=[{"css": css}])) == []
    )


def reviewed(data: dict[str, Any]) -> None:
    """Gives the Pay button a positional locator, as a person may write in a
    reviewed edit (ADR-0025, #52 amendment), before a locator of its own."""
    data["targets"]["pay_button"]["locators"] = [
        {"css": "app-payment-step button:last-child"},
        {"css": "#pay"},
    ]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["targets"]["cart_items"].update(locators=[{"css": "#x"}]),
        lambda d: d["targets"]["pay_button"]["locators"][1].update(css="#pay-now"),
    ],
    ids=["another target", "its other locator"],
)
def test_a_heal_may_keep_a_reviewed_positional_locator(change: Change) -> None:
    assert heal(change, base=reviewed) == []


def inserted(step: dict[str, Any]) -> Change:
    """Inserts `step`, as seq 10, before the sign-in click (index 3)."""
    return lambda d: d["steps"].insert(3, {"seq": 10, **step})


def test_a_heal_may_insert_a_replay_safe_step() -> None:
    # Existing steps keep their seq, so seqs may run out of order.
    step = {"action": "press", "key": "Escape", "side_effect": False}
    assert heal(inserted(step)) == []


# A replay-safe fill (seq 2) and the side-effect sign-in click (seq 4).
@pytest.mark.parametrize("index", [1, 3])
def test_a_heal_removes_no_step(index: int) -> None:
    assert heal(lambda d: d["steps"].pop(index)) == [
        f"/steps/{index}: a heal removes no step"
    ]


def test_a_heal_reorders_no_step() -> None:
    # The password fill (seq 3) moves before the email fill (seq 2).
    assert heal(lambda d: d["steps"].insert(1, d["steps"].pop(2))) == [
        "/steps: a heal reorders no step"
    ]


def test_a_heal_never_changes_a_side_effect_flag() -> None:
    lowered = {"side_effect": False, "side_effect_basis": None}
    assert heal(lambda d: d["steps"][3].update(lowered)) == [
        "/steps/3/side_effect: a heal rewrites no existing step",
        "/steps/3/side_effect_basis: a heal rewrites no existing step",
    ]


@pytest.mark.parametrize(
    ("path", "change"),
    [
        ("/steps/1/value", lambda d: d["steps"][1].update(value="b@example.test")),
        ("/steps/3/target", lambda d: d["steps"][3].update(target="pay_button")),
    ],
    ids=["replay-safe", "side-effect"],
)
def test_a_heal_rewrites_no_existing_step(path: str, change: Change) -> None:
    assert heal(change) == [f"{path}: a heal rewrites no existing step"]


def required(data: dict[str, Any]) -> None:
    """Requires a condition, which the email fill (index 1) meets."""
    data["coverage"]["requires"] = [{"id": "c1", "condition": "signed in as Ann"}]
    data["steps"][1]["satisfies"] = ["c1"]


def test_a_heal_never_moves_a_satisfies_mark() -> None:
    # So every required condition stays met by the step that met it.
    moved = {1: [], 2: ["c1"]}
    assert heal(
        lambda d: [d["steps"][i].update(satisfies=m) for i, m in moved.items()],
        base=required,
    ) == [
        "/steps/1/satisfies: a heal rewrites no existing step",
        "/steps/2/satisfies: a heal rewrites no existing step",
    ]


def test_a_heal_inserts_no_side_effect_step() -> None:
    step = {
        "action": "press",
        "key": "Enter",
        "side_effect": True,
        "side_effect_basis": "network: a write request",
    }
    assert heal(inserted(step)) == [
        "/steps/3/side_effect: a heal inserts only replay-safe steps"
    ]


def test_a_heal_inserts_no_navigation() -> None:
    step = {"action": "navigate", "url": "/checkout/payment", "side_effect": False}
    assert heal(inserted(step)) == [
        (
            "/steps/3/action: a heal inserts no navigation, which could route "
            "around a path that is broken for its users"
        )
    ]


def test_a_heal_inserts_no_reload() -> None:
    assert heal(inserted({"action": "reload", "side_effect": False})) == [
        (
            "/steps/3/action: a heal inserts no reload, which loads the page "
            "again and could route around a path that is broken for its users"
        )
    ]


def test_a_heal_inserts_no_fill_secret() -> None:
    step = {
        "action": "fill_secret",
        "target": "password_input",
        "secret": "TEST_PASSWORD",
        "side_effect": False,
    }
    assert heal(inserted(step)) == [
        (
            "/steps/3/action: a heal inserts no fill_secret, which could steer "
            "a bound secret into another field"
        )
    ]


def test_a_heal_marks_no_inserted_step_as_meeting_a_condition() -> None:
    step = {"action": "press", "key": "Tab", "side_effect": False, "satisfies": ["c1"]}
    assert heal(inserted(step), base=required) == [
        "/steps/3/satisfies: a heal marks no inserted step as meeting a condition"
    ]


def renamed(data: dict[str, Any]) -> None:
    """Names the cart's target with both characters a JSON pointer escapes."""
    data["targets"]["cart/~items"] = data["targets"].pop("cart_items")
    data["assertions"][3]["target"] = "cart/~items"


def test_a_path_escapes_a_target_name() -> None:
    assert heal(
        lambda d: d["targets"]["cart/~items"].update(semantic="the cart's total"),
        base=renamed,
    ) == ["/targets/cart~1~0items/semantic: a heal never changes a target's meaning"]


@pytest.mark.parametrize(
    ("field", "change"),
    [
        ("assertions", lambda d: d["assertions"][0].update(text="card was declined")),
        ("spec_hash", lambda d: d.update(spec_hash="sha256:" + "0" * 64)),
        (
            "compiled_by",
            lambda d: d["compiled_by"].update(subject_contracts="sha256:" + "1" * 64),
        ),
        ("browser", lambda d: d["browser"].update(locale="de-DE")),
        (
            "coverage",
            lambda d: d["coverage"]["expectations"][0].update(claim="says declined"),
        ),
        (
            "probe_baselines",
            lambda d: d["probe_baselines"]["orders_count"].update(json_path="$.n"),
        ),
        ("compiled_at", lambda d: d.update(compiled_at="2026-10-28T00:00:00Z")),
        ("confirmed", lambda d: d.update(confirmed=False)),
        ("spec_id", lambda d: d.update(spec_id="checkout-declined-card")),
    ],
)
def test_a_heal_changes_nothing_outside_targets_and_steps(
    field: str, change: Change
) -> None:
    assert heal(change) == [
        f"/{field}: a heal changes nothing but target locators and inserted steps"
    ]
