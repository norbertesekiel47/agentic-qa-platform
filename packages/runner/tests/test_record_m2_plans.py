"""The recorder's `plan_m2-checks` case against a local stand-in for the
provider, with a fake key (#327; ADR-0024's #327 amendment): the oracle's
plan is promoted, and any other ending is `disallowed` with the oracle's
fixed words, never promoted. No test here reaches a provider."""

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from packages.runner.tests.m2_plan_fixtures import CASE, SPEC_ID
from packages.runner.tests.provider_stand_in import KEY, Start, planned, standing_in
from packages.runner.tests.record_cassettes import record

pytestmark = pytest.mark.usefixtures("reset_tracing")

PHOTO = "the product photo on the order page"
LOGO = "the store logo in the page header"
# The plan the oracle wants, as the model might word it.
ORACLE: dict[str, Any] = {
    "expectations": [
        {
            "expect_index": 0,
            "subject": "the shipping notice",
            "claim": "its text has a contrast of at least 4.5 to 1",
            "checks": [
                {
                    "check": "contrast_min",
                    "target_meaning": "the shipping notice on the order page",
                    "min_ratio": 4.5,
                }
            ],
        },
        {
            "expect_index": 1,
            "subject": "the product photo",
            "claim": "shows a pair of red running shoes",
            "checks": [{"check": "model_verify", "target_meaning": PHOTO}],
        },
        {
            "expect_index": 2,
            "subject": "the store logo",
            "claim": "looks unchanged from its reviewed baseline",
            "checks": [{"check": "pixel_diff", "target_meaning": LOGO}],
        },
    ],
    "requires": [],
}


def changed(index: int, **fields: Any) -> dict[str, Any]:
    """ORACLE, with entry `index`'s `fields` replaced, `checks` left out
    when it is unsupported."""
    plan = copy.deepcopy(ORACLE)
    entry = plan["expectations"][index]
    if "unsupported" in fields:
        del entry["checks"]
    entry.update(fields)
    return plan


def with_extra(index: int, meaning: str) -> dict[str, Any]:
    """ORACLE, with a `visible_unoccluded` check beside entry `index`'s."""
    checks = ORACLE["expectations"][index]["checks"]
    extra = {"check": "visible_unoccluded", "target_meaning": meaning}
    return changed(index, checks=[*checks, extra])


UNSTATED = changed(
    0,
    checks=[
        {
            "check": "contrast_min",
            "target_meaning": "the shipping notice on the order page",
            "min_ratio": 3,
        }
    ],
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


def receipt(attempt: Path) -> dict[str, Any]:
    kept: dict[str, Any] = json.loads((attempt / "receipt.json").read_text())
    return kept


def interactions(cassette: Path) -> list[dict[str, Any]]:
    held: list[dict[str, Any]] = yaml.safe_load(cassette.read_text())["interactions"]
    return held


def test_the_oracles_plan_is_promoted(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    stand_in = provider([planned(0, ORACLE)])
    attempt = tmp_path / "round" / "001"

    [cost] = record(CASE, attempt, library=library)

    assert (cost.input_tokens, cost.output_tokens, cost.status) == (9, 3, "ok")
    assert receipt(attempt)["outcome"] == "accepted"
    assert receipt(attempt)["disallowed"] is None
    candidate = (attempt / f"{CASE}.yaml").read_text()
    assert (library / f"{CASE}.yaml").read_text() == candidate
    assert KEY not in candidate
    [sent] = stand_in.sent
    spec = json.loads(json.loads(sent)["messages"][0]["content"])
    assert spec["id"] == SPEC_ID
    assert [each.get("visual") for each in spec["expect"]] == [None, "model", None]
    assert spec["preconditions"] == {"start_url": "/page/order"}
    intent = json.loads((attempt / "intent.json").read_text())
    assert sorted(intent["inputs"]) == ["config.yaml", f"{SPEC_ID}.spec.md"]


# Each plan the oracle refuses, and the fixed words its receipt says why in.
REFUSED = {
    "the contrast expectation planned unsupported": (
        changed(0, unsupported={"reason": "the spec names no colours"}),
        f"{SPEC_ID}/0: planned unsupported",
        1,
    ),
    # Also a misfit, so asked for once more, and the same plan written again.
    "the model_verify missing": (
        changed(1, checks=[{"check": "visible_unoccluded", "target_meaning": PHOTO}]),
        (
            f"{SPEC_ID}/1: establishing checks differ; {SPEC_ID}: the model gave "
            "no usable plan"
        ),
        2,
    ),
    "an extra check on the contrast expectation": (
        with_extra(0, "the shipping notice on the order page"),
        f"{SPEC_ID}/0: establishing checks differ",
        1,
    ),
    "an extra check on the visual: model expectation": (
        with_extra(1, PHOTO),
        f"{SPEC_ID}/1: establishing checks differ",
        1,
    ),
    "an extra check on the looks-unchanged expectation": (
        with_extra(2, LOGO),
        f"{SPEC_ID}/2: establishing checks differ",
        1,
    ),
    "the pixel_diff missing": (
        changed(2, checks=[{"check": "visible_unoccluded", "target_meaning": LOGO}]),
        f"{SPEC_ID}/2: establishing checks differ",
        1,
    ),
}


@pytest.mark.parametrize("variant", REFUSED)
def test_a_plan_the_oracle_refuses_is_disallowed_in_fixed_words(
    provider: Start, library: Path, tmp_path: Path, variant: str
) -> None:
    plan, why, answers = REFUSED[variant]
    stand_in = provider([planned(0, plan)])
    attempt = tmp_path / "round" / "001"

    with pytest.raises(ValueError, match="disallowed"):
        record(CASE, attempt, library=library)

    kept = receipt(attempt)
    assert (kept["outcome"], kept["disallowed"]) == ("disallowed", why)
    assert len(kept["cost_records"]) == answers == len(stand_in.sent)
    assert list(library.iterdir()) == []


def test_a_misfit_gets_its_one_retry_and_the_oracles_second_plan_is_promoted(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    # A ratio the expectation doesn't state, then the one it does.
    stand_in = provider([planned(0, UNSTATED), planned(1, ORACLE)])
    attempt = tmp_path / "round" / "001"

    costs = record(CASE, attempt, library=library)

    assert [cost.input_tokens for cost in costs] == [9, 15]
    assert receipt(attempt)["outcome"] == "accepted"
    [_, second] = interactions(library / f"{CASE}.yaml")
    correction = json.loads(second["request"]["body"])["messages"][-1]["content"]
    assert (
        "- expect[0]: a contrast_min min_ratio of 3 is not a number the "
        "expectation states"
    ) in correction
    assert len(stand_in.sent) == 2


def test_a_misfit_whose_second_plan_the_oracle_refuses_is_disallowed(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    pixels_missing, why, _ = REFUSED["the pixel_diff missing"]
    provider([planned(0, UNSTATED), planned(1, pixels_missing)])
    attempt = tmp_path / "round" / "001"

    with pytest.raises(ValueError, match="disallowed"):
        record(CASE, attempt, library=library)

    kept = receipt(attempt)
    assert (kept["outcome"], kept["disallowed"]) == ("disallowed", why)
    assert len(kept["cost_records"]) == 2
    assert list(library.iterdir()) == []
