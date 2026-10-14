"""Normal (default) replay's negative checks prove what absence accepts
(ADR-0025's #53 P11b amendment; DATA_MODEL §7): a scope that leaves and
returns around a read is drift, page changed, never absence. Real Chromium
throughout; "read n" is the n-th `Locator.count` whose selector names the
needle (A10's `around`)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from aqa_core.compiled import CompiledScript, Target
from aqa_core.config import ProjectConfig
from aqa_runner.browser_session import BrowserSession
from aqa_runner.evaluation import AssertionResult
from aqa_runner.explore_result import present
from aqa_runner.locator_generation import NegativeCheckError, TargetUses
from aqa_runner.locators import Absent, Resolved, Shown, Unresolved, resolve
from playwright.async_api import Locator as PlaywrightLocator
from playwright.async_api import Page

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.pilot_pages import in_session
from packages.runner.tests.test_every_locator import (
    AREA,
    AREAS,
    BOX,
    CONTROL,
    EMPTY,
    ERROR,
    IN_AREA,
    IN_BOX,
    IN_BUTTON,
    MOVE_BUTTON,
    NESTED,
    NO_SCOPE,
    REMOVE,
    RESTORE,
    UNDUPLICATE,
    around,
    inside,
    on,
    target,
)
from packages.runner.tests.test_every_locator_replay import (
    NOT_VISIBLE,
    NOTHING,
    PASSED,
    a1,
    looks,
    replayed,
    script,
)
from packages.runner.tests.test_explore_result import confirmed, ending, parts
from packages.runner.tests.test_explore_result import replayed as returned
from packages.runner.tests.test_locator_generation import see_header_link
from packages.runner.tests.test_subject_enforcement import HIDDEN, REGIONS, contracted

CHANGED = Unresolved(("page changed",))
TWO_CHANGED = Unresolved(("page changed", "no match"))
SHOWN = ("resolved", 0)
GONE = '<div id="gone"></div>'
BOX_EMPTY = '<div id="area"><p class="box"></p></div>'
TWO = target(inside(".error", "#area"), inside(".error", "#gone"))
NESTED_TWO = target(
    {"css": ".error", "scope": inside("p.box", "#area")}, inside(".error", "#gone")
)
HELD_TWO = contracted(
    inside("span.counter", "app-favorite-button"),
    inside("span.nothing", "div.article-meta"),
)
TWO_SECONDS = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 2}})


class Row(NamedTuple):
    """A page, the target looked at on it, and what the look should say;
    with a barrier, `change` runs around the numbered read naming `needle`."""

    html: str
    looked_at: Target
    expected: object
    needle: str = ""
    change: tuple[int, str, str] = CONTROL

    @property
    def reads(self) -> list[int]:
        """The reads the barrier should run around: none for a control."""
        return [self.change[0]] if self.change[1] else []


@pytest.fixture
def app() -> Iterator[fx.App]:
    with fx.serving_app() as served:
        yield served


async def kept(found: Resolved | Shown | Absent | Unresolved) -> object:
    """A resolution as a literal, a resolved element as its locator index.
    Its handle is let go unread: Playwright 1.63's get_attribute never
    answers on a form whose control is named nodeType."""
    if isinstance(found, Resolved):
        await found.element.dispose()
        return ("resolved", found.locator_index)
    return found


async def looked_at_once(page: Page, looked_at: Target) -> tuple[object, bool]:
    """Normal `resolve`'s negative answer, and whether the page is as it was
    before the look."""
    before = await page.content()
    found = await kept(await resolve(page, looked_at, "negative_check"))
    return found, await page.content() == before


SCOPE_RACES = {
    "shown, scope removed around read 1": Row(
        ERROR, IN_AREA, CHANGED, ".error", (1, AREA, RESTORE)
    ),
    "shown, scope duplicated around read 1": Row(
        ERROR, IN_AREA, Unresolved(("ambiguous",)), ".error", (1, AREAS, UNDUPLICATE)
    ),
    "shown, control": Row(ERROR, IN_AREA, SHOWN),
    "empty, scope removed around read 1": Row(
        EMPTY, IN_AREA, Absent(0), ".error", (1, AREA, RESTORE)
    ),
    "empty, scope removed around read 2": Row(
        EMPTY, IN_AREA, CHANGED, ".error", (2, AREA, RESTORE)
    ),
    "empty, scope duplicated around read 2": Row(
        EMPTY, IN_AREA, CHANGED, ".error", (2, AREAS, UNDUPLICATE)
    ),
    "empty, control": Row(EMPTY, IN_AREA, Absent(0)),
    "nested, inner scope removed around read 1": Row(
        NESTED, IN_BOX, CHANGED, ".error", (1, BOX, RESTORE)
    ),
    "nested, control": Row(NESTED, IN_BOX, SHOWN),
    "nested empty, inner scope removed around read 2": Row(
        BOX_EMPTY, IN_BOX, CHANGED, ".error", (2, BOX, RESTORE)
    ),
    "two locators, first scope removed around read 1": Row(
        ERROR + GONE, TWO, TWO_CHANGED, ".error", (1, AREA, RESTORE)
    ),
    "two locators, second scope gone, control": Row(EMPTY, TWO, Absent(0)),
    "contracted, shown, scope moved out of the region around read 1": Row(
        REGIONS,
        IN_BUTTON,
        Unresolved(("not the part",)),
        "span.counter",
        (1, MOVE_BUTTON, RESTORE),
    ),
    "contracted, hidden, scope moved out of the region around read 1": Row(
        HIDDEN, IN_BUTTON, Absent(0), "span.counter", (1, MOVE_BUTTON, RESTORE)
    ),
    "two locators, first scope removed around its own count": Row(
        ERROR + GONE, TWO, TWO_CHANGED, "#area", (1, AREA, RESTORE)
    ),
    "two locators, first scope duplicated around its own count": Row(
        ERROR + GONE, TWO, TWO_CHANGED, "#area", (1, AREAS, UNDUPLICATE)
    ),
    "two locators, around its own count, control": Row(ERROR + GONE, TWO, SHOWN),
    "two locators, first scope truly missing": Row(GONE, TWO, Absent(1)),
    "two locators, first scope duplicated for good": Row(
        EMPTY + EMPTY + GONE, TWO, Absent(1)
    ),
    "two locators, nested, inner scope removed around its own count": Row(
        NESTED + GONE, NESTED_TWO, TWO_CHANGED, "p.box", (1, BOX, RESTORE)
    ),
    "one locator, scope removed around its own count": Row(
        ERROR, IN_AREA, NO_SCOPE, "#area", (1, AREA, RESTORE)
    ),
    "contracted, two locators, scope moved out around its own count": Row(
        REGIONS,
        HELD_TWO,
        Unresolved(("no scope", "not the part")),
        "app-favorite-button",
        (1, MOVE_BUTTON, RESTORE),
    ),
}


@pytest.mark.parametrize("row", SCOPE_RACES.values(), ids=SCOPE_RACES.keys())
def test_a_normal_negatives_scope_that_leaves_and_returns_around_a_read_is_drift_never_absence(
    monkeypatch: pytest.MonkeyPatch, row: Row
) -> None:
    fired = around(monkeypatch, row.needle, row.change)

    looked = on(row.html, lambda page: looked_at_once(page, row.looked_at))

    assert looked == (row.expected, True)
    assert fired == row.reads


def retrieved_around(
    monkeypatch: pytest.MonkeyPatch, before: str, after: str
) -> list[int]:
    """Run `before` just before the first element retrieval whose selector
    names .error, after a count of one, and `after` once it returns."""
    original = PlaywrightLocator.element_handles
    fired: list[int] = []

    async def element_handles(self: PlaywrightLocator) -> Any:
        if fired or ".error" not in repr(self):
            return await original(self)
        fired.append(1)
        await self.page.evaluate(before)
        try:
            return await original(self)
        finally:
            await self.page.evaluate(after)

    monkeypatch.setattr(PlaywrightLocator, "element_handles", element_handles)
    return fired


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [(AREA, RESTORE, CHANGED), (NOTHING, NOTHING, SHOWN)],
    ids=["scope removed around the retrieval", "control"],
)
def test_a_normal_negatives_scope_that_leaves_around_its_retrieval_is_drift_never_absence(
    monkeypatch: pytest.MonkeyPatch, before: str, after: str, expected: object
) -> None:
    fired = retrieved_around(monkeypatch, before, after)

    assert on(ERROR, lambda page: looked_at_once(page, IN_AREA)) == (expected, True)
    assert fired == [1]


def checked(looked_at: Target) -> CompiledScript:
    """A script whose one check, a1, is that `looked_at` is not visible."""
    dumped = looked_at.model_dump(mode="json", exclude_none=True)
    return script(dumped["locators"], NOT_VISIBLE)


FAILED = (a1("failed", locator_index=0), "failed")
# What each look found, then the check and the run's outcome.
REPLAYED_RACES = {
    "one locator, scope removed around read 1": Row(
        ERROR, IN_AREA, ([CHANGED, 0], FAILED), ".error", (1, AREA, RESTORE)
    ),
    "one locator, unchanged": Row(ERROR, IN_AREA, ([0], FAILED)),
    "two locators, first scope removed around read 1": Row(
        ERROR + GONE, TWO, ([TWO_CHANGED, 0], FAILED), ".error", (1, AREA, RESTORE)
    ),
    "two locators, unchanged": Row(ERROR + GONE, TWO, ([0], FAILED)),
    "two locators, first scope removed around its own count": Row(
        ERROR + GONE, TWO, ([TWO_CHANGED, 0], FAILED), "#area", (1, AREA, RESTORE)
    ),
    "two locators, first scope truly missing": Row(GONE, TWO, ([Absent(1)], PASSED)),
    "one locator, empty, scope removed around read 1": Row(
        EMPTY, IN_AREA, ([Absent(0)], PASSED), ".error", (1, AREA, RESTORE)
    ),
}


@pytest.mark.parametrize("row", REPLAYED_RACES.values(), ids=REPLAYED_RACES.keys())
def test_a_default_replay_never_passes_a_negative_check_whose_scope_left_around_a_read(
    app: fx.App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: Row
) -> None:
    fired = around(monkeypatch, row.needle, row.change)
    found = looks(monkeypatch)

    done = replayed(
        app, tmp_path, checked(row.looked_at), row.html, every=False, config=TWO_SECONDS
    )

    assert (found, (*done.result.assertions, done.result.outcome)) == row.expected
    assert fired == row.reads


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            (1, REMOVE % "ul.navbar-nav", RESTORE),
            "the page broke the look at its negative check",
        ),
        (
            CONTROL,
            (
                "it is still shown: a locator it was seen by finds something "
                "visible in its scope"
            ),
        ),
    ],
    ids=["header links removed around read 1", "unchanged"],
)
def test_a_negative_check_the_page_changes_under_fails_by_name_as_a_broken_look(
    monkeypatch: pytest.MonkeyPatch, change: tuple[int, str, str], message: str
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, list[int]]:
        uses = TargetUses("the header's Sign in link", checks_text=False)
        await see_header_link(session, uses, "Sign in")
        fired = around(monkeypatch, "internal:role=link", change)
        with pytest.raises(NegativeCheckError) as raised:
            await uses.add_negative_check(session.page)
        return str(raised.value), fired

    assert in_session(scenario) == (
        f"the header's Sign in link: {message}",
        [1] if change[1] else [],
    )


def changed_under_every_look(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Remove the error's scope just before every count of the error's
    visible matches, and restore it once each returns: never around the
    proof read (has-not)."""
    original = PlaywrightLocator.count
    fired: list[str] = []

    async def count(self: PlaywrightLocator) -> int:
        selector = repr(self)
        if ".error" not in selector or "has-not" in selector:
            return await original(self)
        fired.append(selector)
        await self.page.evaluate(AREA)
        try:
            return await original(self)
        finally:
            await self.page.evaluate(RESTORE)

    monkeypatch.setattr(PlaywrightLocator, "count", count)
    return fired


def test_a_check_the_page_changes_under_until_its_budget_is_unresolved_and_presented(
    app: fx.App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired = changed_under_every_look(monkeypatch)
    found = looks(monkeypatch)

    done = replayed(
        app, tmp_path, checked(IN_AREA), ERROR, every=False, config=TWO_SECONDS
    )

    assert set(found) == {CHANGED}
    assert len(fired) == len(found) > 1
    unresolved = AssertionResult("a1", "binding_unresolved", ("page changed",))
    assert done.result.assertions == (unresolved,)
    assert done.result.outcome == "failed"
    made = parts(tmp_path)
    attempt = present(ending(made, checks={"a1": unresolved}))
    assert attempt["reasons"] == ["a1: binding_unresolved (page changed)"]
    result = returned(made, outcome="failed", assertions=(unresolved,))
    confirmation: Any = present(confirmed(made, result))
    assert confirmation["confirmation"]["assertions"] == [
        {
            "id": "a1",
            "outcome": "binding_unresolved",
            "misses": ["page changed"],
            "stopped_at": None,
            "reason": None,
        }
    ]
    assert confirmation["reasons"] == [
        "confirmation a1: binding_unresolved (page changed)"
    ]
