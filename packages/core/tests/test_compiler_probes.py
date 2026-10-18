"""Compiling a `probe_equals` check that exploring bound to one of its probe's
JSON paths (ADR-0025's #53 P20 amendment; DATA_MODEL §7, "Compilation
rules"): the check compiles with the path it was bound to, and a probe check
bound to no path, or another check bound to one, fails by name."""

from pathlib import Path

import pytest
from aqa_core.compiled import Target
from aqa_core.compiler import CompileError, ExploredPath, ProbePath, compile_script

from packages.core.tests.test_compiler import (
    BOUND,
    BY,
    EXPECTATIONS,
    KEPT,
    PROBED,
    STEPS,
    demo,
    expectation,
    plan,
)

# The demo plan with its last expectation read from the count probe: a6.
PROBING = plan(*EXPECTATIONS[:3], expectation(3, PROBED))


def bound(**changes: Target | ProbePath | None) -> dict[str, Target | ProbePath | None]:
    """The demo path's bindings for `PROBING`, a6 bound to `$.count`."""
    kept = {check_id: each for check_id, each in BOUND.items() if check_id < "a6"}
    return kept | {"a6": ProbePath("$.count")} | changes


def test_a_probe_check_compiles_with_the_json_path_it_was_bound_to(
    tmp_path: Path,
) -> None:
    spec, config = demo(tmp_path)

    script = compile_script(
        spec, config, PROBING, ExploredPath(STEPS, KEPT, bound()), BY
    )

    assert script.assertions[-1].model_dump(mode="json") == {
        "id": "a6",
        "expect_index": 3,
        "check": "probe_equals",
        "probe": "count",
        "json_path": "$.count",
        "value": 1,
    }


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        (
            {"a6": None},
            "a6 (expect[3], probe_equals): reads a probe, but is bound to no JSON path of it",
        ),
        (
            {"a1": ProbePath("$.count")},
            "a1 (expect[0], url_matches): reads no probe, but is bound to a JSON path",
        ),
        (
            {"a3": ProbePath("$.count")},
            "a3 (expect[1], text_in_target): reads no probe, but is bound to a JSON path",
        ),
    ],
    ids=["a probe check to nothing", "a check of no element", "a check of one"],
)
def test_a_probe_check_bound_against_its_shape_fails_by_name(
    tmp_path: Path, changes: dict[str, Target | ProbePath | None], problem: str
) -> None:
    spec, config = demo(tmp_path)

    with pytest.raises(CompileError) as failed:
        compile_script(
            spec, config, PROBING, ExploredPath(STEPS, KEPT, bound(**changes)), BY
        )

    assert failed.value.problems == (problem,)


def test_a_probe_path_is_a_json_path_this_format_reads() -> None:
    assert ProbePath("$.items[0].id").json_path == "$.items[0].id"
    with pytest.raises(
        ValueError, match=r"^'\$\.a b' is not a JSON path this format reads"
    ):
        ProbePath("$.a b")
