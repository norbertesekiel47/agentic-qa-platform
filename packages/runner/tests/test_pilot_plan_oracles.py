"""Independent REVIEW examples and controls for the finite pilot oracle."""

import asyncio
from collections import Counter
from pathlib import Path

import pytest
from aqa_core.compiled import RequiredCondition
from aqa_core.coverage_plan import (
    CoveragePlan,
    PlannedCheck,
    PlannedExpectation,
    Unsupported,
)
from aqa_core.project import load_project
from aqa_runner.text_search import url_matches
from pydantic import ValidationError

from packages.runner.tests.pilot_plan_oracles import (
    ALIASES,
    LOGIN_ROOT_FRAGMENT_KEY,
    LOGIN_ROOT_KEY,
    PROMPT,
    PROMPT_WITH_COMMENT,
    PROMPT_WITH_JOINER,
    PROMPT_WITHOUT_JOINER,
    ROWS,
    phrase,
    plan_problems,
    source_problems,
)

QA = Path(__file__).resolve().parents[3] / "bench/apps/conduit/qa"


def text(target: str, value: str) -> PlannedCheck:
    return PlannedCheck(check="text_in_target", target_meaning=target, text=value)


def example(rows: list[list[PlannedCheck]]) -> CoveragePlan:
    return CoveragePlan(
        expectations=tuple(
            PlannedExpectation(
                expect_index=index,
                subject="the expectation's subject",
                claim="the expectation's complete claim",
                checks=tuple(checks),
            )
            for index, checks in enumerate(rows)
        ),
        requires=(),
    )


def replace_checks(
    plan: CoveragePlan, index: int, checks: list[PlannedCheck]
) -> CoveragePlan:
    rows = list(plan.expectations)
    rows[index] = rows[index].model_copy(
        update={
            "checks": tuple(checks),
            "unsupported": None
            if checks
            else Unsupported(reason="Required check omitted"),
        }
    )
    return plan.model_copy(update={"expectations": tuple(rows)})


def article() -> CoveragePlan:
    return example(
        [
            [text("article banner title heading", "Testing without flakes")],
            [text("article banner author link", "anna")],
            [text("article banner publication date", "January 4, 2026")],
            [text("article body", "Most flaky tests are really flaky data.")],
            [text("article tag list", "testing")],
            [text("article comment list", "Deterministic data helped us most.")],
            [
                text(
                    "signed-out comment prompt",
                    "Sign in or sign up to add comments on this article",
                )
            ],
        ]
    )


def test_article_checks_establish_the_review_rows_and_reject_the_date_proxy() -> None:
    plan = article()
    assert plan_problems("read-article", plan) == ()
    plan = replace_checks(
        plan, 2, [PlannedCheck(check="text_visible", text="January 4, 2026")]
    )
    assert plan_problems("read-article", plan) == (
        "read-article/2: establishing checks differ",
    )


def login() -> CoveragePlan:
    return example(
        [
            [PlannedCheck(check="url_matches", pattern=r"^https?://[^/?#]+/$")],
            [PlannedCheck(check="text_visible", text="Your Feed")],
            [
                text("header nav", label)
                for label in ("New Article", "Settings", "reader")
            ],
            [
                PlannedCheck(check="not_visible", target_meaning=role)
                for role in ("header sign-in link", "header sign-up link")
            ],
            [
                PlannedCheck(check="visible_unoccluded", target_meaning=role)
                for role in (
                    "header new-article link",
                    "header settings link",
                    "header current-user profile link",
                )
            ],
        ]
    )


def post() -> CoveragePlan:
    return example(
        [
            [text("article comment list", "Thanks for the warm welcome!")],
            [text("new comment card author link", "reader")],
            [PlannedCheck(check="text_visible", text="Glad to be here.")],
            [PlannedCheck(check="probe_equals", probe="comment_count", value=2)],
        ]
    )


def favorite() -> CoveragePlan:
    return example(
        [
            [text("article banner favorite button", "Unfavorite Article")],
            [text("article banner favorites count", "1")],
        ]
    ).model_copy(
        update={
            "requires": (
                RequiredCondition(
                    id="reload",
                    condition="Reload the article after favoriting and before checking the button and count",
                ),
            )
        }
    )


def publish() -> CoveragePlan:
    return example(
        [
            [
                PlannedCheck(
                    check="url_matches",
                    pattern=r"(?i)^https?://[^/?#]+/article/benchmarks-we-trust-",
                ),
                text("article banner title heading", "Benchmarks we trust"),
            ],
            [text("article body", "Every number cites the commit that produced it.")],
            [
                text("article tag list", "testing"),
                text("article tag list", "benchmarks"),
            ],
            [text("article banner author link", "jake")],
            [PlannedCheck(check="probe_equals", probe="jake_article_count", value=6)],
        ]
    )


EXAMPLES = {
    "login": login(),
    "read-article": article(),
    "post-comment": post(),
    "favorite-article": favorite(),
    "publish-article": publish(),
}


@pytest.mark.parametrize("spec_id", EXAMPLES)
def test_each_independent_example_establishes_every_row(spec_id: str) -> None:
    assert plan_problems(spec_id, EXAMPLES[spec_id]) == ()


@pytest.mark.parametrize("spec_id", EXAMPLES)
def test_missing_or_extra_expectations_fail_closed(spec_id: str) -> None:
    plan = EXAMPLES[spec_id]
    for expectations in (
        plan.expectations[:-1],
        (*plan.expectations, plan.expectations[0]),
        plan.expectations[::-1],
    ):
        assert plan_problems(
            spec_id, plan.model_copy(update={"expectations": expectations})
        ) == (f"{spec_id}: expectation indexes differ",)


@pytest.mark.parametrize(
    "condition",
    [
        "",
        "Reload the article before favoriting and before checking the button and count",
        "Reload the article after favoriting and after checking the button and count",
        "Reload another page after favoriting and before checking the button and count",
        "Do not reload the article after favoriting and before checking the button and count",
    ],
)
def test_favorite_requires_the_complete_reload_order(condition: str) -> None:
    requires = (
        (RequiredCondition(id="reload", condition=condition),) if condition else ()
    )
    assert plan_problems(
        "favorite-article", favorite().model_copy(update={"requires": requires})
    ) == (
        "favorite-article/0: reload condition differs",
        "favorite-article/1: reload condition differs",
    )


PROXIES = [
    ("login", 0, [PlannedCheck(check="text_visible", text="Global Feed")]),
    (
        "login",
        2,
        [
            PlannedCheck(check="text_visible", text=label)
            for label in ("New Article", "Settings", "reader")
        ],
    ),
    (
        "login",
        4,
        [text("header nav", label) for label in ("New Article", "Settings", "reader")],
    ),
    ("read-article", 2, [PlannedCheck(check="text_visible", text="January 4, 2026")]),
    ("read-article", 4, [PlannedCheck(check="text_visible", text="testing")]),
    (
        "read-article",
        6,
        [PlannedCheck(check="text_visible", text="to add comments on this article")],
    ),
    ("read-article", 6, [PlannedCheck(check="text_visible", text="Sign in")]),
    ("post-comment", 1, [PlannedCheck(check="text_visible", text="reader")]),
    ("post-comment", 3, [text("article comment list", "Thanks for the warm welcome!")]),
    (
        "favorite-article",
        0,
        [text("article banner favorite button", "Unfavorite Article")],
    ),
    ("favorite-article", 1, [text("article banner favorites count", "1")]),
    (
        "publish-article",
        0,
        [
            PlannedCheck(
                check="url_matches",
                pattern=r"^https?://[^/?#]+/article/benchmarks-we-trust-",
            )
        ],
    ),
    (
        "publish-article",
        0,
        [
            PlannedCheck(
                check="url_matches",
                pattern=r"(?i)^https?://[^/?#]+/article/benchmarks-we-trust-5d7dc78d$",
            )
        ],
    ),
    ("publish-article", 3, [PlannedCheck(check="text_visible", text="jake")]),
    (
        "publish-article",
        4,
        [
            PlannedCheck(
                check="network_seen",
                method="POST",
                url_pattern="/api/articles",
                status_class="2xx",
            )
        ],
    ),
]


@pytest.mark.parametrize(("spec_id", "index", "proxy"), PROXIES)
@pytest.mark.parametrize("addition", [False, True])
def test_each_review_proxy_fails_as_replacement_and_addition(
    spec_id: str, index: int, proxy: list[PlannedCheck], addition: bool
) -> None:
    plan = EXAMPLES[spec_id]
    if spec_id == "favorite-article":
        # REVIEW's favorite proxy is the same check without the reload.
        assert proxy == list(plan.expectations[index].checks)
        conflicting = RequiredCondition(
            id="optimistic",
            condition="Check the button and count before reloading the article",
        )
        changed = plan.model_copy(
            update={"requires": (*plan.requires, conflicting) if addition else ()}
        )
        expected: tuple[str, ...] = (
            "favorite-article/0: reload condition differs",
            "favorite-article/1: reload condition differs",
        )
    else:
        checks = list(plan.expectations[index].checks) if addition else []
        if spec_id == "publish-article" and index == 0 and not addition:
            checks = [text("article banner title heading", "Benchmarks we trust")]
        changed = replace_checks(plan, index, checks + proxy)
        expected = (f"{spec_id}/{index}: establishing checks differ",)
    changed = CoveragePlan.model_validate(changed.model_dump(mode="json"))
    assert plan_problems(spec_id, changed) == expected


@pytest.mark.parametrize("spec_id", EXAMPLES)
def test_every_atomic_check_and_its_scope_are_required(spec_id: str) -> None:
    plan = EXAMPLES[spec_id]
    for index, row in enumerate(plan.expectations):
        for position, check in enumerate(row.checks):
            checks = list(row.checks)
            assert plan_problems(
                spec_id,
                replace_checks(plan, index, checks[:position] + checks[position + 1 :]),
            ) == (f"{spec_id}/{index}: establishing checks differ",)
            if check.target_meaning:
                for target in (
                    f"not {check.target_meaning}",
                    f"{check.target_meaning} in the footer",
                    "unknown target",
                    "comment card selected by its checked text",
                ):
                    checks[position] = check.model_copy(
                        update={"target_meaning": target}
                    )
                    assert plan_problems(
                        spec_id, replace_checks(plan, index, checks)
                    ) == (f"{spec_id}/{index}: establishing checks differ",)


@pytest.mark.parametrize(
    ("spec_id", "index", "replacement"),
    [
        ("read-article", 2, text("comment publication date", "January 4, 2026")),
        (
            "read-article",
            6,
            text("signed-out comment prompt", "to add comments on this article"),
        ),
        ("read-article", 5, text("new comment card author link", "reader")),
        ("post-comment", 1, text("header current-user profile link", "reader")),
        ("post-comment", 2, text("article comment author link", "jake")),
        (
            "post-comment",
            3,
            PlannedCheck(check="probe_equals", probe="comment_count", value="2"),
        ),
        (
            "post-comment",
            3,
            PlannedCheck(check="probe_equals", probe="other_count", value=2),
        ),
        (
            "publish-article",
            4,
            PlannedCheck(check="probe_equals", probe="jake_article_count", value=5),
        ),
        (
            "read-article",
            0,
            PlannedCheck(
                check="text_in_target",
                target_meaning="article banner title heading",
                pattern="Testing.*",
            ),
        ),
        ("login", 0, PlannedCheck(check="url_matches", pattern="^/$")),
        (
            "login",
            0,
            PlannedCheck(check="url_matches", pattern=r"^(?:https|http)://[^/?#]+/$"),
        ),
        (
            "post-comment",
            3,
            PlannedCheck(check="probe_equals", probe="comment_count", value="2.0"),
        ),
    ],
)
@pytest.mark.parametrize("addition", [False, True])
def test_near_misses_and_unclaimed_author_checks_fail(
    spec_id: str, index: int, replacement: PlannedCheck, addition: bool
) -> None:
    checks = list(EXAMPLES[spec_id].expectations[index].checks) if addition else []
    assert plan_problems(
        spec_id, replace_checks(EXAMPLES[spec_id], index, [*checks, replacement])
    ) == (f"{spec_id}/{index}: establishing checks differ",)


def test_exact_link_aliases_case_space_and_unordered_checks_are_accepted() -> None:
    checks = [
        text(role, label)
        for role, label in (
            ("HEADER NEW-ARTICLE LINK", "NEW ARTICLE"),
            (" header   settings link ", "Settings"),
            ("header current-user profile link", "reader"),
        )
    ]
    assert plan_problems("login", replace_checks(login(), 2, checks[::-1])) == ()
    assert plan_problems(
        "login",
        replace_checks(
            login(),
            4,
            [PlannedCheck(check="visible_unoccluded", target_meaning="header nav")],
        ),
    ) == ("login/4: establishing checks differ",)
    assert plan_problems("unknown", article()) == ("unknown: unknown pilot",)


def test_oracle_covers_every_current_review_row() -> None:
    project = load_project(QA)
    assert (
        source_problems(
            (QA / "REVIEW.md").read_text(),
            tuple(s.frontmatter for s in project.specs.values()),
        )
        == ()
    )
    assert {key: len(rows) for key, rows in ROWS.items()} == {
        "login": 5,
        "read-article": 7,
        "post-comment": 4,
        "favorite-article": 2,
        "publish-article": 5,
    }
    assert sum(len(row.required) for rows in ROWS.values() for row in rows) == 30


@pytest.mark.parametrize(
    "change",
    [
        "added",
        "changed-check",
        "changed-proxy",
        "omitted",
        "duplicate",
        "duplicate-heading",
    ],
)
def test_review_changes_invalidate_the_oracle(change: str) -> None:
    review = (QA / "REVIEW.md").read_text()
    first = next(line for line in review.splitlines() if line.startswith("| 0 |"))
    replacement = {
        "added": first + "\n| 9 | New claim | New check | No proxy |",
        "changed-check": first.replace("`url_matches`", "`text_visible`"),
        "changed-proxy": first.replace("Global Feed", "Other Feed"),
        "omitted": "",
        "duplicate": first + "\n" + first,
        "duplicate-heading": "### `login`\n" + first,
    }[change]
    specs = tuple(s.frontmatter for s in load_project(QA).specs.values())
    assert source_problems(review.replace(first, replacement, 1), specs) == (
        "REVIEW rows differ",
    )


@pytest.mark.parametrize(
    "change",
    ["added-row", "changed-row", "duplicate-spec", "omitted-spec", "unknown-spec"],
)
def test_spec_changes_invalidate_the_oracle(change: str) -> None:
    specs = [s.frontmatter for s in load_project(QA).specs.values()]
    first = specs[0]
    if change == "added-row":
        specs[0] = first.model_copy(update={"expect": (*first.expect, first.expect[0])})
    elif change == "changed-row":
        claim = first.expect[0].model_copy(update={"text": "A different expectation"})
        specs[0] = first.model_copy(update={"expect": (claim, *first.expect[1:])})
    elif change == "duplicate-spec":
        specs.append(first)
    elif change == "omitted-spec":
        specs.pop()
    else:
        specs[0] = first.model_copy(update={"id": "unknown"})
    assert source_problems((QA / "REVIEW.md").read_text(), specs) == (
        "spec expectations differ",
    )


@pytest.mark.parametrize(
    ("spec_id", "url", "expected"),
    [
        ("login", "https://conduit.test/", True),
        ("login", "http://localhost:8123/", True),
        ("login", "https://conduit.test/login", False),
        ("login", "https://conduit.test/login?next=/", False),
        ("login", "https://conduit.test/#/", False),
        (
            "publish-article",
            "https://conduit.test/article/Benchmarks-we-trust-abc123",
            True,
        ),
        (
            "publish-article",
            "http://localhost:8123/article/benchmarks-we-trust-def456",
            True,
        ),
        (
            "publish-article",
            "https://conduit.test/ARTICLE/BENCHMARKS-WE-TRUST-XYZ",
            True,
        ),
        ("publish-article", "https://conduit.test/article/another-title-abc123", False),
        ("publish-article", "https://conduit.test/", False),
        (
            "publish-article",
            "https://conduit.test/login?next=/article/benchmarks-we-trust-abc123",
            False,
        ),
        (
            "publish-article",
            "https://conduit.test/article/benchmarks-we-trustworthy-abc123",
            False,
        ),
    ],
)
def test_reviewed_patterns_search_real_complete_urls(
    spec_id: str, url: str, expected: bool
) -> None:
    pattern = ROWS[spec_id][0].required[0].pattern
    assert pattern is not None
    assert asyncio.run(url_matches(pattern, url)) is expected


def test_every_named_proxy_has_an_independent_specimen() -> None:
    assert Counter(
        (spec, index)
        for spec, rows in ROWS.items()
        for index, row in enumerate(rows)
        for _ in row.forbidden
    ) == Counter((spec, index) for spec, index, _ in PROXIES)


def test_extra_conditions_and_unsupported_rows_fail_closed() -> None:
    changed = login().model_copy(
        update={
            "requires": (
                RequiredCondition(id="unknown", condition="An unreviewed condition"),
            )
        }
    )
    assert plan_problems("login", changed) == ("login: unreviewed conditions",)
    assert plan_problems("login", replace_checks(login(), 1, [])) == (
        "login/1: establishing checks differ",
    )


def test_probe_schema_rejects_a_float_before_oracle_matching() -> None:
    with pytest.raises(ValidationError, match="valid integer"):
        PlannedCheck.model_validate(
            {"check": "probe_equals", "probe": "comment_count", "value": 2.0}
        )


@pytest.mark.parametrize("change", ["added", "duplicate", "reformatted"])
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("| {index} |", "|"),
        ("|{index}|", "|"),
        ("|{index} |", "|"),
        ("| {index}|", "|"),
        ("|  {index}  |", "|"),
        ("  | {index} |", "|"),
        ("{index} |", ""),
        ("| {index} |", ""),
        ("{index} |", "|"),
    ],
    ids=[
        "canonical",
        "compact",
        "no-space-before-index",
        "no-space-after-index",
        "multiple-padding",
        "leading-indentation",
        "no-outer-pipes",
        "leading-pipe-only",
        "trailing-pipe-only",
    ],
)
def test_alternate_numbered_review_rows_force_review(
    change: str, prefix: str, suffix: str
) -> None:
    review = (QA / "REVIEW.md").read_text()
    first = next(line for line in review.splitlines() if line.startswith("| 0 |"))
    body = first.split("|", 2)[2].removesuffix("|")
    row = prefix.format(index=9 if change == "added" else 0) + body + suffix
    replacement = row if change == "reformatted" else first + "\n" + row
    expected = (
        ()
        if change == "reformatted" and prefix == "| {index} |" and suffix == "|"
        else ("REVIEW rows differ",)
    )
    specs = tuple(s.frontmatter for s in load_project(QA).specs.values())
    assert source_problems(review.replace(first, replacement, 1), specs) == expected


def test_reordered_review_rows_force_review() -> None:
    review = (QA / "REVIEW.md").read_text()
    first = next(line for line in review.splitlines() if line.startswith("| 0 |"))
    second = next(line for line in review.splitlines() if line.startswith("| 1 |"))
    changed = review.replace(first + "\n" + second, second + "\n" + first, 1)
    specs = tuple(s.frontmatter for s in load_project(QA).specs.values())
    assert source_problems(changed, specs) == ("REVIEW rows differ",)


@pytest.mark.parametrize(
    ("heading", "prose"),
    [
        ("login", "A note mentioning | 9 | is prose."),
        ("login", "9. This numbered list item is prose."),
        ("read-article", "A note with a pipe | is prose."),
    ],
    ids=["inline-pipe", "numbered-list", "between-sections"],
)
def test_unrelated_review_prose_does_not_change_completeness(
    heading: str, prose: str
) -> None:
    review = (QA / "REVIEW.md").read_text()
    anchor = f"### `{heading}`"
    changed = review.replace(anchor, prose + "\n\n" + anchor, 1)
    specs = tuple(s.frontmatter for s in load_project(QA).specs.values())
    assert source_problems(changed, specs) == ()


def test_login_accepts_the_reviewed_second_root_pattern() -> None:
    plan = replace_checks(
        login(), 0, [PlannedCheck(check="url_matches", pattern=LOGIN_ROOT_KEY)]
    )
    assert plan_problems("login", plan) == ()


@pytest.mark.parametrize(
    "pattern",
    [
        r"^https?://[^/]+/?(#/?)?",
        r"^https?://[^/]+",
        r"^https?://[^/]+/?(?:[?#].*)?",
        r"https?://[^/]+/?(?:[?#].*)?$",
        r"(?i)^https?://[^/]+/?(?:[?#].*)?$",
    ],
)
def test_login_rejects_any_other_root_pattern(pattern: str) -> None:
    plan = replace_checks(
        login(), 0, [PlannedCheck(check="url_matches", pattern=pattern)]
    )
    assert plan_problems("login", plan) == ("login/0: establishing checks differ",)


def test_login_second_root_pattern_admits_only_the_root_path() -> None:
    admitted = [
        "http://h",
        "http://h/",
        "http://h:3000/",
        "http://h/?a=1",
        "http://h/#frag",
    ]
    refused = ["http://h/login", "http://h/article/x", "http://h//"]
    assert [asyncio.run(url_matches(LOGIN_ROOT_KEY, url)) for url in admitted] == [
        True
    ] * len(admitted)
    assert [asyncio.run(url_matches(LOGIN_ROOT_KEY, url)) for url in refused] == [
        False
    ] * len(refused)


def test_post_comment_accepts_a_scoped_check_for_the_earlier_comment() -> None:
    plan = replace_checks(post(), 2, [text("article comment list", "Glad to be here.")])
    assert plan_problems("post-comment", plan) == ()


@pytest.mark.parametrize(
    "checks",
    [
        [text("article comment list", "Someone else")],
        [text("header nav", "Glad to be here.")],
        [
            PlannedCheck(check="text_visible", text="Glad to be here."),
            text("article comment list", "Glad to be here."),
        ],
    ],
)
def test_post_comment_row_two_has_no_other_second_key(
    checks: list[PlannedCheck],
) -> None:
    plan = replace_checks(post(), 2, checks)
    assert plan_problems("post-comment", plan) == (
        "post-comment/2: establishing checks differ",
    )


def test_a_second_key_widens_only_its_own_row() -> None:
    plan = replace_checks(
        post(),
        0,
        [PlannedCheck(check="text_visible", text="Thanks for the warm welcome!")],
    )
    assert plan_problems("post-comment", plan) == (
        "post-comment/0: establishing checks differ",
    )


def prompt_pattern(pattern: str, target: str = PROMPT) -> PlannedCheck:
    return PlannedCheck(check="text_in_target", target_meaning=target, pattern=pattern)


def test_login_accepts_the_reviewed_fragment_root_pattern() -> None:
    plan = replace_checks(
        login(),
        0,
        [PlannedCheck(check="url_matches", pattern=LOGIN_ROOT_FRAGMENT_KEY)],
    )
    assert plan_problems("login", plan) == ()


def test_login_accepts_the_feed_tabs_check_for_your_feed() -> None:
    plan = replace_checks(login(), 1, [text("feed tabs", "Your Feed")])
    assert plan_problems("login", plan) == ()


@pytest.mark.parametrize(
    "checks",
    [
        [text("header nav", "Your Feed")],
        [text("feed tabs", "Global Feed")],
        [
            PlannedCheck(check="text_visible", text="Your Feed"),
            text("feed tabs", "Your Feed"),
        ],
    ],
)
def test_login_row_one_has_no_other_second_key(checks: list[PlannedCheck]) -> None:
    plan = replace_checks(login(), 1, checks)
    assert plan_problems("login", plan) == ("login/1: establishing checks differ",)


@pytest.mark.parametrize("pattern", [PROMPT_WITH_JOINER, PROMPT_WITHOUT_JOINER])
def test_read_article_accepts_both_reviewed_prompt_patterns(pattern: str) -> None:
    plan = replace_checks(article(), 6, [prompt_pattern(pattern)])
    assert plan_problems("read-article", plan) == ()


@pytest.mark.parametrize(
    "check",
    [
        prompt_pattern(r"(?i)sign in|sign up"),
        prompt_pattern(r"(?i)to add comments on this article"),
        prompt_pattern(PROMPT_WITHOUT_JOINER, "header nav"),
        prompt_pattern(PROMPT_WITHOUT_JOINER.replace("(?i)", "")),
    ],
)
def test_read_article_row_six_has_no_other_pattern_key(check: PlannedCheck) -> None:
    plan = replace_checks(article(), 6, [check])
    assert plan_problems("read-article", plan) == (
        "read-article/6: establishing checks differ",
    )


def test_the_prompt_patterns_require_both_links_and_the_joiner_one_a_joiner() -> None:
    both = "Sign in or sign up to add comments on this article"
    for pattern in (PROMPT_WITH_JOINER, PROMPT_WITHOUT_JOINER):
        assert asyncio.run(url_matches(pattern, both)) is True
        for proxy in ("to add comments on this article", "Sign in", "Sign up"):
            assert asyncio.run(url_matches(pattern, proxy)) is False
    for pattern in (PROMPT_WITH_JOINER, PROMPT_WITHOUT_JOINER):
        assert asyncio.run(url_matches(pattern, "Sign up or sign in")) is True
        assert asyncio.run(url_matches(pattern, "Sign up and sign in")) is True
    assert asyncio.run(url_matches(PROMPT_WITHOUT_JOINER, "Sign in Sign up")) is True
    assert asyncio.run(url_matches(PROMPT_WITH_JOINER, "Sign in Sign up")) is False


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("Sign in or sign up to add comments on this article.", True),
        ("Sign in Sign up", False),
        ("Sign in", False),
        ("to add comments on this article", False),
        ("Sign in or sign up", False),
    ],
)
def test_the_third_prompt_pattern_needs_both_links_then_the_comment_words(
    sample: str, expected: bool
) -> None:
    assert asyncio.run(url_matches(PROMPT_WITH_COMMENT, sample)) is expected


def test_read_article_accepts_the_third_reviewed_prompt_pattern() -> None:
    plan = replace_checks(article(), 6, [prompt_pattern(PROMPT_WITH_COMMENT)])
    assert plan_problems("read-article", plan) == ()


def test_the_third_prompt_pattern_is_scoped_to_the_prompt() -> None:
    plan = replace_checks(
        article(), 6, [prompt_pattern(PROMPT_WITH_COMMENT, "header nav")]
    )
    assert plan_problems("read-article", plan) == (
        "read-article/6: establishing checks differ",
    )


@pytest.mark.parametrize(
    ("spec_id", "index", "phrase", "role", "literal"),
    [
        (
            "read-article",
            3,
            "the article body content",
            "article body",
            "Most flaky tests are really flaky data.",
        ),
        (
            "post-comment",
            0,
            "the list of comments under the article",
            "article comment list",
            "Thanks for the warm welcome!",
        ),
    ],
)
def test_a_reviewed_recorded_phrase_means_its_oracle_role(
    spec_id: str, index: int, phrase: str, role: str, literal: str
) -> None:
    plan = {"read-article": article(), "post-comment": post()}[spec_id]
    recorded = replace_checks(plan, index, [text(phrase, literal)])
    assert plan_problems(spec_id, recorded) == ()
    assert (
        plan_problems(spec_id, replace_checks(plan, index, [text(role, literal)])) == ()
    )


def test_a_reviewed_phrase_names_one_role_only() -> None:
    plan = replace_checks(
        article(),
        3,
        [
            text(
                "the list of comments under the article",
                "Most flaky tests are really flaky data.",
            )
        ],
    )
    assert plan_problems("read-article", plan) == (
        "read-article/3: establishing checks differ",
    )


def test_the_favorite_reload_condition_has_one_reviewed_wording() -> None:
    plan = favorite()
    wording = "checked after reloading the article page, so the favorite has persisted"
    reworded = plan.model_copy(
        update={"requires": (RequiredCondition(id="c1", condition=wording),)}
    )
    assert plan_problems("favorite-article", reworded) == ()
    other = plan.model_copy(
        update={"requires": (RequiredCondition(id="c1", condition="after a reload"),)}
    )
    assert plan_problems("favorite-article", other) == (
        "favorite-article/0: reload condition differs",
        "favorite-article/1: reload condition differs",
    )


def test_every_reviewed_alias_names_a_role_some_row_requires() -> None:
    roles = {
        phrase(check.target_meaning or "")
        for rows in ROWS.values()
        for row in rows
        for keys in (row.required, *row.alternatives)
        for check in keys
    }
    assert set(ALIASES.values()) <= roles
