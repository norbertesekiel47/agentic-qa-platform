"""An attempt's phase and the run's outcome (#53 P15b; ADR-0024's #53 P15b
amendment): `explore.run_attempt` opens the run's egress proxy, resets the
app, opens the browser, loads the start URL as step 0 and takes the
navigator's turns, then ends the phase and writes `explore.json`, whose
outcome decides. Real Chromium against the executor's fixture app, with a
scripted model; `test_explore_stops.py` holds the stops that combine."""

import asyncio
import errno
import json
import re
import socket
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Self, TypedDict, Unpack

import pytest
from aqa_core.compiler import contracts_by_meaning
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import load_spec, subject_contracts
from aqa_core.spec import Spec
from aqa_runner import executor, explore, sandbox, settling
from aqa_runner.bound_secrets import bound_secrets
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.document_origins import PolicyEvent
from aqa_runner.egress import EgressGate
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.explore import Explorable, Explored, Run, run_attempt
from aqa_runner.model_router import ModelRouter
from aqa_runner.navigator_tools import Attempt
from aqa_runner.redaction import Redactor
from aqa_runner.reset import reset
from aqa_runner.run_record import RunRecord
from aqa_runner.sandbox import HOST_FIXES, OTHER_FIX, SandboxObservations
from aqa_runner.settling import Settled
from langchain_core.messages import BaseMessage
from playwright.async_api import Browser, async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import gate, get, unused_port
from packages.runner.tests.executor_fixtures import PAGES, App, read_steps, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    PLAN,
    REFUSED,
    Move,
    Scripted,
    calling,
    ref,
    tool,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE
from packages.runner.tests.test_executor_runs import while_closing
from packages.runner.tests.test_navigator_checks import ARTICLE, BUTTON_ROW, FAVORITE

# The status's check, bound to the paragraph that reads it after the save.
ASSERT = tool(
    "assert_check",
    lambda m: {"check_id": "a1", "ref": ref(m, "paragraph", "Saved Ada in size S")},
)
FINISH = calling("finish", {"steps": [1, 2]})
# The status's check, bound to the paragraph before anything was saved.
ASSERT_NOW = tool(
    "assert_check",
    lambda m: {"check_id": "a1", "ref": ref(m, "paragraph", "Not saved")},
)

# A project that binds TEST_PASSWORD to password fields on the start
# origin, and the account a bound spec signs in with.
SECRETS = ProjectConfig.model_validate(
    {"secrets": {"TEST_PASSWORD": {"origins": ["start"], "field": "password"}}}
)
ACCOUNT = (
    "  account: { email: reader@example.test, password: { secret: TEST_PASSWORD } }\n"
)

# The pay page's button, whose payment the app holds until the test ends.
PAY = tool(
    "click", lambda m: {"ref": ref(m, "button", "Pay"), "meaning": "the pay button"}
)

# What `explore.json` says of the attempt's phase when nothing was uncertain.
CLEAN_PHASE = {
    "part": "attempt-1",
    "plain_uncertain": 0,
    "tunnel_uncertain": 0,
    "policy_events": 0,
}


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST_PASSWORD's fake value, for a spec that references it."""
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)


class Setup(TypedDict, total=False):
    """How a run is set up, past its app: its start origin (the app's by
    default), the page its spec starts on (the shop), the spec's further
    preconditions, its plan, its project config, its egress gate (one
    allowing only the start origin), the scan its record is made with,
    when not the run's own, and a list the run is added to as it is made,
    for a move that reaches into it."""

    start: str
    page: str
    preconditions: str
    plan: CoveragePlan
    config: ProjectConfig
    egress: EgressGate
    scan: Redactor
    runs: list[Run]


def shop_spec(root: Path, config: ProjectConfig, page: str, preconditions: str) -> Spec:
    """The shop spec under the spec root `root`, read as the loader reads it."""
    path = root / "shop.spec.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "id: shop\n"
        "goal: A shopper saves their name.\n"
        f"preconditions:\n  start_url: '{page}'\n{preconditions}"
        "expect:\n  - The shop says the name was saved\n"
        "---\n"
    )
    return load_spec(path, config)


def explored(
    app: App, tmp_path: Path, client: Scripted, **setup: Unpack[Setup]
) -> Explored:
    """How `run_attempt` ended a run of the shop spec against the app,
    made as explore's preflight makes it: the redactor binding every
    secret the spec references, the record under the spec root, and its
    `attempt-1` part, whose journal the app notes as each request arrives;
    the plan's meanings with their subject contracts; `client` as its
    model."""
    config = setup.get("config", ProjectConfig())
    start = setup.get("start", app.origin)
    root = tmp_path / "qa"
    spec = shop_spec(
        root, config, setup.get("page", "/page/shop"), setup.get("preconditions", "")
    )
    secrets = bound_secrets(spec, start)
    redactor = Redactor(secrets.values())
    record = RunRecord.create(root).redacting(setup.get("scan", redactor))
    attempt = record.part("attempt-1")
    app.record = attempt.path
    plan = setup.get("plan", PLAN)
    explorable = Explorable(
        plan, contracts_by_meaning(plan, subject_contracts(config, "shop"))
    )

    async def scenario() -> Explored:
        # No explore test may hang the suite.
        async with async_playwright() as playwright, asyncio.timeout(120):
            run = Run(
                spec,
                config,
                start,
                redactor,
                record,
                attempt,
                secrets,
                ModelRouter.from_config(config, client),
                playwright,
                asyncio.get_running_loop().time() + 90,
                setup.get("egress") or gate(allowed=(start,)),
            )
            setup.get("runs", []).append(run)
            return await run_attempt(run, explorable)

    return asyncio.run(scenario())


def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    """Each timeout the run's minutes budget enters, as it is made, so a
    move can bring its deadline to now and no test waits for it."""
    made: list[asyncio.Timeout] = []

    def made_at(when: float | None) -> asyncio.Timeout:
        made.append(asyncio.timeout_at(when))
        return made[-1]

    monkeypatch.setattr(explore, "timeout_at", made_at)
    return made


def expiring(
    limits: list[asyncio.Timeout], before: Callable[[], object] = lambda: None
) -> Move:
    """A move that runs `before`, then brings the minutes budget's deadline
    to now and waits on its call until the budget cancels it."""

    async def move(_messages: Sequence[BaseMessage]) -> Any:
        before()
        limits[-1].reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(3600)
        pytest.fail("the minutes budget never ran out")

    return move


def written(explored: Explored) -> dict[str, Any]:
    """The run's `explore.json`."""
    assert explored.record is not None
    document: dict[str, Any] = json.loads(
        (explored.record / "explore.json").read_text()
    )
    return document


def files(explored: Explored) -> list[str]:
    """Every file the run's record holds, by its path in the record."""
    assert explored.record is not None
    return sorted(
        str(path.relative_to(explored.record))
        for path in explored.record.rglob("*")
        if path.is_file()
    )


def test_a_finished_attempt_finalizes_with_its_script_in_memory(
    app: App, tmp_path: Path
) -> None:
    run = explored(app, tmp_path, Scripted(FILL, CLICK, ASSERT, FINISH))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert run.document == written(run)
    assert {key: value for key, value in written(run).items() if key != "run_id"} == {
        "outcome": "gave_up",
        "exit_code": 3,
        "withheld": False,
        "published": None,
        "reasons": [],
        "non_resumable": [],
        "phases": [CLEAN_PHASE],
        "egress": None,
        "infrastructure_events": {"count": 0, "events": []},
        "confirmation": None,
    }
    assert files(run) == [
        "attempt-1/steps.jsonl",
        "costs.jsonl",
        "explore.json",
        "turns/1.json",
        "turns/2.json",
        "turns/3.json",
        "turns/4.json",
    ]
    assert run.cost == Decimal("0.012")
    script = run.script
    assert script is not None
    assert (script.spec_id, script.confirmed) == ("shop", False)
    assert [
        each.model_dump(include={"id", "target"}) for each in script.assertions
    ] == [{"id": "a1", "target": "t3"}]
    # Until the confirmation publishes it (#53 P16), the script stays in memory.
    assert not (tmp_path / "qa" / ".compiled").exists()
    assert list(tmp_path.rglob("*.compiled*")) == []


def test_the_start_url_is_step_0_and_no_side_effect_step(
    app: App, tmp_path: Path
) -> None:
    run = explored(app, tmp_path, Scripted(REFUSED))

    intent = {
        "action": {"action": "navigate", "url": "/page/shop"},
        "seq": 0,
        "side_effect": False,
        "state": "intent",
        "target_used": None,
    }
    assert run.record is not None
    assert read_steps(run.record / "attempt-1") == [
        intent,
        {"locator_used": None, "seq": 0, "settled": "idle", "state": "completed"},
    ]
    # The start URL's intent was on disk before the page was asked for.
    assert app.records[0] == ("GET", "/page/shop", [intent])
    assert written(run)["reasons"] == ["the model refused its turn", "a1: never bound"]
    assert run.exit_code == 3


def test_the_projects_resolve_seconds_bound_the_reset_the_actions_and_the_looks(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets: list[float] = []

    async def resetting(*args: Any, seconds: float) -> None:
        resets.append(seconds)
        await reset(*args, seconds=seconds)

    waits: list[dict[str, float]] = []
    limit_waits = BrowserSession.limit_waits

    def limiting(session: BrowserSession, **seconds: float) -> None:
        waits.append(seconds)
        limit_waits(session, **seconds)

    attempts: list[Attempt] = []

    def attempted(*args: Any, **kwargs: Any) -> Attempt:
        attempts.append(Attempt(*args, **kwargs))
        return attempts[-1]

    monkeypatch.setattr(explore, "reset", resetting)
    monkeypatch.setattr(BrowserSession, "limit_waits", limiting)
    monkeypatch.setattr(explore, "Attempt", attempted)
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 7}})

    explored(
        app,
        tmp_path,
        Scripted(REFUSED),
        config=config,
        preconditions="  reset: { http: POST /write/reset }\n",
    )

    assert resets == [7]
    # Navigation keeps the executor's own bound, as replay's does.
    assert waits == [
        {"action_seconds": 7, "navigation_seconds": executor.NAVIGATION_SECONDS}
    ]
    assert [attempt.budget for attempt in attempts] == [7]


def test_the_phase_counts_the_policy_events_its_session_recorded(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def watched(*args: Any, **kwargs: Any) -> AsyncIterator[BrowserSession]:
        async with open_browser_session(*args, **kwargs) as session:
            session.policy_events.add(
                PolicyEvent("navigation", "http://elsewhere.example.test/", None)
            )
            yield session

    monkeypatch.setattr(explore, "open_browser_session", watched)

    run = explored(app, tmp_path, Scripted(REFUSED))

    assert written(run)["phases"] == [CLEAN_PHASE | {"policy_events": 1}]


@pytest.mark.usefixtures("secret")
def test_a_failed_reset_before_the_attempt_finalizes_with_the_records_scan(
    app: App, tmp_path: Path
) -> None:
    run = explored(
        app,
        tmp_path,
        Scripted(),
        config=SECRETS,
        preconditions=ACCOUNT + "  reset: { http: POST /status/500 }\n",
    )

    assert run.exit_code == 14
    assert {key: value for key, value in written(run).items() if key != "run_id"} == {
        "outcome": "reset_unsafe",
        "exit_code": 14,
        "withheld": True,
        "published": None,
        "reasons": ["the reset hook answered 500", "a1: never bound"],
        "non_resumable": [],
        "phases": [CLEAN_PHASE],
        "egress": None,
        "infrastructure_events": {"count": 0, "events": None},
        "confirmation": None,
    }
    assert files(run) == ["explore.json"]
    assert app.paths() == ["/status/500"]


def test_a_successful_reset_comes_before_the_engines_the_browser_and_step_0(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def spied(name: str) -> Callable[..., Any]:
        called = getattr(explore, name)

        def spy(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return called(*args, **kwargs)

        return spy

    for name in ("reset", "register_identity_engine", "open_browser_session"):
        monkeypatch.setattr(explore, name, spied(name))

    run = explored(
        app,
        tmp_path,
        Scripted(REFUSED),
        preconditions="  reset: { http: POST /write/reset }\n",
    )

    assert calls == ["reset", "register_identity_engine", "open_browser_session"]
    assert app.records[0] == ("POST", "/write/reset", [])
    assert app.records[1][:2] == ("GET", "/page/shop")
    assert [line["state"] for line in app.records[1][2]] == ["intent"]
    assert written(run)["reasons"] == ["the model refused its turn", "a1: never bound"]


@pytest.mark.usefixtures("secret")
def test_a_failed_launch_finalizes_with_the_records_scan(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unproven(
        _browser: Browser,
    ) -> tuple[SandboxObservations | None, list[str]]:
        return None, ["fake: no renderer could be read"]

    monkeypatch.setattr(sandbox, "_observe", unproven)

    run = explored(app, tmp_path, Scripted(), config=SECRETS, preconditions=ACCOUNT)

    assert run.exit_code == 10
    document = written(run)
    assert (document["outcome"], document["withheld"]) == ("no_sandbox", True)
    assert document["reasons"] == [
        (
            "Chromium started without a sandbox the sandbox check can prove: fake: no "
            f"renderer could be read. {HOST_FIXES.get(sys.platform, OTHER_FIX)}"
        ),
        "a1: never bound",
    ]
    assert document["infrastructure_events"] == {"count": 0, "events": None}
    assert files(run) == ["explore.json"]
    assert app.paths() == []


def test_an_unreachable_start_origin_is_exit_13_not_4(app: App, tmp_path: Path) -> None:
    port = unused_port()

    run = explored(app, tmp_path, Scripted(), start=f"http://127.0.0.1:{port}")

    document = written(run)
    assert (run.outcome, run.exit_code) == ("unreachable", 13)
    assert document["non_resumable"] == []
    assert document["reasons"] == [
        (
            "the start URL didn't load: Error: Page.goto: net::ERR_EMPTY_RESPONSE at "
            f"http://127.0.0.1:{port}/page/shop"
        ),
        "a1: never bound",
    ]
    events = document["infrastructure_events"]["events"]
    assert [(event["host"], event["port"]) for event in events] == [("127.0.0.1", port)]
    assert run.record is not None
    assert [line["state"] for line in read_steps(run.record / "attempt-1")] == [
        "intent"
    ]


def test_a_start_url_that_doesnt_load_takes_no_turn(app: App, tmp_path: Path) -> None:
    # Chromium refuses a redirect to a data: URL: no block, no event.
    run = explored(
        app, tmp_path, Scripted(), page="/redirect?to=data:text/html,elsewhere"
    )

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == [
        (
            "the start URL didn't load: Error: Page.goto: net::ERR_UNSAFE_REDIRECT at "
            f"{app.origin}/redirect?to=data:text/html,elsewhere"
        ),
        "a1: never bound",
    ]
    assert files(run) == ["attempt-1/steps.jsonl", "explore.json"]


@pytest.mark.usefixtures("secret")
def test_a_session_with_another_redactor_is_refused(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unscanned(*args: Any, **kwargs: Any) -> Any:
        return open_browser_session(*args, **(kwargs | {"redactor": Redactor([])}))

    monkeypatch.setattr(explore, "open_browser_session", unscanned)

    with pytest.raises(
        ValueError, match=r"^the browser session must scan with the run's redactor$"
    ):
        explored(app, tmp_path, Scripted(), config=SECRETS, preconditions=ACCOUNT)

    assert not list(tmp_path.rglob("steps.jsonl"))
    assert app.paths() == []


# Why a run stopped once its record took no more lines.
UNWRITTEN = "the run record took no more lines, so the attempt stopped"


@pytest.mark.parametrize(
    "case",
    [
        (0, (), ("record_error", 15), []),
        (1, (FILL,), ("non_resumable", 4), [{"part": "attempt-1", "seq": 1}]),
    ],
    ids=["the start URL's", "an action's"],
)
def test_a_completion_line_that_cannot_be_written_ends_the_run(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[int, tuple[Move, ...], tuple[str, int], list[dict[str, object]]],
) -> None:
    seq, moves, outcome, non_resumable = case
    completed = RunRecord.step_completed

    def failing(
        record: RunRecord,
        step: int,
        *,
        locator_used: int | None,
        settled: Settled,
        **kwargs: Any,
    ) -> None:
        if step == seq:
            raise OSError(errno.EIO, "fake completion failure")
        completed(record, step, locator_used=locator_used, settled=settled, **kwargs)

    monkeypatch.setattr(RunRecord, "step_completed", failing)

    run = explored(app, tmp_path, Scripted(*moves))

    document = written(run)
    assert (run.outcome, run.exit_code) == outcome
    assert document["reasons"] == [UNWRITTEN, "a1: never bound"]
    assert document["non_resumable"] == non_resumable
    assert run.document == document


# Why a run stopped with traffic whose outcome is unknown, plain and tunnelled.
PLAIN = "a request was still being answered when the browser closed"
TUNNEL = (
    "an encrypted or WebSocket connection carried data, and the proxy can't see "
    "whether its requests were answered"
)


def test_uncertain_plain_traffic_is_exit_14(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Settling stops waiting for the held payment after a second.
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 1)
    config = ProjectConfig.model_validate({"budgets": {"actions_per_attempt": 1}})

    run = explored(app, tmp_path, Scripted(PAY), page="/page/pay", config=config)

    document = written(run)
    assert (run.outcome, run.exit_code) == ("reset_unsafe", 14)
    assert document["reasons"] == [
        "the attempt took all 1 of its turns",
        PLAIN,
        "a1: never bound",
    ]
    assert document["phases"] == [CLEAN_PHASE | {"plain_uncertain": 1}]
    assert document["non_resumable"] == []


def test_an_uncertain_tunnel_is_exit_14(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        PAGES,
        "socket",
        """<p id="status">Not saved</p><script>
            new WebSocket(`ws://${location.host}/did/socket`);
        </script>""",
    )

    run = explored(app, tmp_path, Scripted(REFUSED), page="/page/socket")

    document = written(run)
    assert (run.outcome, run.exit_code) == ("reset_unsafe", 14)
    assert document["reasons"] == [
        "the model refused its turn",
        TUNNEL,
        "a1: never bound",
    ]
    assert document["phases"] == [CLEAN_PHASE | {"tunnel_uncertain": 1}]


def paying_until_expired(
    app: App, limits: list[asyncio.Timeout], egress: EgressGate | None = None
) -> Move:
    """The pay button's click, after which, once the app holds the payment,
    `egress` records a block, if given, and the minutes budget runs out."""
    expiry: list[asyncio.Task[None]] = []

    async def expire() -> None:
        await until(lambda: "/held/pay" in app.paths())
        if egress is not None:
            egress.record_refusal("analytics.example.test", 80, "host", "fake refusal")
        limits[-1].reschedule(asyncio.get_running_loop().time())

    def move(messages: Sequence[BaseMessage]) -> Any:
        expiry.append(asyncio.get_running_loop().create_task(expire()))
        return PAY(messages)

    return move


def test_the_minutes_budget_expiring_mid_dispatch_is_exit_4(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = deadlines(monkeypatch)

    run = explored(
        app, tmp_path, Scripted(paying_until_expired(app, limits)), page="/page/pay"
    )

    document = written(run)
    assert (run.outcome, run.exit_code) == ("non_resumable", 4)
    assert document["reasons"] == [
        "the minutes budget ran out",
        PLAIN,
        "a1: never bound",
    ]
    assert document["non_resumable"] == [{"part": "attempt-1", "seq": 1}]


def test_a_block_with_an_unresolved_side_effect_intent_exits_6_and_lists_it_non_resumable(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = deadlines(monkeypatch)
    egress = gate(allowed=(app.origin,))

    run = explored(
        app,
        tmp_path,
        Scripted(paying_until_expired(app, limits, egress)),
        page="/page/pay",
        egress=egress,
    )

    document = written(run)
    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    assert document["non_resumable"] == [{"part": "attempt-1", "seq": 1}]
    assert document["egress"] == {
        "error_code": "egress_blocked",
        "refused": [{"host": "analytics.example.test", "port": 80}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }


def test_an_undeclared_host_while_navigating_ends_the_run(
    app: App, tmp_path: Path
) -> None:
    away = tool("navigate", lambda _: {"path": "/page/refused-script"})
    client = Scripted(away, FILL)

    run = explored(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    assert written(run)["reasons"] == ["a1: never bound"]
    assert run.record is not None
    assert json.loads((run.record / "egress.json").read_text()) == {
        "error_code": "egress_blocked",
        "refused": [{"host": "analytics.example.test", "port": 80}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }
    # The block ended the turns: the second move was never asked for.
    assert len(client.requests) == 1


def test_an_unresolved_intent_gives_exit_4_and_ends_the_turns(
    app: App, tmp_path: Path
) -> None:
    fill_button = tool(
        "fill",
        lambda m: {
            "ref": ref(m, "button", "Save"),
            "meaning": "the shop's save button",
            "text": "x",
        },
    )
    client = Scripted(fill_button, FILL)

    run = explored(app, tmp_path, client)

    document = written(run)
    assert (run.outcome, run.exit_code) == ("non_resumable", 4)
    assert document["non_resumable"] == [{"part": "attempt-1", "seq": 1}]
    assert document["reasons"] == [
        (
            "step 1 failed after it was dispatched, so this attempt has ended: Error: "
            "fill: the element didn't take the value: it takes no text, or its page "
            "changed the value"
        ),
        "a1: never bound",
    ]
    assert len(client.requests) == 1


def proxies(monkeypatch: pytest.MonkeyPatch) -> list[tuple[EgressProxy, int]]:
    """Each egress proxy the run opens, with its first phase's port."""
    opened: list[tuple[EgressProxy, int]] = []

    class Opened(EgressProxy):
        async def __aenter__(self) -> Self:
            await super().__aenter__()
            opened.append((self, int(self.url.rsplit(":", 1)[1])))
            return self

    monkeypatch.setattr(explore, "EgressProxy", Opened)
    return opened


# What a listener retired after an accept error leaves in the gate.
RETIRED = (
    "the egress proxy stopped accepting the browser's connections after an "
    "accept error (EMFILE)"
)


def test_a_listener_retired_during_the_attempt_ends_it_with_exit_13(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = proxies(monkeypatch)
    accept = socket.socket.accept

    def accepting(listener: socket.socket) -> tuple[socket.socket, object]:
        # Once the fill's intent is on disk, its page's fetch finds the
        # proxy's listener out of file descriptors.
        filling = app.record is not None and any(
            line["seq"] == 1 for line in read_steps(app.record)
        )
        if filling and opened and listener.getsockname()[1] == opened[0][1]:
            raise OSError(errno.EMFILE, "Too many open files")
        return accept(listener)

    monkeypatch.setattr(socket.socket, "accept", accepting)
    client = Scripted(FILL, CLICK)

    run = explored(app, tmp_path, client)

    document = written(run)
    assert (run.outcome, run.exit_code) == ("unreachable", 13)
    assert document["reasons"] == ["a1: never bound"]
    assert document["infrastructure_events"] == {
        "count": 1,
        "events": [{"host": "127.0.0.1", "port": opened[0][1], "cause": RETIRED}],
    }
    assert document["non_resumable"] == []
    assert len(client.requests) == 1


def test_a_block_recorded_during_teardown_decides_the_outcome(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = proxies(monkeypatch)
    egress = gate(allowed=(app.origin,))
    late: list[socket.socket] = []

    async def refused() -> None:
        # A late request of the browser's, as it closes.
        late.append(socket.create_connection(("127.0.0.1", opened[0][1])))
        late[0].sendall(get("http://evil.example.test/", close=True))
        await until(lambda: bool(egress.refusals))

    while_closing(monkeypatch, refused)

    try:
        run = explored(app, tmp_path, Scripted(REFUSED), egress=egress)
    finally:
        for connection in late:
            connection.close()

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    document = written(run)
    assert document["reasons"] == ["the model refused its turn", "a1: never bound"]
    assert document["egress"] == {
        "error_code": "egress_blocked",
        "refused": [{"host": "evil.example.test", "port": 80}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }


def test_a_planned_check_that_reads_false_is_a_reason_never_a_violation(
    app: App, tmp_path: Path
) -> None:
    run = explored(app, tmp_path, Scripted(ASSERT_NOW, REFUSED))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == ["the model refused its turn", "a1: failed"]


def test_one_egress_proxy_serves_the_attempt_and_is_closed_after_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = proxies(monkeypatch)

    explored(app, tmp_path, Scripted(FILL, CLICK, ASSERT, FINISH))

    assert len(opened) == 1
    # Every request of the attempt reached the app, through it.
    assert app.paths() == ["/page/shop", "/did/fill", "/write/save"]
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", opened[0][1]), timeout=5)


def test_a_check_whose_only_offered_element_is_outside_its_region_gives_up_naming_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "article", ARTICLE)

    def lower(messages: Sequence[BaseMessage]) -> dict[str, object]:
        refs = re.findall(
            r'button "Unfavorite Article \(1\)"[^\n]*?\[ref=(e\d+)\]', messages[-1].text
        )
        return {"check_id": "a1", "ref": refs[1]}

    client = Scripted(tool("assert_check", lower), REFUSED)
    config = ProjectConfig.model_validate(
        {"subjects": [{"spec": "shop", "expect": 0} | BUTTON_ROW.model_dump()]}
    )
    plan = CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": FAVORITE,
                    "claim": "shows one favorite",
                    "checks": [
                        {
                            "check": "text_in_target",
                            "target_meaning": FAVORITE,
                            "text": "Unfavorite Article (1)",
                        }
                    ],
                }
            ],
            "requires": [],
        }
    )

    run = explored(
        app, tmp_path, client, page="/page/article", config=config, plan=plan
    )

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == ["the model refused its turn", "a1: never bound"]
    results = re.search(r"<results>\n(.*?)\n</results>", client.requests[1][-1].text)
    assert results is not None
    assert results[1] == (
        "assert_check did not run: this check's subject binds only inside "
        "div.banner, and this element is outside it"
    )
