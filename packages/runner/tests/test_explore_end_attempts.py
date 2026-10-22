"""An explore run of several attempts as `explore.json` says it (#56): every
phase in run order, named by its part, each part numbered by its attempt,
each confirmation's failures under its part, and an unresolved side effect
in any part non-resumable."""

from pathlib import Path

import pytest
from aqa_runner.evaluation import AssertionResult
from aqa_runner.explore_result import Confirmation, Phase, present
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
