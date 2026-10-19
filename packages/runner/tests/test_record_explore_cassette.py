"""The recorder's explore case (#53 P21a): the fixture explore recorded in
real Chromium from a local stand-in for the provider, with a fake key, within
the recording's spend bound. No test here reaches a provider."""

import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from aqa_core.config import ProjectConfig
from aqa_core.spec import Spec

from packages.runner.tests import record_cassettes as recorder
from packages.runner.tests.executor_fixtures import PAGES
from packages.runner.tests.explore_graph_fixtures import PLAN
from packages.runner.tests.record_cassettes import EXPLORE, record
from packages.runner.tests.test_explore_confirm import resets
from packages.runner.tests.test_explore_graph import configured, shop_spec

pytestmark = pytest.mark.usefixtures("reset_tracing")

# Not a credential, and named so (AGENTS.md rule 9).
KEY = "fake-key-for-the-explore-recording"
PREVIOUS = "previous cassette\n"
HELLO = {
    "id": "msg_fake",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "stop_sequence": None,
}
GOOD_PLAN = PLAN.model_dump(mode="json", exclude_none=True)
# A plan whose expectation names one the spec doesn't have: make_plan asks
# once more.
MISFIT = GOOD_PLAN | {
    "expectations": [GOOD_PLAN["expectations"][0] | {"expect_index": 1}]
}
# The path each turn takes on the shop page: fill, save, check, finish.
MOVES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("fill", {"ref": "e3", "meaning": "the shop's name field", "text": "Ada"}),
    ("click", {"ref": "e13", "meaning": "the shop's save button"}),
    ("assert_check", {"check_id": "a1", "ref": "e21"}),
    ("finish", {"steps": [1, 2]}),
)
FIVE_COSTS = ["0.000048", "0.00006", "0.000072", "0.000084", "0.000096"]
CONFIRMED = {"exit_code": 0, "outcome": "compiled", "published": "confirmed"}

type Answer = tuple[int, bytes]
# A status that closes the connection without an answer.
DROPPED = 0
OVERLOADED = json.dumps(
    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
).encode()


def answer(turn: int, content: list[dict[str, Any]], stop: str, **usage: int) -> Answer:
    """A 200 answer; unless `usage` says otherwise, each turn reports 6 more
    input tokens than the one before, so no two cost the same."""
    usage = usage or {"input_tokens": 9 + 6 * turn, "output_tokens": 3}
    body = HELLO | {"content": content, "stop_reason": stop, "usage": usage}
    return 200, json.dumps(body).encode()


def planned(turn: int, plan: dict[str, Any], **usage: int) -> Answer:
    return answer(
        turn, [{"type": "text", "text": json.dumps(plan)}], "end_turn", **usage
    )


def called(turn: int, name: str, args: dict[str, Any], **usage: int) -> Answer:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"notes before {name}"},
        {"type": "tool_use", "id": f"toolu_fake_{turn}", "name": name, "input": args},
    ]
    return answer(turn, content, "tool_use", **usage)


def explore_answers(
    *plans: dict[str, Any], usage: Callable[[int], dict[str, int]] = lambda _: {}
) -> list[Answer]:
    """The scripted explore: an answer for each plan, then the four turns,
    each turn's usage as `usage` gives it."""
    first = [
        planned(turn, plan, **usage(turn))
        for turn, plan in enumerate(plans or (GOOD_PLAN,))
    ]
    return first + [
        called(len(first) + turn, name, args, **usage(len(first) + turn))
        for turn, (name, args) in enumerate(MOVES)
    ]


@dataclass
class Provider:
    """A local stand-in for the provider's API: each request gets the next of
    its answers, the last one again after it, with a request ID; every body
    sent is kept."""

    answers: list[Answer]
    sent: list[bytes] = field(default_factory=list)


@pytest.fixture
def provider(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[list[Answer]], Provider]]:
    """`provider(answers)` starts a stand-in and points the recorder at it
    with the fake key."""
    servers: list[socketserver.ThreadingTCPServer] = []

    def start(answers: list[Answer]) -> Provider:
        stand_in = Provider(answers)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.rfile.readline()
                length = 0
                while (line := self.rfile.readline().strip()) != b"":
                    name, _, value = line.decode().partition(":")
                    if name.lower() == "content-length":
                        length = int(value)
                stand_in.sent.append(self.rfile.read(length))
                count = len(stand_in.sent)
                status, body = stand_in.answers[min(count, len(stand_in.answers)) - 1]
                if status == DROPPED:
                    return
                self.wfile.write(
                    f"HTTP/1.1 {status} {'OK' if status == 200 else 'Overloaded'}\r\n"
                    "content-type: application/json\r\n"
                    f"request-id: req_fake_{count:02}\r\n"
                    f"content-length: {len(body)}\r\nconnection: close\r\n\r\n".encode()
                    + body
                )

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        monkeypatch.setenv(
            "ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}"
        )
        monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
        return stand_in

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A cassette library holding an earlier explore cassette."""
    path = tmp_path / "library"
    path.mkdir()
    (path / f"{EXPLORE}.yaml").write_text(PREVIOUS)
    return path


@pytest.fixture
def attempt(tmp_path: Path) -> Path:
    """The round's first attempt directory, not yet made."""
    return tmp_path / "round" / "001"


def receipt(attempt: Path) -> dict[str, Any]:
    kept: dict[str, Any] = json.loads((attempt / "receipt.json").read_text())
    return kept


def bodies(cassette: Path) -> list[dict[str, Any]]:
    """The request body of each interaction a cassette holds."""
    interactions = yaml.safe_load(cassette.read_text())["interactions"]
    return [json.loads(each["request"]["body"]) for each in interactions]


def unhooked(root: Path) -> Spec:
    """The fixture spec without its reset hook."""
    return shop_spec(
        configured(root, recorder.FIXTURE_CONFIG),
        recorder.FIXTURE_CONFIG,
        "/page/shop",
        "",
    )


@pytest.mark.parametrize(
    "run", ["confirmed", "unconfirmed", "failed_confirmation", "corrected_plan"]
)
def test_an_explore_capture_promotes_only_a_confirmed_compile(
    provider: Callable[[list[Answer]], Provider],
    library: Path,
    attempt: Path,
    monkeypatch: pytest.MonkeyPatch,
    run: str,
) -> None:
    provider(
        explore_answers(MISFIT, GOOD_PLAN)
        if run == "corrected_plan"
        else explore_answers()
    )
    if run == "unconfirmed":
        monkeypatch.setattr(recorder, "fixture", unhooked)
    if run == "failed_confirmation":
        # The confirmation gives up on its check after 2 s, not 10.
        budgets = recorder.FIXTURE_CONFIG.budgets.model_dump(exclude_unset=True)
        quick = {"budgets": budgets | {"resolve_seconds": 2}}
        monkeypatch.setattr(
            recorder, "FIXTURE_CONFIG", ProjectConfig.model_validate(quick)
        )
        resets(
            monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", PAGES["shop-wrong"])
        )

    if run in {"confirmed", "corrected_plan"}:
        costs = [
            str(cost.cost_usd) for cost in record(EXPLORE, attempt, library=library)
        ]
        kept = receipt(attempt)
        promoted = (library / f"{EXPLORE}.yaml").read_text()
        assert promoted == (attempt / f"{EXPLORE}.yaml").read_text()
        assert KEY not in promoted
    else:
        with pytest.raises(ValueError, match="rejected"):
            record(EXPLORE, attempt, library=library)
        kept = receipt(attempt)
        costs = [cost["cost_usd"] for cost in kept["cost_records"]]
        assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS

    assert (kept["outcome"], kept["explore"]) == {
        "confirmed": ("accepted", CONFIRMED),
        "unconfirmed": (
            "rejected",
            {"exit_code": 0, "outcome": "compiled", "published": "unconfirmed"},
        ),
        "failed_confirmation": (
            "rejected",
            {"exit_code": 3, "outcome": "gave_up", "published": None},
        ),
        "corrected_plan": ("accepted", CONFIRMED),
    }[run]
    if run == "corrected_plan":
        assert costs == [*FIVE_COSTS, "0.000108"]
        assert [
            (
                "output_config" in body,
                body.get("tool_choice"),
                [m["role"] for m in body["messages"]],
            )
            for body in bodies(attempt / f"{EXPLORE}.yaml")
        ] == [
            (True, None, ["user"]),
            (True, None, ["user", "assistant", "user"]),
            *[(False, {"type": "auto"}, ["user"])] * 4,
        ]
    else:
        assert costs == FIVE_COSTS


def test_every_explore_interaction_is_priced_once(
    provider: Callable[[list[Answer]], Provider], library: Path, attempt: Path
) -> None:
    provider(explore_answers())

    record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    [costs] = (attempt / "qa" / ".aqa" / "runs").glob("*/costs.jsonl")
    lines = [json.loads(line) for line in costs.read_text().splitlines()]
    assert [line["cost_usd"] for line in lines] == FIVE_COSTS
    assert kept["cost_records"] == lines
    assert kept["router_records"] == lines
    assert [
        (each["status_code"], each["request_id"]) for each in kept["responses"]
    ] == [(200, f"req_fake_{count:02}") for count in range(1, 6)]
    assert kept["unpriced_responses"] == []
    assert kept["spend"] == {
        "run_usd": "0.20",
        "round_left_usd": "0.699946",
        "spent_usd": "0.000360",
        "refused": False,
        "overrun": False,
    }


def heavy(turn: int) -> dict[str, int]:
    """About $0.049 a call, within the input each request reserves: after
    three the run has spent $0.14706, under explore's own $0.15, and the
    fourth's worst case would take it past $0.20."""
    return {"input_tokens": 7000 + 10 * turn, "output_tokens": 3500}


def test_a_request_past_the_runs_limit_is_refused_before_it_is_sent(
    provider: Callable[[list[Answer]], Provider], library: Path, attempt: Path
) -> None:
    stand_in = provider(explore_answers(usage=heavy))

    with pytest.raises(ValueError, match="spend_refused"):
        record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    assert len(stand_in.sent) == 3
    assert (kept["outcome"], kept["explore"]) == (
        "spend_refused",
        {"exit_code": 11, "outcome": "no_response", "published": None},
    )
    assert [cost["cost_usd"] for cost in kept["cost_records"]] == [
        "0.049",
        "0.04902",
        "0.04904",
    ]
    assert kept["spend"] == {
        "run_usd": "0.20",
        "round_left_usd": "0.699946",
        "spent_usd": "0.14706",
        "refused": True,
        "overrun": False,
    }
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS


EARLIER = {
    "spent": "0.66",
    "unknown": None,
    "negative": "-1",
    "minus_infinity": "-Infinity",
    "not_a_number": "NaN",
    "infinity": "Infinity",
    "overran": "0.1",
}


@pytest.mark.parametrize("earlier", EARLIER)
def test_a_request_past_the_rounds_limit_is_refused_before_it_is_sent(
    provider: Callable[[list[Answer]], Provider],
    library: Path,
    attempt: Path,
    earlier: str,
) -> None:
    stand_in = provider(explore_answers())
    before = attempt.parent / "000"
    before.mkdir(parents=True)
    if EARLIER[earlier] is not None:
        spend = {"spent_usd": EARLIER[earlier], "overrun": earlier == "overran"}
        (before / "receipt.json").write_text(json.dumps({"spend": spend}))
    refusal = {
        "spent": "spend_refused",
        "overran": "an earlier attempt in this round billed past its reservation",
    }.get(earlier, "an earlier attempt in this round has no priced receipt")

    with pytest.raises(ValueError, match=refusal):
        record(EXPLORE, attempt, library=library)

    assert stand_in.sent == []
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS
    if earlier == "spent":
        assert receipt(attempt)["spend"] == {
            "run_usd": "0.20",
            "round_left_usd": "0.039946",
            "spent_usd": "0",
            "refused": True,
            "overrun": False,
        }
    else:
        assert not (attempt / "receipt.json").exists()


@pytest.mark.parametrize("failure", ["overloaded", "dropped"])
def test_an_overloaded_or_unanswered_request_is_sent_once(
    provider: Callable[[list[Answer]], Provider],
    library: Path,
    attempt: Path,
    failure: str,
) -> None:
    stand_in = provider(
        [(529, OVERLOADED) if failure == "overloaded" else (DROPPED, b"")]
    )

    with pytest.raises(ValueError, match="rejected"):
        record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    assert len(stand_in.sent) == 1
    assert (kept["outcome"], kept["explore"]) == (
        "missing_usage" if failure == "overloaded" else "provider_error",
        {"exit_code": 11, "outcome": "no_response", "published": None},
    )
    assert [each["status_code"] for each in kept["responses"]] == (
        [529] if failure == "overloaded" else []
    )
    # An error body is uninspectable, and it reached the SDK unread.
    assert (kept["withheld"], kept["scrubbed"]) == (
        ("uninspectable", [f"{EXPLORE}.yaml", "qa/.aqa"])
        if failure == "overloaded"
        else (None, [])
    )
    # No usage came back, so the plan request keeps its worst case.
    assert (kept["cost_records"], kept["spend"]) == (
        [],
        {
            "run_usd": "0.20",
            "round_left_usd": "0.699946",
            "spent_usd": "0.060496",
            "refused": False,
            "overrun": False,
        },
    )
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS


def test_a_response_billed_past_its_reservation_stops_the_round(
    provider: Callable[[list[Answer]], Provider], library: Path, attempt: Path
) -> None:
    # The plan's answer bills 50,000 input tokens, more than its request's
    # bytes plus 1,024.
    stand_in = provider(
        explore_answers(
            usage=lambda turn: (
                {"input_tokens": 50_000, "output_tokens": 3} if turn == 0 else {}
            )
        )
    )

    with pytest.raises(ValueError, match="overrun"):
        record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    assert len(stand_in.sent) == 1
    assert (kept["outcome"], kept["explore"]) == (
        "overrun",
        {"exit_code": 11, "outcome": "no_response", "published": None},
    )
    assert [cost["cost_usd"] for cost in kept["cost_records"]] == ["0.10003"]
    assert kept["spend"] == {
        "run_usd": "0.20",
        "round_left_usd": "0.699946",
        "spent_usd": "0.10003",
        "refused": True,
        "overrun": True,
    }
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS
    with pytest.raises(ValueError, match="billed past its reservation"):
        record(EXPLORE, attempt.parent / "002", library=library)
    assert len(stand_in.sent) == 1


def escaped(text: str) -> str:
    """`text` as JSON escapes, so the wire body never spells it."""
    return "".join(f"\\u{ord(char):04x}" for char in text)


def holding_the_key(directory: Path) -> list[str]:
    """Every file under `directory` that spells the key, or its escapes."""
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
        and any(
            form in path.read_bytes() for form in (KEY.encode(), escaped(KEY).encode())
        )
    )


def spilled(where: str) -> list[Answer]:
    """The scripted explore with the key spelled, as JSON escapes, in the
    plan's claim or the first turn's fill. A fill of the key never saves
    "Ada", so the attempt calls `finish` until its 12 actions run out: each
    answer reports its own usage, so each one's cost is told apart."""
    answers = explore_answers() + [called(turn, *MOVES[3]) for turn in range(5, 13)]
    if where == "plan":
        claim = GOOD_PLAN["expectations"][0] | {"claim": "KEY"}
        answers[0] = planned(0, GOOD_PLAN | {"expectations": [claim]})
    else:
        answers[1] = called(1, "fill", MOVES[0][1] | {"text": "KEY"})
    spelled = 0 if where == "plan" else 1
    answers[spelled] = (200, answers[spelled][1].replace(b"KEY", escaped(KEY).encode()))
    return answers


@pytest.mark.parametrize(
    "spill", ["plan", "turn", "turn_then_hashes_fail", "turn_then_receipt_fail"]
)
def test_an_explore_capture_withholds_a_key_in_its_plan_or_a_turn(
    provider: Callable[[list[Answer]], Provider],
    library: Path,
    attempt: Path,
    monkeypatch: pytest.MonkeyPatch,
    spill: str,
) -> None:
    provider(spilled("plan" if spill == "plan" else "turn"))
    failing = {
        "turn_then_hashes_fail": "request-hashes.json",
        "turn_then_receipt_fail": "receipt.json",
    }
    write_text = Path.write_text

    def write(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
        if path.name == failing.get(spill):
            raise OSError("fake disk failure")
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)

    with pytest.raises(
        ValueError, match="error" if spill == "turn_then_hashes_fail" else "credential"
    ):
        record(EXPLORE, attempt, library=library)

    assert holding_the_key(attempt) == []
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS
    if spill == "turn_then_receipt_fail":
        assert not (attempt / "receipt.json").exists()
        return
    kept = receipt(attempt)
    assert (kept["outcome"], kept["withheld"], kept["finalization_failures"]) == (
        "error" if spill == "turn_then_hashes_fail" else "credential",
        "credential",
        ["request_hashes"] if spill == "turn_then_hashes_fail" else [],
    )
    assert kept["scrubbed"] == [
        f"{EXPLORE}.yaml",
        "qa/.aqa",
        *(["qa/.compiled"] if spill == "plan" else []),
    ]


def test_a_scrub_that_fails_is_its_own_finalization_failure(
    provider: Callable[[list[Answer]], Provider],
    library: Path,
    attempt: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider(spilled("turn"))

    rmtree = shutil.rmtree

    def refuse(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == ".aqa":
            raise OSError("fake disk failure")
        rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", refuse)

    with pytest.raises(ValueError, match="error"):
        record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    assert (kept["outcome"], kept["finalization_failures"], kept["scrubbed"]) == (
        "error",
        ["scrub"],
        [f"{EXPLORE}.yaml"],
    )
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS


# main() in its own process, at a stand-in, with a library of its own, and
# promotion failing when asked; the attempt is in an ignored directory of
# the workspace, as main() requires.
INVOKE = """import socket, sys
from pathlib import Path
from aqa_runner import anthropic_client
from packages.runner.tests import record_cassettes as recorder
workspace, endpoint, library, failure = sys.argv[1:]
connect = socket.socket.connect
def local_only(sock, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
        raise AssertionError('nonlocal connection forbidden')
    return connect(sock, address)
socket.socket.connect = local_only
recorder.ROOT = Path(workspace)
anthropic_client.API_URL = endpoint
record = recorder.record
recorder.record = lambda name, attempt: record(name, attempt, library=Path(library))
replace = Path.replace
def promote(path, target):
    if path.name == 'promotion.yaml' and failure == 'promotion':
        raise OSError('fake disk failure')
    return replace(path, target)
Path.replace = promote
sys.argv = [sys.argv[0], 'explore_fixture', str(recorder.ROOT / '.scratch/round/001')]
recorder.main()
"""
STOPPED = (
    "Recording stopped; inspect the private attempt receipt before another call.\n"
)


@pytest.mark.parametrize("failure", ["spill", "promotion"])
def test_a_recording_prints_and_logs_nothing_whatever_the_environment_asks(
    provider: Callable[[list[Answer]], Provider],
    library: Path,
    tmp_path: Path,
    failure: str,
) -> None:
    provider(spilled("turn") if failure == "spill" else explore_answers())
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    (workspace / ".gitignore").write_text(".scratch/\n")
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fake@example.test",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        cwd=workspace,
        check=True,
    )
    (tmp_path / "invoke.py").write_text(INVOKE)
    debug_file = tmp_path / "playwright-debug.log"
    # Every switch that makes a library print or write what it sends, types
    # or receives: the SDK's and httpx's loggers, Playwright's protocol trace
    # and its driver's debug log, to stderr or to a file.
    loud = {
        "ANTHROPIC_LOG": "debug",
        "DEBUGP": "1",
        "DEBUG": "pw:*",
        "DEBUG_FILE": str(debug_file),
        "PWDEBUGIMPL": "1",
    }
    kept = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "PLAYWRIGHT_BROWSERS_PATH")
        if name in os.environ
    }

    result = subprocess.run(
        [
            sys.executable,
            str(tmp_path / "invoke.py"),
            str(workspace),
            os.environ["ANTHROPIC_BASE_URL"],
            str(library),
            failure,
        ],
        env=kept
        | loud
        | {
            "PYTHONPATH": str(recorder.ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ANTHROPIC_API_KEY": KEY,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert [KEY in result.stdout, KEY in result.stderr] == [False, False]
    assert (result.returncode, result.stdout, result.stderr) == (1, "", STOPPED)
    assert not debug_file.exists()
    attempt = workspace / ".scratch/round/001"
    assert holding_the_key(attempt) == []
    # An accepted receipt whose cassette could not be promoted: the recorder
    # still exits 1, so success needs both.
    assert receipt(attempt)["outcome"] == (
        "credential" if failure == "spill" else "accepted"
    )
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS
