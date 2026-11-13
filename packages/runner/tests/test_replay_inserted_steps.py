"""A step a repair inserted shows, as it runs, that it changed nothing (#246;
ADR-0024's #246 amendment): its own settle window ended `idle`, the egress
proxy heard every write and read none, no tunnel was open, and the page's main
frame didn't navigate. Otherwise the step fails and the run stops,
`inconclusive`, with no proposal. The rule alone, on hand-made windows; then
`replays.replay_run(..., repair=...)` in real Chromium, with scripted repairs
that insert a step before the drifted toggle's, on pages composed from #240's
moved page. No model and no cassette."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from aqa_core.compiled import Click, CompiledScript, Press, Step, Target
from aqa_core.project import load_config, load_spec
from aqa_runner import settling
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import RunResult
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.repairs import Drift, Offer, StepAt, inserted_failure
from aqa_runner.replay_result import ReplayEnd, present
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.run_record import RunRecord
from aqa_runner.settling import Settled, Window
from playwright.async_api import async_playwright

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.drift_fixtures import (
    DETAILS,
    SHIPPING,
    order_spec,
    page,
    serving,
)
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.result_redaction_fixtures import fake_secret
from packages.runner.tests.test_replay_repairs import rebound

# The reason's opening, and each fault's phrase, in the order a reason lists them.
UNSHOWN = "a repair inserted this step, and it didn't show it changed nothing: "
BUSY = "settle: the page was still busy when settling's wait ran out"
UNHEARD = (
    "network: the egress proxy didn't hear every write from the step's dispatch on"
)
WROTE = "network: a write request"
TUNNEL = (
    "network: an encrypted or WebSocket connection was open, which the egress "
    "proxy can't read"
)
NAVIGATED = "page: the page's main frame navigated"


@pytest.mark.parametrize(
    ("window", "settled", "phrase"),
    [
        pytest.param(Window(heard=True), "timeout", BUSY, id="busy"),
        pytest.param(Window(heard=False), "idle", UNHEARD, id="unheard"),
        pytest.param(Window(heard=True, wrote=True), "idle", WROTE, id="wrote"),
        pytest.param(
            Window(heard=True, tunnelled=True), "idle", TUNNEL, id="tunnelled"
        ),
        pytest.param(
            Window(heard=True, navigated=True), "idle", NAVIGATED, id="navigated"
        ),
    ],
)
def test_each_fault_alone_fails_an_inserted_step_with_its_phrase(
    window: Window, settled: Settled, phrase: str
) -> None:
    assert inserted_failure(window, settled, NO_SECRETS) == UNSHOWN + phrase


def test_an_idle_window_that_heard_everything_and_saw_nothing_passes() -> None:
    assert inserted_failure(Window(heard=True), "idle", NO_SECRETS) is None


def test_several_faults_are_listed_in_a_fixed_order() -> None:
    window = Window(heard=False, wrote=True, tunnelled=True, navigated=True)

    assert inserted_failure(window, "timeout", NO_SECRETS) == (
        UNSHOWN + f"{BUSY}; {UNHEARD}; {WROTE}; {TUNNEL}; {NAVIGATED}"
    )


def test_the_reason_is_scanned_with_the_sessions_redactor() -> None:
    redactor = Redactor([fake_secret("TEST_PASSWORD", "request", "http://app.test")])

    assert inserted_failure(Window(heard=True, wrote=True), "idle", redactor) == (
        UNSHOWN + "network: a write [SECRET:TEST_PASSWORD]"
    )


# The blob SHA the runs' proposals cite: `git hash-object` of `hello\\n`.
BLOB = "ce013625030ba8dba906f756967f9e9ca394464a"
MOVED = page("moved")
# The footer's contact button, which no committed step clicks: a committed
# target an inserted click can go through.
CONTACT = Target.model_validate(
    {
        "semantic": "the footer's contact button",
        "locators": [
            fx.by_role("button", "Contact us"),
            {"css": "button", "scope": {"css": "footer"}},
        ],
    }
)
# The toggle clicked, then the summary read and seen, with the contact button
# a target too.
WITH_CONTACT = DETAILS.model_copy(
    update={"targets": {**DETAILS.targets, "contact": CONTACT}}
)
# The steps a repair inserts: a click through the contact button, and Enter or
# Tab pressed on the page.
CLICK_CONTACT = Click(seq=3, action="click", target="contact", side_effect=False)
PRESS_ENTER = Press(seq=3, action="press", key="Enter", side_effect=False)
PRESS_TAB = Press(seq=3, action="press", key="Tab", side_effect=False)
# A write the app receives, and a request to a host the run doesn't allow.
POST = "fetch('/write/inserted', {method: 'POST'})"
UNDECLARED = "fetch('http://undeclared.example.test:8080/x').catch(() => 0)"


def contact_does(script: str, html: str = MOVED) -> str:
    """`html`, the moved page unless a test composes another, whose contact
    button runs `script` when it is clicked."""
    return html.replace(
        "<button>Contact us</button>",
        f'<button onclick="{script}">Contact us</button>',
    )


def enter_does(script: str, html: str = MOVED) -> str:
    """`html`, the moved page unless a test composes another, which runs
    `script` when Enter is pressed."""
    return html + (
        '<script>addEventListener("keydown", (event) => {'
        f' if (event.key === "Enter") {{ {script}; }} }});</script>'
    )


def inserting(step: Step) -> Callable[[Drift], CompiledScript]:
    """A repair that rebinds the toggle as the moved page has it, with `step`
    inserted right before the drifted step."""

    def offer(drift: Drift) -> CompiledScript:
        assert isinstance(drift.at, StepAt)
        moved = rebound(drift.script)
        seqs = [each.seq for each in moved.steps]
        at = seqs.index(drift.at.step.seq)
        steps = (*moved.steps[:at], step, *moved.steps[at:])
        return moved.model_copy(update={"steps": steps})

    return offer


@dataclass(frozen=True)
class Inserting:
    """A replay whose repair may insert a step: its result, its record's
    directory, each request the app saw, and how the run ended."""

    result: RunResult
    record: Path
    seen: list[tuple[str, str]]
    end: ReplayEnd

    @property
    def document(self) -> dict[str, Any]:
        """The run's `replay.json`, as a reader of its JSON gets it."""
        loaded: dict[str, Any] = json.loads(json.dumps(present(self.end)))
        return loaded

    def ended(self) -> tuple[object, ...]:
        """The document's outcome, exit code, proposal and non-resumable steps."""
        document = self.document
        keys = ("outcome", "exit_code", "heal_proposal", "non_resumable")
        return tuple(document[key] for key in keys)

    def verdict(self, seq: int) -> tuple[object, object]:
        """The document's verdict on step `seq`, and its reason."""
        verdicts: list[dict[str, Any]] = self.document["verdicts"]
        entry = next(each for each in verdicts if each.get("step") == seq)
        return entry["verdict"], entry["reason"]

    def outcomes(self) -> list[tuple[int, str, str | None]]:
        return [(step.seq, step.outcome, step.error) for step in self.result.steps]

    def states(self) -> list[tuple[object, object]]:
        return [(line["seq"], line["state"]) for line in fx.read_steps(self.record)]

    def logged(self, seq: int) -> list[tuple[str, str]]:
        """Each request step `seq`'s evidence logs: its method and URL's path."""
        log = json.loads((self.record / f"evidence/{seq}/network_log.json").read_text())
        return [
            (entry["method"], urlsplit(entry["url"]).path) for entry in log["entries"]
        ]


def replayed(
    tmp_path: Path,
    offer: Callable[[Drift], CompiledScript],
    *,
    html: str,
    script: CompiledScript = WITH_CONTACT,
) -> Inserting:
    """`script` replayed with every locator on `html`, for the order spec, with
    a repair that offers what `offer` gives for each drift."""
    path = order_spec(tmp_path / "qa", SHIPPING)
    config = load_config(path.parent / "config.yaml")
    spec = load_spec(path, config)
    committed = fx.for_spec(script, spec)
    record = RunRecord.create(path.parent)

    async def repair(drift: Drift) -> Offer:
        return Offer(offer(drift))

    with serving(html) as app:
        run_gate = gate(allowed=(app.origin,))
        proxy = EgressProxy(run_gate)

        @asynccontextmanager
        async def opening() -> AsyncIterator[Opened]:
            async with async_playwright() as playwright, proxy:
                await register_identity_engine(playwright)
                yield Opened(playwright.chromium, proxy, record)

        async def scenario() -> RunResult:
            replaying = Replaying(spec, config, app.origin, run_gate)
            # No replay test may hang the suite.
            async with asyncio.timeout(60):
                return await replay_run(
                    committed, replaying, opening, every_locator=True, repair=repair
                )

        result = asyncio.run(scenario())
    end = ReplayEnd(
        record,
        spec,
        committed,
        result,
        proxy.egress_blocks(config.egress.expected_blocked),
        tuple(run_gate.infrastructure_events),
        base=BLOB,
    )
    return Inserting(result, record.path, app.seen, end)


# Every assertion left unevaluated by the inserted step.
STOPPED = (
    AssertionResult("a1", "not_evaluated", stopped_at=3),
    AssertionResult("a2", "not_evaluated", stopped_at=3),
)
# A journal that ends with the inserted step's intent and completion.
STOPPED_STATES = [(0, "intent"), (0, "completed"), (3, "intent"), (3, "completed")]
# What the document says of such a run: no proposal, and nothing non-resumable.
INCONCLUSIVE: tuple[object, ...] = ("inconclusive", 3, None, [])


def test_a_post_from_an_inserted_click_stops_the_run(tmp_path: Path) -> None:
    run = replayed(tmp_path, inserting(CLICK_CONTACT), html=contact_does(POST))

    assert ("POST", "/write/inserted") in run.seen
    assert run.outcomes() == [(0, "completed", None), (3, "failed", UNSHOWN + WROTE)]
    failed = run.result.steps[-1]
    assert (failed.window is None, failed.locator_index, failed.settled) == (
        False,
        0,
        "idle",
    )
    assert ("POST", "/write/inserted") in run.logged(3)
    assert run.result.assertions == STOPPED
    assert run.states() == STOPPED_STATES
    assert run.ended() == INCONCLUSIVE
    assert run.verdict(3) == ("inconclusive", UNSHOWN + WROTE)


def test_an_inserted_press_on_a_page_that_never_settles_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Settling gives up on the retitling page after two seconds.
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)
    retitles = "setInterval(() => { document.title = String(Date.now()); }, 100)"

    run = replayed(tmp_path, inserting(PRESS_ENTER), html=enter_does(retitles))

    assert run.outcomes() == [(0, "completed", None), (3, "failed", UNSHOWN + BUSY)]
    assert run.result.steps[-1].settled == "timeout"
    assert run.result.assertions == STOPPED
    assert run.states() == STOPPED_STATES
    assert run.ended() == INCONCLUSIVE


def test_an_inserted_press_that_loads_a_new_document_stops_the_run(
    tmp_path: Path,
) -> None:
    html = enter_does("location.assign('/raw/elsewhere')")

    run = replayed(tmp_path, inserting(PRESS_ENTER), html=html)

    assert ("GET", "/raw/elsewhere") in run.seen
    assert run.outcomes() == [
        (0, "completed", None),
        (3, "failed", UNSHOWN + NAVIGATED),
    ]
    assert run.result.assertions == STOPPED
    assert run.states() == STOPPED_STATES
    assert run.ended() == INCONCLUSIVE


def test_an_inserted_click_that_moves_the_page_through_its_history_stops_the_run(
    tmp_path: Path,
) -> None:
    """A same-document navigation requests nothing, and still moves the page."""
    html = contact_does("history.pushState(null, '', '/page/contact')")

    run = replayed(tmp_path, inserting(CLICK_CONTACT), html=html)

    assert [path for _, path in run.seen] == ["/page/order"]
    assert run.outcomes() == [
        (0, "completed", None),
        (3, "failed", UNSHOWN + NAVIGATED),
    ]
    assert run.result.assertions == STOPPED
    assert run.states() == STOPPED_STATES
    assert run.ended() == INCONCLUSIVE


# The moved page, with a WebSocket the page opens as it loads, whose
# handshake the app holds, so it stays open and nothing fails meanwhile.
SOCKET = MOVED + "<script>new WebSocket(`ws://${location.host}/held/socket`);</script>"


def test_an_inserted_press_under_an_open_tunnel_stops_the_run(tmp_path: Path) -> None:
    run = replayed(tmp_path, inserting(PRESS_TAB), html=SOCKET)

    assert run.outcomes() == [(0, "completed", None), (3, "failed", UNSHOWN + TUNNEL)]
    assert run.result.assertions == STOPPED
    assert run.states() == STOPPED_STATES
    assert run.ended() == INCONCLUSIVE


def test_an_inserted_step_that_also_hits_an_egress_block_ends_egress_blocked(
    tmp_path: Path,
) -> None:
    html = contact_does(f"{UNDECLARED}; {POST}")

    run = replayed(tmp_path, inserting(CLICK_CONTACT), html=html)

    assert run.outcomes() == [(0, "completed", None), (3, "failed", UNSHOWN + WROTE)]
    assert run.ended() == ("egress_blocked", 6, None, [])


def test_an_inserted_step_after_a_violated_invariant_ends_expectation_violated(
    tmp_path: Path,
) -> None:
    broken = MOVED + "<script>console.error('broken before the heal');</script>"

    run = replayed(tmp_path, inserting(PRESS_ENTER), html=enter_does(POST, broken))

    assert run.outcomes() == [(0, "completed", None), (3, "failed", UNSHOWN + WROTE)]
    assert run.ended() == ("expectation_violated", 1, None, [])


# Every step completed and every assertion passed.
PASSED = (
    AssertionResult("a1", "pass", locator_index=0),
    AssertionResult("a2", "pass", locator_index=0),
)


def test_an_inserted_press_that_changes_nothing_completes(tmp_path: Path) -> None:
    run = replayed(tmp_path, inserting(PRESS_TAB), html=MOVED)

    assert run.outcomes() == [
        (0, "completed", None),
        (3, "completed", None),
        (1, "completed", None),
    ]
    assert run.result.assertions == PASSED
    assert run.result.outcome == "passed"
    assert run.ended()[:2] == ("drift_consistent", 7)


# The moved page, with a frame of the app's below it.
FRAMED = MOVED + '<iframe src="/raw/first"></iframe>'


def test_an_inserted_click_that_navigates_only_a_child_frame_completes(
    tmp_path: Path,
) -> None:
    html = contact_does("frames[0].location.assign('/raw/second')", FRAMED)

    run = replayed(tmp_path, inserting(CLICK_CONTACT), html=html)

    assert ("GET", "/raw/second") in run.seen
    assert run.outcomes() == [
        (0, "completed", None),
        (3, "completed", None),
        (1, "completed", None),
    ]
    assert run.result.assertions == PASSED
    assert run.result.outcome == "passed"
    assert run.ended()[:2] == ("drift_consistent", 7)


# The contact button clicked, a side-effect step, then the toggle.
CONTACT_THEN_TOGGLE = WITH_CONTACT.model_copy(
    update={
        "steps": (
            Click(
                seq=1,
                action="click",
                target="contact",
                side_effect=True,
                side_effect_basis="network: POST /write/inserted",
            ),
            Click(seq=2, action="click", target="toggle", side_effect=False),
        )
    }
)


def test_an_inserted_step_right_after_a_committed_step_that_wrote_completes(
    tmp_path: Path,
) -> None:
    """The write is in the window before the inserted step's, which isn't read."""
    run = replayed(
        tmp_path,
        inserting(PRESS_TAB),
        html=contact_does(POST),
        script=CONTACT_THEN_TOGGLE,
    )

    assert ("POST", "/write/inserted") in run.seen
    assert run.outcomes() == [
        (0, "completed", None),
        (1, "completed", None),
        (3, "completed", None),
        (2, "completed", None),
    ]
    assert run.result.outcome == "passed"
    assert run.ended()[:2] == ("drift_consistent", 7)


# The toggle clicked, then the contact button, flagged replay-safe.
TOGGLE_THEN_CONTACT = WITH_CONTACT.model_copy(
    update={
        "steps": (
            *DETAILS.steps,
            Click(seq=2, action="click", target="contact", side_effect=False),
        )
    }
)


def test_a_committed_replay_safe_step_that_writes_in_a_repaired_run_completes(
    tmp_path: Path,
) -> None:
    """Only a step a repair inserted is judged: a committed one keeps its flag."""
    run = replayed(
        tmp_path,
        lambda drift: rebound(drift.script),
        html=contact_does(POST),
        script=TOGGLE_THEN_CONTACT,
    )

    assert ("POST", "/write/inserted") in run.seen
    assert run.outcomes() == [
        (0, "completed", None),
        (1, "completed", None),
        (2, "completed", None),
    ]
    assert run.result.steps[-1].window is not None
    assert run.result.steps[-1].window.wrote
    assert run.result.outcome == "passed"
