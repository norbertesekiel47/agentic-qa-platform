"""#171's hostile pages through each path that saves a screenshot as evidence
(#251; ADR-0033's #251 amendment): a step that drifted and a step that failed,
in a replay that asks for screenshots as `strict_replay`'s do, and a heal's
first look. Each case's page is set up on the replay's own session, through its
start step's dispatch, with its bound value filled and its attack started, as
`test_screenshots.attack` sets one up; the replay then reaches the path's
capture through its own code. No saved image shows the bound value, each shows
the page's marker, and a case not marked `withholds` saves one. Each case is
captured once a path, at scale 1: #171's own suite captures each 8 times and
covers scale 2. Real Chromium, sandboxed, on the OS that runs it."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_runner import executor
from aqa_runner.browser_session import BrowserSession, launched_browser
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.evidence import Shot
from aqa_runner.executor import Dispatch, RunResult, RunSetup, StepResult
from aqa_runner.heal import Healing
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.model_router import ModelRouter
from aqa_runner.model_turns import Spending
from aqa_runner.redaction import Redactor
from aqa_runner.repairs import Repair
from aqa_runner.run_record import RunRecord
from aqa_runner.secret_fields import SecretNotFilledError
from aqa_runner.settling import Window
from playwright.async_api import CDPSession, Error, async_playwright

from packages.runner.tests.document_fixtures import Sites, run_gate, serving_sites
from packages.runner.tests.executor_fixtures import a_spec, compiled, for_spec
from packages.runner.tests.explore_graph_fixtures import Scripted
from packages.runner.tests.heal_fixtures import NO_TOOL
from packages.runner.tests.screenshot_pages import (
    CASES,
    MARKED,
    armed,
    bound,
    element,
    green_pixels,
    red_pixels,
    secret,
    serve_cases,
)
from packages.runner.tests.test_screenshots import field

# Where the capture is reached: a step that drifted or failed, or a heal's look.
type Through = Literal["drifted", "failed", "heal"]

# A click on a button no case's page has, which drifts, and a key press, whose
# dispatch the test makes fail.
DRIFTS = compiled(
    [{"seq": 1, "action": "click", "target": "never", "side_effect": False}],
    targets={
        "never": {
            "semantic": "a button that never comes",
            "locators": [{"css": "#never-there"}],
        }
    },
)
FAILS = compiled([{"seq": 1, "action": "press", "key": "Shift", "side_effect": False}])
# The drift is found in a second; a heal's look gets two, as the heal tests'.
CONFIG = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
HEAL_SECONDS = 2


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


async def staged(
    session: BrowserSession, sites: Sites, name: str, served: dict[str, str]
) -> Window:
    """The case's page with its bound value in and its attack started, as
    `test_screenshots.attack` sets one up: the window of its last action."""
    case = CASES[name]
    filled = secret(sites, case.binding)
    value = filled.value.get_secret_value()
    served["value"] = value
    window = await session.navigate(f"{sites.app}/shot/{name}")
    if case.into in ("page", "cross-site"):
        trigger = await element(session, "#trigger")
        window = await session.fill_secret(trigger, secret(sites, "TRIGGER"))
    if case.into == "cross-site":
        # fill_secret never fills another origin's frame, so the value gets
        # there as a page would put it.
        if case.partial:
            with pytest.raises(Error):
                await session.fill(await field(session, case), value)
        else:
            window = await session.fill(await field(session, case), value)
    elif case.into != "page":
        if case.partial:
            with pytest.raises(SecretNotFilledError):
                await session.fill_secret(await field(session, case), filled)
        else:
            window = await session.fill_secret(await field(session, case), filled)
    if case.arm is not None:
        await session.page.evaluate(armed(case.arm, value))
    await asyncio.sleep(0.3)
    return window


def staging(
    monkeypatch: pytest.MonkeyPatch, sites: Sites, name: str, *, failing: bool
) -> None:
    """Make the start step's dispatch set the case's page up instead of
    loading the start URL, and, when `failing`, step 1's dispatch raise."""
    dispatched = executor.dispatched

    async def staged_dispatch(
        session: BrowserSession,
        record: RunRecord,
        dispatch: Dispatch,
        act: Callable[[], Awaitable[Window]],
    ) -> StepResult:
        async def stage() -> Window:
            served = await serve_cases(session.page, sites.other)
            return await staged(session, sites, name, served)

        async def fail() -> Window:
            raise Error("fake failure of the step's action")

        if dispatch.seq == 0:
            act = stage
        elif failing:
            act = fail
        return await dispatched(session, record, dispatch, act)

    monkeypatch.setattr(executor, "dispatched", staged_dispatch)


def unseen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make DevTools' DOM snapshot hold no frame's document, as for a
    cross-site frame in a process of its own."""
    send = CDPSession.send

    async def sending(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        found = await send(devtools, method, params)
        if method == "DOMSnapshot.captureSnapshot":
            for document in found["documents"]:
                document["nodes"].pop("contentDocumentIndex", None)
        return found

    monkeypatch.setattr(CDPSession, "send", sending)


async def replayed(
    sites: Sites, tmp_path: Path, through: Through
) -> tuple[RunResult, list[tuple[str, int, int]]]:
    """The replay that reaches a capture `through` its path: its result, and
    each PNG its record holds, by its name there, with its red and green
    pixels."""
    spec = a_spec(tmp_path, CONFIG, start_url="/shot/plain")
    record = RunRecord.create(tmp_path).redacting(Redactor(bound(sites.app)))
    repair: Repair | None = None
    if through == "heal":
        router = ModelRouter.from_config(CONFIG, Scripted(NO_TOOL))
        spending = Spending(record, Decimal(str(CONFIG.budgets.heal_model_usd)))
        repair = Healing(
            router, spending, CONFIG.budgets.heal_turns, HEAL_SECONDS
        ).repair
    script: CompiledScript = FAILS if through == "failed" else DRIFTS
    # The heal's replay doesn't ask, so its one image is the heal's.
    setup = RunSetup(spec, CONFIG, sites.app, record, repair, through != "heal")
    gate = run_gate(sites)
    async with async_playwright() as playwright, EgressProxy(gate) as proxy:
        await register_identity_engine(playwright)
        async with launched_browser(playwright.chromium) as browser:
            # No replay test may hang the suite.
            async with asyncio.timeout(60):
                result = await executor.replay(
                    for_spec(script, spec),
                    setup,
                    chromium=browser,
                    proxy=proxy,
                    gate=gate,
                )
            scratch = await browser.new_page()
            pixels = []
            for png in sorted(record.path.rglob("*.png")):
                data = png.read_bytes()
                pixels.append(
                    (
                        png.relative_to(record.path).as_posix(),
                        await red_pixels(scratch, data),
                        await green_pixels(scratch, data),
                    )
                )
    return result, pixels


@pytest.mark.parametrize("name", list(CASES))
@pytest.mark.parametrize("through", ["drifted", "failed", "heal"])
def test_no_saved_evidence_screenshot_shows_a_bound_value_whatever_the_page_does(
    sites: Sites,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    through: Through,
    name: str,
) -> None:
    case = CASES[name]
    if case.unseen:
        unseen(monkeypatch)
    staging(monkeypatch, sites, name, failing=through == "failed")

    result, pixels = asyncio.run(replayed(sites, tmp_path, through))

    outcome = "failed" if through == "failed" else "drifted"
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, outcome),
    ]
    if through == "heal":
        basis = result.repairs[0].basis
        assert basis is not None
        shot = basis.shot
    else:
        shot = result.shots.get(1)
    assert isinstance(shot, Shot)
    # Saved, or withheld with its reason in the result: never both, never neither.
    assert (shot.path is None) == (shot.withheld is not None)
    assert [saved for saved, _, _ in pixels] == (
        [] if shot.path is None else [shot.path]
    )
    # No red, and the marker: what is saved is the page, not a blank.
    assert [(red, green) for _, red, green in pixels] == [(0, MARKED)] * len(pixels)
    if not case.withholds:
        assert shot.path is not None, shot.withheld
