"""`replay(..., every_locator=True)` (ADR-0025's #53 P11 amendment): all a
target's locators at each use, each action dispatched once, and the index of
the locator that resolved a check's target; the default replay unchanged."""

from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript, Target
from aqa_core.config import ProjectConfig
from aqa_runner.browser_session import BrowserSession
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import replay
from aqa_runner.locators import Absent, Miss, Resolved, Shown, Unresolved, Use

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.test_every_locator import AREA, CONTROL, RESTORE, around
from packages.runner.tests.test_executor_runs import COUNT_ROW, copies, stamped

ONE_SECOND = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
# A side-effect click on the target.
CLICK: dict[str, object] = {"seq": 1, "action": "click", "target": "it"}
CLICK |= {"side_effect": True, "side_effect_basis": "network: POST /write/save"}
SAVE = [{"css": "#save"}, {"role": "button", "name": "Save"}]
NOT_UNANIMOUS: tuple[Miss, ...] = ("not unanimous", "not unanimous")
NOT_VISIBLE = {"check": "not_visible"}
ERROR = '<div id="area"><p class="error">Oops</p></div>'
NOTHING = "() => 0"
a1 = partial(AssertionResult, "a1")
type Outcome = tuple[object, ...]


@pytest.fixture
def app() -> Iterator[fx.App]:
    with fx.serving_app() as served:
        yield served


def script(
    locators: list[dict[str, Any]],
    *checks: dict[str, Any],
    steps: tuple[dict[str, Any], ...] = (),
    **fields: Any,
) -> CompiledScript:
    """`steps`, one target, `it`, found by `locators`, with `fields`, and a
    check `a<n>` on it for each of `checks`."""
    return fx.compiled(
        list(steps),
        targets={"it": {"semantic": "the element", "locators": locators, **fields}},
        assertions=[
            {"id": f"a{n}", "expect_index": 0, "target": "it", **check}
            for n, check in enumerate(checks, 1)
        ]
        or None,
    )


def replayed(
    app: fx.App,
    tmp_path: Path,
    compiled_script: CompiledScript,
    html: str,
    *,
    every: bool,
    config: ProjectConfig = ONE_SECOND,
) -> fx.Run:
    """The script replayed on `html`, with every locator at every use or not."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(fx.PAGES, "p11", html)
        if every:
            patch.setattr(fx, "replay", partial(replay, every_locator=True))
        spec = fx.a_spec(tmp_path, config, start_url="/page/p11")
        return fx.run(app, tmp_path, compiled_script, config=config, spec=spec)


def outcomes(
    app: fx.App,
    tmp_path: Path,
    compiled_script: CompiledScript,
    html: str,
    config: ProjectConfig = ONE_SECOND,
) -> list[Outcome]:
    """The checks and the run's outcome with every locator, then by default."""
    runs = [
        replayed(app, tmp_path, compiled_script, html, every=every, config=config)
        for every in (True, False)
    ]
    return [(*done.result.assertions, done.result.outcome) for done in runs]


def steps(done: fx.Run) -> list[tuple[object, ...]]:
    return [(s.seq, s.outcome, s.locator_index, s.misses) for s in done.result.steps]


def looks(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """What each look finds from now on, a resolved element as its index."""
    resolve = BrowserSession.resolve
    found: list[object] = []

    async def resolving(
        session: BrowserSession, target: Target, use: Use, *, every: bool = False
    ) -> Resolved | Shown | Absent | Unresolved:
        look = await resolve(session, target, use, every=every)
        found.append(look.locator_index if isinstance(look, Resolved) else look)
        return look

    monkeypatch.setattr(BrowserSession, "resolve", resolving)
    return found


def test_confirmation_rejects_a_valid_first_locator_with_a_fallback_on_another_element(
    app: fx.App, tmp_path: Path
) -> None:
    page = (
        "<button id=\"save\" onclick=\"fetch('/write/keep', {method: 'POST'})\">Keep</button>"
        "<button onclick=\"fetch('/write/save', {method: 'POST'})\">Save</button>"
    )
    click = script(SAVE, steps=(CLICK,))

    confirmed = replayed(app, tmp_path, click, page, every=True)

    assert steps(confirmed)[1:] == [(1, "drifted", None, NOT_UNANIMOUS)]
    assert confirmed.result.assertions == (a1("not_evaluated", stopped_at=1),)
    assert confirmed.result.outcome == "failed"
    assert app.bodies == []
    states = [(ln["seq"], ln["state"]) for ln in fx.read_steps(confirmed.record.path)]
    assert states == [(0, "intent"), (0, "completed")]
    # Normal replay clicks the first locator's button.
    default = replayed(app, tmp_path, click, page, every=False)
    assert steps(default)[1] == (1, "completed", 0, ())
    assert [path for path, _ in app.bodies] == ["/write/keep"]


def test_every_locator_is_checked_at_every_use_of_a_target(
    app: fx.App, tmp_path: Path
) -> None:
    # The click moves `primary` to the other button.
    page = (
        '<button id="save" class="primary" onclick="this.classList.remove(\'primary\');'
        " document.querySelector('#other').classList.add('primary');"
        " fetch('/write/save', {method: 'POST'})\">Save</button>"
        '<button id="other">Other</button>'
    )
    text = {"check": "text_in_target", "text": "Save"}
    clicked = script([SAVE[0], {"css": "button.primary"}], text, steps=(CLICK,))

    assert outcomes(app, tmp_path, clicked, page) == [
        # The click dispatched; the check's look then found two buttons.
        (a1("binding_unresolved", NOT_UNANIMOUS), "failed"),
        (a1("pass", locator_index=0), "passed"),
    ]


def test_the_action_is_dispatched_once_under_every_locator(
    app: fx.App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = "<button id=\"save\" onclick=\"fetch('/write/save', {method: 'POST'})\">Keep</button>"
    rename = "() => { document.querySelector('#save').textContent = 'Save' }"
    # The button is renamed once the role locator's first count returns 0.
    renamed = around(monkeypatch, "role=button", (1, NOTHING, rename))
    found = looks(monkeypatch)

    done = replayed(app, tmp_path, script(SAVE, steps=(CLICK,)), page, every=True)

    assert renamed == [1]
    assert found == [Unresolved(("not unanimous", "no match")), 0]
    assert steps(done)[1] == (1, "completed", 0, ())
    assert [path for path, _ in app.bodies] == ["/write/save"]
    lines = [line for line in fx.read_steps(done.record.path) if line["seq"] == 1]
    assert [(line["state"], line.get("locator_used")) for line in lines] == [
        ("intent", None),
        ("completed", 0),
    ]


def test_a_contracted_replay_with_every_locator_never_reads_the_lower_copy(
    app: fx.App, tmp_path: Path
) -> None:
    count = script(
        [{"css": "span.counter"}, {"css": ":scope + div.container.page span.counter"}],
        {"check": "text_in_target", "text": "(3)"},
        contract={"region": "div.banner", "part": "span.counter", "leaf": True},
    )
    html = copies("(3)", "(1)")

    assert outcomes(app, tmp_path, stamped(count, COUNT_ROW), html, COUNT_ROW) == [
        (a1("binding_unresolved", ("not unanimous", "outside region")), "failed"),
        (a1("pass", locator_index=0), "passed"),
    ]


def inside(css: str, scope: str) -> dict[str, Any]:
    return {"css": css, "scope": {"css": scope}}


NO_SCOPE = Unresolved(("no scope",))
ERROR_IN_AREA = script([inside(".error", "#area")], NOT_VISIBLE)
PASSED = (a1("pass"), "passed")
FALLBACK_SHOWN = (a1("failed", locator_index=1), "failed")


@pytest.mark.parametrize(
    ("html", "locators", "expected"),
    [
        (
            '<div id="area"></div><div id="side"></div>',
            [inside(".error", "#area"), inside("[data-testid=error]", "#side")],
            [PASSED, PASSED],
        ),
        (
            '<div id="area"></div>',
            [inside(".error", "#area"), inside(".error", "#gone")],
            [(a1("binding_unresolved", ("no match", "no scope")), "failed"), PASSED],
        ),
        (
            ERROR,
            [inside(".old", "#area"), inside(".error", "#area")],
            [FALLBACK_SHOWN, FALLBACK_SHOWN],
        ),
    ],
    ids=["absent", "missing scope", "visible fallback"],
)
def test_a_replayed_negative_check_under_every_locator(
    app: fx.App,
    tmp_path: Path,
    html: str,
    locators: list[dict[str, Any]],
    expected: list[Outcome],
) -> None:
    assert outcomes(app, tmp_path, script(locators, NOT_VISIBLE), html) == expected


@pytest.mark.parametrize(
    ("every", "expected"),
    [(True, (a1("failed", locator_index=0), "failed")), (False, PASSED)],
    ids=["every locator", "default"],
)
def test_several_visible_matches_that_disappear_still_fail_a_replayed_negative_check(
    app: fx.App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    every: bool,
    expected: Outcome,
) -> None:
    gone = "() => document.querySelectorAll('.error').forEach((e) => e.remove())"
    # Both matches are removed once the first count of them returns.
    fired = around(monkeypatch, ".error", (1, NOTHING, gone))
    two = '<div id="area"><p class="error">A</p><p class="error">B</p></div>'

    done = replayed(app, tmp_path, ERROR_IN_AREA, two, every=every)

    assert (*done.result.assertions, done.result.outcome) == expected
    assert fired == [1]


@pytest.mark.parametrize(
    ("change", "expected"),
    [((1, AREA, RESTORE), [NO_SCOPE, Shown(0)]), (CONTROL, [Shown(0)])],
    ids=["scope removed around read 1", "unchanged"],
)
def test_a_scope_removed_and_restored_around_a_replayed_negative_read_is_never_absence(
    app: fx.App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: tuple[int, str, str],
    expected: list[object],
) -> None:
    fired = around(monkeypatch, ".error", change)
    found = looks(monkeypatch)

    done = replayed(app, tmp_path, ERROR_IN_AREA, ERROR, every=True)

    assert found == expected
    assert done.result.assertions == (a1("failed", locator_index=0),)
    assert done.result.outcome == "failed"
    assert fired == ([1] if change[1] else [])


FIRST_AGREED = (a1("pass", locator_index=0), "passed")
FALLBACK_ALONE = (a1("binding_unresolved", ("no match", "not unanimous")), "failed")


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        ({"css": "#title"}, [FIRST_AGREED, FIRST_AGREED]),
        ({"css": "#gone"}, [FALLBACK_ALONE, (a1("pass", locator_index=1), "passed")]),
    ],
    ids=["all agree", "a fallback alone"],
)
def test_every_locator_reports_the_first_locator_for_a_target_all_agree_on(
    app: fx.App, tmp_path: Path, first: dict[str, str], expected: list[Outcome]
) -> None:
    page = '<h1 id="title" data-testid="heading">Hello</h1>'
    hello = {"check": "text_in_target", "text": "Hello"}
    title = script([first, {"testid": "heading"}], hello)
    assert outcomes(app, tmp_path, title, page) == expected
