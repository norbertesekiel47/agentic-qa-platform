"""The stops of an attempt's phase that combine, or that a run's own state
makes (#53 P15b; ADR-0024's #53 P15b amendment): a record that doesn't scan
with the run's redactor, checks kept when the minutes budget runs out, a
browser whose selector engines were never registered, blocks and events
together with other stops, and a journal poisoned mid-turn. Real Chromium
against the executor's fixture app, with a scripted model."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner import explore
from aqa_runner.binding import register_binding_engine
from aqa_runner.browser_session import open_browser_session
from aqa_runner.chat_client import Reply
from aqa_runner.explore import Run
from aqa_runner.explore_result import ExploreEnd, present
from aqa_runner.locator_generation import TargetUses
from aqa_runner.redaction import Redactor
from langchain_core.messages import BaseMessage
from playwright.async_api import Browser, Playwright

from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import App, read_steps, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    NO_RESPONSE,
    REFUSED,
    Scripted,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE
from packages.runner.tests.test_explore_graph import (
    ACCOUNT,
    ASSERT_NOW,
    SECRETS,
    UNWRITTEN,
    deadlines,
    expiring,
    explored,
    files,
    written,
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST_PASSWORD's fake value, for a spec that references it."""
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)


@pytest.mark.usefixtures("secret")
@pytest.mark.parametrize(
    ("preconditions", "refusal", "seen"),
    [
        # No browser opens: finalization refuses the record.
        (
            "  reset: { http: POST /status/500 }\n",
            "the run's record must scan with the run's redactor",
            ["/status/500"],
        ),
        # A browser opens: the attempt refuses its part before step 0.
        ("", "an attempt's record must be scanned by its session's redactor", []),
    ],
    ids=["no browser", "a browser"],
)
def test_a_run_record_with_another_redactor_is_refused(
    app: App, tmp_path: Path, preconditions: str, refusal: str, seen: list[str]
) -> None:
    client = Scripted()

    with pytest.raises(ValueError, match=f"^{re.escape(refusal)}$"):
        explored(
            app,
            tmp_path,
            client,
            config=SECRETS,
            preconditions=ACCOUNT + preconditions,
            scan=Redactor([]),
        )

    assert not list(tmp_path.rglob("explore.json"))
    assert not list(tmp_path.rglob("steps.jsonl"))
    assert client.requests == []
    assert app.paths() == seen


def test_the_minutes_budget_keeps_each_evaluated_checks_result(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = deadlines(monkeypatch)

    run = explored(app, tmp_path, Scripted(ASSERT_NOW, expiring(limits)))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == ["the minutes budget ran out", "a1: failed"]
    # The cancelled call left no record of its turn.
    assert files(run) == [
        "attempt-1/steps.jsonl",
        "costs.jsonl",
        "explore.json",
        "turns/1.json",
    ]


def test_a_block_with_the_minutes_budget_run_out_exits_6(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = deadlines(monkeypatch)
    egress = gate(allowed=(app.origin,))

    def refused() -> None:
        egress.record_refusal("analytics.example.test", 80, "host", "fake refusal")

    run = explored(app, tmp_path, Scripted(expiring(limits, refused)), egress=egress)

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    assert written(run)["reasons"] == ["the minutes budget ran out", "a1: never bound"]
    assert files(run) == [
        "attempt-1/steps.jsonl",
        "egress.json",
        "explore.json",
    ]
    assert run.record is not None
    assert json.loads((run.record / "egress.json").read_text()) == {
        "error_code": "egress_blocked",
        "refused": [{"host": "analytics.example.test", "port": 80}],
        "overflowed": False,
        "hosts_and_ports_withheld": False,
    }


async def nothing_registered(_playwright: Playwright) -> None:
    pass


@pytest.mark.parametrize(
    ("registered", "unregistered"),
    [(nothing_registered, "binding"), (register_binding_engine, "identity")],
    ids=["none", "binding only"],
)
def test_a_browser_whose_selector_engines_were_never_registered_exits_10(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registered: Callable[[Playwright], Awaitable[None]],
    unregistered: str,
) -> None:
    monkeypatch.setattr(explore, "register_identity_engine", registered)

    run = explored(app, tmp_path, Scripted(CLICK))

    assert (run.outcome, run.exit_code) == ("no_sandbox", 10)
    assert written(run)["reasons"] == [
        (
            f"the {unregistered} engine isn't registered: call register_identity_engine "
            "before the browser session opens"
        ),
        "a1: never bound",
    ]
    assert run.record is not None
    assert [
        (line["seq"], line["state"]) for line in read_steps(run.record / "attempt-1")
    ] == [(0, "intent"), (0, "completed")]
    assert files(run) == [
        "attempt-1/steps.jsonl",
        "costs.jsonl",
        "explore.json",
        "turns/1.json",
    ]


def overtaking(monkeypatch: pytest.MonkeyPatch, limits: list[asyncio.Timeout]) -> None:
    """Have the minutes budget run out as the browser starts to close,
    wherever its span still holds the close."""
    close = Browser.close

    async def overtaken(browser: Browser, *, reason: str | None = None) -> None:
        # Once the budget's span has ended, its deadline can't move, and
        # nothing is left for it to cancel.
        with suppress(RuntimeError):
            limits[-1].reschedule(asyncio.get_running_loop().time())
        await close(browser, reason=reason)

    monkeypatch.setattr(Browser, "close", overtaken)


@pytest.mark.usefixtures("secret")
def test_a_refused_session_stays_refused_when_the_minutes_budget_runs_out_as_it_closes(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overtaking(monkeypatch, deadlines(monkeypatch))

    def unscanned(*args: Any, **kwargs: Any) -> Any:
        return open_browser_session(*args, **(kwargs | {"redactor": Redactor([])}))

    monkeypatch.setattr(explore, "open_browser_session", unscanned)

    with pytest.raises(
        ValueError, match=r"^the browser session must scan with the run's redactor$"
    ):
        explored(app, tmp_path, Scripted(), config=SECRETS, preconditions=ACCOUNT)

    assert not list(tmp_path.rglob("explore.json"))


def test_an_unregistered_engine_exits_10_when_the_minutes_budget_runs_out_as_the_browser_closes(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overtaking(monkeypatch, deadlines(monkeypatch))
    monkeypatch.setattr(explore, "register_identity_engine", nothing_registered)

    run = explored(app, tmp_path, Scripted(CLICK))

    assert (run.outcome, run.exit_code) == ("no_sandbox", 10)
    assert written(run)["reasons"] == [
        (
            "the binding engine isn't registered: call register_identity_engine "
            "before the browser session opens"
        ),
        "a1: never bound",
    ]


def test_explore_json_that_cannot_be_written_ends_the_run_15(
    app: App, tmp_path: Path
) -> None:
    def taken(_messages: Sequence[BaseMessage]) -> Reply:
        # A file is already in explore.json's place, and a record never
        # writes over one.
        assert app.record is not None
        (app.record.parent / "explore.json").write_text("taken\n")
        return REFUSED

    run = explored(app, tmp_path, Scripted(taken))

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.document["reasons"] == [
        "the model refused its turn",
        UNWRITTEN,
        "a1: never bound",
    ]
    assert run.document["exit_code"] == 15
    assert run.record is not None
    assert (run.record / "explore.json").read_text() == "taken\n"


@pytest.mark.usefixtures("secret")
def test_a_document_that_spells_a_bound_value_is_refused_never_redacted_into_place(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leaking(end: ExploreEnd) -> dict[str, object]:
        # A document whose scan let a bound value through, as none of
        # present's may.
        document = present(end)
        reasons = document["reasons"]
        assert isinstance(reasons, list)
        return document | {"reasons": [*reasons, FAKE_VALUE]}

    monkeypatch.setattr(explore, "present", leaking)

    run = explored(
        app, tmp_path, Scripted(REFUSED), config=SECRETS, preconditions=ACCOUNT
    )

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.document["reasons"] == [
        "the model refused its turn",
        UNWRITTEN,
        "a1: never bound",
        FAKE_VALUE,
    ]
    assert files(run) == ["attempt-1/steps.jsonl", "costs.jsonl", "turns/1.json"]


def test_a_timeout_that_isnt_the_minutes_budgets_own_propagates(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timed_out(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(explore, "reset", timed_out)

    with pytest.raises(TimeoutError):
        explored(
            app,
            tmp_path,
            Scripted(),
            preconditions="  reset: { http: POST /write/reset }\n",
        )

    assert not list(tmp_path.rglob("explore.json"))
    assert app.paths() == []


def test_the_spend_budget_is_the_projects_model_usd(app: App, tmp_path: Path) -> None:
    config = ProjectConfig.model_validate({"budgets": {"model_usd": 0.008}})

    run = explored(app, tmp_path, Scripted(FILL, CLICK, FILL), config=config)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == [
        "the spend budget ran out on this turn, so its call didn't run",
        "a1: never bound",
    ]
    assert run.cost == Decimal("0.009")


def test_a_block_with_no_model_response_exits_6(app: App, tmp_path: Path) -> None:
    egress = gate(allowed=(app.origin,))

    def unanswered(_messages: Sequence[BaseMessage]) -> Reply:
        egress.record_refusal("analytics.example.test", 80, "host", "fake refusal")
        raise NO_RESPONSE

    run = explored(app, tmp_path, Scripted(unanswered), egress=egress)

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    assert written(run)["reasons"] == [
        "the model gave no response (APIConnectionError)",
        "a1: never bound",
    ]
    assert files(run) == [
        "attempt-1/steps.jsonl",
        "egress.json",
        "explore.json",
        "turns/1.json",
    ]


def test_an_infrastructure_event_during_an_actions_preparation_exits_13_before_its_intent(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    egress = gate(allowed=(app.origin,))
    generate = TargetUses.add

    async def failing(uses: TargetUses, *args: Any) -> int:
        added = await generate(uses, *args)
        egress.record_failure("127.0.0.1", 9, "fake: connection refused")
        return added

    monkeypatch.setattr(TargetUses, "add", failing)

    run = explored(app, tmp_path, Scripted(CLICK, CLICK), egress=egress)

    assert (run.outcome, run.exit_code) == ("unreachable", 13)
    assert written(run)["reasons"] == ["a1: never bound"]
    assert run.record is not None
    assert [
        (line["seq"], line["state"]) for line in read_steps(run.record / "attempt-1")
    ] == [(0, "intent"), (0, "completed")]
    assert app.paths() == ["/page/shop"]


def test_a_poisoned_steps_journal_mid_turn_exits_15_with_no_dispatch(
    app: App, tmp_path: Path
) -> None:
    runs: list[Run] = []

    def poisoning(messages: Sequence[BaseMessage]) -> Reply:
        # A write to the attempt's journal fails, before the click's
        # intent: the journal takes no more lines.
        part = runs[0].attempt
        (part.path / "steps.jsonl").chmod(0o444)
        with pytest.raises(PermissionError):
            part.step_completed(1, locator_used=None, settled="idle")
        return CLICK(messages)

    run = explored(app, tmp_path, Scripted(FILL, poisoning), runs=runs)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert written(run)["reasons"] == [UNWRITTEN, "a1: never bound"]
    assert run.record is not None
    assert len(read_steps(run.record / "attempt-1")) == 4
    assert app.paths() == ["/page/shop", "/did/fill"]
