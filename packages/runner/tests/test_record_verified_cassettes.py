"""The recorder's verified cases (#339; ADR-0024's #339 amendment): each badge page's
`aqa replay --mode verified` recorded in real Chromium from a local stand-in for the
provider, with a fake key, within the round's own bound, and promoted only when its
one verifier reply parses, cites exactly the screenshot, then the badge's ref, and
its run ends as its page should, which the rule says in fixed words when it doesn't.
Role, mode and allowance come from the case's row in the recorder's table. No test
here reaches a provider."""

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import typer.testing
import yaml
from aqa_cli.main import app as aqa
from aqa_core.config import ModelRoleName, ProjectConfig
from aqa_core.model_costs import CostRecord, Mode, Status, Usage, cost_record
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import vendored
from langchain_core.messages import HumanMessage

from packages.runner.tests import record_cassettes
from packages.runner.tests.provider_stand_in import Answer, Start, planned, standing_in
from packages.runner.tests.record_cassettes import (
    VERIFIED_ROUND_USD,
    answers_expected,
    record,
)
from packages.runner.tests.test_record_heal_cassettes import receipt, snapshot
from packages.runner.tests.test_record_heal_injection_cassettes import (
    never_promoted,
    promoted,
    reserved,
)
from packages.runner.tests.test_verified_cassette import (
    PASS,
    SHOT,
    VIOLATED,
    Case,
    Cassette,
)
from packages.runner.tests.verified_fixtures import (
    CASES,
    CHECKED,
    QUICK,
    TWICE,
    VerifiedCase,
    badge_spec,
    evidence,
    page,
    serving,
    verified_disallowed,
)

pytestmark = pytest.mark.usefixtures("reset_tracing")

PASSING, VIOLATING = CASES
# The two references a request gives, in its order.
GIVEN = [SHOT, "e2"]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Start]:
    with standing_in(monkeypatch) as start:
        yield start


@pytest.fixture
def library(tmp_path: Path) -> Path:
    path = tmp_path / "library"
    path.mkdir()
    return path


def judging(
    verdict: str, citing: Callable[[list[str]], list[str]] = list
) -> Callable[[bytes], Answer]:
    """The stand-in's answer: the verifier's `verdict`, citing what `citing`
    makes of the two references the request gives, in its order: those two,
    unless it says otherwise."""

    def answered(body: bytes) -> Answer:
        given = evidence([HumanMessage(content=snapshot(body))])
        said = {
            "verdict": verdict,
            "evidence_refs": citing(given),
            "confidence": 0.9,
            "rationale": "the badge is drawn in one colour",
        }
        return planned(1, said)

    return answered


@pytest.mark.parametrize("case", [PASS, VIOLATED], ids=lambda c: c.cassette)
def test_the_pages_verdict_citing_the_two_references_in_order_is_promoted(
    provider: Start, library: Path, tmp_path: Path, case: Case
) -> None:
    attempt = tmp_path / "verified" / "001"
    stand_in = provider([judging(case.verdict)])

    costs = record(case.cassette, attempt, library=library)

    assert promoted(case.cassette, attempt, library) == [("verifier", "verified")]
    assert [(cost.role, cost.mode, cost.status) for cost in costs] == [
        ("verifier", "verified", "ok")
    ]
    kept = receipt(attempt)
    assert (kept["explore"], len(kept["responses"])) == (
        {"exit_code": case.exit_code, "outcome": case.outcome, "published": None},
        1,
    )
    assert (kept["spend"]["run_usd"], kept["spend"]["round_left_usd"]) == (
        "0.3",
        str(VERIFIED_ROUND_USD),
    )
    intent = json.loads((attempt / "intent.json").read_text())
    assert intent["role"] == "verifier"
    assert ".compiled/badge.json" in intent["inputs"]
    [reservation] = reserved(case.cassette, attempt, stand_in.sent)
    assert reservation <= min(Decimal("0.3"), VERIFIED_ROUND_USD)


@pytest.mark.parametrize("case", [PASS, VIOLATED], ids=lambda c: c.cassette)
def test_each_promoted_cassette_replays_as_the_replay_test_replays_its_own(
    provider: Start, library: Path, tmp_path: Path, cassette: Cassette, case: Case
) -> None:
    provider([judging(case.verdict)])
    record(case.cassette, tmp_path / "verified" / "001", library=library)
    spec = badge_spec(tmp_path / "qa")

    with (
        serving(page(case.page)) as app,
        cassette(case.cassette, library=library, replay_only=True) as played,
    ):
        result = typer.testing.CliRunner().invoke(
            aqa, ["replay", "--mode", "verified", str(spec), "--url", app.origin]
        )

    assert result.exit_code == case.exit_code, result.output
    [run] = (spec.parent / ".aqa/runs").iterdir()
    document = json.loads((run / "replay.json").read_text())
    assert (document["mode"], document["outcome"]) == ("verified", case.outcome)
    [a1] = [each for each in document["verdicts"] if each.get("assertion") == "a1"]
    assert a1["verdict"] == case.verdict
    assert len(played.sent) == len(played.responses) == 1


@pytest.mark.parametrize(
    ("citing", "outcome"),
    [
        pytest.param(
            lambda _: ["verified/approved/screenshot.png", "e404"],
            "inconclusive",
            id="other_references",
        ),
        pytest.param(lambda given: given[:1], "inconclusive", id="fewer"),
        pytest.param(lambda given: [*given, "e405"], "inconclusive", id="more"),
        # The run takes the two as a set, so it passes.
        pytest.param(lambda given: given[::-1], "passed", id="the_two_reversed"),
    ],
)
def test_a_reply_citing_other_than_the_two_references_in_order_is_never_promoted(
    provider: Start,
    library: Path,
    tmp_path: Path,
    citing: Callable[[list[str]], list[str]],
    outcome: str,
) -> None:
    attempt = tmp_path / "verified" / "001"
    provider([judging("pass", citing)])

    with pytest.raises(ValueError, match="disallowed"):
        record("verified_pass", attempt, library=library)

    kept = never_promoted("verified_pass", attempt, library)
    assert kept["explore"]["outcome"] == outcome
    assert kept["disallowed"] == (
        "the verifier's reply didn't cite exactly the screenshot, then the badge's ref"
    )


def test_a_reply_citing_nothing_is_never_promoted(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    attempt = tmp_path / "verified" / "001"
    provider([judging("pass", lambda _: [])])

    with pytest.raises(ValueError, match="disallowed"):
        record("verified_pass", attempt, library=library)

    kept = never_promoted("verified_pass", attempt, library)
    assert [cost["status"] for cost in kept["cost_records"]] == ["invalid"]
    assert kept["disallowed"] == "the verifier's reply didn't parse as its verdict"


# How a run ends on a verdict that cites the two references.
ENDS = {
    "pass": "passed",
    "expectation_violated": "expectation_violated",
    "inconclusive": "inconclusive",
}


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("verified_pass", "expectation_violated"),
        ("verified_violated", "pass"),
        ("verified_pass", "inconclusive"),
    ],
)
def test_a_cited_verdict_that_ends_the_run_otherwise_is_never_promoted(
    provider: Start, library: Path, tmp_path: Path, name: str, verdict: str
) -> None:
    attempt = tmp_path / "verified" / "001"
    provider([judging(verdict)])
    outcome = ENDS[verdict]

    with pytest.raises(ValueError, match="disallowed"):
        record(name, attempt, library=library)

    kept = never_promoted(name, attempt, library)
    assert kept["explore"]["outcome"] == outcome
    assert kept["disallowed"] == f"it ended {outcome}, which this case doesn't allow"


def test_a_second_verifier_call_is_never_promoted(
    provider: Start,
    library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = record_cassettes.VERIFIED["verified_pass"]
    monkeypatch.setitem(
        record_cassettes.VERIFIED, case.cassette, replace(case, script=TWICE)
    )
    attempt = tmp_path / "verified" / "001"
    stand_in = provider([judging("pass")])

    with pytest.raises(ValueError, match="disallowed"):
        record("verified_pass", attempt, library=library)

    kept = never_promoted("verified_pass", attempt, library)
    assert kept["explore"]["outcome"] == "passed"
    assert len(stand_in.sent) == len(kept["responses"]) == 2
    assert kept["disallowed"] == "it didn't make exactly one verifier call"


def test_a_verified_round_past_its_limit_refuses_the_request(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    earlier = tmp_path / "verified" / "000"
    earlier.mkdir(parents=True)
    spent = {"spend": {"spent_usd": str(VERIFIED_ROUND_USD), "overrun": False}}
    (earlier / "receipt.json").write_text(json.dumps(spent))
    attempt = tmp_path / "verified" / "001"
    stand_in = provider([judging("pass")])

    with pytest.raises(ValueError, match="spend_refused"):
        record("verified_pass", attempt, library=library)

    assert stand_in.sent == []
    assert not (library / "verified_pass.yaml").exists()
    kept = receipt(attempt)
    assert (kept["outcome"], kept["spend"]["refused"]) == ("spend_refused", True)


def test_each_verified_case_says_what_it_is_recorded_as() -> None:
    names = ["verified_pass", "verified_violated"]

    assert {
        name: (case.role, case.mode, case.round_usd, case.whole_run)
        for name, case in record_cassettes.CASES.items()
        if name.startswith("verified_")
    } == dict.fromkeys(names, ("verifier", "verified", VERIFIED_ROUND_USD, True))
    assert [(case.cassette, case.page, case.outcome) for case in CASES] == [
        (each.cassette, each.page, each.outcome) for each in (PASS, VIOLATED)
    ]
    assert [(case.script, case.config) for case in CASES] == [(CHECKED, QUICK)] * 2
    assert "verified_no_evidence" not in record_cassettes.CASES
    config = ProjectConfig.model_validate(yaml.safe_load(QUICK))
    assert record_cassettes.CASES["verified_pass"].run_usd(config) == Decimal("0.3")
    assert answers_expected("verified_pass", (line(),)) == 1
    # The limit ADR-0024's #339 amendment derives, awaiting the maintainer's
    # approval.
    assert record_cassettes.CASES["verified_pass"].round_usd == Decimal("0.15")


def line(
    status: Status = "ok", role: ModelRoleName = "verifier", mode: Mode = "verified"
) -> CostRecord:
    """A cost line as a run writes it, `role`'s in `mode`."""
    roles = resolve_roles(ProjectConfig.model_validate({}), vendored())
    return cost_record(
        role=role,
        mode=mode,
        model=roles[role].model,
        usage=Usage(input_tokens=1612, cached_input_tokens=0, output_tokens=61),
        latency_ms=1,
        status=status,
    )


def badge(
    verdict: str = "pass",
    refs: Sequence[str] | None = tuple(GIVEN),
    *,
    finding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A `replay.json` whose a1 got `verdict`, the verifier citing `refs`, or not
    parsing when None, and holds `finding`."""
    said = None if refs is None else {"verdict": verdict, "evidence_refs": list(refs)}
    return {
        "verdicts": [
            {
                "assertion": "a1",
                "verdict": verdict,
                "finding": finding,
                "verifier": said,
            }
        ]
    }


VISUAL = {"category": "visual", "title": "The status badge is green"}


@pytest.mark.parametrize(
    ("case", "outcome", "document", "costs", "words"),
    [
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=None),
            (),
            "it didn't make exactly one verifier call",
            id="no_call",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(),
            (line(), line()),
            "it didn't make exactly one verifier call",
            id="a_second_call",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(),
            (line(role="healer", mode="heal"),),
            "it didn't make exactly one verifier call",
            id="another_roles_call",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=None),
            (line("invalid"),),
            "the verifier's reply didn't parse as its verdict",
            id="an_invalid_reply",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=None),
            (line(),),
            "the verifier's reply didn't parse as its verdict",
            id="no_verifier_on_the_verdict",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=["verified/approved/screenshot.png", "e404"]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="other_references",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=[SHOT]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="fewer_references",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=[*GIVEN, "e405"]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="more_references",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(refs=[*GIVEN, "e2"]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="a_duplicated_reference",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(refs=GIVEN[::-1]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="the_two_reversed",
        ),
        pytest.param(
            PASSING,
            "expectation_violated",
            badge("expectation_violated", finding=VISUAL),
            (line(),),
            "it ended expectation_violated, which this case doesn't allow",
            id="green_violated",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive"),
            (line(),),
            "it ended inconclusive, which this case doesn't allow",
            id="green_inconclusive",
        ),
        pytest.param(
            VIOLATING,
            "passed",
            badge(),
            (line(),),
            "it ended passed, which this case doesn't allow",
            id="red_passed",
        ),
        pytest.param(
            VIOLATING,
            "expectation_violated",
            badge("expectation_violated"),
            (line(),),
            "it ended expectation_violated with no visual finding",
            id="red_with_no_finding",
        ),
        pytest.param(
            VIOLATING,
            "expectation_violated",
            badge("expectation_violated", finding={**VISUAL, "category": "functional"}),
            (line(),),
            "it ended expectation_violated with no visual finding",
            id="red_with_another_finding",
        ),
    ],
)
def test_the_rule_says_in_fixed_words_why_a_run_is_never_promoted(
    case: VerifiedCase,
    outcome: str,
    document: dict[str, Any],
    costs: tuple[CostRecord, ...],
    words: str,
) -> None:
    assert verified_disallowed(case, outcome, document, costs, GIVEN) == words


@pytest.mark.parametrize(
    ("case", "outcome", "document"),
    [
        pytest.param(PASSING, "passed", badge(), id="green_passed"),
        pytest.param(
            VIOLATING,
            "expectation_violated",
            badge("expectation_violated", finding=VISUAL),
            id="red_violated_with_a_visual_finding",
        ),
    ],
)
def test_the_rule_allows_its_two_endings(
    case: VerifiedCase, outcome: str, document: dict[str, Any]
) -> None:
    assert verified_disallowed(case, outcome, document, (line(),), GIVEN) is None
