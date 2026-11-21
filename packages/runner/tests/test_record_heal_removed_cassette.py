"""The recorder's removed-page heal case (#338; ADR-0024's #338 amendment): the removed
drift page's `aqa run`, and the rule that promotes its recording, which says in fixed
words why it doesn't. No test here reaches a provider."""

from typing import Any

import pytest

from packages.runner.tests.drift_fixtures import SHIPPING
from packages.runner.tests.heal_fixtures import ORDER_PAGE, heal_removed_disallowed

SERVED = [ORDER_PAGE]
# A finding of a run whose checks failed after the heal rebound step 1, the toggle's
# click, and a proposal, which no ending this case promotes carries.
FINDING = {
    "category": "functional",
    "title": SHIPPING,
    "repro_steps": [0, 1],
    "rebound_steps": [1],
}
PROPOSAL = {
    "id": "ah_000000000000",
    "patch": [
        {
            "op": "replace",
            "path": "/targets/toggle/locators",
            "value": [{"css": "button", "scope": {"css": "footer"}}],
        }
    ],
}


def written(
    *findings: dict[str, Any] | None, proposal: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A `replay.json` whose step and assertions have `findings`, with `proposal`."""
    return {
        "heal_proposal": proposal,
        "verdicts": [{"finding": each} for each in findings],
    }


@pytest.mark.parametrize(
    ("outcome", "document", "seen", "words"),
    [
        pytest.param(
            "expectation_violated",
            written(None, FINDING),
            [*SERVED, ("POST", "/write/confirm")],
            "the app served a request other than the order page",
            id="a_post_after_the_order_page",
        ),
        pytest.param(
            "inconclusive",
            written(None, None),
            [("GET", "/page/tracking")],
            "the app served a request other than the order page",
            id="another_page_in_its_place",
        ),
        pytest.param(
            "drift_consistent",
            written(None, None, proposal=PROPOSAL),
            SERVED,
            "it ended drift_consistent, which this case doesn't allow",
            id="drift_consistent_with_a_proposal",
        ),
        pytest.param(
            "passed",
            written(None, None),
            SERVED,
            "it ended passed, which this case doesn't allow",
            id="passed",
        ),
        pytest.param(
            "inconclusive",
            written(None, None, proposal=PROPOSAL),
            SERVED,
            "it ended inconclusive with a proposal",
            id="inconclusive_with_a_proposal",
        ),
        pytest.param(
            "expectation_violated",
            written(None, FINDING, proposal=PROPOSAL),
            SERVED,
            "it ended expectation_violated with a proposal",
            id="expectation_violated_with_a_proposal",
        ),
        pytest.param(
            "expectation_violated",
            written(None, {**FINDING, "rebound_steps": []}),
            SERVED,
            "it ended expectation_violated with no finding that lists the rebound step",
            id="findings_that_list_no_rebound_step",
        ),
        pytest.param(
            "expectation_violated",
            written(None, None),
            SERVED,
            "it ended expectation_violated with no finding that lists the rebound step",
            id="no_finding",
        ),
    ],
)
def test_the_rule_says_in_fixed_words_why_a_run_is_never_promoted(
    outcome: str, document: dict[str, Any], seen: list[tuple[str, str]], words: str
) -> None:
    assert heal_removed_disallowed(outcome, document, seen) == words


@pytest.mark.parametrize(
    ("outcome", "document", "seen"),
    [
        pytest.param(
            "inconclusive", written(None, None), SERVED, id="inconclusive_found_nothing"
        ),
        pytest.param(
            "expectation_violated",
            written(None, FINDING, {**FINDING, "category": "visual"}),
            SERVED,
            id="violated_after_a_rebind",
        ),
        pytest.param(
            "expectation_violated",
            written(None, FINDING),
            SERVED * 2,
            id="the_order_page_served_again",
        ),
    ],
)
def test_the_rule_allows_its_two_endings(
    outcome: str, document: dict[str, Any], seen: list[tuple[str, str]]
) -> None:
    assert heal_removed_disallowed(outcome, document, seen) is None
