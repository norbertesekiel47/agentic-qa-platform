"""The healer's turns on a drift (#244; ADR-0024's #244 amendment): real Chromium
against #240's drift pages, a scripted model, through `replays.replay_run`."""

import errno
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from aqa_core.compiled import ByRole
from aqa_runner import heal
from aqa_runner.browser_session import BrowserSession
from aqa_runner.chat_client import Reply
from aqa_runner.evidence import Shot
from aqa_runner.healer import heal_request
from aqa_runner.model_router import ModelCallError
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import NO_SECRETS, Redacted
from aqa_runner.repairs import AssertionAt
from aqa_runner.run_record import RefusedWriteError, require_clean
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from packages.runner.tests.drift_fixtures import DETAILS, SUMMARY, TOGGLE
from packages.runner.tests.executor_fixtures import compiled
from packages.runner.tests.explore_graph_fixtures import (
    NO_RESPONSE,
    REFUSED,
    USAGE,
    answer,
    calling,
    ref,
)
from packages.runner.tests.heal_fixtures import (
    FOUND_NONE,
    NO_TOOL,
    SHOW,
    Healed,
    basis,
    healed,
    outcomes,
    rebinding,
    turn_files,
    with_budgets,
)
from packages.runner.tests.navigator_fixtures import fail_fsync
from packages.runner.tests.secret_fixtures import BOUND_AT_START, FAKE_VALUE
from packages.runner.tests.test_replay_repairs import secret_spec_at

UNWRITTEN = "the run record took no more lines, so the heal stopped"
# A PNG's signature, and its base64 form's start, which no request may hold.
PNG = b"\x89PNG\r\n\x1a\n"
BASE64_PNG = "iVBORw0KGgo"
CANARY = "canary-only-the-model-wrote"
# The toggle clicked, then a summary one of whose locators the page no longer
# has: a step's drift, then an assertion's.
SUMMARY_DRIFTS = compiled(
    [{"seq": 1, "action": "click", "target": "toggle", "side_effect": False}],
    targets={
        "toggle": TOGGLE,
        "summary": {**SUMMARY, "locators": [{"css": "#summary"}, {"css": "#gone"}]},
    },
    assertions=DETAILS.model_dump(mode="json", exclude_none=True)["assertions"],
)


def turn_record(run: Healed, key: str, number: int) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (run.record / f"heals/{key}/{number}.json").read_text()
    )
    return loaded


def costs(run: Healed) -> list[dict[str, Any]]:
    lines = (run.record / "costs.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_a_drifted_step_is_rebound_to_the_element_the_model_picked(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, SHOW)

    assert outcomes(run) == [(0, "completed"), (1, "completed")]
    assert [check.outcome for check in run.result.assertions] == ["pass", "pass"]
    [repaired] = run.result.repairs
    assert repaired.problems == ()
    assert repaired.script is not None
    assert [
        locator.model_dump(mode="json", exclude_none=True)
        for locator in repaired.script.targets["toggle"].locators
    ] == [
        {"role": "button", "name": "Show details"},
        {"css": "button", "scope": {"css": "#order"}},
    ]
    assert repaired.basis is not None
    assert repaired.basis.said == 'button "Show details"'
    assert repaired.basis.model_reported is not None
    assert repaired.basis.model_reported.confidence == 0.9
    assert repaired.basis.model_reported.rationale == "it moved"
    assert (repaired.basis.stop, repaired.basis.record) == (None, "heals/1")


def test_a_heal_leaves_each_turn_its_cost_lines_and_its_first_observation(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, SHOW)

    sent = run.requests()[0]
    picked = ref(run.asked()[0], "button", "Show details")
    args = {"ref": picked, "confidence": 0.9, "rationale": "it moved"}
    assert turn_record(run, "1", 1) == {
        "turn": 1,
        "sent": sent,
        "outcome": "ok",
        "call": json.dumps({"name": "rebind", "args": args}),
        "costs": [1],
    }
    assert [(line["role"], line["mode"]) for line in costs(run)] == [("healer", "heal")]
    folder = run.record / "heals/1"
    assert sorted(path.name for path in folder.iterdir()) == [
        "1.json",
        "a11y_snapshot.yaml",
        "console_log.json",
        "network_log.json",
        "screenshot.png",
    ]
    empty = {"entries": [], "total": 0}
    assert json.loads((folder / "console_log.json").read_text()) == empty
    assert json.loads((folder / "network_log.json").read_text()) == empty
    snapshot = (folder / "a11y_snapshot.yaml").read_text()
    assert snapshot.replace("<", "&lt;") in sent


def test_a_heals_first_look_saves_a_screenshot_no_request_carries(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, SHOW)

    assert basis(run).shot == Shot("heals/1/screenshot.png")
    assert (run.record / "heals/1/screenshot.png").read_bytes().startswith(PNG)
    assert run.verdict("step", 1)["heal"]["screenshot"] == {
        "path": "heals/1/screenshot.png",
        "withheld": None,
    }
    contents = [message.content for request in run.asked() for message in request]
    blocks = [
        block
        for content in contents
        if not isinstance(content, str)
        for block in content
        if isinstance(block, dict) and block.get("type") in ("image", "image_url")
    ]
    sent = [turn_record(run, "1", number)["sent"] for number in (1,)]
    assert blocks == []
    assert [text for text in (json.dumps(contents), *sent) if BASE64_PNG in text] == []


def test_only_a_heals_first_look_saves_a_screenshot(tmp_path: Path) -> None:
    stale = calling("rebind", {"ref": "e99999", "confidence": 1, "rationale": "x"})

    run = healed(tmp_path, stale, SHOW)

    assert outcomes(run) == [(0, "completed"), (1, "completed")]
    assert turn_files(run) == ["1.json", "2.json"]
    assert sorted(
        path.relative_to(run.record).as_posix() for path in run.record.rglob("*.png")
    ) == ["heals/1/screenshot.png"]


def test_a_heal_that_stopped_before_its_first_look_cites_no_screenshot(
    tmp_path: Path,
) -> None:
    # The first drift's one response spends the run's budget.
    run = healed(
        tmp_path,
        SHOW,
        NO_TOOL,
        script=SUMMARY_DRIFTS,
        config=with_budgets("heal_model_usd: 0.003"),
    )

    later = run.result.repairs[1].at
    assert isinstance(later, AssertionAt)
    assert (basis(run, 1).record, basis(run, 1).shot) == (None, None)
    assert run.verdict("assertion", later.check.id)["heal"]["screenshot"] is None
    assert basis(run).shot == Shot("heals/1/screenshot.png")


def test_a_heal_request_holds_only_the_target_results_and_snapshot(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, SHOW)

    [system, human] = run.asked()[0]
    assert system.text.startswith("You repair one broken binding")
    labels = [
        line.strip("<>")
        for line in human.text.splitlines()
        if line in ("<target>", "<results>", "<snapshot>")
    ]
    assert labels == ["target", "results", "snapshot"]
    assert human.text.startswith("<target>\n")
    target = human.text[len("<target>\n") : human.text.index("\n</target>")]
    assert json.loads(target) == {
        "meaning": TOGGLE["semantic"],
        "locators": TOGGLE["locators"],
    }
    # DETAILS' text check claims this; the moved page's summary is hidden, so a
    # copy here could only have come from the check.
    assert "ships on Monday" not in human.text
    assert "ships on Monday" not in system.text


def test_a_heal_request_takes_only_a_redacted_snapshot() -> None:
    locators = [ByRole(role="button", name="Show details")]

    raw = cast(Redacted, "the page's raw text")

    with pytest.raises(TypeError, match="Redacted"):
        heal_request("a meaning", locators, "", raw, redactor=NO_SECRETS)


FALLBACK = (
    "budgets: { resolve_seconds: 2 }\n"
    "roles: { healer: { fallback: claude-opus-5-5 } }\n"
)


def test_a_reply_that_calls_no_tool_gives_up_and_leaves_its_turn(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL)

    assert basis(run).said == FOUND_NONE
    assert (basis(run).stop, basis(run).record) == (None, "heals/1")
    assert outcomes(run) == [(0, "completed"), (1, "drifted")]
    assert turn_files(run) == ["1.json"]
    assert len(run.requests()) == 1


def test_a_refused_call_gives_up_and_leaves_its_turn(tmp_path: Path) -> None:
    run = healed(tmp_path, REFUSED)

    assert (basis(run).said, basis(run).stop) == ("the model refused", None)
    assert turn_record(run, "1", 1)["outcome"] == "refusal"
    assert turn_record(run, "1", 1)["costs"] == [1]


def test_a_response_that_costs_more_than_was_left_binds_nothing(tmp_path: Path) -> None:
    run = healed(tmp_path, SHOW, config=with_budgets("heal_model_usd: 0.001"))

    assert basis(run).said == (
        "the spend budget ran out on this turn, so its call didn't run"
    )
    assert outcomes(run) == [(0, "completed"), (1, "drifted")]
    assert turn_files(run) == ["1.json"]


def test_a_cost_line_not_on_disk_is_a_record_error_with_no_further_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unwritten(messages: Sequence[BaseMessage]) -> Reply:
        del messages
        # The cost line's own fsync, once the response is priced.
        fail_fsync(monkeypatch)
        return answer(AIMessage(content="whatever"))

    run = healed(tmp_path, unwritten, NO_TOOL)

    assert basis(run).said == (
        "a priced model call's cost line isn't on disk, so the run's cost is incomplete"
    )
    assert basis(run).stop == "record_error"
    assert len(run.requests()) == 1
    assert turn_files(run) == []


def test_a_turn_record_the_record_refuses_is_a_record_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing(redactor: Any, place: str) -> Any:
        if not place.startswith("heals/"):
            return require_clean(redactor, place)

        def check(text: str) -> None:
            del text
            raise RefusedWriteError(f"{place}: refused")

        return check

    monkeypatch.setattr(heal, "require_clean", refusing)

    run = healed(tmp_path, SHOW)

    assert (basis(run).said, basis(run).stop) == (UNWRITTEN, "record_error")
    assert outcomes(run) == [(0, "completed"), (1, "drifted")]


def test_evidence_the_disk_refuses_is_a_record_error_with_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(*_: object, **__: object) -> None:
        raise OSError(errno.ENOSPC, "fake full disk")

    monkeypatch.setattr(heal, "capture_evidence", refused)

    run = healed(tmp_path, SHOW)

    assert (basis(run).said, basis(run).stop) == (UNWRITTEN, "record_error")
    assert run.requests() == []


def test_a_page_that_cant_be_looked_at_ends_the_heal_with_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = BrowserSession.snapshot

    async def unanswered(
        session: BrowserSession, *, record_refusal: bool = True
    ) -> Redacted:
        # Evidence's own look says `record_refusal=False`; the heal's doesn't.
        if record_refusal:
            raise UnansweredError(3)
        return await taken(session, record_refusal=False)

    monkeypatch.setattr(BrowserSession, "snapshot", unanswered)

    run = healed(tmp_path, SHOW)

    assert basis(run).said == (
        "the page couldn't be looked at: the page didn't answer within 3 s"
    )
    assert (basis(run).stop, basis(run).record) == (None, None)
    assert run.requests() == []


def test_a_ref_no_snapshot_gave_takes_another_turn_and_says_why(tmp_path: Path) -> None:
    stale = calling("rebind", {"ref": "e99999", "confidence": 1, "rationale": "x"})

    run = healed(tmp_path, stale, NO_TOOL)

    assert "rebind did not run: 'e99999' is from" not in run.requests()[0]
    assert "rebind did not run: 'e99999' isn't a ref" in run.requests()[1]
    assert basis(run).said == FOUND_NONE
    assert turn_files(run) == ["1.json", "2.json"]


def test_an_invalid_reply_takes_another_turn_and_says_why(tmp_path: Path) -> None:
    cut = Reply(
        message=AIMessage(content="and then"),
        usage=USAGE,
        refused=False,
        complete=False,
        parsed=None,
    )

    run = healed(tmp_path, cut, NO_TOOL)

    assert "the reply's outcome was invalid, so nothing ran" in run.requests()[1]
    assert turn_record(run, "1", 1)["outcome"] == "invalid"
    assert basis(run).said == FOUND_NONE


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(
            {
                "ref": "e1",
                "confidence": 1,
                "rationale": "x",
                "locators": [{"css": "#decoy"}],
            },
            id="writes_locators",
        ),
        pytest.param({"ref": "e1", "confidence": 1.5, "rationale": "x"}, id="over_1"),
        pytest.param({"ref": "e1", "confidence": -0.1, "rationale": "x"}, id="under_0"),
        pytest.param({"ref": "e1", "confidence": True, "rationale": "x"}, id="a_bool"),
        pytest.param({"ref": "e1", "rationale": "x"}, id="no_confidence"),
    ],
)
def test_a_healer_reply_that_writes_locators_runs_nothing(
    tmp_path: Path, args: dict[str, object]
) -> None:
    run = healed(tmp_path, calling("rebind", args), NO_TOOL)

    assert (
        "the call to rebind was not run: its arguments don't fit the tool"
    ) in run.requests()[1]
    assert basis(run).said == FOUND_NONE
    assert run.result.repairs[0].script is None


@pytest.mark.parametrize(
    ("unfit", "said"),
    [
        pytest.param(
            calling(
                "rebind", {"ref": CANARY, "confidence": CANARY, "rationale": CANARY}
            ),
            "the call to rebind was not run: its arguments don't fit the tool",
            id="in_its_arguments",
        ),
        pytest.param(
            calling(CANARY, {"ref": CANARY}),
            "the reply called a tool the healer doesn't have, so nothing ran",
            id="in_a_tools_name",
        ),
    ],
)
def test_nothing_the_model_wrote_in_a_call_it_cant_run_is_echoed(
    tmp_path: Path, unfit: Reply, said: str
) -> None:
    run = healed(tmp_path, unfit, NO_TOOL)

    assert said in run.requests()[1]
    assert CANARY not in run.requests()[1]


def test_a_targets_non_ascii_meaning_reaches_the_request_as_written() -> None:
    [_, human] = heal_request(
        "the café's button",
        [ByRole(role="button", name="Café")],
        "",
        NO_SECRETS.redact(""),
        redactor=NO_SECRETS,
    )

    assert '"meaning": "the café\'s button"' in human.text
    assert '"name": "Café"' in human.text


def test_a_tool_the_healer_doesnt_have_takes_another_turn(tmp_path: Path) -> None:
    run = healed(tmp_path, calling("click", {"ref": "e1"}), NO_TOOL)

    assert (
        "the reply called a tool the healer doesn't have, so nothing ran"
    ) in run.requests()[1]


def test_a_malformed_call_takes_another_turn(tmp_path: Path) -> None:
    broken = answer(
        AIMessage(
            content="",
            invalid_tool_calls=[
                {"name": "rebind", "args": "{", "id": "x", "error": "not JSON"}
            ],
        )
    )

    run = healed(tmp_path, broken, NO_TOOL)

    assert ("a tool call in the reply was malformed, so nothing ran") in run.requests()[
        1
    ]


def test_only_a_replys_first_call_runs(tmp_path: Path) -> None:
    def twice(messages: Sequence[BaseMessage]) -> Reply:
        args = {
            "ref": ref(messages, "button", "Show details"),
            "confidence": 0.5,
            "rationale": "first",
        }
        decoy = {
            "ref": ref(messages, "button", "Contact us"),
            "confidence": 1,
            "rationale": "second",
        }
        return answer(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "rebind", "args": args, "id": "a"},
                    {"name": "rebind", "args": decoy, "id": "b"},
                ],
            )
        )

    run = healed(tmp_path, twice)

    assert basis(run).said == 'button "Show details"'


def test_the_rationale_is_kept_on_one_line_and_cut(tmp_path: Path) -> None:
    said = "first line\n\tsecond " + "x" * 500

    run = healed(tmp_path, rebinding("button", "Show details", rationale=said))

    reported = basis(run).model_reported
    assert reported is not None
    assert reported.rationale.startswith("first line second xxx")
    assert len(reported.rationale) == 200


@pytest.mark.parametrize(
    ("moves", "config", "cost_lines"),
    [
        pytest.param(
            (NO_RESPONSE,), "budgets: { resolve_seconds: 2 }\n", [], id="first"
        ),
        pytest.param((REFUSED, NO_RESPONSE), FALLBACK, [1], id="after_refusal"),
    ],
)
def test_a_heal_with_no_response_stops_the_run_as_no_response(
    tmp_path: Path, moves: tuple[Any, ...], config: str, cost_lines: list[int]
) -> None:
    run = healed(tmp_path, *moves, config=config)

    assert (basis(run).said, basis(run).stop) == (
        "the model gave no response (APIConnectionError)",
        "no_response",
    )
    turn = turn_record(run, "1", 1)
    assert (turn["outcome"], turn["costs"], turn["call"]) == (
        "no_response",
        cost_lines,
        None,
    )


def test_a_defect_behind_a_billed_refusal_is_raised(tmp_path: Path) -> None:
    with pytest.raises(ModelCallError) as raised:
        healed(tmp_path, REFUSED, ValueError("a defect"), config=FALLBACK)

    assert isinstance(raised.value.__cause__, ValueError)


def test_a_defect_on_the_first_call_is_raised(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a defect"):
        healed(tmp_path, ValueError("a defect"))


def test_a_heal_request_that_still_spells_a_value_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)

    def leaking(*args: Any, **kwargs: Any) -> list[BaseMessage]:
        # A gap in the builder's scans: a bound value left in the request.
        return [*heal_request(*args, **kwargs), HumanMessage(content=FAKE_VALUE)]

    monkeypatch.setattr(heal, "heal_request", leaking)

    run = healed(
        tmp_path,
        SHOW,
        spec_at=secret_spec_at,
        config=f"budgets: {{ resolve_seconds: 2 }}\nsecrets: {{ {BOUND_AT_START} }}\n",
    )

    assert run.requests() == []
    assert basis(run).said == (
        "a request still spelled a bound value after its scans, so it wasn't sent"
    )
    assert turn_files(run) == []


def test_heal_turns_are_shared_by_a_step_drift_and_a_later_assertion_drift(
    tmp_path: Path,
) -> None:
    nonsense = calling("nonsense", {})

    run = healed(
        tmp_path,
        SHOW,
        nonsense,
        nonsense,
        nonsense,
        script=SUMMARY_DRIFTS,
        config=with_budgets("heal_turns: 3"),
    )

    assert len(run.requests()) == 3
    assert [repaired.at.key for repaired in run.result.repairs] == ["1", "assertion-0"]
    assert basis(run, 1).said == "the turn budget (3) ran out"
    assert turn_files(run, "assertion-0") == ["1.json", "2.json"]


def test_heal_spend_is_shared_and_stops_once_used(tmp_path: Path) -> None:
    # One scripted response costs $0.003 at the healer's prices.
    run = healed(
        tmp_path,
        SHOW,
        NO_TOOL,
        script=SUMMARY_DRIFTS,
        config=with_budgets("heal_model_usd: 0.003"),
    )

    assert len(run.requests()) == 1
    assert [repaired.at.key for repaired in run.result.repairs] == ["1", "assertion-0"]
    assert basis(run, 1).said == "the spend budget ran out"
    assert run.result.repairs[0].script is not None
    assert (run.record / "heals/assertion-0").exists() is False
    assert basis(run, 1).record is None
