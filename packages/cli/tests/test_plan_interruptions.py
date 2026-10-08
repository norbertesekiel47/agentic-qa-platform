"""`aqa explore --plan-only` stopped while planning (API.md §7; ADR-0024's
#161 amendment): what the plan call was billed before Ctrl-C or a
cancellation is written to the run record once, as an `interrupted` outcome,
and the interruption goes on as it came: Ctrl-C exits 130 and a
cancellation propagates."""

import asyncio
import io
import os
import selectors
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import langsmith
import pytest
import typer.testing
from aqa_cli import explore as explore_module
from aqa_cli.main import app
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.model_costs import Usage
from aqa_runner.chat_client import Reply

from packages.cli.tests.test_explore import (
    CIRCULAR,
    CONFIG,
    PLAN,
    SONNET,
    plan_with,
    project,
    record_of,
)
from packages.runner.tests.test_model_router import reply

OPUS = "claude-opus-5-5"
FALLBACK = CONFIG + "roles: { navigator: { fallback: claude-opus-5-5 } }\n"
KEY = "fake-key-for-tests"
UNWRITTEN = (
    "interrupted: the run record could not be written, so what the plan call "
    "was billed is not kept"
)

Kind = type[KeyboardInterrupt] | type[asyncio.CancelledError]
KINDS = [KeyboardInterrupt, asyncio.CancelledError]
Step = Reply | Callable[[], Awaitable[Reply]]

# A corrected plan whose check is a pattern: judging it starts a search child.
SEARCHED = plan_with(
    [
        {
            "expect_index": 0,
            "subject": "the payment error message",
            "claim": "says the card has expired",
            "checks": [
                {
                    "check": "text_in_target",
                    "target_meaning": "the error message under the payment form",
                    "pattern": "(?i)card has expired",
                }
            ],
        },
        PLAN.expectations[1].model_dump(exclude_none=True),
    ]
)


def answer(plan: CoveragePlan | None = None, *, input_tokens: int = 100) -> Reply:
    """A reply billed for `input_tokens` in and 7 out: at Sonnet's $2 and $10
    per million, 100 in cost $0.00027 and 200 in $0.00047."""
    usage = Usage(input_tokens=input_tokens, cached_input_tokens=0, output_tokens=7)
    return reply(parsed=plan, refused=plan is None, usage=usage)


class Scripted:
    """A model that takes each step in turn: a reply, or what the call does
    instead of answering."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)

    async def call(self, *_: object) -> Reply:
        step = self.steps.pop(0)
        return step if isinstance(step, Reply) else await step()


def raising(kind: Kind) -> Step:
    async def interrupted() -> Reply:
        raise kind()

    return interrupted


@pytest.fixture(autouse=True)
def fake_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)


def interrupted(
    kind: Kind, monkeypatch: pytest.MonkeyPatch, spec: Path, **models: Scripted
) -> list[str]:
    """Run explore until `kind` stops it, and what it said: Ctrl-C exits 130
    with its stderr, and a cancellation propagates with its notes."""
    monkeypatch.setattr(
        explore_module, "AnthropicClient", lambda model, _effort: models[model.name]
    )
    invoke = typer.testing.CliRunner().invoke
    if kind is KeyboardInterrupt:
        result = invoke(app, ["explore", str(spec), "--plan-only"])
        assert result.exit_code == 130, result.output
        return result.stderr.splitlines()
    with pytest.raises(asyncio.CancelledError) as raised:
        invoke(app, ["explore", str(spec), "--plan-only"])
    notes: list[str] = getattr(raised.value, "__notes__", [])
    return notes


def kept(root: Path) -> list[tuple[str, str, str]]:
    """The model, status and cost of each response the interrupted run's
    record kept."""
    record = record_of(root)
    assert (record["outcome"], record["reasons"]) == (
        "interrupted",
        ["planning interrupted"],
    )
    assert (record["plan"], record["plan_hash"]) == (None, None)
    return [(c["model"], c["status"], c["cost_usd"]) for c in record["calls"]]


@pytest.mark.parametrize("kind", KINDS)
def test_retry_interruption_keeps_first_cost_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    spec = project(tmp_path / "qa")

    interrupted(
        kind, monkeypatch, spec, **{SONNET: Scripted(answer(CIRCULAR), raising(kind))}
    )

    assert kept(tmp_path / "qa") == [(SONNET, "ok", "0.00027")]


def test_task_cancellation_keeps_completed_costs_and_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = project(tmp_path / "qa")

    async def cancelled() -> Reply:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return await asyncio.sleep(60, answer(PLAN))

    interrupted(
        asyncio.CancelledError,
        monkeypatch,
        spec,
        **{SONNET: Scripted(answer(CIRCULAR), cancelled)},
    )

    assert kept(tmp_path / "qa") == [(SONNET, "ok", "0.00027")]


@pytest.mark.parametrize("kind", KINDS)
def test_interrupted_retry_fallback_keeps_both_completed_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    spec = project(tmp_path / "qa", config=FALLBACK)
    sonnet = Scripted(answer(CIRCULAR), answer(input_tokens=200))

    interrupted(
        kind, monkeypatch, spec, **{SONNET: sonnet, OPUS: Scripted(raising(kind))}
    )

    # The fallback that never answered is no charge.
    assert kept(tmp_path / "qa") == [
        (SONNET, "ok", "0.00027"),
        (SONNET, "refusal", "0.00047"),
    ]


def test_interrupted_correction_keeps_initial_fallback_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = project(tmp_path / "qa", config=FALLBACK)
    sonnet = Scripted(answer(), raising(asyncio.CancelledError))
    # Opus: $4 and $20 per million tokens.
    opus = Scripted(answer(CIRCULAR))

    interrupted(
        asyncio.CancelledError, monkeypatch, spec, **{SONNET: sonnet, OPUS: opus}
    )

    assert kept(tmp_path / "qa") == [
        (SONNET, "refusal", "0.00027"),
        (OPUS, "ok", "0.00054"),
    ]


def test_cancelled_plan_judgement_keeps_every_completed_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = project(tmp_path / "qa")

    async def corrected_then_cancelled() -> Reply:
        # The cancellation lands at the next suspension: the corrected plan's
        # pattern search, after its response was priced.
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        return answer(SEARCHED, input_tokens=200)

    interrupted(
        asyncio.CancelledError,
        monkeypatch,
        spec,
        **{SONNET: Scripted(answer(CIRCULAR), corrected_then_cancelled)},
    )

    assert kept(tmp_path / "qa") == [
        (SONNET, "ok", "0.00027"),
        (SONNET, "ok", "0.00047"),
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_interruption_before_a_reply_records_no_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    spec = project(tmp_path / "qa")

    interrupted(kind, monkeypatch, spec, **{SONNET: Scripted(raising(kind))})

    assert kept(tmp_path / "qa") == []


@pytest.mark.parametrize("kind", KINDS)
def test_persistence_failure_does_not_replace_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    spec = project(tmp_path / "qa")

    async def taken_then_interrupted() -> Reply:
        # plan.json is written once, so a file already there refuses the write.
        [run] = (tmp_path / "qa" / ".aqa" / "runs").iterdir()
        (run / "plan.json").write_text("written first\n")
        raise kind()

    said = interrupted(
        kind,
        monkeypatch,
        spec,
        **{SONNET: Scripted(answer(CIRCULAR), taken_then_interrupted)},
    )

    assert UNWRITTEN in said
    [written] = (tmp_path / "qa" / ".aqa" / "runs").glob("*/plan.json")
    assert written.read_text() == "written first\n"


def test_a_closed_stderr_does_not_replace_the_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = project(tmp_path / "qa")

    async def taken() -> Reply:
        [run] = (tmp_path / "qa" / ".aqa" / "runs").iterdir()
        (run / "plan.json").write_text("written first\n")
        raise asyncio.CancelledError

    sonnet = Scripted(answer(CIRCULAR), taken)
    monkeypatch.setattr(explore_module, "AnthropicClient", lambda *_: sonnet)
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stderr", closed)

    # Called as the command's function: CliRunner itself fails on a closed stderr.
    with pytest.raises(asyncio.CancelledError) as raised:
        explore_module.explore(spec, plan_only=True)

    assert raised.value.__notes__ == [UNWRITTEN]


# The real command in a child process, its model scripted: the first answer
# is billed, and the corrective request waits until the child is signalled.
CHILD = """
import asyncio, sys
from aqa_cli import explore
from aqa_cli.main import app
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.model_costs import Usage
from aqa_runner.chat_client import Reply
from langchain_core.messages import AIMessage

class Model:
    calls = 0

    async def call(self, messages, tools, schema):
        Model.calls += 1
        if Model.calls == 1:
            usage = Usage(input_tokens=100, cached_input_tokens=0, output_tokens=7)
            plan = CoveragePlan.model_validate_json(sys.argv[2])
            return Reply(AIMessage(content=""), usage, False, plan)
        print("SECOND_REQUEST_READY", flush=True)
        await asyncio.Event().wait()

explore.AnthropicClient = lambda model, effort: Model()
app(prog_name="aqa", args=["explore", sys.argv[1], "--plan-only"])
"""


def test_sigint_during_corrective_request_exits_130_and_keeps_first_cost(
    tmp_path: Path,
) -> None:
    spec = project(tmp_path / "qa")
    command = [sys.executable, "-I", "-B", "-c", CHILD, str(spec)]
    environment = {"PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": KEY}

    with subprocess.Popen(
        [*command, CIRCULAR.model_dump_json()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    ) as child:
        try:
            assert child.stdout is not None
            ready = selectors.DefaultSelector()
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=60), "the corrective request never started"
            assert child.stdout.readline() == "SECOND_REQUEST_READY\n"
            os.kill(child.pid, signal.SIGINT)
            _, stderr = child.communicate(timeout=30)
        finally:
            # Only this recorded child is ever signalled; leaving the block
            # waits for it, so it never outlives the test.
            if child.poll() is None:
                child.kill()

    assert child.returncode == 130, stderr
    assert kept(tmp_path / "qa") == [(SONNET, "ok", "0.00027")]
