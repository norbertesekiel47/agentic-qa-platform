"""`aqa_runner.executor.evaluate`, one assertion on a browser session as it
stands (ADR-0024's #53 P9+P10 amendment): each outcome DATA_MODEL §7 names,
and nothing evaluated on a page that is off the run's allowed origins or
has stopped answering, whatever the check reads. The browser tests launch
real Chromium against the executor's fixture app."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner import probes
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.document_origins import PolicyEvent
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import AssertionResult, Evaluation, evaluate
from aqa_runner.redaction import NO_SECRETS
from playwright.async_api import async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import App, a_spec, compiled, serving_app

TARGETS = {
    "never": {"semantic": "text that never comes", "locators": [{"css": "#never"}]},
    "save": {"semantic": "the save button", "locators": [{"css": "button.save"}]},
}

PROBE: dict[str, Any] = {
    "check": "probe_equals",
    "probe": "count",
    "json_path": "$.count",
    "value": 2,
}

# One check of each kind the page's state can't stand in for: recorded
# traffic, a probe read through the gate, the URL, and an element.
KINDS: dict[str, dict[str, Any]] = {
    "network": {
        "check": "network_seen",
        "method": "POST",
        "url_pattern": "/write/save",
        "status_class": "2xx",
    },
    "probe": PROBE,
    "url": {"check": "url_matches", "pattern": "/"},
    "element": {"check": "text_in_target", "target": "save", "text": "Save"},
}

type Prepare = Callable[[BrowserSession], Awaitable[None]]


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setattr(probes, "READ_SECONDS", 0.02)
    with serving_app() as served:
        served.probes["count"] = [2]
        yield served


async def stay(_: BrowserSession) -> None:
    pass


def evaluated(
    app: App,
    tmp_path: Path,
    check: dict[str, Any],
    *,
    page: str = "/page/form",
    prepare: Prepare = stay,
) -> tuple[AssertionResult, tuple[PolicyEvent, ...]]:
    """`check`, as assertion a1, evaluated once on `page` after `prepare`,
    with a budget of 1 s; and the policy events the session recorded."""
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url=page,
        probes={"count": "GET /probe/count"},
    )
    script = compiled(
        [], targets=TARGETS, assertions=[{"id": "a1", "expect_index": 0, **check}]
    )
    run_gate = gate(allowed=(app.origin,))

    async def scenario() -> tuple[AssertionResult, tuple[PolicyEvent, ...]]:
        async with (
            async_playwright() as playwright,
            EgressProxy(run_gate) as proxy,
            open_browser_session(
                playwright.chromium, egress=proxy, redactor=NO_SECRETS
            ) as session,
            asyncio.timeout(30),
        ):
            await session.navigate(f"{app.origin}{page}")
            await prepare(session)
            evaluation = Evaluation(
                targets=script.targets,
                spec=spec,
                start=app.origin,
                gate=run_gate,
                budget=1,
                redactor=NO_SECRETS,
            )
            result = await evaluate(session, script.assertions[0], evaluation)
            return result, tuple(session.policy_events.kept)

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("check", "values", "expected"),
    [
        (
            {"check": "url_matches", "pattern": "/page/form$"},
            [2],
            AssertionResult("a1", "pass"),
        ),
        (
            {"check": "url_matches", "pattern": "/page/elsewhere$"},
            [2],
            AssertionResult("a1", "failed"),
        ),
        (
            {"check": "text_in_target", "target": "never", "text": "never"},
            [2],
            AssertionResult("a1", "binding_unresolved", ("no match",)),
        ),
        # The probe answers another value every read, so it never holds still.
        (PROBE, [], AssertionResult("a1", "check_timed_out")),
        (
            {**PROBE, "json_path": "$.missing"},
            [2],
            AssertionResult("a1", "not_evaluated", error="the probe could not be read"),
        ),
    ],
    ids=["pass", "failed", "binding_unresolved", "check_timed_out", "not_evaluated"],
)
def test_evaluate_gives_each_outcome_for_one_assertion(
    app: App,
    tmp_path: Path,
    check: dict[str, Any],
    values: list[object],
    expected: AssertionResult,
) -> None:
    app.probes["count"] = values

    result, events = evaluated(app, tmp_path, check)

    assert result == expected
    assert events == ()


async def left(session: BrowserSession) -> None:
    # The leaves page goes to a host the run doesn't allow, whose refusal
    # leaves Chromium's error page, on no origin a run could allow.
    await until(lambda: session.page.url.startswith("chrome-error:"))


@pytest.mark.parametrize("kind", KINDS)
def test_each_check_kind_is_refused_on_a_forbidden_document(
    app: App, tmp_path: Path, kind: str
) -> None:
    result, events = evaluated(
        app, tmp_path, KINDS[kind], page="/page/leaves", prepare=left
    )

    assert result == AssertionResult(
        "a1",
        "not_evaluated",
        error="the page is on no origin a run could allow: nothing there is "
        "observed or acted on; navigate to an allowed origin, or restart",
    )
    assert events[-1] == PolicyEvent("document", "chrome-error://chromewebdata/", None)
    assert app.probe_reads == {}


@pytest.mark.parametrize("kind", KINDS)
def test_each_check_kind_is_not_evaluated_on_a_page_that_stopped_answering(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    async def never(_: BrowserSession) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(BrowserSession, "visible_text", never)
    started = time.monotonic()

    result, events = evaluated(app, tmp_path, KINDS[kind])

    assert result == AssertionResult(
        "a1", "not_evaluated", error="the page didn't answer within 2 s"
    )
    assert events == ()
    assert app.probe_reads == {}
    assert time.monotonic() - started < 20
