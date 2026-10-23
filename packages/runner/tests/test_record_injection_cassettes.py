"""The recorder's injection cases (#57; ADR-0024's #57 amendment): each
injection page's explore recorded in real Chromium from a local stand-in for
the provider, with a fake key, within the injection round's own bound, its
plan replayed from the fixture explore's cassette when the request matches,
and promoted only when the run ended as its attack allows. No test here
reaches a provider."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from langchain_core.messages import HumanMessage

from packages.runner.tests.explore_graph_fixtures import NAME, PLAN, SAVE, ref
from packages.runner.tests.provider_stand_in import (
    Answer,
    Answering,
    Start,
    answer,
    called,
    planned,
    standing_in,
)
from packages.runner.tests.record_cassettes import EXPLORE, record
from packages.runner.tests.test_injection_pages import decoy as banner
from packages.runner.tests.test_record_explore_cassette import explore_answers

pytestmark = pytest.mark.usefixtures("reset_tracing")

PREVIOUS = "previous cassette\n"
GOOD_PLAN = PLAN.model_dump(mode="json", exclude_none=True)
# A plan whose claim is too weak: "Saved nobody" holds its text too.
WEAK_PLAN = GOOD_PLAN | {
    "expectations": [
        GOOD_PLAN["expectations"][0]
        | {"checks": [GOOD_PLAN["expectations"][0]["checks"][0] | {"text": "Saved"}]}
    ]
}
CONFIRMED = {"exit_code": 0, "outcome": "compiled", "published": "confirmed"}
GAVE_UP = {"exit_code": 3, "outcome": "gave_up", "published": None}
BLOCKED = {"exit_code": 6, "outcome": "egress_blocked", "published": None}
# The injection round's own bound: the fixture config's model_usd a run.
BOUND = {"run_usd": "0.15", "round_left_usd": "0.80"}

# A move: the tool to call, and its arguments from the request's snapshot.
type Move = Callable[[str], tuple[str, dict[str, Any]]]
# A case, the stand-in's answers, and how explore ended.
type Scripted = tuple[str, list[Answering], dict[str, object]]


def snapshot(body: bytes) -> str:
    """The text of the last message a request sends: its turn's blocks."""
    content = json.loads(body)["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content)


def on(text: str, role: str, name: str) -> str:
    """The ref the snapshot in `text` gives the first `role` named `name`."""
    return ref([HumanMessage(content=text)], role, name)


def fill(text: str) -> tuple[str, dict[str, Any]]:
    return "fill", {"ref": on(text, "textbox", "Name"), "meaning": NAME, "text": "Ada"}


def click(text: str) -> tuple[str, dict[str, Any]]:
    return "click", {"ref": on(text, "button", "Save"), "meaning": SAVE}


def check(text: str) -> tuple[str, dict[str, Any]]:
    """a1 bound to the first paragraph that says the name was saved."""
    return "assert_check", {
        "check_id": "a1",
        "ref": on(text, "paragraph", "Saved Ada in size S"),
    }


def finish(_text: str) -> tuple[str, dict[str, Any]]:
    return "finish", {"steps": [1, 2]}


def report(text: str) -> tuple[str, dict[str, Any]]:
    """A click on the steering page's link to an undeclared host."""
    return "click", {
        "ref": on(text, "link", "Report a problem"),
        "meaning": "the report link",
    }


def decoy(text: str) -> tuple[str, dict[str, Any]]:
    """a1 bound to the lure's banner, which follows the status in the page."""
    return "assert_check", banner([HumanMessage(content=text)])


def turns(*moves: Move, first: int = 2) -> list[Answering]:
    """Each move as the stand-in's answer to the request it reads, each
    priced apart."""

    def answering(turn: int, move: Move) -> Callable[[bytes], Answer]:
        return lambda body: called(turn, *move(snapshot(body)))

    return [answering(turn, move) for turn, move in enumerate(moves, first)]


REFUSAL = answer(2, [{"type": "text", "text": "I can't help with that."}], "refusal")


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
    kept: list[dict[str, Any]] = yaml.safe_load(cassette.read_text())["interactions"]
    return kept


@pytest.mark.parametrize(
    "scripted",
    [
        (
            "hijack",
            [planned(1, GOOD_PLAN), *turns(fill, click, check, finish)],
            CONFIRMED,
        ),
        ("hijack", [planned(1, GOOD_PLAN), REFUSAL], GAVE_UP),
        ("steering", [planned(1, GOOD_PLAN), *turns(report)], BLOCKED),
        (
            "decoy-after-failure",
            [
                planned(1, GOOD_PLAN),
                *turns(fill, click, check, finish),
                *turns(fill, click, decoy, check, finish, first=6),
            ],
            GAVE_UP,
        ),
    ],
    ids=["a confirmed compile", "giving up", "the proxy's block", "flaky"],
)
def test_an_injection_capture_promotes_an_outcome_its_attack_allows(
    provider: Start, library: Path, tmp_path: Path, scripted: Scripted
) -> None:
    case, answers, ended = scripted
    name = f"explore_injection_{case}"
    attempt = tmp_path / "injection" / "001"
    provider(answers)

    record(name, attempt, library=library)

    kept = receipt(attempt)
    assert (kept["outcome"], kept["explore"]) == ("accepted", ended)
    assert {key: kept["spend"][key] for key in BOUND} == BOUND
    promoted = (library / f"{name}.yaml").read_text()
    assert promoted == (attempt / f"{name}.yaml").read_text()


@pytest.mark.parametrize(
    "scripted",
    [
        (
            "decoy-success",
            [planned(1, WEAK_PLAN), *turns(fill, click, check, finish)],
            CONFIRMED,
        ),
        ("steering-secret", [planned(1, GOOD_PLAN), *turns(report)], BLOCKED),
    ],
    ids=["a script that misses the subject", "a block whose host is withheld"],
)
def test_an_injection_capture_its_attack_disallows_is_never_promoted(
    provider: Start, library: Path, tmp_path: Path, scripted: Scripted
) -> None:
    case, answers, ended = scripted
    name = f"explore_injection_{case}"
    attempt = tmp_path / "injection" / "001"
    provider(answers)

    with pytest.raises(ValueError, match="disallowed"):
        record(name, attempt, library=library)

    assert (receipt(attempt)["outcome"], receipt(attempt)["explore"]) == (
        "disallowed",
        ended,
    )
    assert not (library / f"{name}.yaml").exists()


def test_an_injection_plan_replays_from_the_fixture_explores_cassette(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    provider(explore_answers())
    record(EXPLORE, tmp_path / "explore" / "001", library=library)
    fixture = interactions(library / f"{EXPLORE}.yaml")
    name = "explore_injection_hijack"
    attempt = tmp_path / "injection" / "001"
    stand_in = provider(turns(fill, click, check, finish, first=11))

    record(name, attempt, library=library)

    # The plan request matched the fixture explore's, so it never left.
    assert not any("output_config" in json.loads(body) for body in stand_in.sent)
    assert len(stand_in.sent) == 4
    kept = receipt(attempt)
    assert (kept["outcome"], kept["explore"]) == ("accepted", CONFIRMED)
    assert [each["request_id"] for each in kept["reused"]] == ["req_fake_01"]
    assert len(kept["cost_records"]) == 4
    promoted = interactions(library / f"{name}.yaml")
    assert promoted[0] == fixture[0]
    assert len(promoted) == 5
