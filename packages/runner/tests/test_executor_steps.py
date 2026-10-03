"""The M1 strict executor's steps (#46; ADR-0024's #46 amendment, "The
executor"): the start URL and navigate paths, what M1 refuses to run,
resolution within its budget, bounded waits, and what stops a run. The
browser tests launch real Chromium on the OS that runs them: Linux in CI,
macOS locally."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from aqa_core.compiled import Target
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError
from aqa_runner import executor
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import DocumentChangedError, PolicyEvent
from aqa_runner.egress_proxy import EgressBlocks, RefusedHost
from aqa_runner.locators import Unresolved, Use
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


def test_a_blank_popup_doesnt_end_the_run(app: App, tmp_path: Path) -> None:
    script = compiled(
        [{"seq": 1, "action": "click", "target": "pop", "side_effect": False}],
        targets={
            "pop": {
                "semantic": "the pop button",
                "locators": [by_role("button", "Pop")],
            }
        },
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/blank-popup")

    result = run(app, tmp_path, script, spec=spec).result

    assert [event.kind for event in result.policy_events] == ["popup"]
    assert [step.outcome for step in result.steps] == ["completed", "completed"]
    assert result.outcome == "passed"
    assert [assertion.outcome for assertion in result.assertions] == ["pass"]
    assert result.error_code is None


@pytest.mark.parametrize("redirect", [False, True], ids=["routing", "gate"])
def test_a_request_to_an_undeclared_host_ends_the_run_egress_blocked(
    app: App, tmp_path: Path, redirect: bool
) -> None:
    destination = "http://undeclared.example.test/x"
    if redirect:
        destination = f"/redirect?to={quote(destination, safe='')}"
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url=f"/page/fetches?to={quote(destination, safe='')}",
    )
    script = compiled([{"seq": 1, "action": "reload", "side_effect": False}])

    done = run(app, tmp_path, script, spec=spec)

    result = done.result
    assert [step.seq for step in result.steps] == [0]
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0]
    assert result.outcome == "errored"
    assert result.error_code == "egress_blocked"
    assert result.egress_blocks == EgressBlocks(
        (RefusedHost("undeclared.example.test", 80),), False
    )
    assert {i.name for i in result.invariants if i.outcome == "violated"} == {
        "js_exceptions"
    }
    assert json.loads((done.record.path / "egress.json").read_text()) == {
        "error_code": "egress_blocked",
        "refused": [{"host": "undeclared.example.test", "port": 80}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }


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
            # Tab held down could move the focus before the key goes.
            {"seq": 3, "action": "press", "key": "Tab+a", "side_effect": False},
            # Every modifier held before one key is a key press can take.
            {
                "seq": 4,
                "action": "press",
                "key": "Shift+Control+Alt+Meta+ControlOrMeta+a",
                "side_effect": False,
            },
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
        "steps[1] (seq 3): press takes one key, with only modifiers held before it: 'Tab+a'",
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
    assert result.error_code == "egress_blocked"


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

    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]
    assert [event.kind for event in result.policy_events] == ["popup"]
    assert result.outcome == "errored"
    assert result.error_code == "egress_blocked"


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

    result = run(app, tmp_path, script, origins=(nowhere,)).result

    assert [step.seq for step in result.steps] == [0]
    assert [(event.host, event.port) for event in result.infrastructure_events] == [
        ("127.0.0.1", int(nowhere.rsplit(":", 1)[1]))
    ]
    assert [(a.outcome, a.stopped_at) for a in result.assertions] == [
        ("not_evaluated", 0)
    ]
    assert result.outcome == "errored"
    assert result.error_code is None


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


def test_a_lookup_that_meets_a_page_off_the_allowed_origins_stops_the_run_errored(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "never": {
            "semantic": "a button that never comes",
            "locators": [by_role("button", "Never")],
        }
    }
    script = compiled(
        [{"seq": 1, "action": "click", "target": "never", "side_effect": False}],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/leaves")

    done = run(app, tmp_path, script, spec=spec)

    result = done.result
    # The look itself was refused; nothing was dispatched, so no intent.
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "failed"),
    ]
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0]
    assert result.policy_events == (
        PolicyEvent("document", "chrome-error://chromewebdata/", None),
    )
    assert [(a.outcome, a.stopped_at) for a in result.assertions] == [
        ("not_evaluated", 1)
    ]
    assert result.outcome == "errored"
    assert result.error_code == "egress_blocked"


def test_an_event_during_a_lookup_stops_the_run_before_the_step_is_dispatched(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "pay": {"semantic": "the pay button", "locators": [by_role("button", "Pay")]}
    }
    script = compiled(
        [
            {
                "seq": 1,
                "action": "click",
                "target": "pay",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/pay",
            }
        ],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/popup-then-pay")

    done = run(app, tmp_path, script, spec=spec)

    result = done.result
    assert [(step.seq, step.outcome) for step in result.steps] == [(0, "completed")]
    assert [event.kind for event in result.policy_events] == ["popup"]
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0]
    assert ("POST", "/write/pay") not in app.seen
    assert result.outcome == "errored"
    assert result.error_code == "egress_blocked"


def test_an_action_the_page_never_lets_finish_ends_with_the_budget(
    app: App, tmp_path: Path
) -> None:
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
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    spec = a_spec(tmp_path, config, start_url="/page/stall")
    started = time.monotonic()

    done = run(app, tmp_path, script, config=config, spec=spec)

    step = done.result.steps[1]
    assert step.outcome == "failed"
    assert "didn't finish" in (step.error or "")
    # Dispatched, with its outcome unknown: the intent stays unresolved.
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0, 1]
    assert time.monotonic() - started < 20


def test_a_failed_steps_reason_holds_only_a_bounded_line_of_what_the_page_said(
    app: App, tmp_path: Path
) -> None:
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
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/throws")

    step = run(app, tmp_path, script, spec=spec).result.steps[1]

    assert step.outcome == "failed"
    error = step.error or ""
    assert error.startswith("Error: ElementHandle.evaluate: Error: ")
    assert "\x1b" not in error
    assert len(error) <= 200


def test_an_infrastructure_event_after_a_step_stops_the_run_errored(
    app: App, tmp_path: Path
) -> None:
    nowhere = f"http://127.0.0.1:{unused_port()}"
    script = compiled([{"seq": 1, "action": "reload", "side_effect": False}])
    # The start page fetches from another allowed origin, which nothing serves.
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url=f"/page/fetches?to={quote(nowhere, safe='')}",
    )

    done = run(app, tmp_path, script, spec=spec, origins=(app.origin, nowhere))

    result = done.result
    # The start URL's step completed; the reload after it never ran.
    assert [(step.seq, step.outcome) for step in result.steps] == [(0, "completed")]
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0]
    assert [(event.host, event.port) for event in result.infrastructure_events] == [
        ("127.0.0.1", int(nowhere.rsplit(":", 1)[1]))
    ]
    assert [(a.outcome, a.stopped_at) for a in result.assertions] == [
        ("not_evaluated", 0)
    ]
    assert result.outcome == "errored"
    assert result.error_code is None


def test_an_error_that_isnt_the_pages_or_the_browsers_is_raised(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object) -> Any:
        raise RuntimeError("the runner's own fault")

    monkeypatch.setattr(BrowserSession, "click", broken)
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

    # Not a failed step: a fault in the runner is no outcome of the page's.
    with pytest.raises(RuntimeError, match="the runner's own fault"):
        run(app, tmp_path, script)


def test_a_target_is_looked_for_every_tenth_of_a_second(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    looks: list[float] = []

    async def never(*_: object) -> Any:
        looks.append(time.monotonic())
        return Unresolved(("no match",))

    monkeypatch.setattr(BrowserSession, "resolve", never)
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
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    result = run(app, tmp_path, script, config=config).result

    assert result.steps[1].outcome == "drifted"
    # About ten looks in the second, not a spin.
    assert 5 <= len(looks) <= 12
