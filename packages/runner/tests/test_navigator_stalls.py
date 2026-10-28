"""The navigator's tools on a page that stalls their reads (#206; ADR-0024's
#53 P13 amendment): what each tool reads of the page before its intent, or
in place of one, is bounded by the run's `resolve_seconds` and
`MARGIN_SECONDS` past them, as replay's looks are, so a page that never
answers is a tool error the attempt survives. Real Chromium against the
executor's fixture app, on the OS that runs them."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aqa_core.compiler import Provenance
from aqa_core.coverage_plan import CoveragePlan
from aqa_runner.browser_session import BrowserSession
from aqa_runner.navigator import (
    AssertCheck,
    Click,
    DescribeParts,
    Fill,
    FillSecret,
    Finish,
    NoteForAbsence,
    Select,
)
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
# the page returns, and once every object is one that never settles, a read
# that returns an object or a handle never answers.
STALL = "() => { Object.prototype.then = function () {}; }"
UNSTALL = "() => { delete Object.prototype.then; }"
# The bound at a `resolve_seconds` of 1: that and `MARGIN_SECONDS`.
UNANSWERED = "the page didn't answer within 2 s"


def plan(check: dict[str, str]) -> CoveragePlan:
    """A plan of one expectation, checked by `check` alone, as a1."""
    return CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": "the form",
                    "claim": "is saved",
                    "checks": [check],
                }
            ],
            "requires": [],
        }
    )


READS_SAVE = plan({"check": "text_in_target", "target_meaning": SAVE, "text": "Save"})
SAVE_GONE = plan({"check": "not_visible", "target_meaning": SAVE})
BY = Provenance(
    models={"navigator": "fake-model"},
    price_map="6f0c3a9d2b8e4f17a5c9d0e3b6a1f4c8d2e7b9a0",
    at=datetime(2026, 10, 12, tzinfo=UTC),
)


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


def test_describe_parts_the_page_stalls_is_a_tool_error(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[str]:
        call = DescribeParts(ref=ref(attempt, "combobox", "Size"))
        await attempt.session.page.evaluate(STALL)
        described = await capped(attempt.describe(call))
        return [described.text, described.parts]

    assert on_app(app, tmp_path, scenario, budget=1) == [
        f"describe_parts did not run: {UNANSWERED}",
        "",
    ]


def test_assert_check_the_page_stalls_binds_nothing(app: App, tmp_path: Path) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        call = AssertCheck(check_id="a1", ref=ref(attempt, "button", "Save"))
        await attempt.session.page.evaluate(STALL)
        said = await capped(attempt.check(call))
        return [said, attempt.checks]

    assert on_app(app, tmp_path, scenario, budget=1, plan=READS_SAVE) == [
        f"assert_check did not run: {UNANSWERED}",
        {"a1": None},
    ]


def test_note_for_absence_the_page_stalls_notes_nothing(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        call = NoteForAbsence(check_id="a1", ref=ref(attempt, "button", "Save"))
        await attempt.session.page.evaluate(STALL)
        said = await capped(attempt.note(call))
        return [said, attempt.noted]

    assert on_app(app, tmp_path, scenario, budget=1, plan=SAVE_GONE) == [
        f"note_for_absence did not run: {UNANSWERED}",
        frozenset(),
    ]


def test_finish_on_a_page_that_never_answers_compiles_nothing(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(_: BrowserSession) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def scenario(attempt: Attempt) -> list[object]:
        call = AssertCheck(check_id="a1", ref=ref(attempt, "button", "Save"))
        bound_now = await attempt.check(call)
        monkeypatch.setattr(BrowserSession, "visible_text", never)
        finished = await capped(attempt.finish(Finish(steps=()), BY))
        return [bound_now, finished.script, finished.text]

    assert on_app(app, tmp_path, scenario, budget=1, plan=READS_SAVE) == [
        "a1 is bound, and on this page it is pass",
        None,
        f"finish compiled nothing: on this page a1 is not_evaluated: {UNANSWERED}",
    ]
