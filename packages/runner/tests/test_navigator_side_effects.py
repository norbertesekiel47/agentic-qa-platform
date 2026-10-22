"""`side_effect` inference through the attempt's tools (ADR-0025's #54
amendment): each acceptance criterion in real Chromium against the
executor's fixture app, through the egress proxy. The flags are inferred
once the session's browser has closed and the proxy's phase has ended, as
explore infers them."""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.coverage_plan import CoveragePlan
from aqa_runner import settling
from aqa_runner.browser_session import BrowserSession
from aqa_runner.navigator import AssertCheck, Click, Finish, Navigate
from aqa_runner.navigator_tools import Action, Attempt

from packages.core.tests.test_compiler import BY
from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.navigator_fixtures import (
    bound,
    explored,
    journal,
    on_app,
    ref,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE
from packages.runner.tests.test_routing import socket_server

type Flags = list[tuple[bool, str | None]]

BASIS = "no positive evidence that the step changes nothing"
BUSY = "settle: the page was still busy when settling's wait ran out"
WROTE = "network: a write request"
LATER = "network: a write request came later, which it may have caused"
TUNNEL = (
    "network: an encrypted or WebSocket connection was open, which the egress "
    "proxy can't read"
)
DISAGREED = "model: finish didn't list it in changes_nothing"
SAVE = "the form's save button"

# A plan whose one check reads the page's URL, so finish binds no element.
ON_A_PAGE = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": "the page",
                "claim": "is one of the app's pages",
                "checks": [{"check": "url_matches", "pattern": "/page/"}],
            }
        ],
        "requires": [],
    }
)
# A form posted into a popup that never commits (the app answers 204), so no
# page event reports it (measured, v1's probe); a write to the app under
# another name; a page that keeps changing once clicked; a write that waits
# on a stream, which never holds a window open, until the test lets the app
# answer it; and a button 4,000 px down whose page scroll handler writes once.
PAGES_HERE = {
    "far": """<div style="height: 4000px">Top</div><button>Far</button><script>
        addEventListener("scroll", () => fetch("/write/scrolled", {method: "POST"}),
            {once: true});
    </script>""",
    "popup": """<form method="post" target="_blank" action="/write/popup">
        <button>Send</button></form>""",
    "elsewhere": """<button onclick="fetch(location.origin.replace('127.0.0.1',
        'localhost') + '/write/other', {method: 'POST', mode: 'no-cors'})">Send</button>""",
    "restless": """<button onclick="setInterval(() => {
        document.title = String(Date.now()); }, 100)">Start</button><title>0</title>""",
    "deferred": """<button id="schedule">Schedule</button>
        <button onclick="fetch('/did/next')">Next</button><script>
        document.querySelector("#schedule").addEventListener("click", () => {
            const later = new EventSource("/held/later");
            later.onerror = () => {
                later.close();
                fetch("/write/later", {method: "POST"});
            };
        });
    </script>""",
}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    for name, page in PAGES_HERE.items():
        monkeypatch.setitem(PAGES, name, page)
    with serving_app() as served:
        yield served


async def arrived(app: App, path: str) -> None:
    await until(lambda: ("POST", path) in app.seen)


async def acted(attempt: Attempt, *calls: Action | Callable[[Attempt], Action]) -> None:
    for call in calls:
        done = await attempt.act(call(attempt) if callable(call) else call)
        assert done.step is not None, done.text
        await attempt.observe()


async def finished(attempt: Attempt, kept: list[int], agreed: list[int]) -> Attempt:
    await attempt.check(AssertCheck(check_id="a1", ref=None))
    call = Finish(steps=tuple(kept), changes_nothing=tuple(agreed))
    done = await attempt.finish(call, BY)
    assert done.script is not None, done.text
    return attempt


def inferred(attempt: Attempt) -> Flags:
    """The flags of the script `attempt`'s finish compiled, inferred now."""
    result = attempt.inferred()
    assert result is not None
    return [(step.side_effect, step.side_effect_basis) for step in result.script.steps]


def run(
    app: App, tmp_path: Path, *calls: Any, agreed: list[int], **setup: Any
) -> Flags:
    async def scenario(attempt: Attempt) -> Attempt:
        await acted(attempt, *calls)
        return await finished(attempt, list(range(1, len(calls) + 1)), agreed)

    return inferred(on_app(app, tmp_path, scenario, plan=ON_A_PAGE, **setup))


def save(attempt: Attempt) -> Action:
    return Click(ref=ref(attempt, "button", "Save"), meaning=SAVE)


def test_a_plain_navigation_with_the_models_agreement_compiles_false(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> Attempt:
        await acted(attempt, Navigate(path="/page/form"))
        return await finished(attempt, [1], [1])

    attempt = on_app(app, tmp_path, scenario, plan=ON_A_PAGE, page="/page/start")

    assert inferred(attempt) == [(False, None)]
    # Its intent was written before any evidence, so it stays conservative.
    assert [line.get("side_effect") for line in journal(attempt)] == [True, None]


def test_finish_alone_compiles_every_step_a_side_effect_step(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> Flags:
        await acted(attempt, Navigate(path="/page/form"))
        await attempt.check(AssertCheck(check_id="a1", ref=None))
        done = await attempt.finish(Finish(steps=(1,), changes_nothing=(1,)), BY)
        assert done.script is not None, done.text
        return [(s.side_effect, s.side_effect_basis) for s in done.script.steps]

    assert on_app(app, tmp_path, scenario, plan=ON_A_PAGE, page="/page/start") == [
        (True, BASIS)
    ]


def test_a_post_keeps_the_step_true_naming_it(app: App, tmp_path: Path) -> None:
    assert run(app, tmp_path, save, agreed=[1]) == [(True, WROTE)]


def test_a_write_to_another_host_keeps_the_step_true_naming_it(
    app: App, tmp_path: Path
) -> None:
    other = app.origin.replace("127.0.0.1", "localhost")
    egress = gate(
        allowed=(app.origin, other),
        private=(other,),
        answers={"localhost": ["127.0.0.1"]},
    )

    def send(attempt: Attempt) -> Action:
        return Click(ref=ref(attempt, "button", "Send"), meaning="the send button")

    flags = run(app, tmp_path, send, agreed=[1], page="/page/elsewhere", egress=egress)

    assert flags == [(True, WROTE)]
    assert ("POST", "/write/other") in app.seen


def test_a_settle_timeout_keeps_the_step_true_naming_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 1)

    def start(attempt: Attempt) -> Action:
        return Click(ref=ref(attempt, "button", "Start"), meaning="the start button")

    assert run(app, tmp_path, start, agreed=[1], page="/page/restless") == [
        (True, BUSY)
    ]


def test_a_write_that_arrives_just_before_the_next_action_is_its_steps(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> Attempt:
        await acted(attempt, save)
        await arrived(app, "/write/late")  # 1 s after the click, settled idle
        await asyncio.sleep(0.6)  # until Next shows
        await attempt.observe()
        nxt = Click(ref=ref(attempt, "button", "Next"), meaning="the next button")
        await acted(attempt, nxt)
        return await finished(attempt, [1, 2], [1, 2])

    attempt = on_app(app, tmp_path, scenario, plan=ON_A_PAGE, page="/page/late")

    assert inferred(attempt) == [(True, WROTE), (True, WROTE)]


def test_a_write_finding_the_element_causes_counts_against_its_step(
    app: App, tmp_path: Path
) -> None:
    # Resolution scrolls the off-screen button into view, before the click's
    # window opens, and the page's scroll handler writes.
    def far(attempt: Attempt) -> Action:
        return Click(ref=ref(attempt, "button", "Far"), meaning="the far button")

    assert run(app, tmp_path, far, agreed=[1], page="/page/far") == [(True, WROTE)]
    assert ("POST", "/write/scrolled") in app.seen


def test_only_the_window_just_before_counts_not_every_earlier_one(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> Attempt:
        quiet = Navigate(path="/page/form")
        await acted(attempt, save, quiet, quiet, quiet)
        return await finished(attempt, [1, 2, 3, 4], [1, 2, 3, 4])

    attempt = on_app(app, tmp_path, scenario, plan=ON_A_PAGE)

    # Step 2 follows the write's window; step 3 is two windows after it.
    assert inferred(attempt) == [
        (True, WROTE),
        (True, WROTE),
        (False, None),
        (False, None),
    ]


def test_a_write_delayed_past_the_next_kept_action_counts_against_both(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> Attempt:
        schedule = Click(ref=ref(attempt, "button", "Schedule"), meaning="schedule")
        await acted(attempt, schedule)
        nxt = Click(ref=ref(attempt, "button", "Next"), meaning="the next button")
        await acted(attempt, nxt)
        app.release("later")  # the stream ends, and the page writes
        await arrived(app, "/write/later")
        return await finished(attempt, [1, 2], [1, 2])

    attempt = on_app(app, tmp_path, scenario, plan=ON_A_PAGE, page="/page/deferred")

    assert inferred(attempt) == [(True, LATER), (True, WROTE)]


def test_a_model_that_disagrees_keeps_the_step_true_naming_it(
    app: App, tmp_path: Path
) -> None:
    flags = run(
        app, tmp_path, Navigate(path="/page/form"), agreed=[], page="/page/start"
    )

    assert flags == [(True, DISAGREED)]


def test_a_sign_in_click_compiles_true_because_it_posts(
    app: App, tmp_path: Path
) -> None:
    def sign_in(attempt: Attempt) -> Action:
        return Click(
            ref=ref(attempt, "button", "Sign in"), meaning="the sign-in form's button"
        )

    secret = bound(app.origin, FAKE_VALUE)
    flags = run(
        app, tmp_path, sign_in, agreed=[1], page="/page/signin", secrets=[secret]
    )

    assert flags == [(True, WROTE)]
    assert ("POST", "/write/signin") in app.seen


def test_a_write_only_the_egress_proxy_sees_counts_against_its_step(
    app: App, tmp_path: Path
) -> None:
    def send(attempt: Attempt) -> Action:
        return Click(ref=ref(attempt, "button", "Send"), meaning="the form's send")

    assert run(app, tmp_path, send, agreed=[1], page="/page/popup") == [(True, WROTE)]
    assert ("POST", "/write/popup") in app.seen


def test_a_write_after_finish_compiled_counts_against_the_step(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> Attempt:
        await acted(attempt, Navigate(path="/page/form"))
        await finished(attempt, [1], [1])
        await attempt.session.page.evaluate("fetch('/write/after', {method: 'POST'})")
        await arrived(app, "/write/after")
        return attempt

    attempt = on_app(app, tmp_path, scenario, plan=ON_A_PAGE, page="/page/start")

    assert inferred(attempt) == [(True, WROTE)]


def test_a_session_its_phase_never_told_of_writes_gives_no_positive_evidence(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BrowserSession, "watch", lambda _session, _egress: None)

    flags = run(
        app, tmp_path, Navigate(path="/page/form"), agreed=[1], page="/page/start"
    )

    assert flags == [(True, BASIS)]


# A button that changes nothing, and buttons that open a WebSocket to the
# page's server and send a Blob on it, which Playwright 1.63 never reports
# as sent (measured, v1's probe).
SOCKET_BUTTONS = """() => document.body.innerHTML = `
    <button onclick="document.title = 'marked'">Mark</button>
    <button onclick="window.s = new WebSocket(
        location.origin.replace('http', 'ws') + '/socket')">Open</button>
    <button onclick="window.s.send(new Blob(['only']))">Send</button>`"""


def test_a_websocket_keeps_its_steps_and_every_earlier_one_true(
    tmp_path: Path,
) -> None:
    async def scenario(attempt: Attempt) -> Attempt:
        await attempt.session.page.evaluate(SOCKET_BUTTONS)
        await attempt.observe()

        def marked(at: Attempt) -> Action:
            return Click(ref=ref(at, "button", "Mark"), meaning="the mark button")

        def opened(at: Attempt) -> Action:
            return Click(ref=ref(at, "button", "Open"), meaning="the open button")

        def sent(at: Attempt) -> Action:
            return Click(ref=ref(at, "button", "Send"), meaning="the send button")

        await acted(attempt, marked, opened, sent)
        return await finished(attempt, [1, 2, 3], [1, 2, 3])

    async def served() -> tuple[Attempt, list[str]]:
        async with socket_server() as server:
            start = f"http://127.0.0.1:{server.port}"
            attempt = await asyncio.to_thread(
                explored,
                tmp_path,
                scenario,
                start,
                plan=ON_A_PAGE,
                page="/page/sockets",
            )
            return attempt, [kind for kind, _ in server.received if kind != "control"]

    attempt, received = asyncio.run(served())

    # The mark came first, and its effects may open the socket.
    assert inferred(attempt) == [(True, TUNNEL)] * 3
    assert received == ["binary"]
