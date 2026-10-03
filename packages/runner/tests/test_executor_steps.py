"""The M1 strict executor's steps (#46; ADR-0024's #46 amendment, "The
executor"): the start URL and navigate paths, what M1 refuses to run,
resolution within its budget, bounded waits, and what stops a run. The
browser tests launch real Chromium on the OS that runs them: Linux in CI,
macOS locally."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import Target
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError
from aqa_runner import executor
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import DocumentChangedError, PolicyEvent
from aqa_runner.locators import Use
from playwright.async_api import ElementHandle

from packages.runner.tests.egress_fixtures import unused_port
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    App,
    a_spec,
    by_role,
    compiled,
    read_steps,
    run,
    serving_app,
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def test_the_first_navigation_goes_to_the_start_url_and_navigate_paths_join_as_written(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [
            {
                "seq": 1,
                "action": "navigate",
                "url": "/raw/%2f%2fwhere",
                "side_effect": False,
            },
            {
                "seq": 2,
                "action": "navigate",
                "url": "/raw/a?b=%41#c",
                "side_effect": False,
            },
        ]
    )

    spec = a_spec(tmp_path, ProjectConfig(), start_url="/raw/start?at=%2F")
    result = run(app, tmp_path, script, spec=spec).result

    # Each path as written, never decoded or resolved, on the start origin.
    assert app.paths() == ["/raw/start?at=%2F", "/raw/%2f%2fwhere", "/raw/a?b=%41"]
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
        (2, "completed"),
    ]


def test_unsupported_steps_and_checks_are_refused_by_name_before_the_browser_opens(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            },
            {
                "seq": 2,
                "action": "fill_secret",
                "target": "name",
                "secret": "TEST_PASSWORD",
                "side_effect": False,
            },
            # Tab held down could move the focus before the key goes.
            {"seq": 3, "action": "press", "key": "Tab+a", "side_effect": False},
        ],
        targets=FORM_TARGETS,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"},
            {
                "id": "a2",
                "expect_index": 0,
                "check": "network_none",
                "method": "POST",
                "url_pattern": "/write",
                "status_class": "2xx",
            },
            {
                "id": "a3",
                "expect_index": 0,
                "check": "network_seen",
                "method": "GET",
                "url_pattern": "/did",
                "status_class": "2xx",
            },
            {
                "id": "a4",
                "expect_index": 0,
                "check": "probe_equals_baseline",
                "probe": "count",
            },
            {
                "id": "a5",
                "expect_index": 0,
                "check": "visible_unoccluded",
                "target": "save",
                "min_size_px": [44, 24],
                "in_viewport": True,
            },
        ],
    )
    opened: list[object] = []

    def no_browser(*args: object, **options: object) -> None:
        opened.append((args, options))
        raise AssertionError("the browser was opened")

    monkeypatch.setattr(executor, "open_browser_session", no_browser)

    with pytest.raises(SpecError) as refused:
        run(app, tmp_path, script)

    assert refused.value.problems == (
        "steps[1] (seq 2): fill_secret is not run until #49",
        "steps[2] (seq 3): press takes one key, with only modifiers held before it: 'Tab+a'",
        "assertions[1] (a2): network_none is not evaluated until #48",
        "assertions[2] (a3): network_seen is not evaluated until #48",
        "assertions[3] (a4): probe_equals_baseline is not evaluated until #48",
        "assertions[4] (a5): visible_unoccluded is not evaluated until #48",
    )
    assert opened == []
    assert app.paths() == []


def test_a_step_whose_target_never_resolves_stops_the_run_at_that_step(
    app: App, tmp_path: Path
) -> None:
    targets = FORM_TARGETS | {
        "gone": {
            "semantic": "a button the form doesn't have",
            "locators": [by_role("button", "Gone"), {"css": "#gone"}],
        }
    }
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            },
            {"seq": 2, "action": "click", "target": "gone", "side_effect": False},
            {
                "seq": 3,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/save",
            },
        ],
        targets=targets,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"},
            {"id": "a2", "expect_index": 0, "check": "text_visible", "text": "Saved"},
        ],
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    done = run(app, tmp_path, script, config=config)

    result = done.result
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
        (2, "drifted"),
    ]
    assert result.steps[2].misses == ("no match", "no match")
    # Nothing was dispatched for it: no intent, and the save never reached the app.
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0, 1, 1]
    assert ("POST", "/write/save") not in app.seen
    assert [(a.id, a.outcome, a.stopped_at) for a in result.assertions] == [
        ("a1", "not_evaluated", 2),
        ("a2", "not_evaluated", 2),
    ]
    assert result.outcome == "failed"


def test_a_lookup_that_hangs_ends_with_the_budget(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As is_enabled can for an element moved into another document
    # (ADR-0025's #52 amendment).
    async def hangs(_: ElementHandle) -> bool:
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(ElementHandle, "is_enabled", hangs)
    script = compiled(
        [
            {
                "seq": 1,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/save",
            }
        ],
        targets=FORM_TARGETS,
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    started = time.monotonic()

    result = run(app, tmp_path, script, config=config).result

    # The look was cut off at the budget, before any locator's miss was known.
    assert [(step.seq, step.outcome, step.misses) for step in result.steps] == [
        (0, "completed", ()),
        (1, "drifted", ()),
    ]
    assert time.monotonic() - started < 20


def test_a_lookup_the_page_changed_under_is_made_again(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve: Callable[..., Awaitable[Any]] = BrowserSession.resolve
    looks: list[str] = []

    async def changed_once(session: BrowserSession, target: Target, use: Use) -> Any:
        looks.append(target.semantic)
        if len(looks) == 1:
            raise DocumentChangedError
        return await resolve(session, target, use)

    monkeypatch.setattr(BrowserSession, "resolve", changed_once)
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )

    result = run(app, tmp_path, script).result

    assert looks == ["the name field", "the name field"]
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]


def test_a_timeout_a_lookup_raises_itself_is_not_taken_for_the_budget(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def times_out(*_: object) -> Any:
        raise TimeoutError("the lookup's own")

    monkeypatch.setattr(BrowserSession, "resolve", times_out)
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )

    with pytest.raises(TimeoutError, match="the lookup's own"):
        run(app, tmp_path, script)


def test_a_dispatch_that_raises_stops_the_run_and_leaves_its_intent_unresolved(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [
            # A button takes no text: the session's fill raises.
            {
                "seq": 1,
                "action": "fill",
                "target": "save",
                "value": "Ada",
                "side_effect": False,
            },
            {
                "seq": 2,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/save",
            },
        ],
        targets=FORM_TARGETS,
    )

    done = run(app, tmp_path, script)

    result = done.result
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "failed"),
    ]
    assert "didn't take the value" in (result.steps[1].error or "")
    # Its outcome is unknown: an intent, and no completion after it.
    assert [(line["seq"], line["state"]) for line in read_steps(done.record.path)] == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
    ]
    assert [(a.outcome, a.stopped_at) for a in result.assertions] == [
        ("not_evaluated", 1)
    ]
    assert result.outcome == "errored"


def test_a_step_that_lands_off_the_allowed_origins_stops_the_run_errored(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "away": {"semantic": "the away link", "locators": [by_role("link", "Away")]}
    }
    script = compiled(
        [
            {"seq": 1, "action": "click", "target": "away", "side_effect": False},
            {"seq": 2, "action": "reload", "side_effect": False},
        ],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/away")

    done = run(app, tmp_path, script, spec=spec)

    result = done.result
    # Routing refused the host, so the page became the browser's error page,
    # which settling refused to look at.
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "failed"),
    ]
    assert result.policy_events == (
        PolicyEvent("document", "chrome-error://chromewebdata/", None),
    )
    assert result.outcome == "errored"


def test_a_popup_off_the_allowed_origins_makes_the_run_errored(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "pop": {"semantic": "the pop button", "locators": [by_role("button", "Pop")]}
    }
    script = compiled(
        [{"seq": 1, "action": "click", "target": "pop", "side_effect": False}],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/away")

    result = run(app, tmp_path, script, spec=spec).result

    # The click itself completed; the popup it opened is a policy event.
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]
    assert [event.kind for event in result.policy_events] == ["popup"]
    assert result.outcome == "errored"


def test_an_unreachable_start_origin_ends_the_run_errored_before_any_step(
    app: App, tmp_path: Path
) -> None:
    nowhere = f"http://127.0.0.1:{unused_port()}"
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )

    result = run(app, tmp_path, script, start=nowhere).result

    assert [step.seq for step in result.steps] == [0]
    assert [(event.host, event.port) for event in result.infrastructure_events] == [
        ("127.0.0.1", int(nowhere.rsplit(":", 1)[1]))
    ]
    assert [(a.outcome, a.stopped_at) for a in result.assertions] == [
        ("not_evaluated", 0)
    ]
    assert result.outcome == "errored"


def test_an_action_waits_no_longer_than_resolve_seconds(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        # Playwright waits for an option to appear; this one never does.
        [
            {
                "seq": 1,
                "action": "select",
                "target": "size",
                "option": "XL",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    started = time.monotonic()

    result = run(app, tmp_path, script, config=config).result

    step = result.steps[1]
    assert step.outcome == "failed"
    assert "Timeout 1000ms exceeded" in (step.error or "")
    assert time.monotonic() - started < 20
    assert result.outcome == "errored"


def test_a_navigation_waits_up_to_its_own_bound_not_resolve_seconds(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Playwright's default, made explicit (the coordinator's ruling on #46).
    assert executor.NAVIGATION_SECONDS == 30
    monkeypatch.setattr(executor, "NAVIGATION_SECONDS", 2)
    script = compiled(
        # The app holds this document until the test ends.
        [
            {
                "seq": 1,
                "action": "navigate",
                "url": "/held/document",
                "side_effect": False,
            }
        ]
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    result = run(app, tmp_path, script, config=config).result

    step = result.steps[1]
    assert step.outcome == "failed"
    assert "Timeout 2000ms exceeded" in (step.error or "")
