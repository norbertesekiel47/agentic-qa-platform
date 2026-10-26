"""The recorder's explore case (#53 P21a): the fixture explore recorded in
real Chromium from a local stand-in for the provider, with a fake key, within
the recording's spend bound. No test here reaches a provider."""

import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from aqa_core.config import ProjectConfig
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import vendored
from aqa_core.spec import Spec

from packages.runner.tests import record_cassettes as recorder
from packages.runner.tests.executor_fixtures import PAGES
from packages.runner.tests.explore_graph_fixtures import PLAN
from packages.runner.tests.provider_stand_in import (
    DROPPED,
    KEY,
    Answer,
    Answering,
    Start,
    called,
    planned,
    standing_in,
)
from packages.runner.tests.record_cassettes import EXPLORE, record
from packages.runner.tests.test_explore_confirm import resets
from packages.runner.tests.test_explore_graph import configured, shop_spec

pytestmark = pytest.mark.usefixtures("reset_tracing")

PREVIOUS = "previous cassette\n"
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

OVERLOADED = json.dumps(
    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
).encode()


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


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Start]:
    """`provider(answers)` starts a stand-in and points the recorder at it
    with the fake key."""
    with standing_in(monkeypatch) as start:
        yield start


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
    provider: Start,
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
            *[(False, {"type": "auto", "disable_parallel_tool_use": True}, ["user"])]
            * 4,
        ]
    else:
        assert costs == FIVE_COSTS


def test_every_explore_interaction_is_priced_once(
    provider: Start, library: Path, attempt: Path
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
        "round_left_usd": "0.597950",
        "spent_usd": "0.000360",
        "refused": False,
        "overrun": False,
    }


def test_the_fixture_explore_replays_its_librarys_plan_and_records_its_turns(
    provider: Start, library: Path, tmp_path: Path
) -> None:
    # The plan request is the library's accepted one, so it is neither sent
    # nor priced again, as an injection case's isn't; only the turns are
    # recorded live (ADR-0024's #58 amendment).
    provider(explore_answers())
    record(EXPLORE, tmp_path / "earlier" / "001", library=library)
    attempt = tmp_path / "round" / "001"
    stand_in = provider(
        [called(11 + turn, name, args) for turn, (name, args) in enumerate(MOVES)]
    )

    record(EXPLORE, attempt, library=library)

    assert not any("output_config" in json.loads(body) for body in stand_in.sent)
    assert len(stand_in.sent) == 4
    kept = receipt(attempt)
    assert (kept["outcome"], kept["explore"]) == ("accepted", CONFIRMED)
    assert [each["request_id"] for each in kept["reused"]] == ["req_fake_01"]
    assert len(kept["cost_records"]) == 4


def test_two_responses_billed_alike_are_each_matched_in_order(
    provider: Start, library: Path, attempt: Path
) -> None:
    # A page that doesn't change gives turns billed the same (#58's decoy
    # round); each cost line is matched to the next response with its usage,
    # in order, not refused as ambiguous.
    def same(turn: int) -> dict[str, int]:
        return {"input_tokens": 30, "output_tokens": 3} if turn in (2, 3) else {}

    provider(explore_answers(usage=same))

    costs = record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    assert (kept["outcome"], kept["finalization_failures"]) == ("accepted", [])
    assert [each["status"] for each in kept["cost_records"]] == ["ok"] * 5
    assert kept["cost_records"] == [cost.model_dump(mode="json") for cost in costs]


# The bound's own model, and a request body of 50 bytes, whose worst case is
# 1,074 input tokens and 4,096 output at $2 and $10 a million.
MODEL = resolve_roles(recorder.FIXTURE_CONFIG, vendored())["navigator"].model
SMALL = json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 4096}).encode()


def sent(body: bytes | None) -> SimpleNamespace:
    """A request as VCR's request filter hands it over."""
    return SimpleNamespace(body=body)


@pytest.mark.parametrize(
    ("limit", "admitted"), [("0.043108", True), ("0.043107", False)]
)
def test_the_bound_admits_a_request_whose_worst_case_reaches_its_limit_exactly(
    limit: str, admitted: bool
) -> None:
    bound = recorder.Allowance(MODEL, Decimal(limit), recorder.ROUND_USD)

    if admitted:
        bound.admit(sent(SMALL))
    else:
        with pytest.raises(recorder.SpendRefusedError):
            bound.admit(sent(SMALL))

    assert bound.receipt() == {
        "run_usd": limit,
        "round_left_usd": "0.597950",
        "spent_usd": "0.043108" if admitted else "0",
        "refused": not admitted,
        "overrun": False,
    }


UNPRICEABLE: dict[str, bytes | None] = {
    "not_json": b"not json",
    "not_an_object": b"[]",
    "no_body": None,
    "another_model": json.dumps(
        {"model": "claude-opus-5-5", "max_tokens": 4096}
    ).encode(),
    "no_max_tokens": json.dumps({"model": "claude-sonnet-5-5"}).encode(),
    "max_tokens_a_bool": json.dumps(
        {"model": "claude-sonnet-5-5", "max_tokens": True}
    ).encode(),
    "max_tokens_zero": json.dumps(
        {"model": "claude-sonnet-5-5", "max_tokens": 0}
    ).encode(),
    "max_tokens_past_the_adapters": json.dumps(
        {"model": "claude-sonnet-5-5", "max_tokens": 4097}
    ).encode(),
}


@pytest.mark.parametrize("body", UNPRICEABLE)
def test_the_bound_refuses_a_request_it_cannot_price(body: str) -> None:
    bound = recorder.Allowance(MODEL, recorder.RUN_USD, recorder.ROUND_USD)

    with pytest.raises(recorder.SpendRefusedError):
        bound.admit(sent(UNPRICEABLE[body]))

    assert (bound.receipt()["spent_usd"], bound.refused) == ("0", True)


def test_the_bound_refuses_a_second_request_while_one_is_held() -> None:
    bound = recorder.Allowance(MODEL, recorder.RUN_USD, recorder.ROUND_USD)
    bound.admit(sent(SMALL))
    # VCR filters the held request again just before keeping its response.
    bound.admit(sent(SMALL))

    with pytest.raises(recorder.SpendRefusedError):
        bound.admit(sent(SMALL.replace(b"4096", b"4095")))

    assert (bound.receipt()["spent_usd"], bound.refused) == ("0.043108", True)


def heavy(turn: int) -> dict[str, int]:
    """About $0.049 a call, within the input each request reserves: after
    three the run has spent $0.14706, under explore's own $0.15, and the
    fourth's worst case would take it past $0.20."""
    return {"input_tokens": 7000 + 10 * turn, "output_tokens": 3500}


def test_a_request_past_the_runs_limit_is_refused_before_it_is_sent(
    provider: Start, library: Path, attempt: Path
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
        "round_left_usd": "0.597950",
        "spent_usd": "0.14706",
        "refused": True,
        "overrun": False,
    }
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS


EARLIER = {
    "spent": "0.56",
    "nothing": "0",
    "no_overrun_field": "0",
    "unknown": None,
    "negative": "-1",
    "minus_infinity": "-Infinity",
    "not_a_number": "NaN",
    "infinity": "Infinity",
    "overran": "0.1",
}


@pytest.mark.parametrize("earlier", EARLIER)
def test_a_request_past_the_rounds_limit_is_refused_before_it_is_sent(
    provider: Start,
    library: Path,
    attempt: Path,
    earlier: str,
) -> None:
    stand_in = provider(explore_answers())
    before = attempt.parent / "000"
    before.mkdir(parents=True)
    if EARLIER[earlier] is not None:
        spend = {"spent_usd": EARLIER[earlier], "overrun": earlier == "overran"}
        if earlier == "no_overrun_field":
            del spend["overrun"]
        (before / "receipt.json").write_text(json.dumps({"spend": spend}))
    if earlier == "nothing":
        # An earlier attempt refused before it spent anything leaves the round.
        record(EXPLORE, attempt, library=library)
        assert (len(stand_in.sent), receipt(attempt)["spend"]["round_left_usd"]) == (
            5,
            "0.597950",
        )
        return
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
            "round_left_usd": "0.037950",
            "spent_usd": "0",
            "refused": True,
            "overrun": False,
        }
    else:
        assert not (attempt / "receipt.json").exists()


@pytest.mark.parametrize("failure", ["overloaded", "dropped"])
def test_an_overloaded_or_unanswered_request_is_sent_once(
    provider: Start,
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
    # No usage came back, so the plan request keeps its worst case: its
    # body's bytes plus 1,024 input tokens and 4,096 output, at $2 and $10 a
    # million.
    worst = (len(stand_in.sent[0]) + 1024) * Decimal(2) + 4096 * Decimal(10)
    assert (kept["cost_records"], kept["spend"]) == (
        [],
        {
            "run_usd": "0.20",
            "round_left_usd": "0.597950",
            "spent_usd": str(worst / 1_000_000),
            "refused": False,
            "overrun": False,
        },
    )
    assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS


def test_a_recording_runs_quiet_and_gives_back_the_callers_logging_and_switches(
    provider: Start,
    library: Path,
    attempt: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Values that can't pause the run or write outside tmp_path, had they
    # reached Playwright.
    switches = {
        "DEBUGP": "0",
        "DEBUG": "pw:fake",
        "DEBUG_FILE": str(tmp_path / "playwright-debug.log"),
        "PWDEBUG": "0",
        "PWDEBUGIMPL": "0",
    }
    for name, value in switches.items():
        monkeypatch.setenv(name, value)
    level = logging.root.manager.disable
    during: list[tuple[dict[str, str | None], int]] = []

    def watching(_body: bytes) -> Answer:
        # The stand-in answers while the recording is sending.
        during.append(
            (
                {name: os.environ.get(name) for name in switches},
                logging.root.manager.disable,
            )
        )
        return DROPPED, b""

    provider([watching])

    with pytest.raises(ValueError, match="provider_error"):
        record(EXPLORE, attempt, library=library)

    assert during == [(dict.fromkeys(switches), logging.CRITICAL)]
    assert {name: os.environ.get(name) for name in switches} == switches
    assert logging.root.manager.disable == level


def billing(turn: int, past: int, *, cached: int = 0) -> Callable[[bytes], Answer]:
    """Turn `turn`'s answer, billed `past` input tokens more than its request
    reserved (its body's bytes plus 1,024), `cached` of them read from the
    cache."""

    def answering(body: bytes) -> Answer:
        usage = {
            "input_tokens": len(body) + recorder.HEADROOM_TOKENS + past - cached,
            "cache_read_input_tokens": cached,
            "output_tokens": 3,
        }
        if turn == 0:
            return planned(turn, GOOD_PLAN, **usage)
        name, args = MOVES[turn - 1]
        return called(turn, name, args, **usage)

    return answering


@pytest.mark.parametrize(
    "billed",
    [
        "at_the_reservation",
        "one_past_it",
        "past_it_by_cached_tokens",
        "on_the_last_answer",
    ],
)
def test_a_response_billed_past_its_reservation_stops_the_round(
    provider: Start,
    library: Path,
    attempt: Path,
    billed: str,
) -> None:
    answers: list[Answering] = [*explore_answers()]
    if billed == "on_the_last_answer":
        answers[4] = billing(4, 1)
    else:
        answers[0] = {
            "at_the_reservation": billing(0, 0),
            "one_past_it": billing(0, 1),
            "past_it_by_cached_tokens": billing(0, 1, cached=200),
        }[billed]
    stand_in = provider(answers)

    if billed == "at_the_reservation":
        record(EXPLORE, attempt, library=library)
    else:
        with pytest.raises(ValueError, match="overrun"):
            record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    spend = kept["spend"]
    overran = billed != "at_the_reservation"
    assert (
        len(stand_in.sent),
        kept["outcome"],
        spend["overrun"],
        spend["refused"],
    ) == (
        1 if billed in {"one_past_it", "past_it_by_cached_tokens"} else 5,
        "overrun" if overran else "accepted",
        overran,
        billed in {"one_past_it", "past_it_by_cached_tokens"},
    )
    assert Decimal(spend["spent_usd"]) == sum(
        Decimal(cost["cost_usd"]) for cost in kept["cost_records"]
    )
    if overran:
        sent_before = len(stand_in.sent)
        assert (library / f"{EXPLORE}.yaml").read_text() == PREVIOUS
        with pytest.raises(ValueError, match="billed past its reservation"):
            record(EXPLORE, attempt.parent / "002", library=library)
        assert len(stand_in.sent) == sent_before


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
    provider: Start,
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


@pytest.mark.parametrize("failing", ["removal", "stat"])
def test_a_scrub_that_fails_tries_every_other_name_and_says_so(
    provider: Start,
    library: Path,
    attempt: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    # A plan spill reaches the compiled script as well as the run record.
    provider(spilled("plan"))
    if failing == "removal":
        rmtree = shutil.rmtree

        def refuse(path: Path, *args: Any, **kwargs: Any) -> None:
            if Path(path).name == ".aqa":
                raise OSError("fake disk failure")
            rmtree(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", refuse)
    else:
        # The spec root can't be searched while the attempt is finished, so
        # neither name under it can be looked at.
        finish = recorder.finish

        def unsearchable(*args: Any) -> None:
            (attempt / "qa").chmod(0o600)
            try:
                finish(*args)
            finally:
                (attempt / "qa").chmod(0o755)

        monkeypatch.setattr(recorder, "finish", unsearchable)

    with pytest.raises(ValueError, match="error"):
        record(EXPLORE, attempt, library=library)

    kept = receipt(attempt)
    assert (kept["outcome"], kept["finalization_failures"], kept["scrubbed"]) == (
        "error",
        ["scrub"],
        [f"{EXPLORE}.yaml", *(["qa/.compiled"] if failing == "removal" else [])],
    )
    assert {Path(name).parts[:2] for name in holding_the_key(attempt)} == (
        {("qa", ".aqa")}
        if failing == "removal"
        else {("qa", ".aqa"), ("qa", ".compiled")}
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
    provider: Start,
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
