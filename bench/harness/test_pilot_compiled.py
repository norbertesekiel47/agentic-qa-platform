"""The five pilot scripts `aqa explore` compiled and confirmed (#58, ROADMAP
M1 exit): each up to date with its spec and the project's subject contracts,
none holding a check REVIEW.md marks "Not acceptable", #20's two points, and
the benign rebinding patches."""

import re
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path

from aqa_core.compiled import (
    Assertion,
    Click,
    CompiledScript,
    Fill,
    ProbeEquals,
    Reload,
    TextInTarget,
    TextVisible,
    UrlMatches,
    VisibleUnoccluded,
)
from aqa_core.project import (
    contract_problems,
    contracts_fingerprint,
    load_compiled,
    load_project,
)
from aqa_core.schema import Contract
from pilot_rebinding import apply_rebinding
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
QA = ROOT / "bench/apps/conduit/qa"
PATCHES = ROOT / "bench/harness/patches"
PILOTS = (
    "login",
    "read-article",
    "post-comment",
    "favorite-article",
    "publish-article",
)
COUNT = Contract(region="div.banner", part="span.counter", leaf=True)
COMMENT = "Thanks for the warm welcome!"
ORIGIN = "http://127.0.0.1:4100"
ASSERTION: TypeAdapter[Assertion] = TypeAdapter(Assertion)

type Row = Sequence[Assertion]
type Proxy = Callable[[Row, CompiledScript], bool]


def compiled(spec_id: str) -> CompiledScript:
    path = QA / ".compiled" / f"{spec_id}.json"
    if not path.is_file():
        raise AssertionError(f"{path.relative_to(ROOT)} isn't committed")
    return load_compiled(path, load_project(QA).config)


def row(script: CompiledScript, index: int) -> Row:
    return [check for check in script.assertions if check.expect_index == index]


def visible_text(checks: Row, _: CompiledScript) -> bool:
    return any(isinstance(check, TextVisible) for check in checks)


def no_probe(probe: str, value: int) -> Proxy:
    def missing(checks: Row, _: CompiledScript) -> bool:
        return not any(
            isinstance(check, ProbeEquals)
            and (check.probe, check.value) == (probe, value)
            for check in checks
        )

    return missing


def global_feed(checks: Row, _: CompiledScript) -> bool:
    return not any(isinstance(check, UrlMatches) for check in checks) or any(
        isinstance(check, TextVisible | TextInTarget) and check.matches("Global Feed")
        for check in checks
    )


def not_three_hit_tests(checks: Row, _: CompiledScript) -> bool:
    return len(checks) < 3 or not all(
        isinstance(check, VisibleUnoccluded) for check in checks
    )


def no_reload(_: Row, script: CompiledScript) -> bool:
    reloads = {item.id for item in script.coverage.requires if item.kind == "reload"}
    last = script.steps[-1] if script.steps else None
    return not (isinstance(last, Reload) and reloads and reloads <= set(last.satisfies))


def slug_pinned(checks: Row, _: CompiledScript) -> bool:
    urls = [
        f"{ORIGIN}/article/{slug}"
        for slug in ("Benchmarks-we-trust-5d7dc78d", "benchmarks-we-trust-0f0f0f0f")
    ]
    patterns = [check.pattern for check in checks if isinstance(check, UrlMatches)]
    return not patterns or any(
        re.search(pattern, url) is None for pattern in patterns for url in urls
    )


# REVIEW.md's "Not acceptable" cells, by spec and expectation.
PROXIES: dict[tuple[str, int], tuple[str, Proxy]] = {
    ("login", 0): ("a Global Feed heading for the home page", global_feed),
    ("login", 2): ("text_visible for the header's links", visible_text),
    ("login", 4): ("expectation 2's text checks for the hit test", not_three_hit_tests),
    ("read-article", 2): ("text_visible for the banner's date", visible_text),
    ("read-article", 4): ("text_visible for the tag", visible_text),
    ("read-article", 6): ("text_visible for the comment prompt", visible_text),
    ("post-comment", 1): ("text_visible for the author", visible_text),
    ("post-comment", 3): (
        "the shown comment for the saved count",
        no_probe("comment_count", 2),
    ),
    ("favorite-article", 0): ("the button checked without the reload", no_reload),
    ("favorite-article", 1): ("the count checked without the reload", no_reload),
    ("publish-article", 0): ("a lowercase or exact slug pattern", slug_pinned),
    ("publish-article", 3): ("text_visible for the author", visible_text),
    ("publish-article", 4): (
        "a POST request for the article count",
        no_probe("jake_article_count", 6),
    ),
}


def proxies(spec_id: str, script: CompiledScript) -> list[str]:
    return [
        f"{spec_id}/{index}: {name}"
        for (pilot, index), (name, found) in PROXIES.items()
        if pilot == spec_id and found(row(script, index), script)
    ]


def check(fields: dict[str, object], index: int) -> Assertion:
    return ASSERTION.validate_python({"id": "planted", "expect_index": index, **fields})


def visible(text: str) -> dict[str, object]:
    return {"check": "text_visible", "text": text}


# Each proxy REVIEW.md names, planted in place of its row's checks.
PLANTED: dict[str, tuple[str, int, list[dict[str, object]]]] = {
    "global feed": ("login", 0, [visible("Global Feed")]),
    "header links anywhere": ("login", 2, [visible("New Article")]),
    "links' text for the hit test": (
        "login",
        4,
        [{"check": "text_in_target", "target": "t1", "text": "Settings"}],
    ),
    "date anywhere": ("read-article", 2, [visible("January 4, 2026")]),
    "tag anywhere": ("read-article", 4, [visible("testing")]),
    "partial prompt": ("read-article", 6, [visible("to add comments on this article")]),
    "sign in anywhere": ("read-article", 6, [visible("Sign in")]),
    "author anywhere": ("post-comment", 1, [visible("reader")]),
    "shown comment for the count": (
        "post-comment",
        3,
        [{"check": "text_in_target", "target": "t1", "text": COMMENT}],
    ),
    "lowercase slug": (
        "publish-article",
        0,
        [{"check": "url_matches", "pattern": "/article/benchmarks-we-trust-"}],
    ),
    "exact slug": (
        "publish-article",
        0,
        [
            {
                "check": "url_matches",
                "pattern": "(?i)/article/benchmarks-we-trust-5d7dc78d$",
            }
        ],
    ),
    "byline anywhere": ("publish-article", 3, [visible("jake")]),
    "one POST request": (
        "publish-article",
        4,
        [
            {
                "check": "network_seen",
                "method": "POST",
                "url_pattern": "/api/articles$",
                "status_class": "2xx",
            }
        ],
    ),
}


class CompiledPilotTests(unittest.TestCase):
    def test_each_pilot_script_is_confirmed_and_up_to_date(self) -> None:
        project = load_project(QA)
        for spec_id in PILOTS:
            with self.subTest(spec_id):
                script = compiled(spec_id)
                self.assertTrue(script.confirmed)
                self.assertEqual(script.spec_hash, project.specs[spec_id].spec_hash)
                self.assertEqual(
                    script.compiled_by.subject_contracts,
                    contracts_fingerprint(project.config, spec_id),
                )
                self.assertEqual(contract_problems(script, project.config), [])
                # The pinned settings: Conduit's dates hold only in UTC
                # (LAB_NOTES watch list).
                self.assertEqual(
                    (
                        script.browser.timezone,
                        script.browser.locale,
                        script.browser.viewport,
                    ),
                    ("UTC", "en-US", (1280, 800)),
                )

    def test_no_compiled_assertion_is_a_review_proxy(self) -> None:
        for spec_id in PILOTS:
            with self.subTest(spec_id):
                self.assertEqual(proxies(spec_id, compiled(spec_id)), [])

    def test_each_review_proxy_is_caught_when_planted(self) -> None:
        for name, (spec_id, index, fields) in PLANTED.items():
            with self.subTest(name):
                script = compiled(spec_id)
                others = [c for c in script.assertions if c.expect_index != index]
                planted = script.model_copy(
                    update={"assertions": (*others, *(check(f, index) for f in fields))}
                )
                self.assertIn(
                    f"{spec_id}/{index}: {PROXIES[spec_id, index][0]}",
                    proxies(spec_id, planted),
                )
        for index in (0, 1):
            with self.subTest(f"favorite-article/{index} without its reload"):
                script = compiled("favorite-article")
                planted = script.model_copy(update={"steps": script.steps[:-1]})
                self.assertIn(
                    f"favorite-article/{index}: {PROXIES['favorite-article', index][0]}",
                    proxies("favorite-article", planted),
                )

    def test_the_favorites_counts_check_is_the_count_and_the_claim(self) -> None:
        # #20: conduit-benign-002 moves the count out of the button and drops
        # its parentheses.
        script = compiled("favorite-article")
        [count] = [c for c in row(script, 1) if isinstance(c, TextInTarget)]
        self.assertEqual(script.targets[count.target].contract, COUNT)
        self.assertNotIn(
            count.target,
            {c.target for c in row(script, 0) if isinstance(c, TextInTarget)},
        )
        self.assertEqual(
            [count.matches(text) for text in ("1", "(1)", "1 favorite", "(10)", "11")],
            [True, True, True, False, False],
        )

    def test_the_comment_forms_submit_button_names_no_label(self) -> None:
        # #20: conduit-benign-001 relabels Post Comment to Add Comment.
        script = compiled("post-comment")
        steps = list(script.steps)
        [fill] = [
            i for i, s in enumerate(steps) if isinstance(s, Fill) and s.value == COMMENT
        ]
        submit = next(s for s in steps[fill + 1 :] if isinstance(s, Click))
        meaning = script.targets[submit.target].semantic
        self.assertIsNone(re.search(r"(?i)\b(post|add) comment\b", meaning), meaning)

    def test_each_committed_patch_rebinds_only_target_locators(self) -> None:
        # conduit-benign-002 has none: no locator can restore its count's
        # contracted binding (#229).
        config = load_project(QA).config
        patches = sorted(PATCHES.glob("*.json"))
        self.assertEqual(
            [p.name for p in patches], ["conduit-benign-001.post-comment.json"]
        )
        for path in patches:
            with self.subTest(path.name):
                spec_id = path.name.split(".")[1]
                original = compiled(spec_id)
                patched = apply_rebinding(
                    original, path.read_text(), config, source=path
                )
                self.assertNotEqual(patched, original)


if __name__ == "__main__":
    unittest.main()
