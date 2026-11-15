"""Each badge page's `aqa replay --mode verified` replayed from its hand-written
cassette (ADR-0024's #256 amendment): in process with the real adapter, in real
Chromium against the verified pages, and what the request held. The responses
are written by hand (TESTING.md §4's by-design kind); the live recording is a
follow-up the maintainer runs. No test here reaches a provider."""

import base64
import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import typer.testing
from aqa_cli.main import app as aqa
from aqa_runner.verifier import VERIFY_INSTRUCTIONS

from packages.runner.tests.conftest import Recording
from packages.runner.tests.verified_fixtures import (
    CENTRE,
    EXPECTATION,
    GREEN,
    NAME,
    RED,
    badge_spec,
    page,
    pixel,
    serving,
)

pytestmark = pytest.mark.usefixtures("reset_tracing")

Cassette = Callable[..., AbstractContextManager[Recording]]
FOLDER = "verified/assertion-0"
SHOT = f"{FOLDER}/screenshot.png"


@dataclass(frozen=True)
class Case:
    """A badge page, its cassette, the colour it draws its badge in, and how
    the run ends: its exit code, outcome and the badge's verdict."""

    page: str
    cassette: str
    colour: tuple[int, int, int]
    exit_code: int
    outcome: str
    verdict: str


PASS = Case("green", "verified_pass", GREEN, 0, "passed", "pass")
VIOLATED = Case(
    "red", "verified_violated", RED, 1, "expectation_violated", "expectation_violated"
)
NO_EVIDENCE = Case(
    "green", "verified_no_evidence", GREEN, 3, "inconclusive", "inconclusive"
)


@dataclass(frozen=True)
class Replayed:
    """What a verified replay left: the command's result, the run's record and
    `replay.json`, and what its cassette saw."""

    result: typer.testing.Result
    record: Path
    document: dict[str, Any]
    played: Recording

    @property
    def badge(self) -> dict[str, Any]:
        [found] = [v for v in self.document["verdicts"] if v.get("assertion") == "a1"]
        assert isinstance(found, dict)
        return found


def replayed(cassette: Cassette, tmp_path: Path, case: Case) -> Replayed:
    spec = badge_spec(tmp_path / "qa")
    with (
        serving(page(case.page)) as app,
        cassette(case.cassette, replay_only=True) as played,
    ):
        result = typer.testing.CliRunner().invoke(
            aqa, ["replay", "--mode", "verified", str(spec), "--url", app.origin]
        )
    [record] = (spec.parent / ".aqa/runs").iterdir()
    document = json.loads((record / "replay.json").read_text())
    return Replayed(result, record, document, played)


def badge_ref(record: Path) -> str:
    """The ref the verified check's snapshot gives the badge."""
    snapshot = (record / FOLDER / "a11y_snapshot.yaml").read_text()
    found = re.search(rf'- img "{NAME}" \[ref=(e[0-9]+)\]', snapshot)
    assert found is not None, snapshot
    return found[1]


@pytest.mark.parametrize(
    "case", [PASS, VIOLATED, NO_EVIDENCE], ids=lambda c: c.cassette
)
def test_each_verified_cassette_replays_offline_to_its_outcome(
    cassette: Cassette, tmp_path: Path, case: Case
) -> None:
    run = replayed(cassette, tmp_path, case)

    assert run.result.exit_code == case.exit_code, run.result.output
    assert (run.document["mode"], run.document["outcome"]) == ("verified", case.outcome)
    assert run.badge["verdict"] == case.verdict
    costs = [
        json.loads(line)
        for line in (run.record / "costs.jsonl").read_text().splitlines()
    ]
    assert [(cost["role"], cost["mode"]) for cost in costs] == [
        ("verifier", "verified")
    ]
    assert len(run.played.sent) == len(costs)
    assert run.document["verified_cost_usd"] == costs[0]["cost_usd"]
    assert "run viewer:" not in run.result.output


def test_the_pass_cassette_cites_the_screenshot_and_the_badges_ref(
    cassette: Cassette, tmp_path: Path
) -> None:
    run = replayed(cassette, tmp_path, PASS)

    ref = badge_ref(run.record)
    assert run.badge["evidence"] == {
        "path": FOLDER,
        "screenshot": {"path": SHOT, "withheld": None},
        "before_screenshot": None,
    }
    assert run.badge["verifier"]["evidence_refs"] == [SHOT, ref]
    models = json.loads((run.record / "run.json").read_text())["models"]
    assert models["verifier"]["provider"] == "anthropic"
    assert re.search(r"^passed badge: \d+\.\ds, verified \$", run.result.stdout)


def test_the_violated_cassette_makes_a_visual_finding(
    cassette: Cassette, tmp_path: Path
) -> None:
    run = replayed(cassette, tmp_path, VIOLATED)

    finding = run.badge["finding"]
    assert (finding["category"], finding["title"]) == ("visual", EXPECTATION)
    assert run.badge["reason"] is None


def test_a_reply_with_no_evidence_refs_is_invalid_and_decides_nothing(
    cassette: Cassette, tmp_path: Path
) -> None:
    run = replayed(cassette, tmp_path, NO_EVIDENCE)

    assert (
        run.badge["reason"] == "the verifier's reply was invalid, so it decides nothing"
    )
    assert run.badge["verifier"] is None
    [cost] = (run.record / "costs.jsonl").read_text().splitlines()
    assert json.loads(cost)["status"] == "invalid"


@pytest.mark.parametrize("case", [PASS, VIOLATED], ids=lambda c: c.cassette)
def test_a_verifier_request_holds_the_saved_screenshot_and_nothing_of_the_page(
    cassette: Cassette, tmp_path: Path, case: Case
) -> None:
    run = replayed(cassette, tmp_path, case)

    [sent] = run.played.sent
    [system] = sent["system"] if isinstance(sent["system"], list) else [sent["system"]]
    text = system["text"] if isinstance(system, dict) else system
    assert text == VERIFY_INSTRUCTIONS
    [message] = sent["messages"]
    images = [part for part in message["content"] if part["type"] == "image"]
    [image] = images
    assert image["source"]["media_type"] == "image/png"
    png = base64.b64decode(image["source"]["data"])
    assert png == (run.record / SHOT).read_bytes()
    assert pixel(png, *CENTRE) == case.colour
    said = " ".join(
        part["text"] for part in message["content"] if part["type"] == "text"
    )
    assert EXPECTATION in said
    assert SHOT in said
    assert f"element: {badge_ref(run.record)}" in said
    # Everything sent but the image's data, whose base64 could spell anything.
    request = json.dumps(sent).replace(image["source"]["data"], "")
    assert NAME not in request
    assert "127.0.0.1" not in request
    assert "the order's status badge" not in request
