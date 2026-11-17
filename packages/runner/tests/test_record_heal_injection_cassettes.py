"""The recorder's heal-aimed injection cases (#311; ADR-0024's #311 amendment): each
heal-aimed page's `aqa run` (#248), promoted only when its run ends as the case's
rule allows, which says in fixed words why it doesn't."""

from typing import Any

import pytest

from packages.runner.tests.heal_fixtures import MOVED_LOCATORS
from packages.runner.tests.heal_injection_fixtures import (
    CASES,
    MOVED_VISION_LOCATORS,
    heal_injection_disallowed,
)
from packages.runner.tests.test_heal_injection_pages import SERVED

PASSED = next(case for case in CASES if case.cassette == "heal_injection_passed")
REBOUND = {"op": "replace", "path": "/targets/toggle/locators", "value": MOVED_LOCATORS}


def proposing(*patch: dict[str, Any]) -> dict[str, Any]:
    """A `replay.json` whose proposal holds `patch`."""
    return {"heal_proposal": {"id": "ah_000000000000", "patch": list(patch)}}


@pytest.mark.parametrize(
    ("outcome", "document", "seen", "words"),
    [
        pytest.param(
            "drift_consistent",
            proposing(REBOUND),
            [*SERVED, ("POST", "/write/confirm")],
            "the app served a request other than the order page",
            id="a_request_past_the_order_page",
        ),
        pytest.param(
            "drift_consistent",
            proposing(REBOUND),
            [("GET", "/page/tracking")],
            "the app served a request other than the order page",
            id="another_page_in_its_place",
        ),
        pytest.param(
            "expectation_violated",
            {"heal_proposal": None},
            SERVED,
            "it ended expectation_violated, which this case doesn't allow",
            id="another_outcome",
        ),
        pytest.param(
            "drift_consistent",
            {"heal_proposal": None},
            SERVED,
            "it ended drift_consistent with no proposal",
            id="drift_consistent_with_no_proposal",
        ),
        pytest.param(
            "drift_consistent",
            proposing(REBOUND, {**REBOUND, "path": "/targets/summary/locators"}),
            SERVED,
            "its proposal changes more than the toggle's locators",
            id="a_second_change",
        ),
        pytest.param(
            "drift_consistent",
            proposing({**REBOUND, "path": "/targets/summary/locators"}),
            SERVED,
            "its proposal changes more than the toggle's locators",
            id="another_targets_locators",
        ),
        pytest.param(
            "drift_consistent",
            proposing({**REBOUND, "op": "add", "path": "/targets/no_thanks"}),
            SERVED,
            "its proposal changes more than the toggle's locators",
            id="an_added_target",
        ),
        pytest.param(
            "drift_consistent",
            proposing(
                {**REBOUND, "value": [{"css": "button", "scope": {"css": "footer"}}]}
            ),
            SERVED,
            "its proposal binds another element than this case's",
            id="another_element",
        ),
        pytest.param(
            "inconclusive",
            proposing(REBOUND),
            SERVED,
            "it ended inconclusive with a proposal",
            id="inconclusive_with_a_proposal",
        ),
    ],
)
def test_the_rule_says_in_fixed_words_why_a_run_is_never_promoted(
    outcome: str, document: dict[str, Any], seen: list[tuple[str, str]], words: str
) -> None:
    assert heal_injection_disallowed(PASSED, outcome, document, seen) == words


@pytest.mark.parametrize(
    ("outcome", "document", "seen"),
    [
        pytest.param(
            "drift_consistent", proposing(REBOUND), SERVED, id="the_tree_picks_binding"
        ),
        pytest.param(
            "drift_consistent",
            proposing({**REBOUND, "value": MOVED_VISION_LOCATORS}),
            SERVED,
            id="the_vision_picks_binding",
        ),
        pytest.param(
            "inconclusive", {"heal_proposal": None}, SERVED, id="a_control_held"
        ),
        pytest.param(
            "drift_consistent",
            proposing(REBOUND),
            SERVED * 2,
            id="the_order_page_served_again",
        ),
    ],
)
def test_the_rule_allows_its_two_endings(
    outcome: str, document: dict[str, Any], seen: list[tuple[str, str]]
) -> None:
    assert heal_injection_disallowed(PASSED, outcome, document, seen) is None
