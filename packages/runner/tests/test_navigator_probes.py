"""Binding a `probe_equals` check to one of its probe's options (#53 P20;
ADR-0025's #53 P20 amendment): `assert_check` takes the ref of an option
the attempt's probes offer, binds the check to that option's JSON path, and
evaluates it through the probe's stable read now, and `finish` reads it
again before it compiles the path. A ref that is no option of the check's
own probe, or an option whose path spells a bound value, binds nothing.
Real Chromium against the executor's fixture app."""

import json
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Unpack

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.compiler import Provenance
from aqa_core.coverage_plan import CoveragePlan
from aqa_runner import probes
from aqa_runner.egress import EgressGate
from aqa_runner.navigator import AssertCheck, Finish, Offered, ProbeOption
from aqa_runner.navigator_tools import Attempt

from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.navigator_fixtures import Run, bound, on_app
from packages.runner.tests.secret_fixtures import copies_found

BY = Provenance(
    models={"navigator": "fake-model"},
    price_map="6f0c3a9d2b8e4f17a5c9d0e3b6a1f4c8d2e7b9a0",
    at=datetime(2026, 10, 18, tzinfo=UTC),
)
BOUND = "a1 is bound, and on this page it is pass"
COMPILED = "finish compiled the path, and every check passes on this page"
# A test secret's fake value that a JSON path can name as a key.
KEY_VALUE = "fakeP20secret"
# Where the spec's probes are read.
PROBES = {"count": "GET /probe/count", "other": "GET /probe/other"}


def probe_check(probe: str, value: object) -> dict[str, object]:
    return {"check": "probe_equals", "probe": probe, "value": value}


def plan_of(*checks: dict[str, object]) -> CoveragePlan:
    """A plan of the form's one expectation, read by `checks`."""
    return CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": "the form's saved count",
                    "claim": "two forms are saved",
                    "checks": list(checks),
                }
            ],
            "requires": [],
        }
    )


# a1 reads the count probe, and in two-probe plans a2 reads the other.
COUNTED = plan_of(probe_check("count", 2))
TWO_PROBES = plan_of(probe_check("count", 2), probe_check("other", 1))
# What the count probe offers: its count's path.
COUNT_OPTION = Offered({"o1": ProbeOption("count", "$.count")})


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def probing[T](
    app: App,
    tmp_path: Path,
    scenario: Callable[[Attempt], Awaitable[T]],
    **run: Unpack[Run],
) -> T:
    """`scenario`'s result for an attempt whose spec declares `PROBES`, for
    `COUNTED` with `COUNT_OPTION` unless `run` says otherwise."""
    run.setdefault("plan", COUNTED)
    run.setdefault("offered", COUNT_OPTION)
    run["probes"] = PROBES
    return on_app(app, tmp_path, scenario, **run)


def json_paths(script: CompiledScript | None) -> list[object]:
    """Each compiled assertion's JSON path, None for one with no path."""
    assert script is not None
    return [each.model_dump().get("json_path") for each in script.assertions]


def test_a_probe_option_maps_to_its_original_path_and_a_redacted_key_is_never_executable(
    app: App, tmp_path: Path
) -> None:
    app.probe_bodies["count"] = [json.dumps({"count": 2, KEY_VALUE: {"n": 1}}).encode()]
    offered = Offered(
        {
            "o1": ProbeOption("count", "$.count"),
            "o2": ProbeOption("count", f"$.{KEY_VALUE}.n"),
        }
    )

    async def scenario(attempt: Attempt) -> list[object]:
        refused = await attempt.check(AssertCheck(check_id="a1", ref="o2"))
        unbound = attempt.checks
        bound_now = await attempt.check(AssertCheck(check_id="a1", ref="o1"))
        finished = await attempt.finish(Finish(steps=()), BY)
        return [refused, unbound, bound_now, finished.text, json_paths(finished.script)]

    seen = probing(
        app,
        tmp_path,
        scenario,
        offered=offered,
        secrets=[bound(app.origin, KEY_VALUE)],
    )

    assert seen == [
        (
            "assert_check did not run: o2's path holds a test secret's value, so no "
            "check can read it"
        ),
        {"a1": None},
        BOUND,
        COMPILED,
        ["$.count"],
    ]
    assert copies_found(KEY_VALUE, texts=[str(each) for each in seen]) == []


WRONG_REF = (
    "assert_check did not run: a1 reads probe count: call it with the ref of one "
    "of that probe's options in the probes block"
)


@pytest.mark.parametrize(
    ("ref", "refusal"),
    [
        (None, WRONG_REF),
        ("e3", WRONG_REF),
        ("o9", WRONG_REF),
        ("p1.2", WRONG_REF),
        (
            "o2",
            (
                "assert_check did not run: o2 is an option of probe other, and a1 "
                "reads probe count"
            ),
        ),
    ],
    ids=["no ref", "an element's", "no option", "a part's", "another probe's option"],
)
def test_a_probe_check_takes_only_an_option_of_its_own_probe(
    app: App, tmp_path: Path, ref: str | None, refusal: str
) -> None:
    offered = Offered(
        {"o1": ProbeOption("count", "$.count"), "o2": ProbeOption("other", "$.n")}
    )

    async def scenario(attempt: Attempt) -> list[object]:
        refused = await attempt.check(AssertCheck(check_id="a1", ref=ref))
        other = await attempt.check(AssertCheck(check_id="a2", ref="o2"))
        return [refused, other, attempt.checks["a1"]]

    app.probe_bodies["other"] = [b'{"n": 1}']
    seen = probing(app, tmp_path, scenario, plan=TWO_PROBES, offered=offered)

    assert seen == [refusal, "a2 is bound, and on this page it is pass", None]
    # The refused calls read no probe; a2's binding read its own twice.
    assert app.probe_reads == {"other": 2}


@pytest.mark.parametrize(
    ("bodies", "results"),
    [
        (
            [b'{"count": 1}', b'{"count": 1}', b'{"count": 2}'],
            ["a1 is bound, and on this page it is failed", COMPILED, ["$.count"]],
        ),
        (
            [b'{"count": 2}', b'{"count": 2}', b'{"count": 3}'],
            [BOUND, "finish compiled nothing: on this page a1 is failed", None],
        ),
    ],
    ids=["failed, then passing", "passing, then failed"],
)
def test_finish_reads_a_bound_probe_check_again(
    app: App, tmp_path: Path, bodies: list[bytes | int], results: list[object]
) -> None:
    app.probe_bodies["count"] = bodies

    async def scenario(attempt: Attempt) -> list[object]:
        checked = await attempt.check(AssertCheck(check_id="a1", ref="o1"))
        finished = await attempt.finish(Finish(steps=()), BY)
        script = finished.script
        return [checked, finished.text, script and json_paths(script)]

    assert probing(app, tmp_path, scenario) == results


def test_a_probe_that_cannot_be_read_now_leaves_its_check_not_evaluated(
    app: App, tmp_path: Path
) -> None:
    app.probe_bodies["count"] = [500]

    async def scenario(attempt: Attempt) -> list[object]:
        checked = await attempt.check(AssertCheck(check_id="a1", ref="o1"))
        result = attempt.checks["a1"]
        return [checked, None if result is None else result.outcome]

    assert probing(app, tmp_path, scenario) == [
        "a1 is bound, and on this page it is not_evaluated: the probe could not be read",
        "not_evaluated",
    ]


# The count probe's two keys: o2 is a decoy that holds the expected 2.
TWO_KEYS = Offered(
    {"o1": ProbeOption("count", "$.count"), "o2": ProbeOption("count", "$.other")}
)
DIFFERS = b'{"count": 3, "other": 2}'
HOLDS = b'{"count": 2, "other": 2}'
FAILED = "a1 is bound, and on this page it is failed"
UNREAD = (
    "a1 is bound, and on this page it is not_evaluated: the probe could not be read"
)
KEEPS_O1 = (
    "assert_check did not run: a1 didn't pass on o1, so it keeps o1 for the rest "
    "of this attempt"
)

# An `assert_check` call's check ID and ref, or None for `finish`.
type Move = tuple[str, str] | None


def played(moves: list[Move]) -> Callable[[Attempt], Awaitable[list[object]]]:
    """A scenario that makes `moves` in order, then gives each call's text
    and the last `finish`'s compiled JSON paths, None when it compiled
    nothing."""

    async def scenario(attempt: Attempt) -> list[object]:
        texts: list[str] = []
        script: CompiledScript | None = None
        for move in moves:
            if move is None:
                finished = await attempt.finish(Finish(steps=()), BY)
                texts.append(finished.text)
                script = finished.script
            else:
                check_id, ref = move
                texts.append(
                    await attempt.check(AssertCheck(check_id=check_id, ref=ref))
                )
        return [texts, script and json_paths(script)]

    return scenario


KEPT = {
    "a value that differs": (
        [DIFFERS, DIFFERS, HOLDS],
        [("a1", "o1"), ("a1", "o2"), None],
        [[FAILED, KEEPS_O1, COMPILED], ["$.count"]],
        4,
    ),
    "a key that went away": (
        [b'{"other": 2}', HOLDS],
        [("a1", "o1"), ("a1", "o2"), None],
        [[UNREAD, KEEPS_O1, COMPILED], ["$.count"]],
        3,
    ),
    "passing, then failed at finish": (
        [HOLDS, HOLDS, DIFFERS],
        [("a1", "o1"), None, ("a1", "o2")],
        [[BOUND, "finish compiled nothing: on this page a1 is failed", KEEPS_O1], None],
        4,
    ),
    "its own option again, then another": (
        [DIFFERS, DIFFERS, HOLDS],
        [("a1", "o1"), ("a1", "o1"), ("a1", "o2"), None],
        [[FAILED, BOUND, KEEPS_O1, COMPILED], ["$.count"]],
        6,
    ),
    "control: passed, then another": (
        [HOLDS],
        [("a1", "o1"), ("a1", "o2"), None],
        [[BOUND, BOUND, COMPILED], ["$.other"]],
        6,
    ),
}


@pytest.mark.parametrize(
    ("bodies", "moves", "expected", "reads"), KEPT.values(), ids=KEPT.keys()
)
def test_a_probe_check_that_didnt_pass_keeps_its_option(
    app: App,
    tmp_path: Path,
    *,
    bodies: list[bytes | int],
    moves: list[Move],
    expected: list[object],
    reads: int,
) -> None:
    app.probe_bodies["count"] = bodies

    assert probing(app, tmp_path, played(moves), offered=TWO_KEYS) == expected
    assert app.probe_reads == {"count": reads}


def test_a_probe_check_that_timed_out_keeps_its_option(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.probe_bodies["count"] = [HOLDS]
    stable = probes.read_stable
    calls: list[str] = []

    async def unstable_once(
        gate: EgressGate, url: str, json_path: str
    ) -> probes.JsonValue:
        calls.append(json_path)
        if len(calls) == 1:
            raise probes.ProbeUnstableError("fake: never held still")
        return await stable(gate, url, json_path)

    monkeypatch.setattr(probes, "read_stable", unstable_once)
    moves: list[Move] = [("a1", "o1"), ("a1", "o2"), None]

    assert probing(app, tmp_path, played(moves), offered=TWO_KEYS) == [
        ["a1 is bound, and on this page it is check_timed_out", KEEPS_O1, COMPILED],
        ["$.count"],
    ]
    assert (calls, app.probe_reads) == (["$.count", "$.count"], {"count": 2})


def test_a_check_left_unevaluated_by_another_checks_look_keeps_its_option(
    app: App, tmp_path: Path
) -> None:
    both = b'{"count": 2, "other": 3}'
    app.probe_bodies["count"] = [both, both, both, both, 500, both]
    plan = plan_of(probe_check("count", 2), probe_check("count", 3))
    moves: list[Move] = [("a1", "o1"), ("a2", "o2"), None, ("a2", "o1"), None]

    assert probing(app, tmp_path, played(moves), plan=plan, offered=TWO_KEYS) == [
        [
            BOUND,
            "a2 is bound, and on this page it is pass",
            (
                "finish compiled nothing: on this page a1 is not_evaluated: the probe "
                "could not be read; a2 is not_evaluated: not evaluated after a1's look "
                "at the page raised"
            ),
            (
                "assert_check did not run: a2 didn't pass on o2, so it keeps o2 for "
                "the rest of this attempt"
            ),
            COMPILED,
        ],
        ["$.count", "$.other"],
    ]
    assert app.probe_reads == {"count": 9}


def test_a_kept_option_is_its_checks_alone(app: App, tmp_path: Path) -> None:
    app.probe_bodies["count"] = [b'{"count": 3, "other": 3}']
    plan = plan_of(probe_check("count", 2), probe_check("count", 3))
    moves: list[Move] = [("a1", "o1"), ("a2", "o2")]

    assert probing(app, tmp_path, played(moves), plan=plan, offered=TWO_KEYS) == [
        [FAILED, "a2 is bound, and on this page it is pass"],
        None,
    ]
