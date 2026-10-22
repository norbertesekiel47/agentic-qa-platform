"""`side_effect` inference at the compiler (ADR-0025's #54 amendment; TESTING
§2, test-first). A step compiles `false` only when every run of it showed
positive evidence and the model agreed; any other step names each condition
it failed, in fixed words."""

from itertools import product
from pathlib import Path
from typing import Literal

import pytest
from aqa_core.compiler import (
    ExploredPath,
    Observed,
    PathStep,
    compile_script,
    side_effect_basis,
)

from packages.core.tests.test_compiler import BOUND, BY, PLAN, demo

BASIS = "no positive evidence that the step changes nothing"
BUSY = "settle: the page was still busy when settling's wait ran out"
WROTE = "network: a write request"
LATER = "network: a write request came later, which it may have caused"
TUNNEL = (
    "network: an encrypted or WebSocket connection was open, which the egress "
    "proxy can't read"
)
DISAGREED = "model: finish didn't list it in changes_nothing"
CLEAN = Observed("idle", wrote=False, wrote_later=False, tunnelled=False)
WRITING = Observed("idle", wrote=True, wrote_later=False, tunnelled=False)


def navigate(*observed: Observed | None) -> PathStep:
    return PathStep({"action": "navigate", "url": "/demo"}, observed=observed)


def flags(
    tmp_path: Path,
    steps: dict[int, PathStep],
    kept: tuple[int, ...],
    changes_nothing: tuple[int, ...],
) -> list[tuple[bool, str | None]]:
    spec, config = demo(tmp_path)
    path = ExploredPath(steps, kept, BOUND, changes_nothing=changes_nothing)
    script = compile_script(spec, config, PLAN, path, BY)
    return [(step.side_effect, step.side_effect_basis) for step in script.steps]


@pytest.mark.parametrize(
    ("observed", "agreed", "expected"),
    [
        (CLEAN, True, (False, None)),
        (Observed("timeout", False, False, False), True, (True, BUSY)),
        (WRITING, True, (True, WROTE)),
        (Observed("idle", False, True, False), True, (True, LATER)),
        (Observed("idle", False, False, True), True, (True, TUNNEL)),
        (CLEAN, False, (True, DISAGREED)),
        (
            Observed("timeout", True, True, True),
            False,
            (True, f"{BUSY}; {WROTE}; {LATER}; {TUNNEL}; {DISAGREED}"),
        ),
    ],
    ids=["all-four", "settle", "write", "later-write", "tunnel", "model", "every"],
)
def test_each_missing_condition_keeps_the_step_true_naming_it(
    tmp_path: Path,
    observed: Observed,
    agreed: bool,
    expected: tuple[bool, str | None],
) -> None:
    assert flags(tmp_path, {1: navigate(observed)}, (1,), (1,) if agreed else ()) == [
        expected
    ]


def test_only_all_four_conditions_lower_the_flag() -> None:
    settles: tuple[Literal["idle", "timeout"], ...] = ("idle", "timeout")
    lowered = [
        (settled, wrote, later, tunnelled, agreed)
        for settled in settles
        for wrote, later, tunnelled, agreed in product((False, True), repeat=4)
        if side_effect_basis(
            (Observed(settled, wrote, later, tunnelled),), agreed=agreed
        )
        is None
    ]

    assert lowered == [("idle", False, False, False, True)]


@pytest.mark.parametrize(
    "observed",
    [(), (None,), (CLEAN, None), (None, CLEAN)],
    ids=["never-observed", "unheard", "confirmation-unheard", "attempt-unheard"],
)
def test_a_step_a_run_of_which_showed_nothing_stays_true_whatever_finish_says(
    tmp_path: Path, observed: tuple[Observed | None, ...]
) -> None:
    assert flags(tmp_path, {1: navigate(*observed)}, (1,), (1,)) == [(True, BASIS)]


def test_a_second_run_that_wrote_raises_the_flag_the_first_left_false(
    tmp_path: Path,
) -> None:
    assert flags(tmp_path / "twice", {1: navigate(CLEAN, CLEAN)}, (1,), (1,)) == [
        (False, None)
    ]
    assert flags(tmp_path / "again", {1: navigate(CLEAN, WRITING)}, (1,), (1,)) == [
        (True, WROTE)
    ]


def test_changes_nothing_naming_another_step_lowers_none_it_keeps(
    tmp_path: Path,
) -> None:
    steps = {1: navigate(CLEAN), 2: navigate(CLEAN)}

    assert flags(tmp_path, steps, (1,), (2, 99)) == [(True, DISAGREED)]


def test_leaving_changes_nothing_out_keeps_every_step_true(tmp_path: Path) -> None:
    spec, config = demo(tmp_path)
    path = ExploredPath({1: navigate(CLEAN)}, (1,), BOUND)

    (step,) = compile_script(spec, config, PLAN, path, BY).steps

    assert (step.side_effect, step.side_effect_basis) == (True, DISAGREED)


def test_the_start_needs_the_same_evidence_and_the_models_agreement() -> None:
    # Step 0, the start URL's load, is judged by the same rule (N2).
    assert [
        side_effect_basis((CLEAN,), agreed=True),
        side_effect_basis((CLEAN,), agreed=False),
        side_effect_basis((), agreed=True),
        side_effect_basis((CLEAN, WRITING), agreed=True),
    ] == [None, DISAGREED, BASIS, WROTE]
