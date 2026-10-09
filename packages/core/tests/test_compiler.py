"""Compiling an explored path (DATA_MODEL §7, "Compilation rules"; ADR-0025's
#53 P2 amendment): each planned check becomes the assertion of its type, the
steps `finish` kept become side-effect steps, and each target carries its
meaning's subject contract, or the compile fails naming every problem."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aqa_core.browser import BrowserSettings
from aqa_core.compiled import (
    ByCss,
    ByLabel,
    ByRole,
    ByTestId,
    Click,
    CompiledBy,
    CompiledScript,
    Coverage,
    ExpectationCoverage,
    Fill,
    FillSecret,
    Navigate,
    NetworkNone,
    NetworkSeen,
    NotVisible,
    Press,
    Reload,
    Select,
    Target,
    TextInTarget,
    TextVisible,
    UrlMatches,
    VisibleUnoccluded,
)
from aqa_core.compiler import (
    CompileError,
    ExploredPath,
    PathStep,
    Provenance,
    assertion_for,
    compile_script,
    contracts_by_meaning,
    unsupported_features,
)
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan, PlannedCheck, plan_hash
from aqa_core.project import contract_problems, load_project, parse_compiled
from aqa_core.schema import Contract
from aqa_core.spec import Spec

PILOT = Path(__file__).resolve().parents[3] / "bench" / "apps" / "conduit" / "qa"

COUNT = "the banner's favorites count"
NOTICE = "the empty-list notice under the article list"
ADD = "the add-article button in the header"
BANNER = Contract(region="div.banner", part="span.counter", leaf=True)
FOOTER = Contract(region="div.footer", part="span.counter", leaf=True)

URL = {"check": "url_matches", "pattern": "/demo"}
SHOWN = {"check": "text_visible", "text": "Demo"}
COUNTED = {"check": "text_in_target", "target_meaning": COUNT, "text": "1"}
GONE = {"check": "not_visible", "target_meaning": NOTICE}
UNCOVERED = {"check": "visible_unoccluded", "target_meaning": ADD}
NO_POST = {
    "check": "network_none",
    "method": "POST",
    "url_pattern": "/api/count",
    "status_class": "2xx",
}
GOT = {
    "check": "network_seen",
    "method": "GET",
    "url_pattern": "/api/count",
    "status_class": "2xx",
}
PROBED = {"check": "probe_equals", "probe": "count", "value": 1}
BASELINE = {"check": "probe_equals_baseline", "probe": "count"}


def expectation(index: int, *checks: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "expect_index": index,
        "subject": f"subject {index}",
        "claim": f"claim {index}",
    }
    if checks:
        entry["checks"] = list(checks)
    else:
        entry["unsupported"] = {"reason": "no M1 check reads it"}
    return entry


def plan(
    *expectations: dict[str, Any], requires: tuple[dict[str, str], ...] = ()
) -> CoveragePlan:
    return CoveragePlan.model_validate(
        {"expectations": list(expectations), "requires": list(requires)}
    )


# The demo spec's four expectations, planned with one check of each type the
# compiler compiles: a1 and a2, a3, a4 and a5, a6 and a7.
PLAN = plan(
    expectation(0, URL, SHOWN),
    expectation(1, COUNTED),
    expectation(2, GONE, UNCOVERED),
    expectation(3, NO_POST, GOT),
)


def test_assertion_for_gives_the_assertion_of_each_checks_type() -> None:
    def check(fields: dict[str, Any]) -> PlannedCheck:
        return PlannedCheck.model_validate(fields)

    assert [
        assertion_for("a1", 0, check(URL)),
        assertion_for("a2", 0, check(SHOWN)),
        assertion_for("a3", 1, check(COUNTED), target="t2"),
        assertion_for("a4", 2, check(GONE), target="t3"),
        assertion_for("a5", 2, check(UNCOVERED), target="t1"),
        assertion_for("a6", 3, check(NO_POST)),
        assertion_for("a7", 3, check(GOT)),
    ] == [
        UrlMatches(id="a1", expect_index=0, check="url_matches", pattern="/demo"),
        TextVisible(id="a2", expect_index=0, check="text_visible", text="Demo"),
        TextInTarget(
            id="a3", expect_index=1, check="text_in_target", target="t2", text="1"
        ),
        NotVisible(id="a4", expect_index=2, check="not_visible", target="t3"),
        VisibleUnoccluded(
            id="a5",
            expect_index=2,
            check="visible_unoccluded",
            target="t1",
            min_size_px=(1, 1),
            in_viewport=True,
        ),
        NetworkNone(
            id="a6",
            expect_index=3,
            check="network_none",
            method="POST",
            url_pattern="/api/count",
            status_class="2xx",
        ),
        NetworkSeen(
            id="a7",
            expect_index=3,
            check="network_seen",
            method="GET",
            url_pattern="/api/count",
            status_class="2xx",
        ),
    ]


def test_unsupported_features_name_each_probe_check_and_condition() -> None:
    probing = plan(
        expectation(0, URL),
        expectation(1, PROBED, BASELINE),
        requires=({"id": "c1", "condition": "checked after a reload"},),
    )

    assert unsupported_features(probing) == (
        "a2 (expect[1], probe_equals): exploring can't compile a probe_equals check yet",
        (
            "a3 (expect[1], probe_equals_baseline): exploring can't compile a "
            "probe_equals_baseline check yet"
        ),
        (
            'requires c1 ("checked after a reload"): exploring can\'t keep a required '
            "condition in the path yet"
        ),
    )
    assert unsupported_features(PLAN) == ()


def test_contracts_by_meaning_gives_each_planned_meaning_its_rows_contract() -> None:
    assert contracts_by_meaning(PLAN, {1: BANNER}) == {
        COUNT: BANNER,
        NOTICE: None,
        ADD: None,
    }


def refused(
    rows: dict[int, Contract], *expectations: dict[str, Any]
) -> tuple[str, ...]:
    with pytest.raises(CompileError) as caught:
        contracts_by_meaning(plan(*expectations), rows)
    return caught.value.problems


def test_contracts_by_meaning_refuses_a_row_checked_with_no_target() -> None:
    assert refused(
        {0: BANNER}, expectation(0, URL, SHOWN), expectation(1, COUNTED)
    ) == (
        (
            "subjects expect 0: the plan checks expectation 0 with no element, so no "
            "target can carry its subject contract"
        ),
    )
    assert refused({1: BANNER}, expectation(0, URL), expectation(1)) == (
        (
            "subjects expect 1: the plan leaves expectation 1 unsupported, so no target "
            "can carry its subject contract"
        ),
    )


def test_contracts_by_meaning_refuses_a_meaning_under_two_contracts() -> None:
    count_again = {"check": "not_visible", "target_meaning": COUNT}

    listed_and_unlisted = refused(
        {1: BANNER},
        expectation(0, URL),
        expectation(1, COUNTED),
        expectation(2, count_again),
    )
    two_rows = refused(
        {1: BANNER, 2: FOOTER},
        expectation(0, URL),
        expectation(1, COUNTED),
        expectation(2, count_again),
    )

    for problems in (listed_and_unlisted, two_rows):
        assert problems == (
            (
                f'meaning "{COUNT}" is planned under different subject contracts, for '
                "expect 1 and expect 2: one meaning has one contract or none"
            ),
        )


def test_one_contract_across_expectations_is_one_meanings_contract() -> None:
    count_again = {"check": "not_visible", "target_meaning": COUNT}

    assert contracts_by_meaning(
        plan(expectation(0, URL), expectation(1, COUNTED), expectation(2, count_again)),
        {1: BANNER, 2: BANNER},
    ) == {COUNT: BANNER}


SPEC = """\
---
id: demo
goal: A reader favorites an article from the demo page.
preconditions:
  start_url: /
  probes: { count: "GET /api/count" }
expect:
  - The page is the demo page
  - The banner's favorites count shows 1
  - The empty-list notice is gone and the add button is shown
  - Reading the count sends no write
---
"""
DECLARED = "TEST_PASSWORD"
SECRETS = "secrets: { TEST_PASSWORD: { origins: [start], field: password } }\n"
ROW = "  - { spec: demo, expect: 1, region: div.banner, part: span.counter, leaf: true }\n"
BASIS = "no positive evidence that the step changes nothing"
AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
COMMIT = "6f0c3a9d2b8e4f17a5c9d0e3b6a1f4c8d2e7b9a0"
BY = Provenance(models={"navigator": "claude-sonnet-5-5"}, price_map=COMMIT, at=AT)

# What the attempt's TargetUses gave: the count twice (clicked by its test ID,
# then its text checked), each with the banner row's contract.
COUNT_BUTTON = Target(
    semantic=COUNT, locators=(ByTestId(testid="favorites-count"),), contract=BANNER
)
COUNT_TARGET = Target(
    semantic=COUNT, locators=(ByCss(css="span.counter"),), contract=BANNER
)
NOTICE_TARGET = Target(
    semantic=NOTICE,
    locators=(ByCss(css="p.empty", scope=ByCss(css="app-article-list")),),
)
ADD_TARGET = Target(semantic=ADD, locators=(ByRole(role="link", name="New Article"),))
SEARCH = Target(
    semantic="the header's search field",
    locators=(ByRole(role="textbox", name="Search"),),
)
PASSWORD = Target(
    semantic="the sign-in form's password field", locators=(ByLabel(label="Password"),)
)
SORT = Target(
    semantic="the article list's sort menu",
    locators=(ByRole(role="combobox", name="Sort"),),
)
DETOUR = Target(
    semantic="the footer's about link", locators=(ByRole(role="link", name="About"),)
)

# The attempt's steps by its own numbers. Step 2 is a detour finish drops.
STEPS = {
    1: PathStep({"action": "navigate", "url": "/demo"}),
    2: PathStep({"action": "click"}, DETOUR),
    3: PathStep({"action": "reload"}),
    4: PathStep({"action": "click"}, COUNT_BUTTON),
    5: PathStep({"action": "fill", "value": "flaky"}, SEARCH),
    6: PathStep({"action": "fill_secret", "secret": DECLARED}, PASSWORD),
    7: PathStep({"action": "select", "option": "Newest"}, SORT),
    8: PathStep({"action": "press", "key": "Enter"}),
    9: PathStep({"action": "click"}, ADD_TARGET),
}
KEPT = (9, 1, 3, 4, 5, 6, 7, 8)
# a5 reads a copy of step 9's target: equal targets are one target.
BOUND: dict[str, Target | None] = {
    "a1": None,
    "a2": None,
    "a3": COUNT_TARGET,
    "a4": NOTICE_TARGET,
    "a5": ADD_TARGET.model_copy(),
    "a6": None,
    "a7": None,
}


def demo(tmp_path: Path, subjects: str = ROW) -> tuple[Spec, ProjectConfig]:
    root = tmp_path / "qa"
    root.mkdir()
    rows = f"subjects:\n{subjects}" if subjects else ""
    (root / "config.yaml").write_text(SECRETS + rows)
    (root / "demo.spec.md").write_text(SPEC)
    project = load_project(root)
    return project.specs["demo"], project.config


def compiled(
    tmp_path: Path,
    *,
    planned: CoveragePlan = PLAN,
    steps: dict[int, PathStep] = STEPS,
    kept: tuple[int, ...] = KEPT,
    bound: dict[str, Target | None] = BOUND,
    subjects: str = ROW,
) -> CompiledScript:
    spec, config = demo(tmp_path, subjects)
    return compile_script(spec, config, planned, ExploredPath(steps, kept, bound), BY)


def test_each_planned_check_compiles_to_the_assertion_of_its_type(
    tmp_path: Path,
) -> None:
    assert compiled(tmp_path).assertions == (
        UrlMatches(id="a1", expect_index=0, check="url_matches", pattern="/demo"),
        TextVisible(id="a2", expect_index=0, check="text_visible", text="Demo"),
        TextInTarget(
            id="a3", expect_index=1, check="text_in_target", target="t6", text="1"
        ),
        NotVisible(id="a4", expect_index=2, check="not_visible", target="t7"),
        VisibleUnoccluded(
            id="a5",
            expect_index=2,
            check="visible_unoccluded",
            target="t5",
            min_size_px=(1, 1),
            in_viewport=True,
        ),
        NetworkNone(
            id="a6",
            expect_index=3,
            check="network_none",
            method="POST",
            url_pattern="/api/count",
            status_class="2xx",
        ),
        NetworkSeen(
            id="a7",
            expect_index=3,
            check="network_seen",
            method="GET",
            url_pattern="/api/count",
            status_class="2xx",
        ),
    )


def test_every_compiled_step_is_a_side_effect_step_with_its_basis(
    tmp_path: Path,
) -> None:
    spec, config = demo(tmp_path)
    script = compile_script(spec, config, PLAN, ExploredPath(STEPS, KEPT, BOUND), BY)
    flag, basis = True, BASIS

    assert script.steps == (
        Navigate(
            seq=1,
            action="navigate",
            url="/demo",
            side_effect=flag,
            side_effect_basis=basis,
        ),
        Reload(seq=2, action="reload", side_effect=flag, side_effect_basis=basis),
        Click(
            seq=3,
            action="click",
            target="t1",
            side_effect=flag,
            side_effect_basis=basis,
        ),
        Fill(
            seq=4,
            action="fill",
            target="t2",
            value="flaky",
            side_effect=flag,
            side_effect_basis=basis,
        ),
        FillSecret(
            seq=5,
            action="fill_secret",
            target="t3",
            secret=DECLARED,
            side_effect=flag,
            side_effect_basis=basis,
        ),
        Select(
            seq=6,
            action="select",
            target="t4",
            option="Newest",
            side_effect=flag,
            side_effect_basis=basis,
        ),
        Press(
            seq=7,
            action="press",
            key="Enter",
            side_effect=flag,
            side_effect_basis=basis,
        ),
        Click(
            seq=8,
            action="click",
            target="t5",
            side_effect=flag,
            side_effect_basis=basis,
        ),
    )
    assert parse_compiled(
        script.model_dump_json(), config, source=Path("demo.json")
    ).steps == (script.steps)


def test_visible_unoccluded_compiles_in_viewport_with_a_nonzero_minimum_size(
    tmp_path: Path,
) -> None:
    unoccluded = compiled(tmp_path).assertions[4]

    assert unoccluded == VisibleUnoccluded(
        id="a5",
        expect_index=2,
        check="visible_unoccluded",
        target="t5",
        min_size_px=(1, 1),
        in_viewport=True,
    )


def test_detour_targets_are_left_out(tmp_path: Path) -> None:
    assert compiled(tmp_path).targets == {
        "t1": COUNT_BUTTON,
        "t2": SEARCH,
        "t3": PASSWORD,
        "t4": SORT,
        "t5": ADD_TARGET,
        "t6": COUNT_TARGET,
        "t7": NOTICE_TARGET,
    }


def test_equal_targets_share_one_name_and_unequal_targets_of_one_meaning_stay_apart(
    tmp_path: Path,
) -> None:
    script = compiled(tmp_path)

    assert [
        name for name, target in script.targets.items() if target.semantic == ADD
    ] == ["t5"]
    assert [
        name for name, target in script.targets.items() if target.semantic == COUNT
    ] == [
        "t1",
        "t6",
    ]


def test_compiled_by_records_the_mode_models_price_map_and_contracts(
    tmp_path: Path,
) -> None:
    assert compiled(tmp_path).compiled_by == CompiledBy(
        mode="explore",
        models={"navigator": "claude-sonnet-5-5"},
        price_map=COMMIT,
        subject_contracts=(
            "sha256:cdbce962b717b009c37965f7349af594d396f3d00e1420d91f97f40535e26cd9"
        ),
    )


def test_coverage_records_the_frozen_plan_and_its_assertions(tmp_path: Path) -> None:
    def covered(index: int, *assertions: str) -> ExpectationCoverage:
        return ExpectationCoverage(
            expect_index=index,
            subject=f"subject {index}",
            claim=f"claim {index}",
            assertions=assertions,
        )

    assert compiled(tmp_path).coverage == Coverage(
        plan_hash=plan_hash(PLAN),
        expectations=(
            covered(0, "a1", "a2"),
            covered(1, "a3"),
            covered(2, "a4", "a5"),
            covered(3, "a6", "a7"),
        ),
        requires=(),
    )


def test_a_compiled_script_reads_back_through_parse_compiled(tmp_path: Path) -> None:
    spec, config = demo(tmp_path)
    script = compile_script(spec, config, PLAN, ExploredPath(STEPS, KEPT, BOUND), BY)

    assert (
        parse_compiled(script.model_dump_json(), config, source=Path("demo.json"))
        == script
    )
    assert (script.spec_id, script.spec_hash, script.compiled_at) == (
        "demo",
        spec.spec_hash,
        AT,
    )
    assert (script.confirmed, script.browser, script.probe_baselines) == (
        False,
        BrowserSettings(),
        {},
    )


def test_each_target_carries_its_meanings_contract(tmp_path: Path) -> None:
    contracts = {
        name: target.contract for name, target in compiled(tmp_path).targets.items()
    }

    assert contracts == {
        "t1": BANNER,
        "t2": None,
        "t3": None,
        "t4": None,
        "t5": None,
        "t6": BANNER,
        "t7": None,
    }


def test_one_contract_across_expectations_compiles(tmp_path: Path) -> None:
    hidden = Target(
        semantic=COUNT,
        locators=(ByCss(css="span.counter", scope=ByCss(css="app-favorite-button")),),
        contract=BANNER,
    )
    two_rows = ROW + ROW.replace("expect: 1", "expect: 2")

    script = compiled(
        tmp_path,
        planned=plan(
            expectation(0, URL),
            expectation(1, COUNTED),
            expectation(2, {"check": "not_visible", "target_meaning": COUNT}),
            expectation(3, GOT),
        ),
        kept=(),
        bound={"a1": None, "a2": COUNT_TARGET, "a3": hidden, "a4": None},
        subjects=two_rows,
    )

    assert script.targets == {"t1": COUNT_TARGET, "t2": hidden}


def test_a_zero_step_path_without_a_baseline_check_compiles(tmp_path: Path) -> None:
    script = compiled(tmp_path, kept=())

    assert script.steps == ()
    assert script.targets == {"t1": COUNT_TARGET, "t2": NOTICE_TARGET, "t3": ADD_TARGET}


def test_assertions_and_their_targets_keep_plan_order_past_ten_checks(
    tmp_path: Path,
) -> None:
    def titled(n: int) -> Target:
        return Target(
            semantic=f"card {n}'s title", locators=(ByCss(css=f"h2.card-{n}"),)
        )

    def reads(n: int) -> dict[str, Any]:
        return {
            "check": "text_in_target",
            "target_meaning": f"card {n}'s title",
            "text": "Go",
        }

    seen = [{"check": "text_visible", "text": f"Card {n}"} for n in range(3, 10)]
    long_plan = plan(
        expectation(0, URL, reads(2), *seen),
        expectation(1, reads(10)),
        expectation(2, reads(11)),
        expectation(3, GOT),
    )
    bound: dict[str, Target | None] = {f"a{n}": None for n in range(1, 13)}
    bound |= {"a2": titled(2), "a10": titled(10), "a11": titled(11)}

    script = compiled(tmp_path, planned=long_plan, kept=(), bound=bound, subjects="")

    assert [assertion.id for assertion in script.assertions] == [
        f"a{n}" for n in range(1, 13)
    ]
    assert script.targets == {"t1": titled(2), "t2": titled(10), "t3": titled(11)}
    targeted = [a for a in script.assertions if isinstance(a, TextInTarget)]
    assert [(a.id, a.target) for a in targeted] == [
        ("a2", "t1"),
        ("a10", "t2"),
        ("a11", "t3"),
    ]


AUTHOR = Contract(region="div.banner", part="a.author", leaf=True)
DATE = Contract(region="div.banner", part="span.date", leaf=True)


def test_a_compiled_listed_conduit_script_passes_replays_contract_agreement() -> None:
    project = load_project(PILOT)
    spec, config = project.specs["read-article"], project.config
    author = "the article's author in the banner"
    date = "the article's publication date in the banner"
    shown = [
        "Testing without flakes",
        "Most flaky tests are really flaky data.",
        "testing",
        "Deterministic data helped us most.",
        "Sign in or sign up to add comments on this article",
    ]
    read_article = plan(
        expectation(0, {"check": "text_visible", "text": shown[0]}),
        expectation(
            1, {"check": "text_in_target", "target_meaning": author, "text": "anna"}
        ),
        expectation(
            2,
            {
                "check": "text_in_target",
                "target_meaning": date,
                "text": "January 4, 2026",
            },
        ),
        *(
            expectation(index, {"check": "text_visible", "text": text})
            for index, text in enumerate(shown[1:], start=3)
        ),
    )
    feed_link = Target(
        semantic="the article's link in the global feed",
        locators=(ByRole(role="link", name="Testing without flakes"),),
    )
    by_author = Target(
        semantic=author, locators=(ByCss(css="a.author"),), contract=AUTHOR
    )
    by_date = Target(semantic=date, locators=(ByCss(css="span.date"),), contract=DATE)
    path = ExploredPath(
        steps={1: PathStep({"action": "click"}, feed_link)},
        kept=(1,),
        bound={f"a{n}": None for n in range(1, 8)} | {"a2": by_author, "a3": by_date},
    )

    script = compile_script(spec, config, read_article, path, BY)
    parsed = parse_compiled(
        script.model_dump_json(),
        config,
        source=PILOT / ".compiled" / "read-article.json",
    )

    assert contract_problems(parsed, config) == []
    assert {name: target.contract for name, target in parsed.targets.items()} == {
        "t1": None,
        "t2": AUTHOR,
        "t3": DATE,
    }
