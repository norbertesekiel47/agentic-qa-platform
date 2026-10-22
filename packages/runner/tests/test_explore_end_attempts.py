"""An explore run of several attempts as `explore.json` says it (#56): every
phase in run order, named by its part, each part numbered by its attempt,
each confirmation's failures under its part, an unresolved side effect or
uncertain traffic in any part deciding, and every confirmation's result
this run's."""

from pathlib import Path

import pytest
from aqa_runner.egress_proxy import PhaseTraffic
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import StepResult
from aqa_runner.explore_result import Confirmation, Phase, present
from aqa_runner.invariants import InvariantResult
from aqa_runner.run_record import RunRecord, phase_parts

from packages.runner.tests.test_explore_result import (
    CLICK,
    QUIET,
    REFUSED,
    Parts,
    ending,
    replayed,
)


def two_attempts(tmp_path: Path) -> tuple[Parts, RunRecord, RunRecord]:
    record = RunRecord.create(tmp_path)
    made = Parts(record, record.part("attempt-1"), record.part("confirmation"))
    return made, record.part("attempt-2"), record.part("confirmation-2")


def test_each_phase_is_named_by_its_part_and_each_failed_confirmation_by_its_own(
    tmp_path: Path,
) -> None:
    made, second, confirming = two_attempts(tmp_path)
    failed = replayed(
        made, outcome="failed", assertions=(AssertionResult("a1", "failed"),)
    )
    end = ending(
        made,
        earlier=(
            Phase(made.attempt, QUIET, 0),
            Confirmation(made.confirmation, QUIET, 0, failed),
        ),
        attempt=Phase(second, QUIET, 0),
        confirmation=Confirmation(confirming, QUIET, 0, replayed(made)),
        published="confirmed",
    )

    document = present(end)

    assert (document["outcome"], document["exit_code"]) == ("compiled", 0)
    assert document["phases"] == [
        {"part": part, "plain_uncertain": 0, "tunnel_uncertain": 0, "policy_events": 0}
        for part in ("attempt-1", "confirmation", "attempt-2", "confirmation-2")
    ]
    assert document["reasons"] == ["confirmation a1: failed"]


def test_a_side_effect_left_unresolved_in_an_earlier_attempt_is_non_resumable(
    tmp_path: Path,
) -> None:
    made, second, _ = two_attempts(tmp_path)
    made.attempt.step_intent(3, CLICK, side_effect=True, target_used="save")
    end = ending(
        made, earlier=(Phase(made.attempt, QUIET, 0),), attempt=Phase(second, QUIET, 0)
    )

    document = present(end)

    assert (document["outcome"], document["exit_code"]) == ("non_resumable", 4)
    assert document["non_resumable"] == [{"part": "attempt-1", "seq": 3}]


def test_an_earlier_phase_out_of_its_place_is_refused(tmp_path: Path) -> None:
    made, second, _ = two_attempts(tmp_path)

    with pytest.raises(ValueError, match=REFUSED.format("attempt-1")):
        ending(
            made, earlier=(Phase(second, QUIET, 0),), attempt=Phase(second, QUIET, 0)
        )


def test_parts_are_numbered_by_attempt(tmp_path: Path) -> None:
    record = RunRecord.create(tmp_path)

    assert [phase_parts(n) for n in (1, 2, 12)] == [
        ("attempt-1", "confirmation"),
        ("attempt-2", "confirmation-2"),
        ("attempt-12", "confirmation-12"),
    ]
    for name in ("attempt-2", "confirmation-2", "attempt-12"):
        record.part(name)
    for name in ("attempt-0", "attempt-02", "attempt-", "attempt-\u0663", "part-2"):
        with pytest.raises(ValueError, match="isn't a part"):
            record.part(name)


def test_each_confirmations_step_check_and_invariant_failures_are_under_its_part(
    tmp_path: Path,
) -> None:
    made, second, confirming = two_attempts(tmp_path)
    violated = InvariantResult("js_exceptions", "violated", ("Error: one",), 1)
    first = replayed(
        made, outcome="failed", steps=(StepResult(2, "failed"),), invariants=(violated,)
    )
    last = replayed(
        made,
        outcome="failed",
        steps=(StepResult(3, "drifted", misses=("not the part",)),),
        assertions=(AssertionResult("a1", "failed"),),
        invariants=(violated,),
    )
    end = ending(
        made,
        earlier=(
            Phase(made.attempt, QUIET, 0),
            Confirmation(made.confirmation, QUIET, 0, first),
        ),
        attempt=Phase(second, QUIET, 0),
        confirmation=Confirmation(confirming, QUIET, 0, last),
    )

    assert present(end)["reasons"] == [
        "confirmation step 2: failed",
        "confirmation invariant js_exceptions: violated",
        "confirmation-2 step 3: drifted (not the part)",
        "confirmation-2 a1: failed",
        "confirmation-2 invariant js_exceptions: violated",
    ]


def test_uncertain_traffic_in_an_earlier_attempt_exits_14_under_its_part(
    tmp_path: Path,
) -> None:
    made, second, _ = two_attempts(tmp_path)
    end = ending(
        made,
        earlier=(Phase(made.attempt, PhaseTraffic(2, 1), 0),),
        attempt=Phase(second, QUIET, 3),
        published="unconfirmed",
    )

    document = present(end)

    assert (document["outcome"], document["exit_code"]) == ("reset_unsafe", 14)
    assert document["phases"] == [
        {
            "part": "attempt-1",
            "plain_uncertain": 2,
            "tunnel_uncertain": 1,
            "policy_events": 0,
        },
        {
            "part": "attempt-2",
            "plain_uncertain": 0,
            "tunnel_uncertain": 0,
            "policy_events": 3,
        },
    ]


@pytest.mark.parametrize("part", ["confirmation", "confirmation-2"])
def test_a_confirmations_result_of_another_run_is_refused_in_every_part(
    tmp_path: Path, part: str
) -> None:
    made, second, confirming = two_attempts(tmp_path)
    another = replayed(made, run_id="another-run")

    with pytest.raises(ValueError, match=f"^the {part}'s result is another run's$"):
        ending(
            made,
            earlier=(
                Phase(made.attempt, QUIET, 0),
                Confirmation(
                    made.confirmation,
                    QUIET,
                    0,
                    another if part == "confirmation" else replayed(made),
                ),
            ),
            attempt=Phase(second, QUIET, 0),
            confirmation=Confirmation(
                confirming,
                QUIET,
                0,
                another if part == "confirmation-2" else replayed(made),
            ),
        )
