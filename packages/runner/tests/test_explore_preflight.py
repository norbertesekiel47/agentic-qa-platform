"""Exploring a spec from its preflight (#53 P15c; ADR-0024's #53 P15c
amendment): `explore.explore` binds the spec's test secrets and admits only
what the run can keep, makes the run record and its `attempt-1` part, plans
with every request checked before it is sent, refuses a plan exploring
can't keep, writes `plan.json`, then runs the attempt's phase. Real Chromium
against the executor's fixture app, with a scripted model."""

import asyncio
import errno
import json
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan, plan_hash
from aqa_core.project import SpecError, load_compiled, load_spec
from aqa_core.spec import Spec
from aqa_runner import coverage_plan
from aqa_runner import explore as explore_module
from aqa_runner.bound_secrets import (
    MissingSecretError,
    SecretLoggedError,
    bound_secrets,
)
from aqa_runner.chat_client import Reply
from aqa_runner.explore import Explorable, Explored, Run, explore
from aqa_runner.explore_result import ExploreEnd, present
from aqa_runner.model_router import ModelCallError, ModelRouter
from aqa_runner.navigator_tools import Attempt
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RunRecord
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from playwright.async_api import async_playwright

from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    NO_RESPONSE,
    PLAN,
    REFUSED,
    STATUS,
    USAGE,
    Move,
    Scripted,
)
from packages.runner.tests.navigator_fixtures import fail_fsync
from packages.runner.tests.secret_fixtures import FAKE_VALUE
from packages.runner.tests.test_explore_graph import (
    ACCOUNT,
    ASSERT,
    FINISH,
    SECRETS,
    UNWRITTEN,
    configured,
    deadlines,
    expiring,
    files,
    written,
)

# The shop spec, as its file holds it between its fences.
SHOP = (
    "id: shop\n"
    "goal: A shopper saves their name.\n"
    "preconditions:\n"
    "  start_url: /page/shop\n"
    "expect:\n"
    "  - The shop says the name was saved\n"
)


def planned(plan: CoveragePlan | None = PLAN) -> Reply:
    """The plan call's answer: `plan`, parsed, at the usual usage."""
    return Reply(
        message=AIMessage(content="the plan"),
        usage=USAGE,
        refused=False,
        complete=True,
        parsed=plan,
    )


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def spec_at(root: Path, config: ProjectConfig, frontmatter: str, name: str) -> Spec:
    """The spec `name` under the spec root `root`, with `frontmatter`, read
    as the loader reads it."""
    path = root / f"{name}.spec.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n")
    return load_spec(path, config)


def exploring(
    app: App,
    tmp_path: Path,
    client: Scripted,
    *,
    config: ProjectConfig | None = None,
    frontmatter: str = SHOP,
    name: str = "shop",
) -> Explored:
    """How `explore` ended a run of the spec `name` (the shop's unless
    `frontmatter` says otherwise) under the spec root `tmp_path / "qa"`,
    from the app's origin, with `client` as its model."""
    config = config or ProjectConfig()
    root = tmp_path / "qa"
    spec = spec_at(root, config, frontmatter, name)

    async def scenario() -> Explored:
        # No explore test may hang the suite.
        async with async_playwright() as playwright, asyncio.timeout(120):
            return await explore(
                spec,
                config,
                root,
                app.origin,
                router=ModelRouter.from_config(config, client),
                playwright=playwright,
            )

    return asyncio.run(scenario())


def plan_json(run: Explored) -> dict[str, Any]:
    """The run's `plan.json`, less its run ID, which the run made."""
    assert run.record is not None
    document: dict[str, Any] = json.loads((run.record / "plan.json").read_text())
    assert document.pop("run_id") == run.record.name
    return document


def test_a_scripted_explore_of_the_shop_spec_compiles_in_memory(
    app: App, tmp_path: Path
) -> None:
    configured(tmp_path / "qa")
    client = Scripted(planned(), FILL, CLICK, ASSERT, FINISH)

    run = exploring(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("compiled", 0)
    assert run.document["reasons"] == []
    assert run.document["published"] == "unconfirmed"
    assert plan_json(run) == {
        "spec_id": "shop",
        "spec_hash": shop(tmp_path).spec_hash,
        "outcome": "planned",
        "reasons": [],
        "plan": PLAN.model_dump(mode="json", exclude_none=True),
        "plan_hash": plan_hash(PLAN),
        "costs": 1,
    }
    assert files(run) == [
        "attempt-1/steps.jsonl",
        "costs.jsonl",
        "explore.json",
        "plan.json",
        "turns/1.json",
        "turns/2.json",
        "turns/3.json",
        "turns/4.json",
    ]
    assert run.record is not None
    assert len((run.record / "costs.jsonl").read_text().splitlines()) == 5
    # The plan call asked for the plan's schema with no tools; each turn
    # offered tools with no schema.
    assert [(bool(tools), schema) for tools, schema in client.offered] == [
        (False, CoveragePlan),
        *[(True, None)] * 4,
    ]
    script = run.script
    assert script is not None
    assert (script.spec_id, script.confirmed) == ("shop", False)
    assert script.compiled_by.models == {"navigator": "claude-sonnet-5-5"}
    assert {
        name: [locator.model_dump(exclude_none=True) for locator in target.locators]
        for name, target in script.targets.items()
    } == {
        "t1": [
            {"role": "textbox", "name": "Name"},
            {"label": "Name"},
            {"css": "#name"},
            {"css": "input"},
        ],
        "t2": [{"role": "button", "name": "Save"}, {"css": "#save"}, {"css": "button"}],
        "t3": [
            {"testid": "status"},
            {"css": "#status"},
            {"role": "paragraph"},
            {"css": "p"},
        ],
    }
    assert [each.model_dump(exclude_none=True) for each in script.assertions] == [
        {
            "id": "a1",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "t3",
            "text": "Saved Ada",
        }
    ]
    assert run.cost == Decimal("0.015")
    compiled = tmp_path / "qa" / ".compiled"
    assert sorted(path.name for path in compiled.iterdir()) == ["shop.json"]
    assert load_compiled(compiled / "shop.json", ProjectConfig()) == script


def unplanned(
    spec: Spec, outcome: str, reasons: list[str], costs: int
) -> dict[str, Any]:
    """What `plan.json` says, past its run ID, of a run of `spec` that
    planning stopped with no plan."""
    return {
        "spec_id": "shop",
        "spec_hash": spec.spec_hash,
        "outcome": outcome,
        "reasons": reasons,
        "plan": None,
        "plan_hash": None,
        "costs": costs,
    }


def shop(tmp_path: Path) -> Spec:
    """The shop spec the run explored, read again."""
    return load_spec(tmp_path / "qa" / "shop.spec.md", ProjectConfig())


def test_a_refused_plan_gives_up_before_the_proxy(app: App, tmp_path: Path) -> None:
    run = exploring(app, tmp_path, Scripted(REFUSED))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert plan_json(run) == unplanned(
        shop(tmp_path), "gave_up", ["the model refused to write the plan"], 1
    )
    assert run.record is not None
    assert run.document == {**plan_json(run), "run_id": run.record.name}
    assert files(run) == ["costs.jsonl", "plan.json"]
    assert run.cost == Decimal("0.003")
    assert app.paths() == []


class Unkept(NamedTuple):
    """A plan exploring can't keep: the plan, the spec's frontmatter and the
    project config it was written for, and the reasons `plan.json` gives."""

    plan: CoveragePlan
    frontmatter: str
    config: ProjectConfig
    reasons: list[str]


def plan_of(expectation: dict[str, object]) -> CoveragePlan:
    """A plan of the shop's one expectation, as `expectation` says."""
    return CoveragePlan.model_validate(
        {"expectations": [{"expect_index": 0} | expectation], "requires": []}
    )


UNKEPT = {
    "uncovered": Unkept(
        plan_of(
            {
                "subject": STATUS,
                "claim": "says the name Ada was saved",
                "unsupported": {"reason": "only a person can tell"},
            }
        ),
        SHOP,
        ProjectConfig(),
        [
            "an expectation has no establishing check",
            (
                'expect[0] "The shop says the name was saved": no M1 check can '
                "establish it: only a person can tell"
            ),
        ],
    ),
    "a baseline probe check": Unkept(
        plan_of(
            {
                "subject": "the shop's saved count",
                "claim": "one more shop was saved",
                "checks": [{"check": "probe_equals_baseline", "probe": "count"}],
            }
        ),
        SHOP.replace("expect:", "  probes: { count: GET /probe/count }\nexpect:"),
        ProjectConfig(),
        [
            "the plan has checks exploring can't keep",
            (
                "a1 (expect[0], probe_equals_baseline): exploring can't compile a "
                "probe_equals_baseline check yet"
            ),
        ],
    ),
    "a subject contract": Unkept(
        plan_of(
            {
                "subject": STATUS,
                "claim": "says the name Ada was saved",
                "checks": [{"check": "text_visible", "text": "Saved Ada"}],
            }
        ),
        SHOP,
        ProjectConfig.model_validate(
            {"subjects": [{"spec": "shop", "expect": 0, "region": "main", "part": "p"}]}
        ),
        [
            "the plan contradicts a subject contract",
            (
                "subjects expect 0: the plan checks expectation 0 with no element, "
                "so no target can carry its subject contract"
            ),
        ],
    ),
}


@pytest.mark.parametrize("case", UNKEPT.values(), ids=UNKEPT.keys())
def test_a_plan_exploring_cant_keep_exits_5_naming_it_before_the_proxy(
    app: App, tmp_path: Path, case: Unkept
) -> None:
    run = exploring(
        app,
        tmp_path,
        Scripted(planned(case.plan)),
        config=case.config,
        frontmatter=case.frontmatter,
    )

    assert (run.outcome, run.exit_code) == ("spec_error", 5)
    spec = load_spec(tmp_path / "qa" / "shop.spec.md", case.config)
    assert plan_json(run) == unplanned(spec, "spec_error", case.reasons, 1) | {
        "plan": case.plan.model_dump(mode="json", exclude_none=True),
        "plan_hash": plan_hash(case.plan),
    }
    assert files(run) == ["costs.jsonl", "plan.json"]
    assert app.paths() == []


@pytest.mark.parametrize("budget", [0.002, 0.003], ids=["past it", "all of it"])
def test_a_plan_over_budget_gives_up_with_exit_3_and_its_cost_on_disk(
    app: App, tmp_path: Path, budget: float
) -> None:
    # The plan's response costs $0.003.
    config = ProjectConfig.model_validate({"budgets": {"model_usd": budget}})

    run = exploring(app, tmp_path, Scripted(planned()), config=config)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert plan_json(run) == unplanned(
        shop(tmp_path), "gave_up", ["the spend budget ran out with the plan"], 1
    ) | {
        "plan": PLAN.model_dump(mode="json", exclude_none=True),
        "plan_hash": plan_hash(PLAN),
    }
    assert files(run) == ["costs.jsonl", "plan.json"]
    assert app.paths() == []


NO_ANSWER = "the model gave no response (APIConnectionError)"
# A plan of the shop's one expectation under the wrong index: it doesn't
# fit, so it is asked for once more.
SAVED = PLAN.expectations[0].model_dump(mode="json")
SHORT = plan_of(SAVED | {"expect_index": 1})


@pytest.mark.parametrize(
    ("moves", "costs"),
    [((NO_RESPONSE,), 0), ((planned(SHORT), NO_RESPONSE), 1)],
    ids=["first call", "after a billed answer"],
)
def test_no_response_while_planning_is_exit_11(
    app: App, tmp_path: Path, moves: tuple[Move, ...], costs: int
) -> None:
    run = exploring(app, tmp_path, Scripted(*moves))

    assert (run.outcome, run.exit_code) == ("no_response", 11)
    assert plan_json(run) == unplanned(
        shop(tmp_path), "no_response", [NO_ANSWER], costs
    )
    assert files(run) == (["costs.jsonl", "plan.json"] if costs else ["plan.json"])


def test_no_response_while_navigating_is_exit_11(app: App, tmp_path: Path) -> None:
    run = exploring(app, tmp_path, Scripted(planned(), NO_RESPONSE))

    assert (run.outcome, run.exit_code) == ("no_response", 11)
    assert written(run)["reasons"] == [NO_ANSWER, "a1: never bound"]
    assert plan_json(run)["outcome"] == "planned"


def test_the_minutes_budget_running_out_while_planning_gives_up_before_the_proxy(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = deadlines(monkeypatch)

    run = exploring(app, tmp_path, Scripted(expiring(limits)))

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    assert plan_json(run) == unplanned(
        shop(tmp_path), "gave_up", ["the minutes budget ran out"], 0
    )
    assert files(run) == ["plan.json"]
    assert app.paths() == []


def test_a_plan_cost_line_not_on_disk_is_exit_15(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unwritten(_messages: Sequence[BaseMessage]) -> Reply:
        # The cost line's own fsync, once the response is priced.
        fail_fsync(monkeypatch)
        return planned()

    run = exploring(app, tmp_path, Scripted(unwritten))

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert plan_json(run) == unplanned(
        shop(tmp_path),
        "record_error",
        [
            (
                "a priced model call's cost line isn't on disk, so the run's cost "
                "is incomplete"
            )
        ],
        1,
    )
    assert app.paths() == []


def test_a_plan_json_that_cannot_be_written_exits_15_before_the_proxy(
    app: App, tmp_path: Path
) -> None:
    def taken(_messages: Sequence[BaseMessage]) -> Reply:
        # The run record is made by now: its plan.json is taken first.
        (record,) = (tmp_path / "qa" / ".aqa" / "runs").iterdir()
        (record / "plan.json").write_text("taken\n")
        return planned()

    run = exploring(app, tmp_path, Scripted(taken))

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.record is not None
    assert run.document == {
        "run_id": run.record.name,
        **unplanned(shop(tmp_path), "record_error", [UNWRITTEN], 1),
        "plan": PLAN.model_dump(mode="json", exclude_none=True),
        "plan_hash": plan_hash(PLAN),
    }
    assert (run.record / "plan.json").read_text() == "taken\n"
    assert app.paths() == []


# The shop spec, signing in with TEST_PASSWORD, which SECRETS binds.
SIGNED_IN = SHOP.replace("expect:", f"{ACCOUNT}expect:")
UNSENT = "a request still spelled a bound value after its scans, so it wasn't sent"


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST_PASSWORD's fake value, for a spec that references it."""
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)


@pytest.mark.usefixtures("secret")
def test_a_correction_request_that_spells_a_bound_value_is_never_sent(
    app: App, tmp_path: Path
) -> None:
    # A plan under the wrong index, whose check reads the bound value: its
    # correction quotes the plan.
    checks = [{"check": "text_visible", "text": FAKE_VALUE}]
    quoting = plan_of(SAVED | {"expect_index": 1, "checks": checks})
    client = Scripted(planned(quoting), planned())

    run = exploring(app, tmp_path, client, config=SECRETS, frontmatter=SIGNED_IN)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    spec = load_spec(tmp_path / "qa" / "shop.spec.md", SECRETS)
    assert plan_json(run) == unplanned(spec, "gave_up", [UNSENT], 1)
    assert len(client.requests) == 1
    assert files(run) == ["costs.jsonl", "plan.json"]
    assert app.paths() == []


@pytest.mark.usefixtures("secret")
def test_a_plan_request_that_still_spells_a_bound_value_is_never_sent(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = coverage_plan.plan_request

    def spelling(spec: Spec) -> list[BaseMessage]:
        # A value a join at a marker's edge re-forms, past every scan.
        return [*request(spec), HumanMessage(content=FAKE_VALUE)]

    monkeypatch.setattr(coverage_plan, "plan_request", spelling)
    client = Scripted(planned())

    run = exploring(app, tmp_path, client, config=SECRETS, frontmatter=SIGNED_IN)

    assert (run.outcome, run.exit_code) == ("gave_up", 3)
    spec = load_spec(tmp_path / "qa" / "shop.spec.md", SECRETS)
    assert plan_json(run) == unplanned(spec, "gave_up", [UNSENT], 0)
    assert client.requests == []
    assert files(run) == ["plan.json"]


# A bound value that fits a URL, a name and a selector.
PLAIN = "fake-token-1234"
UNSCANNABLE = (
    "test secrets: a marker, [SECRET:<name>], spells or completes a bound value, "
    "so no scan could take it out: rename the secret or change its value"
)
SPELLED = "a bound value's spelling is in it"


def bound(field: object = "password", origin: str = "start") -> ProjectConfig:
    """A project binding TEST_PASSWORD to `field` on `origin`."""
    secret = {"origins": [origin], "field": field}
    return ProjectConfig.model_validate({"secrets": {"TEST_PASSWORD": secret}})


def subject(**contract: str) -> ProjectConfig:
    """SECRETS, with the shop's expectation 0 under `contract`."""
    rows = [{"spec": "shop", "expect": 0} | contract]
    return SECRETS.model_copy(
        update={"subjects": ProjectConfig.model_validate({"subjects": rows}).subjects}
    )


class Admission(NamedTuple):
    """A spec the run can't admit: the bound value, the spec's file name and
    frontmatter, the project config, and the one problem, SPEC standing for
    the spec's path."""

    value: str
    name: str
    frontmatter: str
    config: ProjectConfig
    problem: str


def signed_in(old: str, new: str) -> str:
    """SIGNED_IN, with `old` replaced by `new`."""
    assert old in SIGNED_IN
    return SIGNED_IN.replace(old, new)


def place(where: str, old: str, new: str, value: str = PLAIN) -> Admission:
    """SIGNED_IN with `old` replaced by `new`, refused at the place `where`."""
    name = value if where == "id" else "shop"
    problem = f"SPEC: {where}: {SPELLED}"
    return Admission(value, name, signed_in(old, new), SECRETS, problem)


EXPECT = "expect:"
CONTRACT = (
    "subjects (shop, expect 0): a bound value's spelling is in its region or part"
)
REFUSED_PLACES = {
    "goal": place("goal", "their name.", f"{PLAIN}."),
    "steps": place("steps", EXPECT, f"steps:\n  - Type {PLAIN}\n{EXPECT}"),
    "expect": place("expect", "the name was", f"{PLAIN} was"),
    "the start path's query": place(
        "preconditions.start_url", "/page/shop", f"/page/shop?token={PLAIN}"
    ),
    "a probe's path": place(
        "preconditions.probes", EXPECT, f"  probes: {{ c: GET /p/{PLAIN} }}\n{EXPECT}"
    ),
    "a probe's name": place(
        "preconditions.probes", EXPECT, f"  probes: {{ {PLAIN}: GET /p }}\n{EXPECT}"
    ),
    "allowed_origins": place(
        "allowed_origins", EXPECT, f"allowed_origins: [http://{PLAIN}.test]\n{EXPECT}"
    ),
    "id": place("id", "id: shop", f"id: {PLAIN}"),
    "browser": place(
        "browser",
        EXPECT,
        f"browser: {{ timezone: Europe/Berlin }}\n{EXPECT}",
        "Europe/Berlin",
    ),
    "invariants": place(
        "invariants", EXPECT, "invariants: { disable: [http_5xx] }\nexpect:", "http_5xx"
    ),
    "the account's email": Admission(
        PLAIN,
        "shop",
        signed_in("reader@example.test", f"{PLAIN}@example.test"),
        SECRETS,
        (
            "SPEC: preconditions.account.email: it holds the value of a bound "
            "test secret, which never reaches the model: write { secret: NAME }"
        ),
    ),
    "a password written out": Admission(
        PLAIN,
        "shop",
        signed_in("{ secret: TEST_PASSWORD }", "hunter2-written-out"),
        ProjectConfig(),
        (
            "SPEC: preconditions.account.password: write { secret: NAME }, never "
            "the password itself: exploring shows the account to the model"
        ),
    ),
    "a contract's region": Admission(
        PLAIN, "shop", SIGNED_IN, subject(region=f"div#{PLAIN}", part="p"), CONTRACT
    ),
    "a contract's part": Admission(
        PLAIN, "shop", SIGNED_IN, subject(region="main", part=f"p.{PLAIN}"), CONTRACT
    ),
    "a field's name": Admission(
        PLAIN,
        "shop",
        SIGNED_IN,
        bound({"role": "textbox", "name": f"{PLAIN} field"}),
        "test secret TEST_PASSWORD: a bound value's spelling is in its field's name",
    ),
    "a marker": Admission("TEST_PASSWORD", "shop", SIGNED_IN, SECRETS, UNSCANNABLE),
    # The field's line would print the secret's name, which is the value.
    "a secret named as its value": Admission(
        "TEST_PASSWORD",
        "shop",
        SIGNED_IN,
        bound({"role": "textbox", "name": "TEST_PASSWORD"}),
        UNSCANNABLE,
    ),
    # The id's scan leaves "[SECRET:TEST_PASSWORD]zzz9", whose "]zzz9" is
    # the value again.
    "a value a marker's edge completes": Admission(
        "]zzz9",
        "]zzz9zzz9",
        signed_in("id: shop", "id: ']zzz9zzz9'"),
        SECRETS,
        UNSCANNABLE,
    ),
}


@pytest.mark.parametrize("case", REFUSED_PLACES.values(), ids=REFUSED_PLACES.keys())
def test_admission_refuses_each_place_a_bound_value_would_reach(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Admission
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", case.value)
    client = Scripted(planned())

    with pytest.raises(SpecError) as refused:
        exploring(
            app,
            tmp_path,
            client,
            config=case.config,
            frontmatter=case.frontmatter,
            name=case.name,
        )

    # The spec's path names the place, scanned like every refusal.
    path = str(tmp_path / "qa" / f"{case.name}.spec.md")
    shown = path.replace(case.value, "[SECRET:TEST_PASSWORD]")
    assert list(refused.value.problems) == [case.problem.replace("SPEC", shown)]
    assert case.value not in str(refused.value)
    assert not (tmp_path / "qa" / ".aqa").exists()
    assert client.requests == []
    assert app.paths() == []


class Unbound(NamedTuple):
    """A run whose secrets can't be bound: the environment, the project
    config, and what it raises, with its message, SPEC standing for the
    spec's path and ORIGIN for the start origin."""

    environment: dict[str, str]
    config: ProjectConfig
    error: type[Exception]
    message: str


UNBOUND = {
    "a missing value": Unbound(
        {},
        SECRETS,
        MissingSecretError,
        (
            "AQA_SECRET_TEST_PASSWORD is not set: set it to the value of test "
            "secret TEST_PASSWORD, which SPEC references at "
            "preconditions.account.password"
        ),
    ),
    "protocol logging": Unbound(
        {"AQA_SECRET_TEST_PASSWORD": FAKE_VALUE, "DEBUGP": "1"},
        SECRETS,
        SecretLoggedError,
        (
            "DEBUGP is set: Playwright would log every protocol message, a test "
            "secret's value among them; unset it to run with test secrets"
        ),
    ),
    "a binding the run can't allow": Unbound(
        {"AQA_SECRET_TEST_PASSWORD": FAKE_VALUE},
        bound(origin="http://elsewhere.example.test"),
        SpecError,
        (
            "SPEC: preconditions.account.password: secret TEST_PASSWORD is bound "
            "to http://elsewhere.example.test, which is not one of this run's "
            "allowed origins: ORIGIN"
        ),
    ),
}


@pytest.mark.parametrize("case", UNBOUND.values(), ids=UNBOUND.keys())
def test_spec_and_secret_errors_raise_before_the_record_or_any_call(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Unbound
) -> None:
    monkeypatch.delenv("AQA_SECRET_TEST_PASSWORD", raising=False)
    for name, value in case.environment.items():
        monkeypatch.setenv(name, value)
    client = Scripted(planned())

    with pytest.raises(case.error) as raised:
        exploring(app, tmp_path, client, config=case.config, frontmatter=SIGNED_IN)

    path = tmp_path / "qa" / "shop.spec.md"
    assert str(raised.value) == case.message.replace("SPEC", str(path)).replace(
        "ORIGIN", app.origin
    )
    assert not (tmp_path / "qa" / ".aqa").exists()
    assert client.requests == []
    assert app.paths() == []


UNMADE = "the run record could not be created; nothing was planned"


def test_a_run_record_that_cannot_be_made_exits_15_before_planning(
    app: App, tmp_path: Path
) -> None:
    # Where `.aqa` goes, a file: its directory can't be made.
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / ".aqa").write_text("not a directory\n")
    client = Scripted(planned())

    run = exploring(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.document == {"outcome": "record_error", "reasons": [UNMADE]}
    assert (run.record, run.cost) == (None, Decimal(0))
    assert client.requests == []


@pytest.mark.parametrize("value", ["", "]zzz9"], ids=["unbound", "completed"])
def test_a_link_at_aqa_is_a_spec_error_before_planning(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Bound, the record's place is under "]zzz9zzz9", which its scan leaves
    # as "[SECRET:TEST_PASSWORD]zzz9": the value again.
    root = tmp_path / ("]zzz9zzz9" if value else "") / "qa"
    root.mkdir(parents=True)
    (root / ".aqa").symlink_to(tmp_path)
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value or FAKE_VALUE)
    client = Scripted(planned())

    with pytest.raises(SpecError) as refused:
        exploring(app, root.parent, client, config=SECRETS, frontmatter=SIGNED_IN)

    plain = f"{root / '.aqa'}: a link, and a run record stays in {root}"
    assert list(refused.value.problems) == [UNSCANNABLE if value else plain]
    assert client.requests == []


@pytest.mark.parametrize(
    "failure",
    [OSError(errno.ENOSPC, "fake: no space left"), "a link"],
    ids=["an OSError", "a link in its place"],
)
def test_an_attempt_part_that_cannot_be_made_exits_15_before_planning(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: OSError | str
) -> None:
    part = RunRecord.part

    def failing(self: RunRecord, name: str) -> RunRecord:
        if isinstance(failure, OSError):
            raise failure
        (self.path / name).symlink_to(tmp_path)
        return part(self, name)

    monkeypatch.setattr(RunRecord, "part", failing)
    client = Scripted(planned())

    run = exploring(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.document == {"outcome": "record_error", "reasons": [UNMADE]}
    assert run.record is not None
    assert run.record.parent == tmp_path / "qa" / ".aqa" / "runs"
    assert client.requests == []
    assert app.paths() == []


@pytest.mark.parametrize("made", ["of another record", "with another redactor"])
def test_an_attempt_part_of_another_record_is_refused_before_planning(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, made: str
) -> None:
    part = RunRecord.part

    def foreign(self: RunRecord, name: str) -> RunRecord:
        if made == "with another redactor":
            return part(self, name).redacting(Redactor([]))
        # A part scanning with the run's redactor, made from another record.
        (tmp_path / "other").mkdir(exist_ok=True)
        other = RunRecord.create(tmp_path / "other").redacting(self.redactor)
        return part(other, name)

    monkeypatch.setattr(RunRecord, "part", foreign)
    client = Scripted(planned())

    with pytest.raises(
        ValueError,
        match=(
            r"^the run's attempt-1 must be a part made from its record, scanning "
            r"with the run's redactor$"
        ),
    ):
        exploring(app, tmp_path, client)

    assert client.requests == []
    # Nothing was written: no plan.json, no cost line.
    assert [path for path in made_record(tmp_path).rglob("*") if path.is_file()] == []


@pytest.mark.usefixtures("secret")
def test_the_run_scans_with_one_redactor_covering_every_referenced_secret(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_EMAIL", "fake-reader@example.test")
    config = ProjectConfig.model_validate(
        {
            "secrets": {
                "TEST_PASSWORD": {"origins": ["start"], "field": "password"},
                "TEST_EMAIL": {
                    "origins": ["start"],
                    "field": {"role": "textbox", "name": "Email"},
                },
            }
        }
    )
    frontmatter = signed_in("reader@example.test", "{ secret: TEST_EMAIL }")
    runs: list[Run] = []
    attempt = explore_module.run_attempt

    async def attempting(run: Run, explorable: Explorable) -> Explored:
        runs.append(run)
        return await attempt(run, explorable)

    attempts: list[Attempt] = []

    def attempted(*args: Any, **kwargs: Any) -> Attempt:
        attempts.append(Attempt(*args, **kwargs))
        return attempts[-1]

    monkeypatch.setattr(explore_module, "run_attempt", attempting)
    monkeypatch.setattr(explore_module, "Attempt", attempted)

    explored = exploring(
        app,
        tmp_path,
        Scripted(planned(), REFUSED),
        config=config,
        frontmatter=frontmatter,
    )

    (run,) = runs
    (made,) = attempts
    assert run.record.redactor is run.redactor
    assert run.attempt.redactor is run.redactor
    assert made.session.redactor is run.redactor
    spec = load_spec(tmp_path / "qa" / "shop.spec.md", config)
    bound = bound_secrets(spec, app.origin)
    assert sorted(bound) == ["TEST_EMAIL", "TEST_PASSWORD"]
    assert all(run.redactor.covers(secret) for secret in bound.values())
    # Neither was filled, and the run is still withheld.
    assert written(explored)["withheld"] is True
    assert written(explored)["reasons"] == [
        "the model refused its turn",
        "a1: never bound",
    ]


UNSHOWN = "the document still spelled a bound value, so it isn't shown"


@pytest.mark.usefixtures("secret")
def test_a_plan_that_spells_a_bound_value_is_never_shown(
    app: App, tmp_path: Path
) -> None:
    # The model wrote the value it was never sent: plan.json refuses it.
    check = {"check": "text_in_target", "target_meaning": STATUS, "text": FAKE_VALUE}
    spelling = plan_of(SAVED | {"checks": [check]})

    run = exploring(
        app,
        tmp_path,
        Scripted(planned(spelling)),
        config=SECRETS,
        frontmatter=SIGNED_IN,
    )

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.document == {"outcome": "record_error", "reasons": [UNSHOWN]}
    assert files(run) == ["costs.jsonl"]
    assert app.paths() == []


@pytest.mark.usefixtures("secret")
def test_a_run_document_that_spells_a_bound_value_is_never_shown(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leaking(end: ExploreEnd) -> dict[str, object]:
        # A document whose scan let a bound value through, as none of
        # present's may.
        document = present(end)
        reasons = document["reasons"]
        assert isinstance(reasons, list)
        return document | {"reasons": [*reasons, FAKE_VALUE]}

    monkeypatch.setattr(explore_module, "present", leaking)

    run = exploring(
        app,
        tmp_path,
        Scripted(planned(), REFUSED),
        config=SECRETS,
        frontmatter=SIGNED_IN,
    )

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert run.document == {"outcome": "record_error", "reasons": [UNSHOWN]}
    assert files(run) == [
        "attempt-1/steps.jsonl",
        "costs.jsonl",
        "plan.json",
        "turns/1.json",
    ]


def test_the_run_total_counts_each_priced_response_once(
    app: App, tmp_path: Path
) -> None:
    # A plan asked for twice, then one turn: three priced responses.
    run = exploring(app, tmp_path, Scripted(planned(SHORT), planned(), REFUSED))

    assert run.cost == Decimal("0.009")
    assert run.record is not None
    assert len((run.record / "costs.jsonl").read_text().splitlines()) == 3
    assert plan_json(run)["costs"] == 2
    assert (run.outcome, run.exit_code) == ("gave_up", 3)


def made_record(tmp_path: Path) -> Path:
    """The one run record made under the spec root."""
    (record,) = (tmp_path / "qa" / ".aqa" / "runs").iterdir()
    return record


def test_a_timeout_that_isnt_the_minutes_budgets_own_propagates_from_planning(
    app: App, tmp_path: Path
) -> None:
    with pytest.raises(TimeoutError):
        exploring(app, tmp_path, Scripted(TimeoutError()))

    # Nothing says the minutes budget ran out: no plan.json.
    assert [path.name for path in made_record(tmp_path).iterdir()] == ["attempt-1"]


def test_a_planning_failure_that_isnt_the_providers_propagates_with_its_cost_kept(
    app: App, tmp_path: Path
) -> None:
    fault = RuntimeError("fake: not the provider's")

    with pytest.raises(ModelCallError) as raised:
        exploring(app, tmp_path, Scripted(planned(SHORT), fault))

    assert raised.value.__cause__ is fault
    record = made_record(tmp_path)
    assert sorted(path.name for path in record.iterdir()) == [
        "attempt-1",
        "costs.jsonl",
    ]
    assert len((record / "costs.jsonl").read_text().splitlines()) == 1
