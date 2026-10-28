"""`replays.replay_run` (#237): the spec's reset hook goes through the run's
gate before the caller opens anything, then the script replays in what the
caller opened, first-match or with every locator, as the caller says. Real
Chromium against the executor's fixture app."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.spec import Reset, Spec
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import RunResult
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.reset import ResetFailedError
from aqa_runner.run_record import RunRecord
from playwright.async_api import async_playwright

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.egress_fixtures import gate

# A click on the form's save button, whose first locator finds nothing.
CLICK = {"seq": 1, "action": "click", "target": "save", "side_effect": True}
CLICK |= {"side_effect_basis": "network: POST /write/save"}
SCRIPT = fx.compiled([CLICK], targets={"save": fx.FORM_TARGETS["save"]})


@pytest.fixture
def app() -> Iterator[fx.App]:
    with fx.serving_app() as served:
        yield served


@dataclass(frozen=True)
class Replayed:
    """A replay's result, or its reset's refusal; the app's paths as they
    stood each time the caller's opening was entered; and the record that
    opening gave."""

    outcome: RunResult | ResetFailedError
    entered: list[list[str]]
    record: RunRecord

    @property
    def result(self) -> RunResult:
        assert isinstance(self.outcome, RunResult)
        return self.outcome


def form_spec(tmp_path: Path, config: ProjectConfig, hook: str | None) -> Spec:
    """The form's spec, with `hook` as its reset hook when given."""
    spec = fx.a_spec(tmp_path, config, start_url="/page/form")
    reset = None if hook is None else Reset(http=hook)
    preconditions = spec.frontmatter.preconditions.model_copy(update={"reset": reset})
    frontmatter = spec.frontmatter.model_copy(update={"preconditions": preconditions})
    return replace(spec, frontmatter=frontmatter)


def replayed(
    app: fx.App,
    tmp_path: Path,
    hook: str | None,
    *,
    every_locator: bool = False,
    config: ProjectConfig | None = None,
) -> Replayed:
    """SCRIPT replayed for the form's spec with reset hook `hook`, through an
    opening that starts a driver and a proxy on the run's gate."""
    config = config or ProjectConfig()
    spec = form_spec(tmp_path, config, hook)
    run_gate = gate(allowed=(app.origin,))
    record = RunRecord.create(tmp_path)
    entered: list[list[str]] = []

    @asynccontextmanager
    async def opening() -> AsyncIterator[Opened]:
        entered.append(app.paths())
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            await register_identity_engine(playwright)
            yield Opened(playwright.chromium, proxy, record)

    async def scenario() -> RunResult | ResetFailedError:
        replaying = Replaying(spec, config, app.origin, run_gate)
        script = fx.for_spec(SCRIPT, spec)
        try:
            # No replay test may hang the suite.
            async with asyncio.timeout(60):
                return await replay_run(
                    script, replaying, opening, every_locator=every_locator
                )
        except ResetFailedError as failed:
            return failed

    return Replayed(asyncio.run(scenario()), entered, record)


def steps(run: Replayed) -> list[tuple[object, ...]]:
    return [(step.seq, step.outcome, step.locator_index) for step in run.result.steps]


def test_the_reset_passes_before_the_caller_opens_anything(
    app: fx.App, tmp_path: Path
) -> None:
    run = replayed(app, tmp_path, "POST /write/reset")

    assert run.entered == [["/write/reset"]]
    assert (run.result.outcome, run.result.run_id) == ("passed", run.record.run_id)


def test_a_rejected_reset_opens_nothing(app: fx.App, tmp_path: Path) -> None:
    run = replayed(app, tmp_path, "POST /status/500")

    assert isinstance(run.outcome, ResetFailedError)
    assert str(run.outcome) == "the reset hook answered 500"
    assert run.entered == []
    assert app.paths() == ["/status/500"]


def test_the_reset_is_bound_by_the_projects_resolve_seconds(
    app: fx.App, tmp_path: Path
) -> None:
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    run = replayed(app, tmp_path, "POST /held/reset", config=config)

    assert isinstance(run.outcome, ResetFailedError)
    assert str(run.outcome) == "the reset hook didn't answer within 1 s"
    assert run.entered == []


def test_without_a_reset_hook_the_caller_opens_at_once(
    app: fx.App, tmp_path: Path
) -> None:
    run = replayed(app, tmp_path, None)

    assert run.entered == [[]]
    assert run.result.outcome == "passed"


def test_the_caller_chooses_first_match_or_every_locator(
    app: fx.App, tmp_path: Path
) -> None:
    first = replayed(app, tmp_path, None, every_locator=False)
    every = replayed(app, tmp_path, None, every_locator=True)

    assert steps(first) == [(0, "completed", None), (1, "completed", 1)]
    assert steps(every) == [(0, "completed", None), (1, "drifted", None)]
