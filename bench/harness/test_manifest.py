"""Tests for manifest.py: the benchmark ground-truth manifest (ADR-0022).

Run: uv run python -m unittest discover -s bench/harness
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import manifest

REPO = Path(__file__).resolve().parents[2]


def bug_case() -> dict[str, Any]:
    return {
        "app": "conduit",
        "kind": "bug",
        "category": "data_display",
        "split": "dev",
        "family": "conduit-article-meta",
        "flag": "k3q9",
        "summary": "Favorites count on the article page is off by one",
        "expected": [
            {
                "spec": "favorite-article",
                "verdict": "expectation_violated",
                "expect": [1],
            },
        ],
    }


def benign_case() -> dict[str, Any]:
    return {
        "app": "conduit",
        "kind": "benign",
        "split": "dev",
        "family": "conduit-nav",
        "flag": "h3k8",
        "summary": "Sign-in link relabeled",
        "expected": [{"spec": "login", "verdict": "drift_consistent"}],
    }


def valid_data() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "cases": {"conduit-bug-001": bug_case(), "conduit-benign-001": benign_case()},
    }


def spec_text(spec_id: str) -> str:
    """A spec file with three expectations, one of them a mapping (DATA_MODEL §6)."""
    return (
        "---\n"
        f"id: {spec_id}\n"
        "goal: A reader does something.\n"
        "preconditions: { start_url: / }\n"
        "expect:\n"
        "  - The first thing holds\n"
        "  - text: The button is visible and not covered by any overlay\n"
        "    visual: deterministic\n"
        "  - The third thing holds\n"
        "invariants: { inherit: true }\n"
        "---\n\n"
        "Notes for humans.\n"
    )


class Workspace:
    """A throwaway repo root with the app directory, its project config and
    the spec files the cases reference."""

    def __init__(self, specs: tuple[str, ...] = ("favorite-article", "login")) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        qa = self.root / "bench" / "apps" / "conduit" / "qa"
        qa.mkdir(parents=True)
        (qa / "config.yaml").write_text("# Every key is optional (DATA_MODEL §9).\n")
        for spec in specs:
            (qa / f"{spec}.spec.md").write_text(spec_text(spec))

    def write_manifest(
        self, data: dict[str, Any], split_hash: str | None = None
    ) -> None:
        bench = self.root / "bench"
        (bench / "manifest.v1.json").write_text(json.dumps(data, indent=2))
        digest = manifest.split_hash(data) if split_hash is None else split_hash
        (bench / "manifest.v1.split.sha256").write_text(digest + "\n")

    def close(self) -> None:
        self._tmp.cleanup()


class ManifestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.close)

    def errors_for(self, data: object) -> list[str]:
        return manifest.validate(data, self.ws.root)

    def assert_error(self, data: object, fragment: str) -> None:
        errors = self.errors_for(data)
        self.assertTrue(
            any(fragment in e for e in errors),
            f"expected an error containing {fragment!r}, got {errors!r}",
        )


class ValidManifestTest(ManifestTestCase):
    def test_valid_manifest_has_no_errors(self) -> None:
        self.assertEqual(self.errors_for(valid_data()), [])

    def test_empty_case_set_is_valid(self) -> None:
        self.assertEqual(self.errors_for({"schema_version": 1, "cases": {}}), [])

    def test_load_returns_typed_cases(self) -> None:
        self.ws.write_manifest(valid_data())
        loaded = manifest.load(self.ws.root)
        bug = loaded.cases["conduit-bug-001"]
        self.assertEqual(bug.flag, "k3q9")
        self.assertEqual(bug.category, "data_display")
        self.assertEqual(bug.expected[0].expect, (1,))
        benign = loaded.cases["conduit-benign-001"]
        self.assertIsNone(benign.category)
        self.assertEqual(benign.expected[0].verdict, "drift_consistent")
        self.assertEqual(benign.expected[0].expect, ())
        self.assertEqual(bug.expected[0].invariants, ())
        self.assertEqual(loaded.case_for_flag("h3k8"), benign)
        self.assertIsNone(loaded.case_for_flag("zzzz"))

    def test_a_bug_may_also_cause_drift_in_another_spec(self) -> None:
        data = valid_data()
        data["cases"]["conduit-bug-001"]["expected"].append(
            {"spec": "login", "verdict": "drift_consistent"}
        )
        self.assertEqual(self.errors_for(data), [])


class TopLevelTest(ManifestTestCase):
    def test_non_object(self) -> None:
        self.assert_error([], "manifest must be a JSON object")

    def test_wrong_schema_version(self) -> None:
        data = valid_data()
        data["schema_version"] = 2
        self.assert_error(data, "schema_version must be 1")

    def test_missing_cases(self) -> None:
        self.assert_error({"schema_version": 1}, "missing key 'cases'")

    def test_unknown_top_level_key(self) -> None:
        data = valid_data()
        data["notes"] = "x"
        self.assert_error(data, "unknown key 'notes'")


class CaseFieldTest(ManifestTestCase):
    def mutate(self, case_id: str, **changes: object) -> dict[str, Any]:
        data = valid_data()
        case = data["cases"][case_id]
        for key, value in changes.items():
            if value is None:
                case.pop(key, None)
            else:
                case[key] = value
        return data

    def test_malformed_case_id(self) -> None:
        data = valid_data()
        data["cases"]["conduit-bug-1"] = data["cases"].pop("conduit-bug-001")
        self.assert_error(
            data, "case id 'conduit-bug-1' must look like <app>-<bug|benign>-NNN"
        )

    def test_case_id_must_agree_with_app(self) -> None:
        data = valid_data()
        data["cases"]["medusa-bug-001"] = data["cases"].pop("conduit-bug-001")
        self.assert_error(
            data, "medusa-bug-001: app 'conduit' does not match the case id"
        )

    def test_case_id_must_agree_with_kind(self) -> None:
        data = valid_data()
        data["cases"]["conduit-benign-002"] = data["cases"].pop("conduit-bug-001")
        self.assert_error(
            data, "conduit-benign-002: kind 'bug' does not match the case id"
        )

    def test_missing_field(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", flag=None),
            "conduit-bug-001: missing key 'flag'",
        )

    def test_unknown_field(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", tier="frontend"),
            "conduit-bug-001: unknown key 'tier'",
        )

    def test_app_without_directory(self) -> None:
        data = valid_data()
        data["cases"]["shop-bug-001"] = {
            **bug_case(),
            "app": "shop",
            "family": "shop-cart",
            "flag": "s1s1",
        }
        self.assert_error(data, "shop-bug-001: no app directory bench/apps/shop")

    def test_bug_needs_category(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", category=None),
            "conduit-bug-001: a bug needs a category",
        )

    def test_benign_has_no_category(self) -> None:
        self.assert_error(
            self.mutate("conduit-benign-001", category="visual"),
            "conduit-benign-001: a benign case has no category",
        )

    def test_unknown_category(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", category="layout"),
            "conduit-bug-001: category 'layout'",
        )

    def test_unknown_split(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", split="train"),
            "conduit-bug-001: split 'train'",
        )

    def test_family_needs_app_prefix(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", family="article-meta"),
            "conduit-bug-001: family 'article-meta' must look like conduit-<name>",
        )

    def test_family_shares_one_split(self) -> None:
        data = valid_data()
        data["cases"]["conduit-bug-002"] = {
            **bug_case(),
            "flag": "m4m4",
            "split": "test",
        }
        self.assert_error(data, "family 'conduit-article-meta' spans splits: dev, test")

    def test_flag_pattern(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", flag="conduit-bug-001"),
            "conduit-bug-001: flag 'conduit-bug-001' must be 4 lowercase letters or digits",
        )

    def test_flags_are_unique(self) -> None:
        self.assert_error(
            self.mutate("conduit-benign-001", flag="k3q9"),
            "flag 'k3q9' is used by conduit-benign-001, conduit-bug-001",
        )

    def test_selftest_flag_is_reserved(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", flag="0000"),
            "conduit-bug-001: flag '0000' is reserved for the harness self-test",
        )

    def test_summary_is_one_nonempty_line(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", summary="  "), "conduit-bug-001: summary"
        )
        self.assert_error(
            self.mutate("conduit-bug-001", summary="a\nb"), "conduit-bug-001: summary"
        )

    def test_wrong_field_type(self) -> None:
        self.assert_error(
            self.mutate("conduit-bug-001", split=["dev"]),
            "conduit-bug-001: split must be a string",
        )

    def test_null_is_not_a_string(self) -> None:
        # mutate() treats None as "remove the key", so set JSON null directly.
        for key in ("app", "kind", "category", "split", "family", "flag", "summary"):
            with self.subTest(key=key):
                data = valid_data()
                data["cases"]["conduit-bug-001"][key] = None
                self.assert_error(data, f"conduit-bug-001: {key} must be a string")

    def test_all_errors_are_reported_together(self) -> None:
        data = self.mutate("conduit-bug-001", split="train", flag="BAD")
        errors = self.errors_for(data)
        self.assertTrue(any("split 'train'" in e for e in errors), errors)
        self.assertTrue(any("flag 'BAD'" in e for e in errors), errors)


class ExpectedTest(ManifestTestCase):
    def with_expected(self, case_id: str, expected: object) -> dict[str, Any]:
        data = valid_data()
        data["cases"][case_id]["expected"] = expected
        return data

    def test_expected_must_be_nonempty_list(self) -> None:
        self.assert_error(
            self.with_expected("conduit-bug-001", []),
            "conduit-bug-001: expected must be a non-empty list",
        )

    def test_null_spec_or_verdict_is_not_a_string(self) -> None:
        for key in ("spec", "verdict"):
            with self.subTest(key=key):
                data = valid_data()
                data["cases"]["conduit-bug-001"]["expected"][0][key] = None
                self.assert_error(
                    data, f"conduit-bug-001: expected[0]: {key} must be a string"
                )

    def test_unknown_verdict(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-bug-001", [{"spec": "login", "verdict": "pass"}]
            ),
            "conduit-bug-001: expected[0]: verdict 'pass'",
        )

    def test_bug_needs_a_violated_expectation(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-bug-001", [{"spec": "login", "verdict": "drift_consistent"}]
            ),
            "conduit-bug-001: a bug needs at least one expectation_violated entry",
        )

    def test_benign_is_drift_consistent_everywhere(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-benign-001",
                [{"spec": "login", "verdict": "expectation_violated", "expect": [0]}],
            ),
            "conduit-benign-001: expected[0]: a benign case is drift_consistent in every spec",
        )

    def test_violation_names_expect_indexes_or_invariants(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-bug-001",
                [{"spec": "login", "verdict": "expectation_violated"}],
            ),
            "conduit-bug-001: expected[0]: an expectation_violated entry names "
            "expect indexes, invariants or both",
        )

    def test_invariant_only_violation(self) -> None:
        entry = {
            "spec": "login",
            "verdict": "expectation_violated",
            "invariants": ["js_exceptions"],
        }
        self.assertEqual(
            self.errors_for(self.with_expected("conduit-bug-001", [entry])), []
        )

    def test_invariants_are_known_distinct_names(self) -> None:
        for bad in ([], ["exceptions"], ["http_5xx", "http_5xx"], "http_5xx"):
            with self.subTest(invariants=bad):
                self.assert_error(
                    self.with_expected(
                        "conduit-bug-001",
                        [
                            {
                                "spec": "login",
                                "verdict": "expectation_violated",
                                "invariants": bad,
                            }
                        ],
                    ),
                    "conduit-bug-001: expected[0]: invariants must be a non-empty list "
                    "of distinct names from console_errors, js_exceptions, http_5xx, "
                    "broken_images",
                )

    def test_drift_has_no_invariants(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-benign-001",
                [
                    {
                        "spec": "login",
                        "verdict": "drift_consistent",
                        "invariants": ["http_5xx"],
                    }
                ],
            ),
            "conduit-benign-001: expected[0]: invariants is only for expectation_violated",
        )

    def test_expect_index_must_exist_in_the_spec(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-bug-001",
                [
                    {
                        "spec": "login",
                        "verdict": "expectation_violated",
                        "expect": [2, 3],
                    }
                ],
            ),
            "conduit-bug-001: expected[0]: expect index 3 is out of range: "
            "spec 'login' has 3 expectations",
        )

    def violation_in_login(self) -> dict[str, Any]:
        return self.with_expected(
            "conduit-bug-001",
            [{"spec": "login", "verdict": "expectation_violated", "expect": [0]}],
        )

    def test_spec_file_without_expect_list(self) -> None:
        spec = self.ws.root / "bench" / "apps" / "conduit" / "qa" / "login.spec.md"
        spec.write_text(
            "---\nid: login\ngoal: x\npreconditions: { start_url: / }\n---\n"
        )
        self.assert_error(
            self.violation_in_login(),
            f"conduit-bug-001: expected[0]: {spec}: expect: missing key",
        )

    def test_spec_file_id_must_match_its_name(self) -> None:
        spec = self.ws.root / "bench" / "apps" / "conduit" / "qa" / "login.spec.md"
        spec.write_text(spec_text("sign-in"))
        self.assert_error(
            self.violation_in_login(),
            f"conduit-bug-001: expected[0]: {spec}: id: 'sign-in' doesn't match the file name",
        )

    def test_spec_file_is_read_by_the_spec_parser(self) -> None:
        # The line-based reader this replaced ignored keys it didn't know.
        spec = self.ws.root / "bench" / "apps" / "conduit" / "qa" / "login.spec.md"
        spec.write_text(spec_text("login").replace("goal:", "owner: qa\ngoal:"))
        self.assert_error(
            self.violation_in_login(),
            f"conduit-bug-001: expected[0]: {spec}: owner: unknown key",
        )

    def test_spec_file_is_read_with_its_apps_project_config(self) -> None:
        qa = self.ws.root / "bench" / "apps" / "conduit" / "qa"
        (qa / "config.yaml").unlink()
        self.assert_error(
            self.violation_in_login(),
            f"conduit-bug-001: expected[0]: {qa / 'config.yaml'}: no such file",
        )

    def test_drift_has_no_expect_indexes(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-benign-001",
                [{"spec": "login", "verdict": "drift_consistent", "expect": [0]}],
            ),
            "conduit-benign-001: expected[0]: expect is only for expectation_violated",
        )

    def test_expect_indexes_are_distinct_non_negative_integers(self) -> None:
        for bad in ([], [-1], [True], ["0"], [0, 0]):
            with self.subTest(expect=bad):
                self.assert_error(
                    self.with_expected(
                        "conduit-bug-001",
                        [
                            {
                                "spec": "login",
                                "verdict": "expectation_violated",
                                "expect": bad,
                            }
                        ],
                    ),
                    "conduit-bug-001: expected[0]: expect must be a non-empty list of distinct non-negative integers",
                )

    def test_spec_listed_once(self) -> None:
        entry = {"spec": "login", "verdict": "expectation_violated", "expect": [0]}
        self.assert_error(
            self.with_expected("conduit-bug-001", [entry, copy.deepcopy(entry)]),
            "conduit-bug-001: spec 'login' is listed twice",
        )

    def test_spec_file_must_exist(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-bug-001",
                [
                    {
                        "spec": "checkout",
                        "verdict": "expectation_violated",
                        "expect": [0],
                    }
                ],
            ),
            "conduit-bug-001: expected[0]: no spec file bench/apps/conduit/qa/checkout.spec.md",
        )

    def test_unknown_entry_key(self) -> None:
        self.assert_error(
            self.with_expected(
                "conduit-bug-001",
                [
                    {
                        "spec": "login",
                        "verdict": "expectation_violated",
                        "expect": [0],
                        "note": "x",
                    }
                ],
            ),
            "conduit-bug-001: expected[0]: unknown key 'note'",
        )


class SplitHashTest(unittest.TestCase):
    def test_empty_manifest_hash(self) -> None:
        expected = hashlib.sha256(b"{}").hexdigest()
        self.assertEqual(
            manifest.split_hash({"schema_version": 1, "cases": {}}), expected
        )

    def test_hash_covers_split_and_family_only(self) -> None:
        base = manifest.split_hash(valid_data())
        for field, value in (
            ("summary", "Reworded"),
            ("category", "functional"),
            ("flag", "zz99"),
        ):
            with self.subTest(field=field):
                data = valid_data()
                data["cases"]["conduit-bug-001"][field] = value
                self.assertEqual(manifest.split_hash(data), base)
        for field, value in (("split", "test"), ("family", "conduit-other")):
            with self.subTest(field=field):
                data = valid_data()
                data["cases"]["conduit-bug-001"][field] = value
                self.assertNotEqual(manifest.split_hash(data), base)

    def test_hash_ignores_key_order(self) -> None:
        data = valid_data()
        reordered = {
            "cases": dict(reversed(list(data["cases"].items()))),
            "schema_version": 1,
        }
        self.assertEqual(manifest.split_hash(reordered), manifest.split_hash(data))

    def test_hash_is_canonical_json_of_split_assignment(self) -> None:
        canonical = json.dumps(
            {
                "conduit-benign-001": ["dev", "conduit-nav"],
                "conduit-bug-001": ["dev", "conduit-article-meta"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            manifest.split_hash(valid_data()),
            hashlib.sha256(canonical.encode()).hexdigest(),
        )


class CheckTest(ManifestTestCase):
    def test_valid_manifest_and_matching_hash(self) -> None:
        self.ws.write_manifest(valid_data())
        self.assertEqual(manifest.check(self.ws.root), [])

    def test_hash_mismatch(self) -> None:
        self.ws.write_manifest(valid_data(), split_hash="0" * 64)
        errors = manifest.check(self.ws.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("split assignment changed", errors[0])

    def test_missing_hash_file(self) -> None:
        self.ws.write_manifest(valid_data())
        (self.ws.root / "bench" / "manifest.v1.split.sha256").unlink()
        self.assertIn(
            "missing bench/manifest.v1.split.sha256", manifest.check(self.ws.root)[0]
        )

    def test_duplicate_case_id_in_file(self) -> None:
        path = self.ws.root / "bench" / "manifest.v1.json"
        case = json.dumps(bug_case())
        path.write_text(
            f'{{"schema_version": 1, "cases": {{"conduit-bug-001": {case}, "conduit-bug-001": {case}}}}}'
        )
        (self.ws.root / "bench" / "manifest.v1.split.sha256").write_text(
            "0" * 64 + "\n"
        )
        self.assertIn(
            "duplicate key 'conduit-bug-001'", manifest.check(self.ws.root)[0]
        )

    def test_invalid_json(self) -> None:
        (self.ws.root / "bench" / "manifest.v1.json").write_text("{")
        self.assertIn("not valid JSON", manifest.check(self.ws.root)[0])

    def test_load_raises_with_every_error(self) -> None:
        data = valid_data()
        data["cases"]["conduit-bug-001"]["split"] = "train"
        self.ws.write_manifest(data)
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.load(self.ws.root)
        self.assertTrue(any("split 'train'" in e for e in caught.exception.errors))

    def test_load_rejects_a_null_spec(self) -> None:
        data = valid_data()
        data["cases"]["conduit-bug-001"]["expected"][0]["spec"] = None
        self.ws.write_manifest(data)
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.load(self.ws.root)
        self.assertIn(
            "conduit-bug-001: expected[0]: spec must be a string",
            caught.exception.errors,
        )


class CliTest(ManifestTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = manifest.main(["--root", str(self.ws.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_check_ok(self) -> None:
        self.ws.write_manifest(valid_data())
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("2 cases", out)

    def test_check_fails(self) -> None:
        self.ws.write_manifest(valid_data(), split_hash="0" * 64)
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("split assignment changed", err)

    def test_print_split_hash(self) -> None:
        self.ws.write_manifest(valid_data(), split_hash="0" * 64)
        code, out, _ = self.run_cli("--split-hash")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), manifest.split_hash(valid_data()))


class RepoManifestTest(unittest.TestCase):
    def test_committed_manifest_is_valid_and_frozen(self) -> None:
        self.assertEqual(manifest.check(REPO), [])


if __name__ == "__main__":
    unittest.main()
