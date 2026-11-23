"""The recorder's removed-page heal case (#338; ADR-0024's #338 amendment): the removed
drift page's `aqa run` recorded in real Chromium from a local stand-in for the
provider, with a fake key, within its round's own bound and its run's, and promoted
only when its run ends as the case's rule allows, which says in fixed words why it
doesn't. It holds one priced response per line of its run's own cost lines. Role,
mode and allowance come from the case's row in the recorder's table. No test here
reaches a provider."""

import json
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.model_costs import Usage, cost_record
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import vendored

from packages.runner.tests import record_cassettes
from packages.runner.tests.drift_fixtures import SHIPPING, page
from packages.runner.tests.heal_fixtures import (
    MOVED,
    ORDER_PAGE,
    RECORDING_CONFIG,
    heal_removed_disallowed,
)
from packages.runner.tests.provider_stand_in import Start, answer, standing_in
from packages.runner.tests.record_cassettes import (
    HEAL_REMOVED_ROUND_USD,
    answers_expected,
    record,
)
from packages.runner.tests.test_record_heal_cassettes import picking, receipt
from packages.runner.tests.test_record_heal_injection_cassettes import (
    NO_POINT,
    NO_TOOL,
    never_promoted,
    promoted,
    replay_json,
    reserved,
)

pytestmark = pytest.mark.usefixtures("reset_tracing")

NAME = "heal_removed"
# The removed page with its footer's button writing to the app, a planted fault.
WRITING = page("removed").replace(
    "<button>Contact us",
    "<button onclick=\"fetch('/write/confirm', {method: 'POST'})\">Contact us",
)
# A tree turn's reply calling no tool, billed USD 0.044 at the healer's list prices,
# within its request's reservation: 2,000 input tokens and 4,000 output tokens.
COSTLY_NO_TOOL = answer(
    1,
    [{"type": "text", "text": "nothing fits"}],
    "end_turn",
    input_tokens=2000,
    output_tokens=4000,
)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Start]:
    with standing_in(monkeypatch) as start:
        yield start


@pytest.fixture
def library(tmp_path: Path) -> Path:
    path = tmp_path / "library"
    path.mkdir()
    return path


def test_the_removed_case_says_what_it_is_recorded_as() -> None:
    roles = resolve_roles(ProjectConfig.model_validate({}), vendored())
    ok = cost_record(
        role="healer",
        mode="heal",
        model=roles["healer"].model,
        usage=Usage(input_tokens=9, cached_input_tokens=0, output_tokens=3),
        latency_ms=1,
        status="ok",
    )
    case = record_cassettes.CASES[NAME]

    assert (case.role, case.mode, case.whole_run, case.round_usd) == (
        "healer",
        "heal",
        True,
        HEAL_REMOVED_ROUND_USD,
    )
    assert NAME not in record_cassettes.HEALED
    assert NAME not in record_cassettes.HEAL_INJECTED
    assert answers_expected(NAME, (ok, ok)) == 2
    assert answers_expected("heal_moved", (ok, ok)) == 1


def test_a_tree_turn_and_a_vision_turn_that_find_nothing_are_promoted_inconclusive(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    attempt = tmp_path / "heal-removed" / "001"
    stand_in = provider([NO_TOOL, NO_POINT])

    record(NAME, attempt, library=library)

    lines = promoted(NAME, attempt, library)
    assert lines == [("healer", "heal"), ("vision_fallback", "heal")]
    kept = receipt(attempt)
    assert all(response["request_id"] for response in kept["responses"])
    assert kept["explore"] == {
        "exit_code": 3,
        "outcome": "inconclusive",
        "published": None,
    }
    assert replay_json(attempt)["heal_proposal"] is None
    assert (kept["spend"]["run_usd"], kept["spend"]["round_left_usd"]) == (
        "0.3",
        str(HEAL_REMOVED_ROUND_USD),
    )
    # The vision request, the larger by its image, was admitted within the run's
    # bound and the round's.
    tree, vision = reserved(NAME, attempt, stand_in.sent)
    assert tree < vision <= min(Decimal("0.3"), HEAL_REMOVED_ROUND_USD)


def test_a_rebind_of_the_footers_button_is_promoted_expectation_violated(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    attempt = tmp_path / "heal-removed" / "001"
    stand_in = provider([picking("Contact us")])

    record(NAME, attempt, library=library)

    assert promoted(NAME, attempt, library) == [("healer", "heal")]
    kept = receipt(attempt)
    assert (kept["explore"]["exit_code"], kept["explore"]["outcome"]) == (
        1,
        "expectation_violated",
    )
    document = replay_json(attempt)
    assert document["heal_proposal"] is None
    findings = [each["finding"] for each in document["verdicts"] if each["finding"]]
    # Both checks of the summary failed after the rebind, and each says so.
    assert [finding["rebound_steps"] for finding in findings] == [[1], [1]]
    reserved(NAME, attempt, stand_in.sent)


def test_the_moved_page_under_the_case_is_never_promoted(
    provider: Start,
    library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = record_cassettes.HEAL_REMOVED[NAME]
    monkeypatch.setitem(record_cassettes.HEAL_REMOVED, NAME, replace(case, html=MOVED))
    attempt = tmp_path / "heal-removed" / "001"
    stand_in = provider([picking("Show details")])

    with pytest.raises(ValueError, match="disallowed"):
        record(NAME, attempt, library=library)

    kept = never_promoted(NAME, attempt, library)
    assert kept["explore"]["outcome"] == "drift_consistent"
    assert kept["disallowed"] == (
        "it ended drift_consistent, which this case doesn't allow"
    )
    reserved(NAME, attempt, stand_in.sent)


def test_a_request_past_the_order_page_is_never_promoted(
    provider: Start,
    library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = record_cassettes.HEAL_REMOVED[NAME]
    monkeypatch.setitem(
        record_cassettes.HEAL_REMOVED, NAME, replace(case, html=WRITING)
    )
    attempt = tmp_path / "heal-removed" / "001"
    stand_in = provider([picking("Contact us")])

    with pytest.raises(ValueError, match="disallowed"):
        record(NAME, attempt, library=library)

    kept = never_promoted(NAME, attempt, library)
    assert kept["disallowed"] == "the app served a request other than the order page"
    reserved(NAME, attempt, stand_in.sent)


def test_a_removed_round_past_its_limit_refuses_the_request(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    earlier = tmp_path / "heal-removed" / "000"
    earlier.mkdir(parents=True)
    spent = {"spend": {"spent_usd": str(HEAL_REMOVED_ROUND_USD), "overrun": False}}
    (earlier / "receipt.json").write_text(json.dumps(spent))
    attempt = tmp_path / "heal-removed" / "001"
    stand_in = provider([NO_TOOL, NO_POINT])

    with pytest.raises(ValueError, match="spend_refused"):
        record(NAME, attempt, library=library)

    assert stand_in.sent == []
    assert not (library / f"{NAME}.yaml").exists()
    kept = receipt(attempt)
    assert (kept["outcome"], kept["spend"]["refused"]) == ("spend_refused", True)


def test_a_vision_request_past_the_runs_bound_is_refused_unsent(
    provider: Start,
    library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The heal cases' config, whose USD 0.10 run bound this page's vision request
    # alone fits under (ADR-0024's #338 amendment measures it), so the tree turn
    # spends the rest first.
    case = record_cassettes.HEAL_REMOVED[NAME]
    monkeypatch.setitem(
        record_cassettes.HEAL_REMOVED, NAME, replace(case, config=RECORDING_CONFIG)
    )
    attempt = tmp_path / "heal-removed" / "001"
    stand_in = provider([COSTLY_NO_TOOL, NO_POINT])

    with pytest.raises(ValueError, match="spend_refused"):
        record(NAME, attempt, library=library)

    assert len(stand_in.sent) == 1
    assert not (library / f"{NAME}.yaml").exists()
    kept = receipt(attempt)
    assert (kept["outcome"], kept["spend"]["refused"]) == ("spend_refused", True)
    assert kept["spend"]["run_usd"] == "0.1"
    assert Decimal(kept["spend"]["spent_usd"]) == Decimal("0.044")
    reserved(NAME, attempt, stand_in.sent)


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
