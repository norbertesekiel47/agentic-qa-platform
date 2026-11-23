"""The healer inserts a click to uncover a covered target (#335; ADR-0024's #335
amendment): on #240's modal page, every locator of the toggle finds it and none
can click it, so the heal takes an `uncover` turn of its own. The model picks
the element to click first; our code checks no target of the script finds it,
writes its locators, names the target it adds and inserts the click before the
drifted step, and the seam judges it as any repair's offer. Real Chromium, a
scripted model and no provider, through `replays.replay_run`, on pages composed
here from the modal page."""

import json
from pathlib import Path
from typing import cast

import pytest
from aqa_core.compiled import Click, CompiledScript, Target
from aqa_runner.healer import (
    HEAL_INSTRUCTIONS,
    UNCOVER_INSTRUCTIONS,
    Uncover,
    Unfit,
    uncover_request,
    uncovered,
)
from aqa_runner.model_router import Routed
from aqa_runner.redaction import NO_SECRETS, Redacted
from langchain_core.messages import AIMessage

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.drift_fixtures import (
    DETAILS,
    GOVERNED,
    NO_THANKS,
    SUBJECTS,
    SUMMARY,
    TOGGLE,
    page,
)
from packages.runner.tests.explore_graph_fixtures import Move, calling
from packages.runner.tests.heal_fixtures import (
    BUDGETS,
    NO_POINT,
    NO_TOOL,
    SHOW,
    Healed,
    basis,
    healed,
    outcomes,
    turn_files,
    uncovering,
    with_budgets,
)

MODAL = page("modal")
# The dialog's dismiss button, picked to uncover the toggle.
NO_THANKS_PICK = uncovering("button", "No thanks")
# What the heal writes of the target it adds for the toggle's uncovering click.
MEANING = (
    "the element a heal clicks to uncover the button that shows the order's details"
)
# Why a heal on a covered target stopped, in fixed words.
UNCOVERS_NONE = "the healer found no element that uncovers the target"
ALREADY_FOUND = (
    "uncover did not run: a target of the script already finds this element, "
    "and a heal adds a target only for an element none finds"
)
VISION_ONLY = (
    "the target is covered, and only a tree turn proposes a click to uncover it, "
    "which vision_only never takes"
)
TUNNELLED = (
    "the target is covered, and an encrypted or WebSocket connection is open, so "
    "replay would refuse a step a heal inserts, and no model was asked"
)
# What the document says of a run that healed nothing.
INCONCLUSIVE = ("inconclusive", 3, None)


def ended(run: Healed) -> tuple[object, ...]:
    """The document's outcome, exit code and proposal."""
    document = run.document
    return tuple(document[key] for key in ("outcome", "exit_code", "heal_proposal"))


def cost_lines(run: Healed) -> list[tuple[str, str]]:
    """Each `costs.jsonl` line's role and mode, none when there is no file."""
    path = run.record / "costs.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [(line["role"], line["mode"]) for line in lines]


def results_of(request: str) -> str:
    """A request's `<results>` block, as sent."""
    start = request.index("<results>\n") + len("<results>\n")
    return request[start : request.index("\n</results>")]


def step_reason(run: Healed, seq: int) -> str:
    reason = run.verdict("step", seq)["reason"]
    assert isinstance(reason, str)
    return reason


def inserted_seqs(run: Healed) -> set[int]:
    """The seqs `steps.jsonl` has a line for that the committed script lacks."""
    committed = {0, *(step.seq for step in run.committed.steps)}
    return {line["seq"] for line in fx.read_steps(run.record)} - committed


def test_a_covered_toggle_is_uncovered_by_a_click_the_heal_inserts(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_THANKS_PICK, html=MODAL)

    assert ended(run)[:2] == ("drift_consistent", 7)
    assert outcomes(run) == [(0, "completed"), (2, "completed"), (1, "completed")]
    assert run.client.offered == [(("uncover",), None)]
    [system, human] = run.asked()[0]
    assert system.text == UNCOVER_INSTRUCTIONS
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
    # DETAILS' text check claims this; the modal page's summary is hidden, so a
    # copy here could only have come from the check.
    assert "ships on Monday" not in human.text
    assert cost_lines(run) == [("healer", "heal")]
    assert turn_files(run) == ["1.json"]
    repaired = json.loads((run.record / "repairs/1.json").read_text())
    assert repaired["basis"]["said"] == 'button "No thanks"'
    assert repaired["basis"]["model_reported"] == {
        "confidence": 0.9,
        "rationale": "it closes the dialog",
    }


def test_the_heal_names_the_target_it_adds_and_the_click_it_inserts(
    tmp_path: Path,
) -> None:
    """Our code writes the seq, the name, the meaning and the flag: the model
    gave a ref, a number and a sentence."""
    run = healed(tmp_path, NO_THANKS_PICK, html=MODAL)

    [repaired] = run.result.repairs
    assert repaired.script is not None
    added = repaired.script.targets["uncover_2"]
    assert (added.semantic, added.contract) == (MEANING, None)
    assert list(repaired.script.targets) == ["toggle", "summary", "uncover_2"]
    assert repaired.script.steps == (
        Click(seq=2, action="click", target="uncover_2", side_effect=False),
        *DETAILS.steps,
    )
    assert repaired.script.targets["toggle"] == run.committed.targets["toggle"]


def test_the_added_targets_seq_steps_past_a_name_the_script_already_has(
    tmp_path: Path,
) -> None:
    """A hand edit may have taken `uncover_2`: the seq goes up until the name is
    new."""
    taken = DETAILS.model_copy(
        update={
            "targets": {**DETAILS.targets, "uncover_2": Target.model_validate(SUMMARY)}
        }
    )

    run = healed(tmp_path, NO_THANKS_PICK, script=taken, html=MODAL)

    [repaired] = run.result.repairs
    assert repaired.script is not None
    assert repaired.script.steps[0] == Click(
        seq=3, action="click", target="uncover_3", side_effect=False
    )
    assert ended(run)[:2] == ("drift_consistent", 7)


def test_a_moved_target_is_still_offered_rebind_with_its_own_instructions(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, SHOW, html=page("moved"))

    assert run.client.offered == [(("rebind",), None)]
    [system, _] = run.asked()[0]
    assert system.text == HEAL_INSTRUCTIONS
    assert ended(run)[:2] == ("drift_consistent", 7)


# The modal page with the toggle renamed under the overlay: its name no longer
# matches, while its scoped css still finds it covered.
RENAMED_UNDER = MODAL.replace(">Show details</button>", ">View details</button>")


def test_a_covered_target_some_locator_misses_otherwise_is_offered_rebind(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL, NO_POINT, html=RENAMED_UNDER)

    assert run.result.repairs[0].misses == ("no match", "not actionable")
    assert run.client.offered[0] == (("rebind",), None)


# The modal page with a second dialog button, whose click writes.
SUBSCRIBING = MODAL.replace(
    "No thanks</button>",
    "No thanks</button>"
    "<button onclick=\"fetch('/write/subscribe', {method: 'POST'})\">"
    "Subscribe</button>",
)
UNSHOWN = "a repair inserted this step, and it didn't show it changed nothing: "
HELD = "network: a write request, held by the egress proxy, so it never reached the app"


def test_a_pick_steered_to_an_element_that_writes_is_held_and_ends_3(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, uncovering("button", "Subscribe"), html=SUBSCRIBING)

    assert ("POST", "/write/subscribe") not in run.seen
    assert [(step.seq, step.outcome, step.error) for step in run.result.steps] == [
        (0, "completed", None),
        (2, "failed", UNSHOWN + HELD),
    ]
    assert ended(run) == INCONCLUSIVE


# The toggle clicked, then the dialog's dismiss button, which a committed target
# names, clicked as a side effect.
DISMISSES_AFTER = DETAILS.model_copy(
    update={
        "targets": {**DETAILS.targets, "no_thanks": Target.model_validate(NO_THANKS)},
        "steps": (
            *DETAILS.steps,
            Click(
                seq=2,
                action="click",
                target="no_thanks",
                side_effect=True,
                side_effect_basis="network: POST /write/subscribe",
            ),
        ),
    }
)
STALE = calling("uncover", {"ref": "e99999", "confidence": 1, "rationale": "x"})


@pytest.mark.parametrize(
    ("pick", "script", "said"),
    [
        pytest.param(STALE, DETAILS, "uncover did not run: ", id="a-stale-ref"),
        pytest.param(
            uncovering("button", "Show details"),
            DETAILS,
            ALREADY_FOUND,
            id="the-covered-toggle",
        ),
        pytest.param(
            NO_THANKS_PICK,
            DISMISSES_AFTER,
            ALREADY_FOUND,
            id="a-committed-targets-element",
        ),
    ],
)
def test_a_pick_the_heal_refuses_takes_another_turn_and_says_why(
    tmp_path: Path, pick: Move, script: CompiledScript, said: str
) -> None:
    run = healed(tmp_path, pick, NO_TOOL, script=script, html=MODAL)

    second = results_of(run.requests()[1])
    assert second.startswith(said)
    assert ended(run) == INCONCLUSIVE
    assert inserted_seqs(run) == set()
    assert basis(run).said == UNCOVERS_NONE


# The modal page under a subject row on the summary, with the heal tests' budgets.
SUBJECT_BUDGETS = f"{BUDGETS}subjects: {json.dumps(SUBJECTS)}\n"
# `GOVERNED` with the summary meaning what the heal writes for the target it
# adds, so the subject row governs that target too.
GOVERNED_AS_ADDED = GOVERNED.model_copy(
    update={
        "targets": {
            **GOVERNED.targets,
            "summary": GOVERNED.targets["summary"].model_copy(
                update={"semantic": MEANING}
            ),
        }
    }
)


def test_an_added_target_whose_meaning_a_subject_row_governs_is_refused_at_the_seam(
    tmp_path: Path,
) -> None:
    run = healed(
        tmp_path,
        NO_THANKS_PICK,
        script=GOVERNED_AS_ADDED,
        html=MODAL,
        config=SUBJECT_BUDGETS,
    )

    repaired = json.loads((run.record / "repairs/1.json").read_text())
    assert repaired["problems"] == [
        "targets.uncover_2: lacks subject contract for order expect 0"
    ]
    assert 2 not in {line["seq"] for line in fx.read_steps(run.record)}
    assert (*ended(run), run.document["non_resumable"]) == (*INCONCLUSIVE, [])


def test_an_added_target_with_its_own_meaning_heals_under_the_subject_row(
    tmp_path: Path,
) -> None:
    run = healed(
        tmp_path, NO_THANKS_PICK, script=GOVERNED, html=MODAL, config=SUBJECT_BUDGETS
    )

    assert ended(run)[:2] == ("drift_consistent", 7)


def test_a_reply_that_calls_no_tool_ends_the_heal_with_no_vision_turn(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, NO_TOOL, html=MODAL)

    assert run.client.offered == [(("uncover",), None)]
    assert UNCOVERS_NONE in step_reason(run, 1)
    assert ended(run) == INCONCLUSIVE


def test_a_vision_only_heal_asks_nothing_about_a_covered_target(
    tmp_path: Path,
) -> None:
    config = f"{BUDGETS}heal: {{ perception: vision_only }}\n"

    run = healed(tmp_path, html=MODAL, config=config)

    assert run.client.requests == []
    assert VISION_ONLY in step_reason(run, 1)
    assert ended(run) == INCONCLUSIVE
    # The first look's evidence is kept, as for the other stops.
    assert (basis(run).record, basis(run).said) == ("heals/1", VISION_ONLY)


# The modal page, with a WebSocket it opens as it loads, whose handshake the app
# holds, so the session has a tunnel open by the time the toggle drifts.
SOCKETED = (
    MODAL + "<script>new WebSocket(`ws://${location.host}/held/socket`);</script>"
)


def test_a_tunnelled_session_asks_no_model_about_a_covered_target(
    tmp_path: Path,
) -> None:
    run = healed(tmp_path, html=SOCKETED)

    assert run.client.requests == []
    assert cost_lines(run) == []
    assert TUNNELLED in step_reason(run, 1)
    assert ended(run) == INCONCLUSIVE


def test_uncover_turns_count_against_the_turn_budget(tmp_path: Path) -> None:
    run = healed(
        tmp_path,
        STALE,
        NO_THANKS_PICK,
        html=MODAL,
        config=with_budgets("heal_turns: 1"),
    )

    assert len(run.requests()) == 1
    assert "the turn budget (1) ran out" in basis(run).said
    assert cost_lines(run) == [("healer", "heal")]
    assert ended(run) == INCONCLUSIVE


def test_an_uncover_request_takes_only_a_redacted_snapshot() -> None:
    raw = cast(Redacted, "the page's raw text")

    with pytest.raises(TypeError, match="Redacted"):
        uncover_request("a meaning", [], "", raw, redactor=NO_SECRETS)


def replied(message: AIMessage) -> Routed:
    return Routed(message, None, "ok", ())


def test_only_a_first_call_to_uncover_runs() -> None:
    wrote = {"ref": "e1", "confidence": 0.5, "rationale": "it closes"}

    assert uncovered(replied(calling("uncover", wrote).message)) == (
        Uncover.model_validate(wrote)
    )
    assert uncovered(replied(calling("rebind", wrote).message)) == Unfit(
        "the reply called a tool the healer doesn't have, so nothing ran"
    )
    assert uncovered(replied(calling("uncover", {"ref": "e1"}).message)) == Unfit(
        "the call to uncover was not run: its arguments don't fit the tool"
    )
    assert uncovered(replied(AIMessage(content="nothing uncovers it"))) is None
