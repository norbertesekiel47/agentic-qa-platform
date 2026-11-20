"""The M2 checks' plan, recorded live once (TESTING §4) and replayed offline
against its oracle (#327; ADR-0024's #327 amendment), through `make_plan` and
through `aqa explore --plan-only`. Its cassette is in AWAITING_RERECORD, so
until the maintainer's round records it each test here is expected to fail,
and fails once it replays: then its name leaves the set."""

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
import typer.testing
from aqa_cli.main import app
from aqa_core.coverage_plan import CoveragePlan, plan_hash
from aqa_core.project import load_config
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.coverage_plan import make_plan
from aqa_runner.model_router import ModelRouter
from playwright._impl._browser_type import BrowserType

from packages.runner.tests.conftest import Recording
from packages.runner.tests.m2_plan_fixtures import CASE, SPEC_ID, m2_spec, plan_problems

pytestmark = pytest.mark.usefixtures("reset_tracing")

Cassette = Callable[..., AbstractContextManager[Recording]]


def test_the_m2_plan_replays_through_make_plan_as_its_oracle_wants(
    cassette: Cassette, tmp_path: Path
) -> None:
    spec = m2_spec(tmp_path / "qa")
    router = ModelRouter.from_config(
        load_config(tmp_path / "qa" / "config.yaml"), AnthropicClient
    )

    with cassette(CASE, replay_only=True):
        planned = asyncio.run(make_plan(router, spec))

    assert planned.plan is not None
    assert planned.problems == ()
    assert plan_problems(planned.plan) == ()


def test_the_m2_plan_replays_through_explore_plan_only(
    cassette: Cassette, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = m2_spec(tmp_path / "qa")
    for name in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):
        monkeypatch.setattr(BrowserType, name, None)
    args = [str(spec.path), "--plan-only", "--url", "http://127.0.0.1:9"]

    with cassette(CASE, replay_only=True):
        result = typer.testing.CliRunner().invoke(app, ["explore", *args])

    assert result.exit_code == 0, result.output
    [written] = (tmp_path / "qa" / ".aqa" / "runs").glob("*/plan.json")
    record = json.loads(written.read_text())
    plan = CoveragePlan.model_validate(record["plan"])
    assert (record["outcome"], record["spec_id"]) == ("planned", SPEC_ID)
    assert record["plan_hash"] == plan_hash(plan)
    assert plan_problems(plan) == ()
