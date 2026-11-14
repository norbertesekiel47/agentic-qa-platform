"""The "before" screenshot (#314; ADR-0033's #314 amendment): with
`RunSetup.before_screenshots`, which only `strict_replay` sets, when
`--before-screenshots` asks, the page is captured ahead of each compiled step,
before its baselines, its look and its intent, and held in memory. Only a
step that fails saves it, as `before_screenshot.png` beside its
`screenshot.png`, and what became of it is in `RunResult.before_shots`. A step
that completes or drifts drops its capture, seq 0 has none, and the capture
changes no result. Real Chromium against the executor's fixture app."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.spec import Spec
from aqa_runner import evidence, executor, probes
from aqa_runner.browser_session import BrowserSession
from aqa_runner.evidence import Shot
from aqa_runner.replay_result import ReplayEnd, present
from aqa_runner.screenshots import Screenshot, take_screenshot
from aqa_runner.settling import Window
from playwright.async_api import Error

from packages.runner.tests.executor_fixtures import (
    FORM_STEPS,
    FORM_TARGETS,
    PAGES,
    App,
    Run,
    a_spec,
    asking_screenshots,
    compiled,
    for_spec,
    run,
    serving_app,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE, secret_spec
from packages.runner.tests.test_replay_inserted_steps import (
    CLICK_CONTACT,
    MOVED,
    POST,
    contact_does,
    inserting,
    replayed,
)
from packages.runner.tests.test_replay_repairs import rebound

FILL = {
    "seq": 1,
    "action": "fill",
    "target": "name",
    "value": "Ada",
    "side_effect": False,
}
DRIFTS = compiled(
    [{"seq": 1, "action": "click", "target": "never", "side_effect": False}],
    targets={
        "never": {
            "semantic": "a button that never comes",
            "locators": [{"css": "#never"}],
        }
    },
)
# The drift is found sooner than the default 10 s.
QUICK = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
BEFORE = "evidence/1/before_screenshot.png"
AFTER = "evidence/1/screenshot.png"


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def pngs(record: Path) -> list[str]:
    """Each PNG file under `record`, by its path there."""
    return sorted(path.relative_to(record).as_posix() for path in record.rglob("*.png"))


def counting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A list that gets an entry for each capture evidence takes."""
    taken: list[int] = []

    async def counted(session: BrowserSession, *, record_refusal: bool) -> Screenshot:
        taken.append(len(taken))
        return await take_screenshot(session, record_refusal=record_refusal)

    monkeypatch.setattr(evidence, "take_screenshot", counted)
    return taken


def test_a_step_that_fails_saves_its_before_screenshot_beside_its_screenshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asking_screenshots(monkeypatch, before=True)
    # The page's own execCommand throws, so the fill's dispatch fails.
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/throws")

    script = compiled([FILL], targets=FORM_TARGETS)

    done = run(app, tmp_path, script, spec=spec)

    assert [step.outcome for step in done.result.steps] == ["completed", "failed"]
    assert done.result.shots == {1: Shot(AFTER)}
    assert done.result.before_shots == {1: Shot(BEFORE)}
    assert pngs(done.record.path) == [BEFORE, AFTER]
    result = done.result
    end = ReplayEnd(
        done.record,
        spec,
        for_spec(script, spec),
        result,
        result.egress_blocks,
        result.infrastructure_events,
    )
    verdicts = cast("list[dict[str, Any]]", present(end)["verdicts"])
    assert [verdict["evidence"]["before_screenshot"] for verdict in verdicts] == [
        None,
        {"path": BEFORE, "withheld": None},
    ]


def test_a_step_that_fails_before_its_dispatch_saves_its_before_screenshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unread(*_: object) -> None:
        raise probes.ProbeError("fake probe that couldn't be read")

    monkeypatch.setattr(executor, "capture_baselines", unread)
    asking_screenshots(monkeypatch, before=True)

    done = run(app, tmp_path, compiled([FILL], targets=FORM_TARGETS))

    assert [step.outcome for step in done.result.steps] == ["completed", "failed"]
    assert done.result.before_shots == {1: Shot(BEFORE)}
    assert pngs(done.record.path) == [BEFORE, AFTER]


def test_a_step_that_drifts_drops_its_before_screenshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asking_screenshots(monkeypatch, before=True)
    spec = a_spec(tmp_path, QUICK, start_url="/page/form")

    done = run(app, tmp_path, DRIFTS, config=QUICK, spec=spec)

    assert [step.outcome for step in done.result.steps] == ["completed", "drifted"]
    assert done.result.shots == {1: Shot(AFTER)}
    assert done.result.before_shots == {}
    assert pngs(done.record.path) == [AFTER]


def test_a_start_navigation_that_fails_has_no_before_screenshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navigate = BrowserSession.navigate

    async def failing(session: BrowserSession, url: str) -> Window:
        await navigate(session, url)
        raise Error("fake failure once the page loaded")

    monkeypatch.setattr(BrowserSession, "navigate", failing)
    asking_screenshots(monkeypatch, before=True)
    taken = counting(monkeypatch)

    done = run(app, tmp_path, compiled([FILL], targets=FORM_TARGETS))

    assert [step.outcome for step in done.result.steps] == ["failed"]
    assert done.result.before_shots == {}
    assert pngs(done.record.path) == ["evidence/0/screenshot.png"]
    # The one capture is the start navigation's own screenshot.
    assert len(taken) == 1


@pytest.mark.parametrize("before", [False, True], ids=["off", "on"])
def test_each_compiled_step_is_captured_once_and_only_when_asked(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: bool
) -> None:
    asking_screenshots(monkeypatch, before=before)
    taken = counting(monkeypatch)

    done = run(app, tmp_path, compiled(FORM_STEPS, targets=FORM_TARGETS))

    assert {step.outcome for step in done.result.steps} == {"completed"}
    assert len(taken) == (len(FORM_STEPS) if before else 0)
    assert (done.result.shots, done.result.before_shots) == ({}, {})
    assert pngs(done.record.path) == []


def test_a_step_a_repair_took_is_captured_again_and_its_drift_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = counting(monkeypatch)

    done = replayed(
        tmp_path,
        lambda drift: rebound(drift.script),
        html=MOVED,
        screenshots=True,
        before_screenshots=True,
    )

    assert [(step.seq, step.outcome) for step in done.result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]
    # The drifted attempt's capture, then the repaired step's, both dropped.
    assert len(taken) == 2
    assert (done.result.shots, done.result.before_shots) == ({}, {})
    assert pngs(done.record) == []


def test_a_step_a_repair_inserted_that_fails_saves_its_before_screenshot(
    tmp_path: Path,
) -> None:
    done = replayed(
        tmp_path,
        inserting(CLICK_CONTACT),
        html=contact_does(POST),
        screenshots=True,
        before_screenshots=True,
    )

    assert [(step.seq, step.outcome) for step in done.result.steps] == [
        (0, "completed"),
        (3, "failed"),
    ]
    # The drifted step the repair took keeps nothing of its attempt.
    assert done.result.shots == {3: Shot("evidence/3/screenshot.png")}
    assert done.result.before_shots == {3: Shot("evidence/3/before_screenshot.png")}
    assert pngs(done.record) == [
        "evidence/3/before_screenshot.png",
        "evidence/3/screenshot.png",
    ]


# A sign-in page whose size list breaks the page's own execCommand, which a
# fill types through, so a fill after the choice fails.
BREAKS = """<label>Password <input type="password" name="password"></label>
<label>Name <input name="name"></label>
<label>Size <select name="size" onchange="document.execCommand = () => {
    throw new Error('fake broken page');
}"><option>S</option><option>M</option></select></label>"""
BREAKS_TARGETS = {
    "password": {
        "semantic": "the password field",
        "locators": [{"css": "[name=password]"}],
    },
    "name": {"semantic": "the name field", "locators": [{"css": "[name=name]"}]},
    "size": {"semantic": "the size list", "locators": [{"css": "[name=size]"}]},
}
# The secret filled, two steps that complete, then a fill that fails.
SIGNS_IN_THEN_FAILS = [
    {
        "seq": 1,
        "action": "fill_secret",
        "target": "password",
        "secret": "TEST_PASSWORD",
        "side_effect": False,
    },
    {**FILL, "seq": 2},
    {
        "seq": 3,
        "action": "select",
        "target": "size",
        "option": "M",
        "side_effect": False,
    },
    {**FILL, "seq": 4, "value": "Grace"},
]


def compared(done: Run, script: CompiledScript, spec: Spec) -> tuple[object, ...]:
    """What a capture must leave as it was: each step's seq, outcome, locator,
    settling, misses and error; every assertion and invariant; the policy
    events and their total; each kept snapshot, as saved; and each verdict,
    its evidence without its "before"."""
    result = done.result
    end = ReplayEnd(
        done.record,
        spec,
        for_spec(script, spec),
        result,
        result.egress_blocks,
        result.infrastructure_events,
    )
    verdicts = cast("list[dict[str, Any]]", present(end)["verdicts"])
    snapshots = {
        path.relative_to(done.record.path).as_posix(): path.read_bytes()
        for path in sorted(done.record.path.rglob("a11y_snapshot.yaml"))
    }
    return (
        [
            (s.seq, s.outcome, s.locator_index, s.settled, s.misses, s.error)
            for s in result.steps
        ],
        result.assertions,
        result.invariants,
        result.policy_events,
        result.policy_events_total,
        snapshots,
        [
            verdict
            | {
                "evidence": {
                    key: value
                    for key, value in verdict["evidence"].items()
                    if key != "before_screenshot"
                }
            }
            for verdict in verdicts
        ],
    )


def test_the_before_screenshot_changes_no_result(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setitem(PAGES, "breaks", BREAKS)
    spec = secret_spec(tmp_path, start_url="/page/breaks")
    script = compiled(SIGNS_IN_THEN_FAILS, targets=BREAKS_TARGETS)
    done: dict[bool, Run] = {}
    for before in (False, True):
        with monkeypatch.context() as patch:
            asking_screenshots(patch, before=before)
            done[before] = run(app, tmp_path, script, spec=spec)

    off, on = done[False], done[True]
    assert [(step.seq, step.outcome) for step in on.result.steps] == [
        (0, "completed"),
        (1, "completed"),
        (2, "completed"),
        (3, "completed"),
        (4, "failed"),
    ]
    assert compared(on, script, spec) == compared(off, script, spec)
    # Only the failed step's capture is saved; the rest, renders once the
    # secret was filled, are dropped.
    assert on.result.before_shots == {4: Shot("evidence/4/before_screenshot.png")}
    assert off.result.before_shots == {}
    assert pngs(on.record.path) == [
        "evidence/4/before_screenshot.png",
        "evidence/4/screenshot.png",
    ]
    assert pngs(off.record.path) == ["evidence/4/screenshot.png"]
