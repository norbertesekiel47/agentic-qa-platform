import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aqa_core import project
from aqa_core.config import ProjectConfig
from aqa_core.spec import Spec
from aqa_runner import executor, probes
from aqa_runner.egress import (
    EgressGate,
    EgressRefusedError,
    EgressUpstreamError,
    InfrastructureEvent,
    Refusal,
)

from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    App,
    a_spec,
    compiled,
    read_steps,
    run,
    serving_app,
)

PROBE: dict[str, Any] = {
    "id": "a1",
    "expect_index": 0,
    "check": "probe_equals",
    "probe": "count",
    "json_path": "$.count",
    "value": 2,
}
AFTER: dict[str, Any] = {
    "id": "after",
    "expect_index": 0,
    "check": "url_matches",
    "pattern": "/",
}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setattr(probes, "READ_SECONDS", 0.02)
    with serving_app() as served:
        served.probes["count"] = [2]
        yield served


@pytest.fixture
def spec(tmp_path: Path) -> Spec:
    return a_spec(
        tmp_path,
        ProjectConfig(),
        start_url="/page/form",
        probes={"count": "GET /probe/count"},
    )


def test_probe_url_joins_the_declared_path_as_written(tmp_path: Path) -> None:
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url="/",
        probes={"count": "GET /raw/%2f%2fwhere?at=%41"},
    )

    assert project.probe_url(spec, "count", "http://example.test") == (
        "http://example.test/raw/%2f%2fwhere?at=%41"
    )


@pytest.mark.parametrize(
    ("value", "outcome"),
    [(2, "pass"), (2.0, "failed"), (True, "failed"), ("2", "failed")],
)
def test_probe_equals_reads_until_stable_and_compares_canonical_json(
    app: App,
    tmp_path: Path,
    spec: Spec,
    value: object,
    outcome: str,
) -> None:
    app.probes["count"] = [1, value, value]
    result = run(
        app, tmp_path, compiled([], assertions=[PROBE, AFTER]), spec=spec
    ).result

    assert [a.outcome for a in result.assertions] == [outcome, "pass"]
    assert result.outcome == ("passed" if outcome == "pass" else "failed")
    assert app.probe_reads == {"count": 3}


def test_probe_equals_selects_declared_nested_json_path(
    app: App, tmp_path: Path, spec: Spec
) -> None:
    app.probes["count"] = [
        {"nested": 2, "ignored": 0},
        {"nested": 2, "ignored": 1},
    ]
    check = {**PROBE, "json_path": "$.count.nested", "value": 2}
    result = run(
        app, tmp_path, compiled([], assertions=[check, AFTER]), spec=spec
    ).result

    assert [a.outcome for a in result.assertions] == ["pass", "pass"]
    assert result.outcome == "passed"
    assert app.probe_reads == {"count": 2}


BASELINE: dict[str, Any] = {
    "id": "a1",
    "expect_index": 0,
    "check": "probe_equals_baseline",
    "probe": "count",
}
BASELINES = {"count": {"capture_before_seq": 9, "json_path": "$.count"}}
CLICK = {
    "seq": 9,
    "action": "click",
    "target": "save",
    "side_effect": True,
    "side_effect_basis": "network: POST /write/save",
}


def test_probe_baseline_ignores_changes_outside_its_declared_json_path(
    app: App, tmp_path: Path, spec: Spec
) -> None:
    app.probes["count"] = [
        {"nested": 2, "ignored": 0},
        {"nested": 2, "ignored": 0},
        {"nested": 2, "ignored": 1},
        {"nested": 2, "ignored": 1},
    ]
    script = compiled(
        [CLICK],
        targets=FORM_TARGETS,
        assertions=[BASELINE],
        probe_baselines={
            "count": {"capture_before_seq": 9, "json_path": "$.count.nested"}
        },
    )
    result = run(app, tmp_path, script, spec=spec).result

    assert [a.outcome for a in result.assertions] == ["pass"]
    assert result.outcome == "passed"
    assert app.probe_reads == {"count": 4}
    assert app.seen.count(("POST", "/write/save")) == 1


@pytest.mark.parametrize(
    ("before", "after", "outcome"),
    [(0, 0, "pass"), (0, 1, "failed"), (0, 0.0, "failed"), (None, None, "pass")],
)
def test_baselines_are_captured_before_the_named_step_and_compared_after_the_last(
    app: App,
    tmp_path: Path,
    spec: Spec,
    *,
    before: object,
    after: object,
    outcome: str,
) -> None:
    app.probes["count"] = [before, before, after, after]
    script = compiled(
        [
            {"seq": 2, "action": "reload", "side_effect": False},
            CLICK,
            {"seq": 15, "action": "reload", "side_effect": False},
        ],
        targets=FORM_TARGETS,
        assertions=[BASELINE],
        probe_baselines=BASELINES,
    )
    done = run(app, tmp_path, script, spec=spec)

    assert [a.outcome for a in done.result.assertions] == [outcome]
    assert done.result.outcome == ("passed" if outcome == "pass" else "failed")
    captures = [lines for _, path, lines in app.records if path == "/probe/count"]
    assert [(lines[-1]["seq"], lines[-1]["state"]) for lines in captures] == [
        (2, "completed"),
        (2, "completed"),
        (15, "completed"),
        (15, "completed"),
    ]
    assert app.seen.count(("POST", "/write/save")) == 1
    assert [line["seq"] for line in read_steps(done.record.path)] == [
        0,
        0,
        2,
        2,
        9,
        9,
        15,
        15,
    ]


@pytest.mark.parametrize("baseline", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        (probes.ProbeError("fake-sensitive"), "the probe could not be read"),
        (ValueError("fake-sensitive"), "the probe could not be read"),
        (
            EgressRefusedError(
                Refusal("fake-sensitive.invalid", 80, "host", "fake-sensitive")
            ),
            "the probe request was refused by the egress policy",
        ),
        (
            EgressUpstreamError(
                InfrastructureEvent("fake-sensitive.invalid", 80, "fake-sensitive")
            ),
            "the probe request could not reach its allowed origin",
        ),
        (
            probes.ProbeUnstableError("fake-sensitive"),
            "the probe value did not stabilize within its read bound",
        ),
    ],
)
def test_probe_request_errors_never_copy_app_text(
    app: App,
    tmp_path: Path,
    spec: Spec,
    monkeypatch: pytest.MonkeyPatch,
    *,
    case: tuple[Exception, str],
    baseline: bool,
) -> None:
    error, reason = case

    async def failed(_gate: EgressGate, _url: str, _path: str) -> probes.JsonValue:
        raise error

    monkeypatch.setattr(probes, "read_stable", failed)
    script = compiled(
        [CLICK] if baseline else [],
        targets=FORM_TARGETS,
        assertions=[BASELINE if baseline else PROBE, AFTER],
        probe_baselines=BASELINES if baseline else {},
    )
    done = run(app, tmp_path, script, spec=spec)
    result = done.result
    timed_out = isinstance(error, probes.ProbeUnstableError) and not baseline
    assert result.outcome == ("failed" if timed_out else "errored")
    assert [a.outcome for a in result.assertions] == (
        ["check_timed_out", "pass"] if timed_out else ["not_evaluated"] * 2
    )
    if baseline:
        assert (
            result.steps[-1].seq,
            result.steps[-1].outcome,
            result.steps[-1].error,
        ) == (9, "failed", reason)
        assert [a.stopped_at for a in result.assertions] == [9, 9]
    else:
        assert result.assertions[0].error == (None if timed_out else reason)
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0]
    assert ("POST", "/write/save") not in app.seen
    assert "fake-sensitive" not in repr(result)
    assert all(
        "fake-sensitive" not in p.read_text() for p in done.record.path.iterdir()
    )


@pytest.mark.parametrize("targeted", [False, True])
def test_a_block_during_baseline_capture_prevents_dispatch(
    app: App,
    tmp_path: Path,
    spec: Spec,
    monkeypatch: pytest.MonkeyPatch,
    targeted: bool,
) -> None:
    original = probes.read_stable

    async def blocked(gate: EgressGate, url: str, path: str) -> probes.JsonValue:
        value = await original(gate, url, path)
        gate.record_refusal("blocked.invalid", 80, "host", "undeclared")
        return value

    monkeypatch.setattr(probes, "read_stable", blocked)
    step = CLICK if targeted else {"seq": 9, "action": "reload", "side_effect": False}
    done = run(
        app,
        tmp_path,
        compiled(
            [step],
            targets=FORM_TARGETS,
            assertions=[BASELINE],
            probe_baselines=BASELINES,
        ),
        spec=spec,
    )
    assert [s.seq for s in done.result.steps] == [0]
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0]
    assert app.seen.count(("GET", "/page/form")) == 1
    assert ("POST", "/write/save") not in app.seen
    assert (done.result.outcome, done.result.error_code) == (
        "errored",
        "egress_blocked",
    )
    assert [a.outcome for a in done.result.assertions] == ["not_evaluated"]
    assert done.result.invariants
    assert (done.record.path / "egress.json").exists()


def test_invalid_probe_names_are_refused_before_browser_open(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[object] = []

    def no_browser(*args: object, **kwargs: object) -> None:
        opened.append((args, kwargs))
        raise AssertionError("the browser was opened")

    monkeypatch.setattr(executor, "open_browser_session", no_browser)
    script = compiled(
        [CLICK],
        targets=FORM_TARGETS,
        assertions=[PROBE, BASELINE | {"id": "base"}],
        probe_baselines=BASELINES | {"unused": BASELINES["count"]},
    )
    with pytest.raises(project.SpecError) as refused:
        run(app, tmp_path, script)
    assert refused.value.problems == (
        "assertions[0] (a1): probe count is not declared by the spec",
        "assertions[1] (base): probe count is not declared by the spec",
        "probe_baselines[count]: probe count is not declared by the spec",
        "probe_baselines[unused]: probe unused is not declared by the spec",
    )
    assert opened == []
    assert app.seen == []


@pytest.mark.parametrize(
    "endpoint", ["/probe/count", "/held/no-answer", "/probe/malformed"]
)
def test_bounded_or_malformed_probe_reads_have_distinct_outcomes(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    app.probes["count"] = []
    monkeypatch.setattr(probes, "STABLE_SECONDS", 0.3)
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url="/page/form",
        probes={"count": f"GET {endpoint}"},
    )
    start = time.monotonic()
    done = run(app, tmp_path, compiled([], assertions=[PROBE, AFTER]), spec=spec)
    assert time.monotonic() - start < 8
    unstable = endpoint == "/probe/count"
    assert [a.outcome for a in done.result.assertions] == (
        ["check_timed_out", "pass"] if unstable else ["not_evaluated"] * 2
    )
    assert done.result.outcome == ("failed" if unstable else "errored")
    if unstable:
        assert app.probe_reads["count"] >= 2
    else:
        assert done.result.assertions[0].error == (
            "the probe request could not reach its allowed origin"
            if endpoint.endswith("malformed")
            else "the probe could not be read"
        )
    assert "fake-sensitive" not in repr(done.result.assertions)


def test_probe_replay_sends_no_browser_or_response_cookies(
    app: App, tmp_path: Path
) -> None:
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url="/page/probe-cookies",
        probes={"count": "GET /probe/count"},
    )
    result = run(app, tmp_path, compiled([], assertions=[PROBE]), spec=spec).result
    assert result.outcome == "passed"
    assert ("/did/cookie", "fake-browser=value") in app.cookies
    assert [cookie for path, cookie in app.cookies if path == "/probe/count"] == [
        None,
        None,
    ]


def test_probe_replay_refuses_an_origin_outside_the_gate(
    app: App,
    tmp_path: Path,
    spec: Spec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = probes.read_stable

    async def forbidden(gate: EgressGate, url: str, path: str) -> probes.JsonValue:
        gate.policy = replace(gate.policy, allowed_origins=())
        return await original(gate, url, path)

    monkeypatch.setattr(probes, "read_stable", forbidden)
    result = run(
        app, tmp_path, compiled([], assertions=[PROBE, AFTER]), spec=spec
    ).result
    assert (result.outcome, result.error_code) == ("errored", "egress_blocked")
    assert (
        result.assertions[0].error
        == "the probe request was refused by the egress policy"
    )
    assert [a.outcome for a in result.assertions] == ["not_evaluated"] * 2
    assert app.probe_reads == {}


def test_two_baselines_capture_their_own_sequence_and_state(
    app: App, tmp_path: Path
) -> None:
    app.probes.update(count=[1], other=[7])
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url="/page/form",
        probes={"count": "GET /probe/count", "other": "GET /probe/other"},
    )
    script = compiled(
        [CLICK, {"seq": 15, "action": "reload", "side_effect": False}],
        targets=FORM_TARGETS,
        assertions=[BASELINE, BASELINE | {"id": "other", "probe": "other"}],
        probe_baselines=BASELINES
        | {"other": {"capture_before_seq": 15, "json_path": "$.count"}},
    )
    result = run(app, tmp_path, script, spec=spec).result
    assert result.outcome == "passed"
    assert [a.outcome for a in result.assertions] == ["pass", "pass"]
    assert [
        (path, lines[-1]["seq"])
        for _, path, lines in app.records
        if path.startswith("/probe/")
    ] == [
        ("/probe/count", 0),
        ("/probe/count", 0),
        ("/probe/other", 9),
        ("/probe/other", 9),
        ("/probe/count", 15),
        ("/probe/count", 15),
        ("/probe/other", 15),
        ("/probe/other", 15),
    ]
