"""Screenshots as a replay's evidence (#251; ADR-0033's #251 amendment): with
`RunSetup.screenshots`, which only `strict_replay` sets, a kept step that drifted
or failed saves one masked screenshot in its evidence folder, and what became of
it is in `RunResult.shots`. A completed step, a drifted step a repair took, and
a replay that doesn't ask, as explore's confirmation and the bench pilot don't,
save none. Real Chromium against the executor's fixture app."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner.browser_session import BrowserSession
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.evidence import Shot
from aqa_runner.executor import RunResult
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.run_record import RunRecord
from aqa_runner.settling import Window
from playwright.async_api import Error, async_playwright

from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    App,
    a_spec,
    asking_screenshots,
    compiled,
    for_spec,
    run,
    serving_app,
)
from packages.runner.tests.test_replay_inserted_steps import (
    CLICK_CONTACT,
    POST,
    contact_does,
    inserting,
    replayed,
)

# A fill on the form's name field, and a click on a button no page has.
FILL = {
    "seq": 1,
    "action": "fill",
    "target": "name",
    "value": "Ada",
    "side_effect": False,
}
NEVER = {
    "never": {"semantic": "a button that never comes", "locators": [{"css": "#never"}]}
}
DRIFTS = compiled(
    [{"seq": 1, "action": "click", "target": "never", "side_effect": False}],
    targets=NEVER,
)
# The drift is found sooner than the default 10 s.
QUICK = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def pngs(record: Path) -> list[str]:
    """Each PNG file under `record`, by its path there."""
    return sorted(path.relative_to(record).as_posix() for path in record.rglob("*.png"))


def test_a_step_that_fails_saves_its_screenshot_and_no_other_step_does(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asking_screenshots(monkeypatch)
    # The page's own execCommand throws, so the fill's dispatch fails.
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/throws")

    done = run(app, tmp_path, compiled([FILL], targets=FORM_TARGETS), spec=spec)

    assert [step.outcome for step in done.result.steps] == ["completed", "failed"]
    assert done.result.shots == {1: Shot("evidence/1/screenshot.png")}
    assert pngs(done.record.path) == ["evidence/1/screenshot.png"]


def test_a_start_navigation_that_fails_saves_its_screenshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navigate = BrowserSession.navigate

    async def failing(session: BrowserSession, url: str) -> Window:
        await navigate(session, url)
        raise Error("fake failure once the page loaded")

    monkeypatch.setattr(BrowserSession, "navigate", failing)
    asking_screenshots(monkeypatch)

    done = run(app, tmp_path, compiled([]))

    assert [step.outcome for step in done.result.steps] == ["failed"]
    assert done.result.shots == {0: Shot("evidence/0/screenshot.png")}
    assert pngs(done.record.path) == ["evidence/0/screenshot.png"]


def test_an_inserted_step_that_changed_something_saves_its_screenshot(
    tmp_path: Path,
) -> None:
    done = replayed(
        tmp_path, inserting(CLICK_CONTACT), html=contact_does(POST), screenshots=True
    )

    assert [(step.seq, step.outcome) for step in done.result.steps] == [
        (0, "completed"),
        (3, "failed"),
    ]
    # The drifted step the repair took keeps no evidence of its drift.
    assert done.result.shots == {3: Shot("evidence/3/screenshot.png")}
    assert pngs(done.record) == ["evidence/3/screenshot.png"]


def test_a_replay_that_doesnt_ask_saves_no_screenshot(app: App, tmp_path: Path) -> None:
    spec = a_spec(tmp_path, QUICK, start_url="/page/form")
    record = RunRecord.create(tmp_path)
    run_gate = gate(allowed=(app.origin,))

    @asynccontextmanager
    async def opening() -> AsyncIterator[Opened]:
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            await register_identity_engine(playwright)
            yield Opened(playwright.chromium, proxy, record)

    async def scenario() -> RunResult:
        # As explore's confirmation and the bench pilot replay.
        replaying = Replaying(spec, QUICK, app.origin, run_gate)
        async with asyncio.timeout(60):
            return await replay_run(
                for_spec(DRIFTS, spec), replaying, opening, every_locator=True
            )

    result = asyncio.run(scenario())

    assert [step.outcome for step in result.steps] == ["completed", "drifted"]
    assert result.shots == {}
    assert pngs(record.path) == []
