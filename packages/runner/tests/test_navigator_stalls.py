"""The navigator's tools on a page that stalls their reads (#206; ADR-0024's
#53 P13 amendment): what each tool reads of the page before its intent, or
in place of one, is bounded by the run's `resolve_seconds` and
`MARGIN_SECONDS` past them, as replay's looks are, so a page that never
answers is a tool error the attempt survives. Real Chromium against the
executor's fixture app, on the OS that runs them."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from aqa_runner.navigator import Click, Fill, FillSecret, Select
from aqa_runner.navigator_tools import Action, Attempt

from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.navigator_fixtures import (
    NAME,
    SAVE,
    SIGN_IN,
    SIZE,
    bound,
    journal,
    on_app,
    ref,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE

# The page's own stall: Playwright awaits a thenable that an evaluation in
# the page's world returns, and once every object is one that never
# settles, each read returning an object or a handle waits forever.
STALL = "() => { Object.prototype.then = function () {}; }"
UNSTALL = "() => { delete Object.prototype.then; }"
# The bound at a `resolve_seconds` of 1: that and `MARGIN_SECONDS`.
UNANSWERED = "the page didn't answer within 2 s"


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


async def capped[T](call: Awaitable[T]) -> T:
    """`call`'s result, or `TimeoutError` well past the bound, so a call
    the bound doesn't end fails here instead of at the suite's own limit."""
    async with asyncio.timeout(20):
        return await call


@dataclass(frozen=True)
class Case:
    """An action on an element: its tool, the page it starts on, and the call."""

    tool: str
    page: str
    call: Callable[[Attempt], Action]


@pytest.mark.parametrize(
    "case",
    [
        Case(
            "click",
            "/page/form",
            lambda attempt: Click(ref=ref(attempt, "button", "Save"), meaning=SAVE),
        ),
        Case(
            "fill",
            "/page/form",
            lambda attempt: Fill(
                ref=ref(attempt, "textbox", "Name"), meaning=NAME, text="Ada"
            ),
        ),
        Case(
            "fill_secret",
            "/page/signin",
            lambda attempt: FillSecret(
                ref=ref(attempt, "textbox", "Password"),
                meaning=SIGN_IN,
                name="TEST_PASSWORD",
            ),
        ),
        Case(
            "select",
            "/page/form",
            lambda attempt: Select(
                ref=ref(attempt, "combobox", "Size"), meaning=SIZE, option="M"
            ),
        ),
    ],
    ids=lambda case: case.tool,
)
def test_an_action_the_page_stalls_before_its_intent_is_a_tool_error(
    app: App, tmp_path: Path, case: Case
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        call = case.call(attempt)
        await attempt.session.page.evaluate(STALL)
        stalled = await capped(attempt.act(call))
        lines = journal(attempt)
        # The attempt goes on: once the page answers, the same call runs.
        await attempt.session.page.evaluate(UNSTALL)
        ran = await capped(attempt.act(call))
        return [stalled.step, stalled.text, lines, ran.text]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page=case.page,
        budget=1,
        secrets=[bound(app.origin, FAKE_VALUE)],
    ) == [
        None,
        f"{case.tool} did not run: {UNANSWERED}",
        [],
        "step 1 ran, and the page settled",
    ]
