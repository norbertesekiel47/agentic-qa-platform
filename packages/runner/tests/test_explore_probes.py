"""Exploring a plan whose checks read probes (#53 P20; ADR-0025's and
ADR-0024's #53 P20 amendments): after the attempt's reset and before its
browser opens, each probe a `probe_equals` check reads is read once through
the run's gate, and the JSON paths of its values reach the model as options
in a `<probes>` block, never the values; a probe that offers none stops the
attempt before the browser; a check bound to an option compiles, confirms
and publishes with that path. Admission scans each probe's endpoint whole.
Real Chromium against the executor's fixture app, with a scripted model."""

import asyncio
import json
import socket
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import SpecError
from aqa_runner import explore, probes
from aqa_runner.egress import EgressGate, EgressPolicy
from aqa_runner.explore import Explored
from aqa_runner.model_router import ModelRouter
from langchain_core.messages import AIMessage, BaseMessage
from playwright.async_api import async_playwright

from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    Move,
    Scripted,
    answer,
    calling,
)
from packages.runner.tests.secret_fixtures import copies_found
from packages.runner.tests.test_explore_confirm import published
from packages.runner.tests.test_explore_graph import (
    SECRETS,
    configured,
    files,
    shop_spec,
    written,
)
from packages.runner.tests.test_explore_preflight import planned

RESET = "  reset: { http: POST /write/reset }\n"
COUNT = '  probes: { count: "GET /probe/count" }\n'
FINISH = calling("finish", {"steps": []})
# A reply that calls no tool: its turn runs nothing.
IDLE = answer(AIMessage(content="I'll look again."))
BOUND = "a1 is bound, and on this page it is pass"
UNREAD = "probe count offers no option: the probe could not be read"


def check(ref: str | None, check_id: str = "a1") -> Move:
    return calling("assert_check", {"check_id": check_id, "ref": ref})


def probe_check(probe: str = "count", value: object = 2) -> dict[str, object]:
    return {"check": "probe_equals", "probe": probe, "value": value}


def plan_of(*checks: Mapping[str, object]) -> CoveragePlan:
    """A plan of the shop's one expectation, read by `checks`."""
    return CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": "the shop's saved names",
                    "claim": "exactly 2 names are saved",
                    "checks": list(checks),
                }
            ],
            "requires": [],
        }
    )


COUNTED = plan_of(probe_check())


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        served.probes["count"] = [2]
        yield served


def probed(
    app: App,
    tmp_path: Path,
    client: Scripted,
    preconditions: str,
    *,
    config: ProjectConfig | None = None,
    start: str | None = None,
) -> Explored:
    """How `explore` ended a run of the shop spec with `preconditions`
    under the spec root `tmp_path / "qa"`, whose `config.yaml` holds
    `config`, from `start` (the app's origin), with `client` as its
    model."""
    config = config or ProjectConfig()
    root = configured(tmp_path / "qa", config)
    spec = shop_spec(root, config, "/page/shop", preconditions)

    async def scenario() -> Explored:
        async with async_playwright() as playwright, asyncio.timeout(120):
            return await explore.explore(
                spec,
                config,
                root,
                start or app.origin,
                router=ModelRouter.from_config(config, client),
                playwright=playwright,
            )

    return asyncio.run(scenario())


def block(messages: Sequence[BaseMessage], label: str) -> str | None:
    """The text of the turn's block `label`, or None without one."""
    text = messages[-1].text
    opened, closed = f"<{label}>\n", f"\n</{label}>"
    if opened not in text:
        return None
    return text.split(opened, 1)[1].split(closed, 1)[0]


def blocks(client: Scripted, label: str) -> list[str | None]:
    """Each navigator request's block `label`, past the plan's request."""
    return [block(request, label) for request in client.requests[1:]]


def record_texts(run: Explored) -> list[str]:
    """Every file of the run's record, as text."""
    assert run.record is not None
    return [
        path.read_text() for path in sorted(run.record.rglob("*")) if path.is_file()
    ]


def sent(client: Scripted) -> list[str]:
    """Every message of every request, as text."""
    return [str(message.content) for request in client.requests for message in request]


def test_each_probe_is_read_once_after_the_reset_and_before_the_browser(
    app: App, tmp_path: Path
) -> None:
    reads: list[dict[str, int]] = []

    def first(_messages: Sequence[BaseMessage]) -> Any:
        reads.append(dict(app.probe_reads))
        return check("o1")

    client = Scripted(planned(COUNTED), first, FINISH)

    run = probed(app, tmp_path, client, RESET + COUNT)

    assert [(method, path) for method, path, _ in app.records[:3]] == [
        ("POST", "/write/reset"),
        ("GET", "/probe/count"),
        ("GET", "/page/shop"),
    ]
    assert reads == [{"count": 1}]
    turn = client.requests[1][-1].text
    assert (
        "<results>\n\n</results>\n\n<probes>\no1 count $.count\n</probes>\n\n"
        "<snapshot>\n"
    ) in turn
    assert (run.outcome, run.exit_code) == ("compiled", 0)


def test_a_bound_probe_check_compiles_confirms_and_publishes_its_path(
    app: App, tmp_path: Path
) -> None:
    run = probed(
        app, tmp_path, Scripted(planned(COUNTED), check("o1"), FINISH), RESET + COUNT
    )

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert (written(run)["published"], written(run)["reasons"]) == ("confirmed", [])
    script = published(tmp_path)
    assert script.confirmed
    assert [each.model_dump(mode="json") for each in script.assertions] == [
        {
            "id": "a1",
            "expect_index": 0,
            "check": "probe_equals",
            "probe": "count",
            "json_path": "$.count",
            "value": 2,
        }
    ]
    assert {"turns/1.json", "turns/2.json"} <= set(files(run))
    # The capture, the binding's two reads, finish's two, the replay's two.
    assert app.probe_reads == {"count": 7}


def test_probe_values_never_reach_the_model(app: App, tmp_path: Path) -> None:
    app.probe_bodies["count"] = [
        b'{"count": 2, "note": "fake-probe-sentinel", "total": 918273}'
    ]
    client = Scripted(planned(COUNTED), check("o1"), FINISH)

    run = probed(app, tmp_path, client, COUNT)

    assert (
        blocks(client, "probes")
        == ["o1 count $.count\no2 count $.note\no3 count $.total"] * 2
    )
    texts = [*sent(client), *record_texts(run), published(tmp_path).model_dump_json()]
    assert [
        copies_found(value, texts=texts) for value in ("fake-probe-sentinel", "918273")
    ] == [[], []]
    assert len(client.requests) == 3


def test_a_probe_key_holding_a_bound_value_shows_its_marker_and_never_binds(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "fakeP20secret"
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)
    app.probe_bodies["count"] = [json.dumps({"count": 2, value: {"n": 1}}).encode()]
    account = "  account: { email: reader@example.test, password: { secret: TEST_PASSWORD } }\n"
    client = Scripted(planned(COUNTED), check("o2"), check("o1"), FINISH)

    run = probed(app, tmp_path, client, COUNT + account, config=SECRETS)

    assert (
        blocks(client, "probes")
        == ["o1 count $.count\no2 count $.[SECRET:TEST_PASSWORD].n"] * 3
    )
    assert blocks(client, "results")[1:] == [
        "assert_check did not run: o2's path holds a test secret's value, so no check can read it",
        BOUND,
    ]
    assert (
        run.outcome,
        published(tmp_path).assertions[0].model_dump().get("json_path"),
    ) == ("compiled", "$.count")
    assert copies_found(value, texts=[*sent(client), *record_texts(run)]) == []


def test_a_probe_with_paths_too_long_to_offer_says_so_and_keeps_the_rest(
    app: App, tmp_path: Path
) -> None:
    long = "k" * 127
    app.probe_bodies["count"] = [json.dumps({"count": 2, long: 1}).encode()]
    client = Scripted(planned(COUNTED), check("o1"), FINISH)

    run = probed(app, tmp_path, client, COUNT)

    listing = (
        "o1 count $.count\ncount: values under paths over 128 characters aren't offered"
    )
    assert blocks(client, "probes") == [listing] * 2
    assert run.record is not None
    turn = json.loads((run.record / "turns" / "1.json").read_text())
    assert f"<probes>\n{listing}\n</probes>" in turn["sent"]
    assert copies_found(long, texts=[*sent(client), *record_texts(run)]) == []
    assert (run.outcome, run.exit_code) == ("compiled", 0)


def test_a_probe_that_cannot_be_read_now_leaves_its_check_not_evaluated(
    app: App, tmp_path: Path
) -> None:
    app.probe_bodies["count"] = [b'{"count": 2}', 500]
    client = Scripted(planned(COUNTED), check("o1"), IDLE)
    budget = ProjectConfig.model_validate({"budgets": {"actions_per_attempt": 2}})

    run = probed(app, tmp_path, client, COUNT, config=budget)

    assert blocks(client, "results") == [
        "",
        "a1 is bound, and on this page it is not_evaluated: the probe could not be read",
    ]
    assert (run.exit_code, written(run)["reasons"]) == (
        3,
        [
            "the attempt took all 2 of its turns",
            "a1: not_evaluated: the probe could not be read",
        ],
    )


def test_two_probes_number_their_options_on_from_each_other_in_plan_order(
    app: App, tmp_path: Path
) -> None:
    sixty = json.dumps({f"v{i}": i for i in range(60)}).encode()
    app.probe_bodies |= {"a": [sixty], "b": [sixty]}
    declared = '  probes: { a: "GET /probe/a", b: "GET /probe/b" }\n'
    plan = plan_of(probe_check("b", 0), probe_check("a", 59), probe_check("b", 59))
    reads: list[dict[str, int]] = []

    def first(_messages: Sequence[BaseMessage]) -> Any:
        reads.append(dict(app.probe_reads))
        return check("o1")

    client = Scripted(
        planned(plan), first, check("o120", "a2"), check("o60", "a3"), FINISH
    )

    run = probed(app, tmp_path, client, declared)

    listing = blocks(client, "probes")[0]
    assert listing is not None
    lines = listing.split("\n")
    captured = [path for _, path, _ in app.records if path.startswith("/probe/")][:2]
    assert (captured, reads) == (["/probe/b", "/probe/a"], [{"b": 1, "a": 1}])
    assert (len(lines), len(listing)) == (120, 1431)
    assert (lines[0], lines[59], lines[60], lines[119]) == (
        "o1 b $.v0",
        "o60 b $.v59",
        "o61 a $.v0",
        "o120 a $.v59",
    )
    assert blocks(client, "results")[1:] == [
        BOUND,
        "a2 is bound, and on this page it is pass",
        "a3 is bound, and on this page it is pass",
    ]
    assert [
        each.model_dump().get("json_path") for each in published(tmp_path).assertions
    ] == [
        "$.v0",
        "$.v59",
        "$.v59",
    ]
    assert run.exit_code == 0


def test_a_probe_of_exactly_100_values_offers_them_all(
    app: App, tmp_path: Path
) -> None:
    app.probe_bodies["count"] = [json.dumps(list(range(100))).encode()]
    client = Scripted(planned(COUNTED), check("o3"), FINISH)

    run = probed(app, tmp_path, client, COUNT)

    listing = blocks(client, "probes")[0]
    assert listing is not None
    lines = listing.split("\n")
    assert (len(lines), lines[0], lines[99]) == (
        100,
        "o1 count $[0]",
        "o100 count $[99]",
    )
    assert (run.outcome, run.exit_code) == ("compiled", 0)


def test_a_refusal_met_at_the_capture_stops_before_the_browser_and_is_exit_6(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gates: list[EgressGate] = []

    def recording(policy: EgressPolicy) -> EgressGate:
        gates.append(EgressGate(policy))
        return gates[-1]

    async def refused(*_: object, **__: object) -> probes.JsonValue:
        raise gates[0].record_refusal("analytics.example.test", 80, "host", "fake")

    monkeypatch.setattr(explore, "EgressGate", recording)
    monkeypatch.setattr(probes, "read_once", refused)
    client = Scripted(planned(COUNTED))

    run = probed(app, tmp_path, client, COUNT)

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    assert written(run)["reasons"] == [
        "probe count offers no option: the probe request was refused by the egress policy",
        "a1: never bound",
    ]
    assert (len(client.requests), app.paths()) == (1, [])


NO_OPTION = (
    "probe count offers no option: its response has {} values that a JSON path of "
    "at most 128 characters names, and an attempt offers 1 to 100"
)
CAPTURES = {
    "a 500": ('  probes: { count: "GET /status/500" }\n', None, UNREAD),
    "not JSON": ('  probes: { count: "GET /page/shop" }\n', None, UNREAD),
    "over 64 KiB": (COUNT, b'{"count": "' + b"x" * 70_000 + b'"}', UNREAD),
    "101 values": (COUNT, json.dumps(list(range(101))).encode(), NO_OPTION.format(101)),
    "no nameable key": (COUNT, b'{"a b": 1}', NO_OPTION.format(0)),
    "only a long key": (
        COUNT,
        json.dumps({"k" * 200: 2}).encode(),
        NO_OPTION.format(0),
    ),
}


@pytest.mark.parametrize(
    ("declared", "body", "why"), CAPTURES.values(), ids=CAPTURES.keys()
)
def test_a_probe_that_offers_no_option_stops_before_the_browser(
    app: App, tmp_path: Path, declared: str, body: bytes | None, why: str
) -> None:
    if body is not None:
        app.probe_bodies["count"] = [body]
    client = Scripted(planned(COUNTED))

    run = probed(app, tmp_path, client, declared)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert written(run)["reasons"] == [why, "a1: never bound"]
    assert len(client.requests) == 1
    assert files(run) == ["costs.jsonl", "explore.json", "plan.json"]
    # The probe alone was read: the browser never opened.
    assert app.paths() == [declared.split("GET ", 1)[1].split('"', 1)[0]]


def test_a_probe_that_never_answers_stops_within_resolve_seconds(
    app: App, tmp_path: Path
) -> None:
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    began = time.monotonic()

    run = probed(
        app,
        tmp_path,
        Scripted(planned(COUNTED)),
        '  probes: { count: "GET /held/never" }\n',
        config=config,
    )
    took = time.monotonic() - began
    app.release("never")

    assert written(run)["reasons"] == [UNREAD, "a1: never bound"]
    # Bounded by resolve_seconds, not by a stable read's 10 s.
    assert 1 <= took < probes.STABLE_SECONDS


def test_an_unreachable_origin_met_at_the_capture_is_exit_13(
    app: App, tmp_path: Path
) -> None:
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]

    run = probed(
        app,
        tmp_path,
        Scripted(planned(COUNTED)),
        COUNT,
        start=f"http://127.0.0.1:{port}",
    )

    assert (run.outcome, run.exit_code) == ("unreachable", 13)
    assert written(run)["reasons"] == [
        "probe count offers no option: the probe request could not reach its allowed origin",
        "a1: never bound",
    ]


def test_a_block_during_a_probe_check_is_exit_6(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gates: list[EgressGate] = []

    def recording(policy: EgressPolicy) -> EgressGate:
        gates.append(EgressGate(policy))
        return gates[-1]

    stable = probes.read_stable

    async def blocked(*args: Any) -> probes.JsonValue:
        gates[0].record_refusal(
            "analytics.example.test", 80, "host", "fake: not allowed"
        )
        return await stable(*args)

    monkeypatch.setattr(explore, "EgressGate", recording)
    monkeypatch.setattr(probes, "read_stable", blocked)
    client = Scripted(planned(COUNTED), check("o1"), FINISH)

    run = probed(app, tmp_path, client, COUNT)

    assert (run.outcome, run.exit_code) == ("egress_blocked", 6)
    assert run.record is not None
    egress = json.loads((run.record / "egress.json").read_text())
    assert egress["refused"] == [{"host": "analytics.example.test", "port": 80}]
    assert len(client.requests) == 2


def test_a_baseline_beside_a_probe_equals_check_exits_5_naming_only_it(
    app: App, tmp_path: Path
) -> None:
    baseline = {"check": "probe_equals_baseline", "probe": "count"}
    client = Scripted(planned(plan_of(probe_check(), baseline)))

    run = probed(app, tmp_path, client, COUNT)

    assert (run.outcome, run.exit_code) == ("spec_error", 5)
    assert run.document["reasons"] == [
        "the plan has checks exploring can't keep",
        (
            "a2 (expect[0], probe_equals_baseline): exploring can't compile a "
            "probe_equals_baseline check yet"
        ),
    ]
    assert (len(client.requests), app.probe_reads) == (1, {})


def test_a_plan_without_probe_checks_reads_no_probe(app: App, tmp_path: Path) -> None:
    url = {"check": "url_matches", "pattern": "/page/shop$"}
    client = Scripted(planned(plan_of(url)), check(None), FINISH)

    run = probed(app, tmp_path, client, COUNT)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert app.probe_reads == {}
    assert blocks(client, "probes") == [None, None]


def test_admission_refuses_a_probe_whose_query_spells_a_bound_value(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "fake-token-1234"
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)
    account = "  account: { email: reader@example.test, password: { secret: TEST_PASSWORD } }\n"
    declared = f'  probes: {{ count: "GET /probe/count?token={value}" }}\n'
    client = Scripted(planned(COUNTED))

    with pytest.raises(SpecError) as refused:
        probed(app, tmp_path, client, declared + account, config=SECRETS)

    path = str(tmp_path / "qa" / "shop.spec.md")
    assert list(refused.value.problems) == [
        f"{path}: preconditions.probes: a bound value's spelling is in it"
    ]
    assert value not in str(refused.value)
    assert not (tmp_path / "qa" / ".aqa").exists()
    assert (client.requests, app.paths()) == ([], [])


# Offers o1 for the count, which isn't 2, and o2 for a decoy key that holds 2.
DECOY = b'{"count": 3, "other": 2}'


def test_a_failed_probe_check_is_refused_another_option_and_publishes_nothing(
    app: App, tmp_path: Path
) -> None:
    app.probe_bodies["count"] = [DECOY]
    client = Scripted(planned(COUNTED), check("o1"), check("o2"), FINISH)
    budget = ProjectConfig.model_validate({"budgets": {"actions_per_attempt": 3}})

    run = probed(app, tmp_path, client, COUNT, config=budget)

    assert blocks(client, "results") == [
        "",
        "a1 is bound, and on this page it is failed",
        (
            "assert_check did not run: a1 didn't pass on o1, so it keeps o1 for the "
            "rest of this attempt"
        ),
    ]
    assert (run.outcome, run.exit_code, written(run)["reasons"]) == (
        "gave_up",
        3,
        ["the attempt took all 3 of its turns", "a1: failed"],
    )
    assert not (tmp_path / "qa" / ".compiled").exists()
    # The capture, o1's binding's two reads, finish's two; o2's none.
    assert app.probe_reads == {"count": 5}


def test_an_option_bound_first_publishes_its_path(app: App, tmp_path: Path) -> None:
    app.probe_bodies["count"] = [DECOY]
    client = Scripted(planned(COUNTED), check("o2"), FINISH)

    run = probed(app, tmp_path, client, RESET + COUNT)

    assert (run.outcome, run.exit_code, written(run)["published"]) == (
        "compiled",
        0,
        "confirmed",
    )
    assert [
        each.model_dump().get("json_path") for each in published(tmp_path).assertions
    ] == ["$.other"]
