"""An attempt's navigator turns (#53 P15a; ADR-0024's #53 P15a amendment):
a LangGraph graph of plain-data state whose turns each take one observation,
send one fresh request that passes the run's request check, run at most one
tool call and keep a turn record and its cost lines; the turn and spend
budgets; and an action stopped before its intent when the run is interrupted
while it is prepared. Real Chromium against the executor's fixture app, with
a scripted model."""

import asyncio
import json
import re
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_runner import explore_turns, model_router, navigator
from aqa_runner.browser_session import BrowserSession
from aqa_runner.chat_client import Reply
from aqa_runner.egress import EgressGate
from aqa_runner.explore_turns import (
    Turning,
    UnsentRequestError,
    take_turns,
    unspelled,
)
from aqa_runner.locator_generation import TargetUses
from aqa_runner.model_router import ModelCallError
from aqa_runner.navigator import Click
from aqa_runner.navigator_tools import ActionResult, Attempt
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.run_record import LineCheck, RefusedWriteError, require_clean
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.checkpoint.memory import InMemorySaver

from packages.runner.tests.document_fixtures import Sites, run_gate, serving_sites
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    NAME,
    NO_RESPONSE,
    PLAN,
    REFUSED,
    SAVE,
    STATUS,
    USAGE,
    Scripted,
    answer,
    calling,
    record_of,
    ref,
    taking,
    tool,
    turned,
    turning,
)
from packages.runner.tests.navigator_fixtures import (
    SIGN_IN,
    bound,
    explored,
    fail_fsync,
    journal,
    on_app,
)
from packages.runner.tests.navigator_fixtures import ref as ref_seen
from packages.runner.tests.secret_fixtures import FAKE_VALUE, copies_found

# A project whose navigator falls back to Opus after a refusal.
FALLBACK = ProjectConfig.model_validate(
    {"roles": {"navigator": {"fallback": "claude-opus-5-5"}}}
)

# What an action interrupted while it was prepared says, after its tool.
INTERRUPTED = (
    "did not run: the run stopped: the page reached a host the run doesn't "
    "allow, or an allowed host couldn't be reached"
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def blocked_while_generating(app: App, monkeypatch: pytest.MonkeyPatch) -> EgressGate:
    """A gate for `app` that records an egress block as each target is
    generated, before the action's intent."""
    egress = gate(allowed=(app.origin,))
    generate = TargetUses.add

    async def blocking(uses: TargetUses, *args: Any) -> int:
        added = await generate(uses, *args)
        egress.record_refusal("analytics.example.test", 80, "host", "fake refusal")
        return added

    monkeypatch.setattr(TargetUses, "add", blocking)
    return egress


def test_an_egress_block_during_an_actions_preparation_stops_it_before_its_intent(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    egress = blocked_while_generating(app, monkeypatch)

    async def scenario(attempt: Attempt) -> tuple[ActionResult, list[Any], bool]:
        click = Click(ref=ref_seen(attempt, "button", "Save"), meaning=SAVE)
        clicked = await attempt.act(click)
        return clicked, journal(attempt), attempt.ended

    clicked, steps, ended = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/shop",
        plan=PLAN,
        egress=egress,
    )

    assert clicked.text == f"click {INTERRUPTED}"
    assert clicked.step is None
    assert steps == []
    assert ended is False
    assert app.paths() == ["/page/shop"]


# The status's check, bound to the paragraph that reads it now.
ASSERT = tool(
    "assert_check",
    lambda m: {"check_id": "a1", "ref": ref(m, "paragraph", "Saved Ada in size S")},
)
FINISH = calling("finish", {"steps": [1, 2]})


def results(request: Sequence[BaseMessage]) -> str:
    """The results block of a navigator request's turn."""
    found = re.search(r"<results>\n(.*?)\n</results>", request[2].text, re.DOTALL)
    assert found is not None
    return found[1]


def stops(context: Turning) -> list[tuple[str, str]]:
    """Each stop the turns made: its outcome and reason."""
    return [(stop.outcome, stop.reason) for stop in context.stops]


def turn_record(record: Path, number: int) -> dict[str, Any]:
    """`turns/<number>.json`."""
    kept: dict[str, Any] = json.loads((record / f"turns/{number}.json").read_text())
    return kept


def test_finish_compiles_the_path_in_memory_and_writes_no_compiled_script(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(FILL, CLICK, ASSERT, FINISH)

    context = turned(app, tmp_path, client)

    script = context.script
    assert script is not None
    assert {
        name: [locator.model_dump(exclude_none=True) for locator in target.locators]
        for name, target in script.targets.items()
    } == {
        "t1": [
            {"role": "textbox", "name": "Name"},
            {"label": "Name"},
            {"css": "#name"},
            {"css": "input"},
        ],
        "t2": [{"role": "button", "name": "Save"}, {"css": "#save"}, {"css": "button"}],
        "t3": [
            {"testid": "status"},
            {"css": "#status"},
            {"role": "paragraph"},
            {"css": "p"},
        ],
    }
    assert [each.model_dump(exclude_none=True) for each in script.assertions] == [
        {
            "id": "a1",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "t3",
            "text": "Saved Ada",
        }
    ]
    assert script.confirmed is False
    assert script.compiled_by.models == {"navigator": "claude-sonnet-5-5"}
    assert client.models == ["claude-sonnet-5-5 at effort None"]
    assert context.stops == []
    assert not (tmp_path / ".compiled").exists()
    assert not list(tmp_path.rglob("*.compiled.json"))


def test_the_run_record_holds_each_turn_and_its_cost_lines(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(FILL, CLICK, ASSERT, FINISH)

    context = turned(app, tmp_path, client)

    record = record_of(context)
    request = client.requests[0]
    name = ref(request, "textbox", "Name")
    assert turn_record(record, 1) == {
        "turn": 1,
        "sent": request[2].text,
        "outcome": "ok",
        "call": (
            f'{{"name": "fill", "args": {{"ref": "{name}", "meaning": '
            '"the shop\'s name field", "text": "Ada"}}'
        ),
        "unrun": 0,
        "notes": "notes before fill",
        "costs": [1],
    }
    assert turn_record(record, 4)["costs"] == [4]
    assert sorted(path.name for path in (record / "turns").iterdir()) == [
        "1.json",
        "2.json",
        "3.json",
        "4.json",
    ]
    assert len((record / "costs.jsonl").read_text().splitlines()) == 4


def test_each_turn_sends_one_fresh_request_with_one_snapshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[Redacted] = []
    snapshot = BrowserSession.snapshot

    async def counted(session: BrowserSession) -> Redacted:
        taken.append(await snapshot(session))
        return taken[-1]

    monkeypatch.setattr(BrowserSession, "snapshot", counted)
    client = Scripted(FILL, CLICK, ASSERT, FINISH)

    turned(app, tmp_path, client)

    # The attempt's fixture looks once before the turns.
    assert len(taken) == 1 + 4
    assert [len(request) for request in client.requests] == [3, 3, 3, 3]
    assert all(request[:2] == client.requests[0][:2] for request in client.requests)
    turns = [request[2].text for request in client.requests]
    assert [
        earlier in later for i, later in enumerate(turns) for earlier in turns[:i]
    ] == [False] * 6
    tools = "navigate reload click fill fill_secret select press describe_parts assert_check"
    assert (
        client.offered
        == [((*tools.split(), "note_for_absence", "finish", "restart"), None)] * 4
    )


# A status page and its successor without the status's id, so a check bound
# on the first is no longer established on the second.
STATUS_PAGES = {
    "status-one": '<p id="status" class="status">Saved</p>',
    "status-two": '<p class="status">Saved</p>',
}
# Ten checks, so plan order (a9, a10) isn't the IDs' sorted order: eight the
# turns never bind, then the status read twice.
TEN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": STATUS,
                "claim": "says the form was saved",
                "checks": [
                    *(
                        {"check": "url_matches", "pattern": f"/page/{n}"}
                        for n in range(8)
                    ),
                    {
                        "check": "text_in_target",
                        "target_meaning": STATUS,
                        "text": "Saved",
                    },
                    {
                        "check": "text_in_target",
                        "target_meaning": STATUS,
                        "text": "Saved Bob",
                    },
                ],
            }
        ],
        "requires": [],
    }
)


def asserting(check_id: str) -> Callable[[Sequence[BaseMessage]], Reply]:
    return tool(
        "assert_check",
        lambda m: {"check_id": check_id, "ref": ref(m, "paragraph", "Saved")},
    )


def test_each_request_says_what_the_attempt_has_bound_in_plan_order(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model keeps no memory but its notes: each request says how every
    # planned check stood at its last evaluation (ADR-0024's #58 amendment).
    for name, page in STATUS_PAGES.items():
        monkeypatch.setitem(PAGES, name, page)
    client = Scripted(
        asserting("a10"),
        asserting("a9"),
        calling("navigate", {"path": "/page/status-two"}),
        # finish evaluates both on the page without the status's id.
        calling("finish", {"steps": [1]}),
        calling("finish", {"steps": [1]}),
    )

    turned(app, tmp_path, client, turns=5, page="/page/status-one", plan=TEN)

    unbound = [f"a{n}: unbound" for n in range(1, 9)]
    assert [
        re.findall(r"<checks>\n(.*?)\n</checks>", request[2].text, re.DOTALL)
        for request in client.requests
    ] == [
        ["\n".join([*unbound, "a9: unbound", "a10: unbound"])],
        ["\n".join([*unbound, "a9: unbound", "a10: bound, failed when last checked"])],
        *[
            [
                "\n".join(
                    [
                        *unbound,
                        "a9: bound, passed when last checked",
                        "a10: bound, failed when last checked",
                    ]
                )
            ]
        ]
        * 2,
        [
            "\n".join(
                [
                    *unbound,
                    "a9: bound, not established when last checked",
                    "a10: bound, not established when last checked",
                ]
            )
        ],
    ]


# A header's Sign in link, checked not to show: noting it binds nothing.
HEADER_LINK = "the header's Sign in link"
GONE = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": HEADER_LINK,
                "claim": "is gone",
                "checks": [{"check": "not_visible", "target_meaning": HEADER_LINK}],
            }
        ],
        "requires": [],
    }
)


def test_the_request_after_a_note_says_to_assert_the_check_once_it_is_gone(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        PAGES,
        "signed-out",
        '<nav class="navbar"><a href="/page/form">Sign in</a></nav>',
    )
    note = tool(
        "note_for_absence",
        lambda m: {"check_id": "a1", "ref": ref(m, "link", "Sign in")},
    )
    client = Scripted(note, calling("finish", {"steps": []}))

    turned(app, tmp_path, client, turns=2, page="/page/signed-out", plan=GONE)

    assert [
        re.findall(r"<checks>\n(.*?)\n</checks>", request[2].text, re.DOTALL)
        for request in client.requests
    ] == [["a1: unbound"], ["a1: noted; assert it once it is gone"]]


def test_each_priced_response_is_on_disk_before_the_next_call(
    app: App, tmp_path: Path
) -> None:
    on_disk: list[int] = []

    def counting(move: Any) -> Any:
        def counted(messages: Sequence[BaseMessage]) -> Reply:
            costs = list(tmp_path.glob(".aqa/runs/*/costs.jsonl"))
            on_disk.append(len(costs[0].read_text().splitlines()) if costs else 0)
            return move if isinstance(move, Reply) else move(messages)

        return counted

    turned(app, tmp_path, Scripted(*map(counting, (FILL, CLICK, ASSERT, FINISH))))

    assert on_disk == [0, 1, 2, 3]


def test_the_turn_budget_gives_up(app: App, tmp_path: Path) -> None:
    client = Scripted(FILL, FILL, FILL)

    context = turned(app, tmp_path, client, turns=2)

    assert stops(context) == [("gave_up", "the attempt took all 2 of its turns")]
    assert len(client.requests) == 2


def unchosen(
    role: str, name: str, meaning: str = NAME
) -> Callable[[Sequence[BaseMessage]], Reply]:
    """A select of no option, which is refused before its intent, on the
    element with `role` and `name`, by the ref the request's snapshot gives
    it: a fresh one each turn."""
    return tool(
        "select",
        lambda m: {"ref": ref(m, role, name), "meaning": meaning, "option": ""},
    )


# Why the turns stop once the navigator repeats one refused call (#232).
REPEATED = "the navigator made the same refused call 5 times in a row"


@pytest.mark.parametrize("turns", [40, 5])
def test_the_same_refused_call_five_times_in_a_row_ends_the_attempt(
    app: App, tmp_path: Path, turns: int
) -> None:
    client = Scripted(*[unchosen("textbox", "Name")] * 6)

    context = turned(app, tmp_path, client, turns=turns)

    # Whether or not that was the last turn too.
    assert stops(context) == [("gave_up", REPEATED)]
    assert len(client.requests) == 5
    # Every call named the same field by the fresh ref of its turn.
    assert len({ref(request, "textbox", "Name") for request in client.requests}) == 5


@pytest.mark.parametrize(
    "refused",
    [
        calling("select", {"ref": "e1"}),
        FINISH,
        calling("assert_check", {"check_id": "a1", "ref": None}),
    ],
    ids=["arguments that don't fit", "a finish that compiled nothing", "a check's"],
)
def test_each_kind_of_refused_call_counts(
    app: App, tmp_path: Path, refused: Reply | Callable[[Sequence[BaseMessage]], Reply]
) -> None:
    client = Scripted(*[refused] * 6)

    context = turned(app, tmp_path, client)

    assert stops(context) == [("gave_up", REPEATED)]
    assert len(client.requests) == 5


def test_calls_that_ran_never_count_as_repeats(app: App, tmp_path: Path) -> None:
    client = Scripted(*[FILL] * 7)

    context = turned(app, tmp_path, client, turns=6)

    assert stops(context) == [("gave_up", "the attempt took all 6 of its turns")]


@pytest.mark.parametrize(
    "between",
    [
        unchosen("combobox", "Size"),
        unchosen("textbox", "Name", meaning=SAVE),
        FILL,
    ],
    ids=["another target", "other arguments", "a call that ran"],
)
def test_another_call_between_refused_calls_starts_their_count_again(
    app: App, tmp_path: Path, between: Callable[[Sequence[BaseMessage]], Reply]
) -> None:
    refused = unchosen("textbox", "Name")
    client = Scripted(*[refused] * 4, between, *[refused] * 4)

    context = turned(app, tmp_path, client, turns=9)

    assert stops(context) == [("gave_up", "the attempt took all 9 of its turns")]
    assert len(client.requests) == 9


def test_the_spend_budget_stops_before_the_turns_action(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(FILL, CLICK, FILL)

    # Each response costs $0.003: the second leaves $0.005 overspent.
    context = turned(app, tmp_path, client, usd="0.005")

    assert stops(context) == [
        ("gave_up", "the spend budget ran out on this turn, so its call didn't run")
    ]
    request = client.requests[1]
    assert turn_record(record_of(context), 2) == {
        "turn": 2,
        "sent": request[2].text,
        "outcome": "ok",
        "call": (
            f'{{"name": "click", "args": {{"ref": "{ref(request, "button", "Save")}", '
            '"meaning": "the shop\'s save button"}}'
        ),
        "unrun": 0,
        "notes": "notes before click",
        "costs": [2],
    }
    assert app.paths() == ["/page/shop", "/did/fill"]
    assert len(client.requests) == 2


def test_no_turn_starts_once_the_spend_is_gone(app: App, tmp_path: Path) -> None:
    client = Scripted(FILL, CLICK, FILL)

    context = turned(app, tmp_path, client, usd="0.006")

    assert stops(context) == [("gave_up", "the spend budget ran out")]
    assert len(client.requests) == 2
    assert "/write/save" in app.paths()


def test_a_refusal_with_no_fallback_gives_up_and_keeps_its_turn(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(REFUSED)

    context = turned(app, tmp_path, client)

    assert stops(context) == [("gave_up", "the model refused its turn")]
    record = record_of(context)
    assert turn_record(record, 1) == {
        "turn": 1,
        "sent": client.requests[0][2].text,
        "outcome": "refusal",
        "call": None,
        "unrun": 0,
        "notes": "I can't help with that.",
        "costs": [1],
    }
    assert len((record / "costs.jsonl").read_text().splitlines()) == 1
    assert app.paths() == ["/page/shop"]


def test_no_response_keeps_its_turn_record(app: App, tmp_path: Path) -> None:
    client = Scripted(NO_RESPONSE)

    context = turned(app, tmp_path, client)

    assert stops(context) == [
        ("no_response", "the model gave no response (APIConnectionError)")
    ]
    record = record_of(context)
    assert turn_record(record, 1) == {
        "turn": 1,
        "sent": client.requests[0][2].text,
        "outcome": "no_response",
        "call": None,
        "unrun": 0,
        "notes": "",
        "costs": [],
    }
    assert not (record / "costs.jsonl").exists()


def test_a_fallback_with_no_response_keeps_the_refusals_cost_line(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(REFUSED, NO_RESPONSE)

    context = turned(app, tmp_path, client, config=FALLBACK)

    assert stops(context) == [
        ("no_response", "the model gave no response (APIConnectionError)")
    ]
    assert client.models == [
        "claude-sonnet-5-5 at effort None",
        "claude-opus-5-5 at effort None",
    ]
    record = record_of(context)
    assert turn_record(record, 1)["costs"] == [1]
    assert turn_record(record, 1)["outcome"] == "no_response"
    assert len((record / "costs.jsonl").read_text().splitlines()) == 1


def test_a_navigator_request_that_still_spells_a_bound_value_is_never_sent(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = navigator.navigator_request

    def leaking(*args: Any, **kwargs: Any) -> list[BaseMessage]:
        # A gap in the builder's scans: a bound value left in the request.
        return [*built(*args, **kwargs), HumanMessage(content=FAKE_VALUE)]

    monkeypatch.setattr(explore_turns, "navigator_request", leaking)
    client = Scripted(FILL)

    context = turned(app, tmp_path, client, secrets=[bound(app.origin, FAKE_VALUE)])

    assert stops(context) == [
        (
            "gave_up",
            "a request still spelled a bound value after its scans, so it wasn't sent",
        )
    ]
    assert client.requests == []
    assert sorted(path.name for path in record_of(context).iterdir()) == ["attempt-1"]


def test_a_cost_line_not_on_disk_stops_the_turns_with_no_further_call(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted(FILL, FILL)

    def unwritten(messages: Sequence[BaseMessage]) -> Reply:
        # The cost line's own fsync, once the response is priced.
        fail_fsync(monkeypatch)
        return FILL(messages)

    client.moves[0] = unwritten

    context = turned(app, tmp_path, client)

    assert stops(context) == [
        (
            "record_error",
            (
                "a priced model call's cost line isn't on disk, so the run's cost "
                "is incomplete"
            ),
        )
    ]
    assert len(client.requests) == 1
    assert len(context.spending.calls) == 1
    assert app.paths() == ["/page/shop"]


def test_a_journal_that_takes_no_more_lines_stops_the_turns(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate = TargetUses.add

    async def failing_next(uses: TargetUses, *args: Any) -> int:
        added = await generate(uses, *args)
        # The next fsync is the fill's intent's.
        fail_fsync(monkeypatch)
        return added

    monkeypatch.setattr(TargetUses, "add", failing_next)
    client = Scripted(FILL, FILL)

    context = turned(app, tmp_path, client)

    assert stops(context) == [
        ("record_error", "the run record took no more lines, so the attempt stopped")
    ]
    assert len(client.requests) == 1
    assert app.paths() == ["/page/shop"]


def test_a_block_during_the_turns_observation_makes_no_call(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    egress = gate(allowed=(app.origin,))
    observe = Attempt.observe

    async def blocked_while_looking(attempt: Attempt) -> Redacted:
        looked = await observe(attempt)
        egress.record_refusal("analytics.example.test", 80, "host", "fake refusal")
        return looked

    client = Scripted(CLICK)

    async def scenario(attempt: Attempt) -> Turning:
        monkeypatch.setattr(Attempt, "observe", blocked_while_looking)
        context = turning(attempt, client)
        await take_turns(context)
        return context

    context = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/shop",
        plan=PLAN,
        egress=egress,
    )

    assert context.stops == []
    assert client.requests == []
    assert not (record_of(context) / "turns").exists()


def test_a_block_during_a_turns_action_ends_the_turns_with_no_further_call(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    egress = blocked_while_generating(app, monkeypatch)
    client = Scripted(CLICK, CLICK)

    # Its last turn: the interruption, not the turn budget, ends the turns.
    context = turned(app, tmp_path, client, egress=egress, turns=1)

    assert context.stops == []
    assert len(client.requests) == 1
    assert sorted(path.name for path in (record_of(context) / "turns").iterdir()) == [
        "1.json"
    ]
    assert not (record_of(context) / "attempt-1" / "steps.jsonl").exists()
    assert app.paths() == ["/page/shop"]


def test_a_page_that_cannot_be_looked_at_is_said_in_the_next_request(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observe = Attempt.observe
    looks: list[int] = []

    async def unanswered_once(attempt: Attempt) -> Redacted:
        looks.append(1)
        if len(looks) == 1:
            raise UnansweredError(5)
        return await observe(attempt)

    client = Scripted(REFUSED)

    async def scenario(attempt: Attempt) -> Turning:
        monkeypatch.setattr(Attempt, "observe", unanswered_once)
        context = turning(attempt, client)
        await take_turns(context)
        return context

    on_app(app, tmp_path, scenario, page="/page/shop", plan=PLAN)

    assert "<results>\nthe page couldn't be looked at: the page didn't answer " in (
        client.requests[0][2].text
    )
    assert "<snapshot>\n\n</snapshot>" in client.requests[0][2].text


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


FILL_SECRET = tool(
    "fill_secret",
    lambda m: {
        "ref": ref(m, "textbox", "Password"),
        "meaning": SIGN_IN,
        "name": "TEST_PASSWORD",
    },
)


def test_a_secret_the_page_shows_reaches_the_navigator_as_its_marker(
    sites: Sites, tmp_path: Path
) -> None:
    client = Scripted(FILL_SECRET, REFUSED)

    context = explored(
        tmp_path,
        taking(client),
        sites.app,
        page="/signin",
        egress=run_gate(sites),
        secrets=[bound(sites.app, FAKE_VALUE)],
    )

    assert results(client.requests[1]) == "step 1 ran, and the page settled"
    assert "[SECRET:TEST_PASSWORD]" in client.requests[1][2].text
    sent = [message.text for request in client.requests for message in request]
    record = record_of(context)
    assert copies_found(FAKE_VALUE, texts=sent, files=record.rglob("*.json*")) == []
    assert stops(context) == [("gave_up", "the model refused its turn")]


def test_fill_secret_after_leaving_a_framed_page_is_refused_before_its_intent(
    sites: Sites, tmp_path: Path
) -> None:
    client = Scripted(calling("navigate", {"path": "/signin"}), FILL_SECRET, REFUSED)

    context = explored(
        tmp_path,
        taking(client),
        sites.app,
        page="/framed",
        egress=run_gate(sites),
        secrets=[bound(sites.app, FAKE_VALUE)],
    )

    # Playwright still lists the old page's frames, and the frame walks fail
    # closed on them (LAB_NOTES, 2026-10-14; #192).
    assert results(client.requests[2]) == (
        "fill_secret did not run: fill_secret: TEST_PASSWORD wasn't filled: the "
        "field takes no text, its page changed the value, or the page broke or "
        "closed during the fill or its checks"
    )
    assert [(step["seq"], step["state"]) for step in journal(context.attempt)] == [
        (1, "intent"),
        (1, "completed"),
    ]
    assert context.attempt.ended is False
    assert stops(context) == [("gave_up", "the model refused its turn")]


def test_each_reply_runs_at_most_its_first_tool_call(app: App, tmp_path: Path) -> None:
    def both(messages: Sequence[BaseMessage]) -> Reply:
        name = ref(messages, "textbox", "Name")
        save = ref(messages, "button", "Save")
        return answer(
            AIMessage(
                content="fill, then save",
                tool_calls=[
                    {
                        "name": "fill",
                        "args": {
                            "ref": name,
                            "meaning": "the shop's name field",
                            "text": "Ada",
                        },
                        "id": "call-fill",
                    },
                    {
                        "name": "click",
                        "args": {"ref": save, "meaning": SAVE},
                        "id": "call-click",
                    },
                ],
            )
        )

    note = tool(
        "note_for_absence",
        lambda m: {"check_id": "a1", "ref": ref(m, "paragraph", "Not saved")},
    )
    cut_off = Reply(
        message=AIMessage(content="cut off", tool_calls=[]),
        usage=USAGE,
        refused=False,
        complete=False,
        parsed=None,
    )
    client = Scripted(
        both,
        answer(AIMessage(content="looking around")),
        calling("teleport", {}),
        note,
        cut_off,
        REFUSED,
    )

    context = turned(app, tmp_path, client)

    assert [results(request) for request in client.requests[1:]] == [
        (
            "step 1 ran, and the page settled\n"
            "only the first of the reply's 2 tool calls ran: a turn runs one"
        ),
        "the reply called no tool, so nothing ran",
        "the reply called a tool the navigator doesn't have, so nothing ran",
        (
            "note_for_absence did not run: a1 is a text_in_target check, and it "
            "notes only a not_visible check's element"
        ),
        "the reply's outcome was invalid, so nothing ran",
    ]
    assert turn_record(record_of(context), 5)["outcome"] == "invalid"
    assert "<log>\nstep 1: fill " in client.requests[4][2].text
    assert (
        'step 1: fill {"meaning": "the shop\'s name field", "text": "Ada"}\n</log>'
    ) in client.requests[4][2].text
    assert "<notes>\nlooking around\n</notes>" in client.requests[2][2].text
    assert turn_record(record_of(context), 1)["unrun"] == 1
    assert app.paths() == ["/page/shop", "/did/fill"]
    assert stops(context) == [("gave_up", "the model refused its turn")]


def test_an_action_that_ends_the_attempt_ends_the_turns(
    app: App, tmp_path: Path
) -> None:
    fill_secret = tool(
        "fill_secret",
        lambda m: {
            "ref": ref(m, "textbox", "Password"),
            "meaning": SIGN_IN,
            "name": "TEST_PASSWORD",
        },
    )
    retype = tool(
        "fill",
        lambda m: {
            "ref": ref(m, "textbox", "Password"),
            "meaning": SIGN_IN,
            "text": "Ada",
        },
    )
    client = Scripted(fill_secret, retype, REFUSED)

    context = turned(
        app,
        tmp_path,
        client,
        page="/page/signin-rethrows",
        secrets=[bound(app.origin, FAKE_VALUE)],
    )

    assert stops(context) == [
        (
            "gave_up",
            (
                "step 2 failed after it was dispatched, so this attempt has ended: "
                "Error: ElementHandle.evaluate: the rest is withheld, since the "
                "page was handed a test secret"
            ),
        )
    ]
    assert len(client.requests) == 2
    assert [(step["seq"], step["state"]) for step in journal(context.attempt)] == [
        (1, "intent"),
        (1, "completed"),
        (2, "intent"),
    ]


def test_cancellation_during_a_turn_keeps_every_priced_response_on_disk(
    app: App, tmp_path: Path
) -> None:
    limits: list[asyncio.Timeout] = []

    async def expiring(_request: Sequence[BaseMessage]) -> Reply:
        limits[0].reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(3600)
        raise AssertionError("the deadline never came")

    client = Scripted(FILL, expiring)

    async def scenario(attempt: Attempt) -> None:
        context = turning(attempt, client)
        async with asyncio.timeout(None) as limit:
            limits.append(limit)
            await take_turns(context)

    with pytest.raises(TimeoutError):
        on_app(app, tmp_path, scenario, page="/page/shop", plan=PLAN)

    [record] = (tmp_path / ".aqa" / "runs").iterdir()
    assert len((record / "costs.jsonl").read_text().splitlines()) == 1
    assert sorted(path.name for path in (record / "turns").iterdir()) == ["1.json"]
    assert len(client.requests) == 2


def test_every_checkpoint_holds_plain_data_only(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    savers: list[InMemorySaver] = []

    def keeping() -> InMemorySaver:
        savers.append(InMemorySaver())
        return savers[-1]

    monkeypatch.setattr(explore_turns, "InMemorySaver", keeping)

    turned(app, tmp_path, Scripted(FILL, CLICK, ASSERT, FINISH))

    [saver] = savers
    states = [
        saved.checkpoint["channel_values"]
        for saved in saver.list(None)
        if "turn" in saved.checkpoint["channel_values"]
    ]
    assert sorted(state["turn"] for state in states) == [0, 1, 2, 3, 4]
    plain = {
        key: {type(value).__name__ for state in states for value in [state[key]]}
        for key in ("turn", "log", "notes", "results", "done")
    }
    # Saved through LangGraph's serializer, a tuple comes back as a list.
    assert plain == {
        "turn": {"int"},
        "log": {"list"},
        "notes": {"str"},
        "results": {"list"},
        "done": {"bool"},
    }
    assert {
        type(line).__name__
        for state in states
        for key in ("log", "results")
        for line in state[key]
    } == {"str"}


@pytest.mark.usefixtures("reset_tracing")
@pytest.mark.parametrize("guarded", [True, False])
def test_the_turns_export_no_trace_with_langsmith_tracing_set(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    customers_environment: Callable[[str], None],
    exported_to_langsmith: Callable[[float], bool],
    guarded: bool,
) -> None:
    customers_environment("LANGSMITH_TRACING")
    if not guarded:
        # The control: without the router's guard, this setup exports.
        monkeypatch.setattr(model_router, "ignore_ambient_tracing", lambda: None)

    context = turned(app, tmp_path, Scripted(REFUSED))
    wait_for_all_tracers()

    assert stops(context) == [("gave_up", "the model refused its turn")]
    assert exported_to_langsmith(1 if guarded else 5) is not guarded


@pytest.mark.parametrize(
    "leaked",
    [
        SystemMessage(content=f"instructions {FAKE_VALUE}"),
        HumanMessage(content=[{"type": "text", "text": FAKE_VALUE}]),
        HumanMessage(content=[{"type": "document", "source": FAKE_VALUE}]),
        AIMessage(
            content="",
            tool_calls=[{"name": "fill", "args": {"text": FAKE_VALUE}, "id": "c1"}],
        ),
    ],
    ids=["system", "text block", "document block", "tool call"],
)
def test_a_request_spelling_a_bound_value_in_any_part_is_refused(
    leaked: BaseMessage,
) -> None:
    check = unspelled(Redactor([bound("http://127.0.0.1", FAKE_VALUE)]))
    clean = [
        SystemMessage(content="instructions"),
        HumanMessage(content="[SECRET:TEST_PASSWORD]"),
    ]

    check(clean)
    with pytest.raises(UnsentRequestError) as refused:
        check([leaked, *clean])

    assert str(refused.value) == (
        "a request still spelled a bound value after its scans, so it wasn't sent"
    )


@pytest.mark.parametrize(
    ("value", "text"),
    [("fake\\u2019", "the fake\u2019s cart"), ("Human", "a page")],
    ids=["an escape of other text", "a message's own fields"],
)
def test_a_request_spelling_no_bound_value_is_sent(value: str, text: str) -> None:
    check = unspelled(Redactor([bound("http://127.0.0.1", value)]))

    assert (
        check([SystemMessage(content="instructions"), HumanMessage(content=text)])
        is None
    )


def test_a_block_during_the_call_runs_none_of_the_reply(
    app: App, tmp_path: Path
) -> None:
    egress = gate(allowed=(app.origin,))

    def blocked(messages: Sequence[BaseMessage]) -> Reply:
        egress.record_refusal("analytics.example.test", 80, "host", "fake refusal")
        return calling(
            "assert_check",
            {"check_id": "a1", "ref": ref(messages, "paragraph", "Not saved")},
        )

    client = Scripted(blocked)

    context = turned(app, tmp_path, client, egress=egress)

    assert context.attempt.checks == {"a1": None}
    assert context.stops == []
    assert turn_record(record_of(context), 1)["outcome"] == "ok"


def refusing_turn_records(redactor: Redactor, place: str) -> LineCheck:
    """`require_clean`, refusing every turn record too, as it does a
    document left spelling a bound value."""
    clean = require_clean(redactor, place)

    def check(text: str) -> None:
        if place.startswith("turns/"):
            raise RefusedWriteError(f"{place}: refused by the test")
        clean(text)

    return check


@pytest.mark.parametrize("cause", ["taken", "refused"])
def test_a_turn_record_the_record_cannot_take_stops_the_turns_before_the_call_runs(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    def kept_already(messages: Sequence[BaseMessage]) -> Reply:
        [record] = (tmp_path / ".aqa" / "runs").iterdir()
        (record / "turns").mkdir()
        (record / "turns" / "1.json").write_text("{}")
        return FILL(messages)

    if cause == "refused":
        monkeypatch.setattr(explore_turns, "require_clean", refusing_turn_records)
    client = Scripted(kept_already if cause == "taken" else FILL)

    context = turned(app, tmp_path, client)

    assert stops(context) == [
        ("record_error", "the run record took no more lines, so the attempt stopped")
    ]
    assert len(client.requests) == 1
    assert app.paths() == ["/page/shop"]


def test_a_fallback_that_fails_for_another_reason_raises(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(REFUSED, ValueError("a fake client bug"))

    with pytest.raises(ModelCallError) as failed:
        turned(app, tmp_path, client, config=FALLBACK)

    assert isinstance(failed.value.__cause__, ValueError)


def test_no_fallback_runs_past_the_spend_left(app: App, tmp_path: Path) -> None:
    client = Scripted(FILL, REFUSED, CLICK)

    # $0.002 is left for the second turn, whose refusal costs $0.003.
    context = turned(app, tmp_path, client, config=FALLBACK, usd="0.005")

    assert stops(context) == [("gave_up", "the model refused its turn")]
    assert len(client.requests) == 2
    assert client.models == ["claude-sonnet-5-5 at effort None"]
