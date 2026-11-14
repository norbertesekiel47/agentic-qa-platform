import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aqa_core import text
from aqa_core.compiled import ByCss, ByTestId, CompiledScript, TextInTarget
from aqa_core.heal_patch import (
    PatchOp,
    apply_patch,
    blob_sha,
    heal_patch_problems,
    json_patch,
    literal_claim_spelled,
    proposal_id,
)
from aqa_core.project import SpecError, parse_compiled

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


def claiming(text: str) -> Change:
    """Makes a4, which checks the cart items' text, claim the literal `text`
    instead of its pattern."""

    def change(data: dict[str, Any]) -> None:
        data["assertions"][3].pop("pattern")
        data["assertions"][3]["text"] = text

    return change


def viewed(change: Change) -> Change:
    """`change`, then another target of the cart items' meaning, as
    `another_cart_view` adds it."""

    def both(data: dict[str, Any]) -> None:
        change(data)
        another_cart_view(data)

    return both


LITERAL = claiming("Classic Hoodie M")
SPELLS = (
    "a heal finds no assertion's target by a string that spells its text check's claim"
)


# #261: nor by a string that holds what its claim says, however CSS spells it.
@pytest.mark.parametrize(
    ("base", "target", "locator", "path"),
    [
        (LITERAL, "cart_items", {"css": 'ul[aria-label="Classic Hoodie M"]'}, "/css"),
        (
            LITERAL,
            "cart_items",
            {"css": "ul[title*='classic hoodie m, size' i]"},
            "/css",
        ),
        (LITERAL, "cart_items", {"css": r'ul[title="Classic\20 Hoodie\20 M"]'}, "/css"),
        (LITERAL, "cart_items", {"css": r"ul[title=Classic\ Hoodie\ M]"}, "/css"),
        (LITERAL, "cart_items", {"css": r"ul[title=\43 lassic\ Hoodie\ M]"}, "/css"),
        (LITERAL, "cart_items", {"css": 'ul[title="Classic   Hoodie\tM"]'}, "/css"),
        (LITERAL, "cart_items", {"testid": "Classic Hoodie M"}, "/testid"),
        (
            LITERAL,
            "cart_items",
            {"testid": "cart-lines", "scope": {"css": '[title="Classic Hoodie M"]'}},
            "/scope/css",
        ),
        (
            viewed(LITERAL),
            "cart_view",
            {"css": 'ul[aria-label="Classic Hoodie M"]'},
            "/css",
        ),
        (
            viewed(claiming("returning@example.test")),
            "cart_view",
            {
                "css": "ul",
                "holds_value": {"step": 2, "value": "returning@example.test"},
            },
            "/holds_value/value",
        ),
    ],
    ids=[
        "attribute",
        "in a longer value, any case",
        "hex escapes",
        "unquoted, escaped spaces",
        "an escape first",
        "runs of white space",
        "testid",
        "scope",
        "meaning",
        "held value",
    ],
)
def test_a_heal_finds_no_assertion_target_by_a_string_spelling_its_claim(
    base: Change, target: str, locator: dict[str, Any], path: str
) -> None:
    assert heal(
        lambda d: d["targets"][target].update(locators=[locator]), base=base
    ) == [f"/targets/{target}/locators/0{path}: {SPELLS}"]


# In a string, CSS reads a backslash before a line break as nothing, once
# \r\n, \r and \f are line breaks too (CSS Syntax 3, §3.3 and §4.3.5), so
# Playwright executes ul[aria-label="Classic Hoodie M"].
@pytest.mark.parametrize("base", [LITERAL, unchanged], ids=["literal", "pattern"])
@pytest.mark.parametrize(
    "newline", ["\n", "\r\n", "\r", "\f"], ids=["LF", "CRLF", "CR", "FF"]
)
def test_a_heal_finds_no_assertion_target_by_a_claim_split_by_a_line_continuation(
    base: Change, newline: str
) -> None:
    locator = {"css": f'ul[aria-label="Clas\\{newline}sic Hoodie M"]'}
    assert heal(
        lambda d: d["targets"]["cart_items"].update(locators=[locator]), base=base
    ) == [f"/targets/cart_items/locators/0/css: {SPELLS}"]


# Playwright drops a comment and writes the names either side of it as one,
# so it executes ul[aria-label=Classic\ Hoodie\ M].
@pytest.mark.parametrize("base", [LITERAL, unchanged], ids=["literal", "pattern"])
@pytest.mark.parametrize(
    "css",
    [
        r"ul[aria-label=Clas/**/sic\ Hoodie\ M]",
        r"ul#Clas/* a */sic\ Hoodie\ M",
        r"ul.Clas/**//**/sic\ Hoodie\ M",
    ],
    ids=["attribute", "id", "class"],
)
def test_a_heal_finds_no_assertion_target_by_a_claim_split_by_a_comment(
    base: Change, css: str
) -> None:
    assert heal(
        lambda d: d["targets"]["cart_items"].update(locators=[{"css": css}]),
        base=base,
    ) == [f"/targets/cart_items/locators/0/css: {SPELLS}"]


def test_a_heal_finds_no_assertion_target_by_a_string_its_pattern_matches() -> None:
    # a4's own pattern, Classic Hoodie.*\bM\b.
    locator = {"css": 'ul[aria-label="Classic Hoodie M"]'}
    assert heal(lambda d: d["targets"]["cart_items"].update(locators=[locator])) == [
        f"/targets/cart_items/locators/0/css: {SPELLS}"
    ]


def test_a_pattern_out_of_time_refuses_the_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shorter than the 2 s rule, which the runner's test_text_search measures.
    monkeypatch.setattr(text, "SEARCH_SECONDS", 0.5)

    def rebind(data: dict[str, Any]) -> None:
        data["targets"]["cart_items"]["locators"] = [{"testid": "a" * 40 + "b"}]

    # Exponential for re: each extra "a" doubles the work before the "b" fails it.
    catastrophic = "(a+)+$"
    assert heal(
        rebind, base=lambda d: d["assertions"][3].update(pattern=catastrophic)
    ) == [
        (
            "/targets/cart_items/locators/0/testid: a heal finds no assertion's "
            "target by a string where the search for its text check's pattern "
            "ran out of time"
        )
    ]
    # Under a4's own pattern the same search ends within the same bound, so
    # the pattern is what ran out of time, not the process starting.
    assert heal(rebind) == []


def test_a_pattern_search_that_fails_refuses_the_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A child that dies before it answers, as one out of memory would.
    monkeypatch.setattr(text, "SEARCH_CHILD", "raise SystemExit('out of memory')")
    locator = {"css": "ul.cart-lines"}
    assert heal(lambda d: d["targets"]["cart_items"].update(locators=[locator])) == [
        (
            "/targets/cart_items/locators/0/css: a heal finds no assertion's "
            "target by a string where the search for its text check's pattern "
            "failed"
        )
    ]


def test_a_pattern_search_of_no_strings_starts_no_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        pytest.fail("a search of no strings must not start a process")

    monkeypatch.setattr(subprocess, "run", unexpected)
    # A universal selector, which spells nothing.
    locator = {"css": "*"}
    assert heal(lambda d: d["targets"]["cart_items"].update(locators=[locator])) == []


# Structure, part of the claim, and the claim quoted in a comment, which
# Playwright drops; under a literal claim and under a pattern.
@pytest.mark.parametrize("base", [LITERAL, unchanged], ids=["literal", "pattern"])
@pytest.mark.parametrize(
    "locator",
    [
        {"css": "ul.cart-lines"},
        {"testid": "cart-lines"},
        {"css": "ul[aria-label*=Hoodie]"},
        {"css": 'ul.cart-lines /* "Classic Hoodie M" */'},
    ],
    ids=["css", "testid", "part", "comment"],
)
def test_a_heal_may_find_an_assertion_target_by_strings_without_its_claim(
    base: Change, locator: dict[str, Any]
) -> None:
    assert (
        heal(lambda d: d["targets"]["cart_items"].update(locators=[locator]), base=base)
        == []
    )


def test_a_heal_may_keep_a_reviewed_locator_that_spells_the_claim() -> None:
    def spelled(data: dict[str, Any]) -> None:
        LITERAL(data)
        data["targets"]["cart_items"]["locators"].insert(
            0, {"css": 'ul[aria-label="Classic Hoodie M"]'}
        )

    assert (
        heal(
            lambda d: d["targets"]["cart_items"]["locators"][1].update(testid="cart"),
            base=spelled,
        )
        == []
    )


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


def a_contrast_notice(data: dict[str, Any]) -> None:
    """Adds a notice that only a contrast_min check reads."""
    data["targets"]["notice"] = {
        "semantic": "the payment step's notice",
        "locators": [{"testid": "notice"}],
    }
    data["assertions"].append(
        {
            "id": "a7",
            "expect_index": 4,
            "check": "contrast_min",
            "target": "notice",
            "min_ratio": 4.5,
        }
    )


def test_a_heal_never_changes_a_contrast_min_ratio() -> None:
    assert heal(
        lambda d: d["assertions"][6].update(min_ratio=3), base=a_contrast_notice
    ) == ["/assertions: a heal changes nothing but target locators and inserted steps"]


@pytest.mark.parametrize(
    "locator",
    [{"label": "Notice"}, {"placeholder": "Notice"}],
    ids=["label", "placeholder"],
)
def test_a_heal_finds_no_contrast_min_target_by_a_label_or_placeholder(
    locator: dict[str, Any],
) -> None:
    assert heal(
        lambda d: d["targets"]["notice"].update(locators=[locator]),
        base=a_contrast_notice,
    ) == [
        (
            "/targets/notice/locators/0: a heal finds no assertion's target by a "
            "label or placeholder, or by a name when its text is checked"
        )
    ]


def ops(*changes: dict[str, Any]) -> list[PatchOp]:
    return [PatchOp.model_validate(change) for change in changes]


def rebinds_email_and_a_named_target(data: dict[str, Any]) -> None:
    data["targets"]["a/b~c"] = {
        "semantic": "a target whose name needs escaping",
        "locators": [{"css": "#b"}],
    }
    data["targets"]["email_input"]["locators"] = [
        {"css": "input", "scope": {"css": "#checkout"}}
    ]


def test_json_patch_replaces_each_rebound_targets_locators() -> None:
    def named(data: dict[str, Any]) -> None:
        data["targets"]["a/b~c"] = {
            "semantic": "a target whose name needs escaping",
            "locators": [{"css": "#a"}],
        }

    committed = script(named)
    final = script(rebinds_email_and_a_named_target)

    patch = json_patch(committed, final)

    assert patch == [
        {
            "op": "replace",
            "path": "/targets/email_input/locators",
            "value": [{"css": "input", "scope": {"css": "#checkout"}}],
        },
        {
            "op": "replace",
            "path": "/targets/a~1b~0c/locators",
            "value": [{"css": "#b"}],
        },
    ]
    document = committed.model_dump(mode="json", exclude_none=True)
    apply_patch(document, ops(*patch))
    assert document == final.model_dump(mode="json", exclude_none=True)


def test_json_patch_of_a_script_with_its_own_locators_is_empty() -> None:
    assert json_patch(script(unchanged), script(unchanged)) == []


def test_blob_sha_is_gits_for_the_bytes_as_they_are() -> None:
    # `printf 'hello\\n' | git hash-object --stdin`
    assert blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_proposal_id_is_ah_and_12_hex_of_its_content() -> None:
    patch: list[dict[str, Any]] = [
        {"op": "replace", "path": "/targets/toggle/locators", "value": [{"css": "#t"}]}
    ]

    # The first 12 hex of the sha256 of the sorted, compact JSON of
    # {"base": "abc123", "patch": ..., "spec_id": "order"}.
    assert proposal_id("order", "abc123", patch) == "ah_d2ef532da612"
    assert proposal_id("order", "abc124", patch) != "ah_d2ef532da612"
    assert proposal_id("other", "abc123", patch) != "ah_d2ef532da612"
    assert proposal_id("order", "abc123", []) != "ah_d2ef532da612"


def test_proposal_id_reads_a_values_keys_in_sorted_order() -> None:
    patch: list[dict[str, Any]] = [
        {
            "op": "replace",
            "path": "/targets/toggle/locators",
            "value": [{"role": "button", "name": "Save"}],
        }
    ]

    # `shasum -a 256` of {"base":"abc123","patch":[{"op":"replace","path":
    # "/targets/toggle/locators","value":[{"name":"Save","role":"button"}]}],
    # "spec_id":"order"}.
    assert proposal_id("order", "abc123", patch) == "ah_b314df31871b"


@pytest.mark.parametrize(
    ("path", "steps"),
    [
        ("/steps/0", ["new", "a", "b"]),
        ("/steps/1", ["a", "new", "b"]),
        ("/steps/2", ["a", "b", "new"]),
        ("/steps/-", ["a", "b", "new"]),
    ],
)
def test_apply_patch_inserts_a_step_by_its_position(
    path: str, steps: list[str]
) -> None:
    document = {"steps": ["a", "b"]}

    apply_patch(document, ops({"op": "add", "path": path, "value": "new"}))

    assert document == {"steps": steps}


def test_apply_patch_replaces_a_member_or_an_item_where_it_stands() -> None:
    document: dict[str, Any] = {"first": 1, "steps": ["a", "b"], "last": 2}

    apply_patch(
        document,
        ops(
            {"op": "replace", "path": "/first", "value": 3},
            {"op": "replace", "path": "/steps/1", "value": "c"},
        ),
    )

    assert document == {"first": 3, "steps": ["a", "c"], "last": 2}
    assert list(document) == ["first", "steps", "last"]


def test_apply_patch_adds_a_member_after_the_others() -> None:
    document = {"targets": {"b": 1, "a": 2}}

    apply_patch(document, ops({"op": "add", "path": "/targets/c", "value": 3}))

    assert list(document["targets"].items()) == [("b", 1), ("a", 2), ("c", 3)]


def test_apply_patch_reads_each_path_against_what_the_last_change_left() -> None:
    document = {"steps": ["a", "b"]}

    apply_patch(
        document,
        ops(
            {"op": "add", "path": "/steps/0", "value": "new"},
            {"op": "replace", "path": "/steps/1", "value": "c"},
        ),
    )

    assert document == {"steps": ["new", "c", "b"]}


def test_apply_patch_reads_a_pointer_as_rfc_6901() -> None:
    document: dict[str, Any] = {"targets": {"a/b": 1, "~1": 2, "c~/d": 3}}

    apply_patch(
        document,
        ops(
            {"op": "replace", "path": "/targets/a~1b", "value": 4},
            {"op": "replace", "path": "/targets/~01", "value": 5},
            {"op": "replace", "path": "/targets/c~0~1d", "value": 6},
        ),
    )

    assert document == {"targets": {"a/b": 4, "~1": 5, "c~/d": 6}}


@pytest.mark.parametrize(
    ("op", "path"),
    [
        ("replace", ""),
        ("add", ""),
        ("replace", "targets/a"),
        ("add", "xtargets/b"),
        ("replace", "/missing"),
        ("replace", "/missing/deeper"),
        ("add", "/missing/deeper"),
        ("replace", "/targets/a/deeper"),
        ("add", "/targets/a/deeper"),
        ("replace", "/targets/~2"),
        ("add", "/targets/~2"),
        ("replace", "/targets/~"),
        ("add", "/targets/~"),
        ("replace", "/steps/-1"),
        ("add", "/steps/-1"),
        ("add", "/steps/+1"),
        ("add", "/steps/01"),
        ("add", "/steps/1_0"),
        ("add", "/steps/٣"),
        ("add", "/steps/1 "),
        ("add", "/steps/one"),
        ("add", "/steps/3"),
        ("replace", "/steps/2"),
        ("replace", "/steps/2/deeper"),
        ("replace", "/steps/-"),
        ("add", "/steps/-/deeper"),
        ("add", f"/steps/{'9' * 5000}"),
    ],
)
def test_apply_patch_refuses_a_path_it_cant_follow(op: str, path: str) -> None:
    document = {"targets": {"a": 1}, "steps": ["a", "b"]}

    with pytest.raises(SpecError) as refused:
        apply_patch(document, ops({"op": op, "path": path, "value": "new"}))

    assert refused.value.problems == (f"{path}: a patch can't follow this path",)
    assert document == {"targets": {"a": 1}, "steps": ["a", "b"]}


CLAIM = TextInTarget(
    id="a1", expect_index=0, check="text_in_target", target="t", text="ships on Monday"
)
PATTERN = TextInTarget(
    id="a2", expect_index=0, check="text_in_target", target="t", pattern="Monday"
)


@pytest.mark.parametrize(
    ("locator", "spelled"),
    [
        (ByTestId(testid="ships-on-monday"), False),
        (ByCss(css='[title="ships on Monday"]'), True),
        (ByTestId(testid="ships on Monday"), True),
        (ByCss(css="p", scope=ByTestId(testid="ships on Monday")), True),
        (ByTestId(testid="summary"), False),
    ],
)
def test_a_locator_that_spells_a_literal_claim_is_found_at_any_level(
    locator: ByTestId | ByCss, spelled: bool
) -> None:
    assert literal_claim_spelled(locator, [CLAIM]) is spelled


def test_a_pattern_claim_is_left_to_the_validators_bounded_search() -> None:
    assert literal_claim_spelled(ByTestId(testid="Monday"), [PATTERN]) is False
