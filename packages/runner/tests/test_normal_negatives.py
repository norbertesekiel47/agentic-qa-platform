"""Normal (default) replay's negative checks prove what absence accepts
(ADR-0025's #53 P11b amendment; DATA_MODEL §7): a scope that leaves and
returns around a read is drift, page changed, never absence. The file also
holds the rows, in both modes, of a form whose control is named nodeType
and of a scope that is or shadows the page's root element. Real Chromium
throughout; "read n" is the n-th `Locator.count` whose selector names the
needle (A10's `around`)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from aqa_core.compiled import CompiledScript, Target
from aqa_core.config import ProjectConfig
from aqa_core.schema import Contract
from aqa_runner.browser_session import BrowserSession
from aqa_runner.evaluation import AssertionResult
from aqa_runner.explore_result import present
from aqa_runner.locator_generation import NegativeCheckError, TargetUses
from aqa_runner.locators import (
    Absent,
    Resolved,
    Shown,
    Unresolved,
    resolve,
    resolve_every,
)
from playwright.async_api import Locator as PlaywrightLocator
from playwright.async_api import Page

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.pilot_pages import in_session, put
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
    outcomes,
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
    with a barrier, `change` runs around the numbered read naming `needle`.
    `setup` runs on the page once it is loaded, before the look."""

    html: str
    looked_at: Target
    expected: object
    needle: str = ""
    change: tuple[int, str, str] = CONTROL
    setup: str = ""

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
    # The barrier ran in every look; the budget may cut the last one short.
    assert len(fired) >= len(found) > 1
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


async def looked(
    session: BrowserSession, row: Row, *, every: bool
) -> tuple[object, list[int], bool]:
    """One mode's answer for `row` on a fresh page, the reads its barrier ran
    around, and whether the page is as it was before the look."""
    await put(session, row.html)
    if row.setup:
        await session.page.evaluate(row.setup)
    page = session.page
    before = await page.content()
    with pytest.MonkeyPatch.context() as patch:
        fired = around(patch, row.needle, row.change)
        if every:
            found = await kept(
                await resolve_every(page, row.looked_at, "negative_check")
            )
        else:
            found = await kept(await resolve(page, row.looked_at, "negative_check"))
    return found, fired, await page.content() == before


def both_modes(
    row: Row,
) -> tuple[tuple[object, object], list[tuple[list[int], bool]]]:
    """`row` looked at with every locator, then normally, each on a fresh
    page: the two answers, then for each look the reads its barrier ran
    around and whether the page was as it was before."""

    async def scenario(session: BrowserSession) -> list[tuple[object, list[int], bool]]:
        return [await looked(session, row, every=every) for every in (True, False)]

    every, normal = in_session(scenario)
    return (every[0], normal[0]), [every[1:], normal[1:]]


NT = '<input type="hidden" name="nodeType">'
OD = '<input type="hidden" name="ownerDocument">'
OOPS = '<p class="error">Oops</p>'
BOXED = '<p class="box"><span class="error">x</span></p>'
# The target form, its attributes and its control filled in.
FORM_IN_AREA = '<div id="area"><form id="f"{}>{}<p>Sign in</p></form></div>'
# A scope form holding a control named nodeType, its content filled in.
IN_FORM = '<form id="area">' + NT + "{}</form>"
SHOWN_PAIR = (Shown(0), SHOWN)
ABSENT_PAIR = (Absent(0), Absent(0))
FORM_TARGET = target(inside("form#f", "div#area"))
ERROR_IN_FORM = target(inside(".error", "form#area"))
ERROR_IN_BOX_IN_FORM = target({"css": ".error", "scope": inside("p.box", "form#area")})
ERROR_IN_FORM_BOX = target({"css": ".error", "scope": inside("form.box", "#area")})
BANNER = (
    '<div class="banner"><div class="inner"><form class="f">{}<p>Sign in</p>'
    "</form></div></div>"
)
FORM_PART = contracted(
    inside("form.f", "div.inner"),
    contract=Contract(region="div.banner", part="form.f", leaf=False),
)
FORM_REGION_PAGE = (
    '<form class="banner">' + NT + '<span class="counter"{}>(3)</span></form>'
)
FORM_REGION = contracted(
    {"css": "span.counter"},
    contract=Contract(region="form.banner", part="span.counter", leaf=True),
)
FORMS = {
    "target form, nodeType, shown": Row(
        FORM_IN_AREA.format("", NT), FORM_TARGET, SHOWN_PAIR
    ),
    "target form, plain, shown": Row(
        FORM_IN_AREA.format("", ""), FORM_TARGET, SHOWN_PAIR
    ),
    "target form, nodeType, hidden": Row(
        FORM_IN_AREA.format(" hidden", NT), FORM_TARGET, ABSENT_PAIR
    ),
    "unscoped target form, nodeType, shown": Row(
        f'<form id="f">{NT}<p>Sign in</p></form>', target({"css": "form#f"}), SHOWN_PAIR
    ),
    "target form, ownerDocument, hidden": Row(
        FORM_IN_AREA.format(" hidden", OD), FORM_TARGET, ABSENT_PAIR
    ),
    "scope form, nodeType, shown": Row(IN_FORM.format(OOPS), ERROR_IN_FORM, SHOWN_PAIR),
    "scope form, nodeType, empty": Row(IN_FORM.format(""), ERROR_IN_FORM, ABSENT_PAIR),
    "nested in a scope form, shown": Row(
        IN_FORM.format(BOXED), ERROR_IN_BOX_IN_FORM, SHOWN_PAIR
    ),
    "nested in a scope form, empty": Row(
        IN_FORM.format('<p class="box"></p>'), ERROR_IN_BOX_IN_FORM, ABSENT_PAIR
    ),
    "inner scope form, shown": Row(
        f'<div id="area"><form class="box">{NT}<span class="error">x</span></form></div>',
        ERROR_IN_FORM_BOX,
        SHOWN_PAIR,
    ),
    "inner scope form, empty": Row(
        f'<div id="area"><form class="box">{NT}</form></div>',
        ERROR_IN_FORM_BOX,
        ABSENT_PAIR,
    ),
    "contracted, part a nodeType form, shown": Row(
        BANNER.format(NT), FORM_PART, SHOWN_PAIR
    ),
    "contracted, part a plain form, shown": Row(
        BANNER.format(""), FORM_PART, SHOWN_PAIR
    ),
    "contracted, region a nodeType form, part shown": Row(
        FORM_REGION_PAGE.format(""), FORM_REGION, SHOWN_PAIR
    ),
    "contracted, region a nodeType form, part hidden": Row(
        FORM_REGION_PAGE.format(" hidden"), FORM_REGION, ABSENT_PAIR
    ),
}


@pytest.mark.parametrize("row", FORMS.values(), ids=FORMS.keys())
def test_a_form_whose_control_is_named_nodetype_reads_as_the_element_it_is_in_both_modes(
    row: Row,
) -> None:
    answers, sides = both_modes(row)

    assert answers == row.expected
    assert sides == [(row.reads, True)] * 2


SCOPE_AND_MATCH = Unresolved(("no scope", "no match"))
FORM_AREA = REMOVE % "form#area"
TWO_IN_FORM = target(
    {"css": ".error", "scope": inside("p.box", "form#area")}, inside(".error", "#gone")
)
FORM_RACES = {
    "scope form shown, removed around read 1": Row(
        IN_FORM.format(OOPS),
        ERROR_IN_FORM,
        (NO_SCOPE, CHANGED),
        ".error",
        (1, FORM_AREA, RESTORE),
    ),
    "scope form empty, removed around read 1": Row(
        IN_FORM.format(""),
        ERROR_IN_FORM,
        ABSENT_PAIR,
        ".error",
        (1, FORM_AREA, RESTORE),
    ),
    "scope form empty, removed around read 2": Row(
        IN_FORM.format(""),
        ERROR_IN_FORM,
        (NO_SCOPE, CHANGED),
        ".error",
        (2, FORM_AREA, RESTORE),
    ),
    "two locators, nested in a scope form, inner scope removed around its count": Row(
        IN_FORM.format(BOXED) + GONE,
        TWO_IN_FORM,
        (SCOPE_AND_MATCH, TWO_CHANGED),
        "p.box",
        (1, BOX, RESTORE),
    ),
    "two locators, nested in a scope form, control": Row(
        IN_FORM.format(BOXED) + GONE, TWO_IN_FORM, SHOWN_PAIR
    ),
}


@pytest.mark.parametrize("row", FORM_RACES.values(), ids=FORM_RACES.keys())
def test_a_form_scope_that_leaves_and_returns_around_a_read_is_drift_never_absence_in_both_modes(
    row: Row,
) -> None:
    answers, sides = both_modes(row)

    assert answers == row.expected
    assert sides == [(row.reads, True)] * 2


# The page replaces its root element with a form holding a control named
# nodeType: Playwright's has and has-not can't read through it.
SWAP_ROOT = (
    "() => { const f = document.createElement('form');"
    " f.innerHTML = '<input type=hidden name=nodeType>' + document.body.innerHTML;"
    " document.replaceChild(f, document.documentElement) }"
)
TWO_AREA = target(inside(".error", "#area"), {"css": ".gone"})
SWAPPED_ROOTS = {
    "shown": Row(ERROR, IN_AREA, (NO_SCOPE, SHOWN), setup=SWAP_ROOT),
    "empty": Row(EMPTY, IN_AREA, (NO_SCOPE, CHANGED), setup=SWAP_ROOT),
    "two locators, scope removed around its count": Row(
        ERROR,
        TWO_AREA,
        (SCOPE_AND_MATCH, TWO_CHANGED),
        "#area",
        (1, AREA, RESTORE),
        SWAP_ROOT,
    ),
}


@pytest.mark.parametrize("row", SWAPPED_ROOTS.values(), ids=SWAPPED_ROOTS.keys())
def test_a_page_whose_root_element_is_a_clobbered_form_never_reads_absent(
    row: Row,
) -> None:
    answers, sides = both_modes(row)

    assert answers == row.expected
    assert sides == [(row.reads, True)] * 2


REPLAYED_FORMS = {
    "a shown nodeType form as the target": Row(
        FORM_IN_AREA.format("", NT), FORM_TARGET, [FAILED] * 2
    ),
    "the error shown in a nodeType form scope": Row(
        IN_FORM.format(OOPS), ERROR_IN_FORM, [FAILED] * 2
    ),
    "that scope empty": Row(IN_FORM.format(""), ERROR_IN_FORM, [PASSED] * 2),
}


@pytest.mark.parametrize("row", REPLAYED_FORMS.values(), ids=REPLAYED_FORMS.keys())
def test_a_replayed_negative_check_reads_a_clobbered_form_as_the_element_it_is(
    app: fx.App, tmp_path: Path, row: Row
) -> None:
    # With every locator, then by default.
    assert outcomes(app, tmp_path, checked(row.looked_at), row.html) == row.expected


ROOT_ID = '<html id="area"><body><p class="error">Oops</p></body></html>'
ROOT_ID_EMPTY = '<html id="area"><body><p>Fine</p></body></html>'
UNID = '() => document.documentElement.removeAttribute("id")'
REID = '() => { document.documentElement.id = "area" }'
UNROOT = (
    "() => { window.kept = document.documentElement;"
    " document.removeChild(window.kept) }"
)
REROOT = "() => { document.appendChild(window.kept) }"
NEST_HTML = '() => { document.body.append(document.createElement("html")) }'
# RESTORE, then an empty copy of the scope nested in the body, left there.
RESTORE_NEST_HTML = (
    "() => { window.at[0].insertBefore(window.kept, window.at[1]);"
    ' document.body.append(document.createElement("html")) }'
)
RESTORE_NEST_AREA = (
    "() => { window.at[0].insertBefore(window.kept, window.at[1]);"
    ' const d = document.createElement("div"); d.id = "area";'
    " document.body.append(d) }"
)
NESTING = {RESTORE_NEST_HTML, RESTORE_NEST_AREA}
ERROR_GONE = REMOVE % "p.error"
IN_HTML = target(inside(".error", "html"))
TWO_HTML = target(inside(".error", "html"), {"css": ".gone"})
ROOT_REGION = contracted(
    {"css": "span.counter"},
    contract=Contract(region="html", part="span.counter", leaf=True),
)
ROOTS = {
    "root is #area, its id removed around its count, two locators": Row(
        ROOT_ID, TWO_AREA, (SCOPE_AND_MATCH, TWO_CHANGED), "#area", (1, UNID, REID)
    ),
    "root is #area, control": Row(ROOT_ID, TWO_AREA, SHOWN_PAIR),
    "root is #area, empty, its id removed around its count, two locators": Row(
        ROOT_ID_EMPTY, TWO_AREA, (Absent(0), TWO_CHANGED), "#area", (1, UNID, REID)
    ),
    "root is #area, empty, control": Row(ROOT_ID_EMPTY, TWO_AREA, ABSENT_PAIR),
    "html scope, root removed around its count, two locators": Row(
        OOPS, TWO_HTML, (SCOPE_AND_MATCH, TWO_CHANGED), "css=html", (1, UNROOT, REROOT)
    ),
    "html scope, control": Row(OOPS, TWO_HTML, SHOWN_PAIR),
    "html scope, error removed around its count, an empty html nested on restore": Row(
        OOPS,
        IN_HTML,
        (NO_SCOPE, CHANGED),
        ".error",
        (1, ERROR_GONE, RESTORE_NEST_HTML),
    ),
    "root is #area, error removed around its count, an empty #area nested on restore": Row(
        ROOT_ID,
        IN_AREA,
        (NO_SCOPE, CHANGED),
        ".error",
        (1, ERROR_GONE, RESTORE_NEST_AREA),
    ),
    "html scope, error removed around its count, restored": Row(
        OOPS, IN_HTML, (NO_SCOPE, CHANGED), ".error", (1, ERROR_GONE, RESTORE)
    ),
    "an empty html nested for good, error shown": Row(
        OOPS, IN_HTML, (NO_SCOPE, NO_SCOPE), setup=NEST_HTML
    ),
    "root is #area and holds an empty #area, error shown": Row(
        ROOT_ID.replace("</p>", '</p><div id="area"></div>'),
        IN_AREA,
        (NO_SCOPE, NO_SCOPE),
    ),
    "root scope html, shown": Row(OOPS, IN_HTML, SHOWN_PAIR),
    "root scope html, empty": Row("<p>Fine</p>", IN_HTML, ABSENT_PAIR),
    "root scope #area, shown": Row(ROOT_ID, IN_AREA, SHOWN_PAIR),
    "root scope #area, empty": Row(ROOT_ID_EMPTY, IN_AREA, ABSENT_PAIR),
    "contracted, region is the root element, part shown": Row(
        '<span class="counter">(3)</span>', ROOT_REGION, SHOWN_PAIR
    ),
    "contracted, region is the root element, part hidden": Row(
        '<span class="counter" hidden>(3)</span>', ROOT_REGION, ABSENT_PAIR
    ),
}


@pytest.mark.parametrize("row", ROOTS.values(), ids=ROOTS.keys())
def test_a_scope_that_is_or_shadows_the_root_element_reads_as_the_page_does_in_both_modes(
    row: Row,
) -> None:
    answers, sides = both_modes(row)

    assert answers == row.expected
    # A barrier that nests a copy on its way back leaves the copy there.
    assert sides == [(row.reads, row.change[2] not in NESTING)] * 2


# The app writes each page after `<!doctype html><title>app</title>`, so a
# stray `<html id="area">` tag gives the root element its id.
NEST_HTML_TAG = '<script>document.body.append(document.createElement("html"))</script>'
UNSCOPED = (a1("binding_unresolved", ("no scope",)), "failed")
REPLAYED_ROOTS = {
    "an html scope, an empty html nested in the body, error shown": Row(
        OOPS + NEST_HTML_TAG, IN_HTML, [UNSCOPED] * 2
    ),
    "the root element is #area, error shown": Row(
        '<html id="area">' + OOPS, IN_AREA, [FAILED] * 2
    ),
    "the root element is #area, empty": Row(
        '<html id="area"><p>Fine</p>', IN_AREA, [PASSED] * 2
    ),
    "the root element is #area and holds an empty #area, error shown": Row(
        '<html id="area">' + OOPS + '<div id="area"></div>',
        IN_AREA,
        [UNSCOPED] * 2,
    ),
}


@pytest.mark.parametrize("row", REPLAYED_ROOTS.values(), ids=REPLAYED_ROOTS.keys())
def test_a_replayed_negative_check_reads_a_scope_at_the_root_element_as_the_page_does(
    app: fx.App, tmp_path: Path, row: Row
) -> None:
    # With every locator, then by default.
    assert outcomes(app, tmp_path, checked(row.looked_at), row.html) == row.expected
