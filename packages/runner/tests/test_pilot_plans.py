"""The five pilot plans, recorded live once (TESTING §4) and replayed offline
against REVIEW.md's oracle (ADR-0024's B2a and B2b amendments)."""

import asyncio
import json
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
import typer.testing
import yaml
from aqa_cli.main import app
from aqa_core.coverage_plan import CoveragePlan, PlannedCheck, plan_hash, uncovered
from aqa_core.project import load_project
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.coverage_plan import make_plan, plan_request
from aqa_runner.model_router import ModelRouter
from playwright._impl._browser_type import BrowserType

from packages.runner.tests.conftest import CASSETTES, Recording
from packages.runner.tests.pilot_plan_oracles import plan_problems
from packages.runner.tests.test_pilot_plan_oracles import QA
from packages.runner.tests.test_record_pilot_plans import EXPECTATIONS

pytestmark = pytest.mark.usefixtures("reset_tracing")

Cassette = Callable[..., AbstractContextManager[Recording]]

# The expectations whose recorded checks the reviewed oracle refuses (#41's
# criterion 4; ADR-0024's B2b amendment), each with exactly the checks its
# recording holds, so a re-record or an oracle change that touches one fails
# (#161). Every other expectation's recorded checks are tested against the
# oracle.
TITLE = "the article title heading at the top of the article page"
ANY_ARTICLE = PlannedCheck(check="url_matches", pattern="(?i)/article/")
COMMENTS = "the list of comments under the article"
OPEN_ROWS: dict[tuple[str, int], tuple[PlannedCheck, ...]] = {
    ("read-article", 0): (
        ANY_ARTICLE,
        PlannedCheck(
            check="text_in_target", target_meaning=TITLE, text="Testing without flakes"
        ),
    ),
    ("read-article", 5): (
        PlannedCheck(
            check="text_in_target",
            target_meaning=COMMENTS,
            text="Deterministic data helped us most.",
        ),
        PlannedCheck(check="text_in_target", target_meaning=COMMENTS, text="reader"),
    ),
    ("publish-article", 0): (
        ANY_ARTICLE,
        PlannedCheck(
            check="text_in_target", target_meaning=TITLE, text="Benchmarks we trust"
        ),
    ),
}


def accepted_rows_problems(pilot: str, plan: CoveragePlan) -> tuple[str, ...]:
    open_rows = tuple(f"{row}/{index}:" for row, index in OPEN_ROWS if row == pilot)
    return tuple(
        problem
        for problem in plan_problems(pilot, plan)
        if not problem.startswith(open_rows)
    )


@pytest.mark.parametrize(("pilot", "index"), OPEN_ROWS)
def test_each_open_row_replays_exactly_its_recorded_checks(
    cassette: Cassette, pilot: str, index: int
) -> None:
    project = load_project(QA)
    router = ModelRouter.from_config(project.config, AnthropicClient)

    with cassette(f"plan_{pilot}", replay_only=True):
        planned = asyncio.run(make_plan(router, project.specs[pilot]))

    assert planned.plan is not None
    assert planned.plan.expectations[index].checks == OPEN_ROWS[pilot, index]
    assert f"{pilot}/{index}: establishing checks differ" in plan_problems(
        pilot, planned.plan
    )


@pytest.mark.parametrize("pilot", EXPECTATIONS)
def test_each_recorded_pilot_establishes_every_expectation(
    cassette: Cassette, pilot: str
) -> None:
    project = load_project(QA)
    spec = project.specs[pilot]
    router = ModelRouter.from_config(project.config, AnthropicClient)

    with cassette(f"plan_{pilot}", replay_only=True):
        planned = asyncio.run(make_plan(router, spec))

    assert planned.plan is not None
    assert planned.misfits == ()
    assert uncovered(planned.plan, spec.frontmatter) == ()
    assert accepted_rows_problems(pilot, planned.plan) == ()


@pytest.mark.parametrize("pilot", EXPECTATIONS)
def test_each_pilot_cassette_holds_real_answers_priced_as_they_were_billed(
    cassette: Cassette, pilot: str
) -> None:
    # One answer, or two when the first plan couldn't be used and was asked
    # for once more (ADR-0024's #161 amendment).
    project = load_project(QA)
    interactions = yaml.safe_load((CASSETTES / f"plan_{pilot}.yaml").read_text())[
        "interactions"
    ]
    sent = [json.loads(interaction["request"]["body"]) for interaction in interactions]
    answers = [
        json.loads(interaction["response"]["body"]["string"])
        for interaction in interactions
    ]
    assert {request["model"] for request in sent} == {"claude-sonnet-5-5"}
    assert sent[0]["messages"] == [
        {"role": "user", "content": plan_request(project.specs[pilot])[1].content}
    ]
    assert [request["messages"][:1] for request in sent[1:]] == [
        sent[0]["messages"]
    ] * (len(sent) - 1)
    assert {answer["stop_reason"] for answer in answers} == {"end_turn"}
    assert all(
        interaction["response"]["headers"]["request-id"][0].startswith("req_")
        for interaction in interactions
    )
    router = ModelRouter.from_config(project.config, AnthropicClient)

    with cassette(f"plan_{pilot}", replay_only=True):
        planned = asyncio.run(make_plan(router, project.specs[pilot]))

    assert [
        (call.status, call.input_tokens, call.output_tokens)
        for call in planned.routed.calls
    ] == [
        ("ok", answer["usage"]["input_tokens"], answer["usage"]["output_tokens"])
        for answer in answers
    ]


@pytest.mark.parametrize("pilot", EXPECTATIONS)
def test_each_pilot_plan_only_replays_through_the_cli_from_a_moved_root(
    cassette: Cassette,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pilot: str,
) -> None:
    root = tmp_path / "elsewhere" / "qa"
    shutil.copytree(QA, root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AQA_UNRELATED", "fake-unrelated-value")
    for name in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):
        monkeypatch.setattr(BrowserType, name, None)
    args = [
        str(root / f"{pilot}.spec.md"),
        "--plan-only",
        "--url",
        "http://127.0.0.1:9",
    ]

    with cassette(f"plan_{pilot}", replay_only=True):
        result = typer.testing.CliRunner().invoke(app, ["explore", *args])

    assert result.exit_code == 0, result.output
    [written] = (root / ".aqa" / "runs").glob("*/plan.json")
    record = json.loads(written.read_text())
    plan = CoveragePlan.model_validate(record["plan"])
    assert (record["outcome"], record["spec_id"]) == ("planned", pilot)
    assert record["plan_hash"] == plan_hash(plan)
    assert accepted_rows_problems(pilot, plan) == ()
