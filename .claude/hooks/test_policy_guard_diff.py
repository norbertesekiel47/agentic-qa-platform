"""Tests for policy_guard.py --diff, driven through its command line against
throwaway git repositories.

Run: python3 -m unittest discover -s .claude/hooks
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).with_name("policy_guard.py")
APPROVED = {"AQA_FLOOR_CHANGE_APPROVED": "true"}

MEMBER = "aqa-core = { workspace = true }"
NEW_MEMBER = "aqa-api = { workspace = true }"
GIT_SOURCE = 'litellm = { git = "https://github.com/BerriAI/litellm" }'
PYPROJECT = f"""\
[project]
name = "aqa"
version = "0.1.0"

[tool.uv]
constraint-dependencies = ["litellm<0"]

[tool.uv.sources]
{MEMBER}

[tool.coverage.report]
fail_under = 94
"""
CONSTRAINTS_MD = """\
# Constraints

| Dimension | Threshold |
|---|---|
| Coverage, overall | ≥ 94% |
"""
WORKFLOW_YML = """\
name: ci
on: pull_request
jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - run: uv sync --locked
"""

Result = subprocess.CompletedProcess[str]


class DiffTestCase(unittest.TestCase):
    """A repository whose `main` holds a base commit, checked out on `work`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name).resolve()
        # No git setting or variable of the caller's may leak in: a global
        # gpgsign or hook, or the GIT_DIR of a hook that runs these tests.
        self.environ = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "AQA_")) and key != "CLAUDE_PROJECT_DIR"
        }
        self.environ.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Guard Test",
            GIT_AUTHOR_EMAIL="guard-test@example.invalid",
            GIT_COMMITTER_NAME="Guard Test",
            GIT_COMMITTER_EMAIL="guard-test@example.invalid",
        )
        self.git("init", "-q", "-b", "main")
        self.put("ADRs/0008-runner-image-lambda.md", "# ADR-0008\n")
        self.commit("base")
        self.git("switch", "-q", "-c", "work")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.project,
            env=self.environ,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def put(self, rel: str, text: str) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message: str) -> None:
        self.git("add", "--all")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def guard(
        self, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> Result:
        """policy_guard.py with `args`, run from `cwd`: the project by default."""
        return subprocess.run(
            [sys.executable, str(HOOK), *args],
            cwd=cwd or self.project,
            env={**self.environ, **(env or {})},
            input="",
            capture_output=True,
            text=True,
            check=False,
        )

    def diff(self, base: str = "main", **env: str) -> Result:
        """`policy_guard.py --diff <base>`, run from the project as CI runs it."""
        return self.guard("--diff", base, env=env)

    def assert_refused(self, rel: str) -> str:
        """A refused finding fails --diff even with the maintainer's approval."""
        result = self.diff(**APPROVED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(rel, result.stdout)
        return result.stdout

    def assert_needs_approval(self, rel: str) -> str:
        """An approval-class finding fails --diff until the maintainer approves,
        and is still listed once approved."""
        result = self.diff()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(rel, result.stdout)
        approved = self.diff(**APPROVED)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertIn(rel, approved.stdout)
        return result.stdout

    def assert_clean(self) -> None:
        result = self.diff()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def on_main(self, files: dict[str, str]) -> None:
        """Commit `files` to the base, so the branch starts from them."""
        self.git("switch", "-q", "main")
        for rel, text in files.items():
            self.put(rel, text)
        self.commit("base files")
        self.git("switch", "-q", "-C", "work")


class RefusedTests(DiffTestCase):
    def test_a_new_suppression_is_refused(self) -> None:
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        self.commit("work")
        self.assertIn("`# type: ignore`", self.assert_refused("packages/core/a.py"))

    def test_a_new_skip_is_refused(self) -> None:
        self.put(
            "packages/core/tests/test_a.py", "@pytest.mark.skip\ndef test_a(): ...\n"
        )
        self.commit("work")
        output = self.assert_refused("packages/core/tests/test_a.py")
        self.assertIn("pytest skip/xfail", output)

    def test_a_new_tautology_is_refused(self) -> None:
        self.put("packages/core/tests/test_a.py", "def test_a():\n    assert True\n")
        self.commit("work")
        output = self.assert_refused("packages/core/tests/test_a.py")
        self.assertIn("tautological assertion", output)

    def test_a_new_stub_outside_tests_is_refused(self) -> None:
        self.put(
            "packages/core/a.py", "def f() -> int:\n    raise NotImplementedError\n"
        )
        self.put(
            "packages/core/tests/test_a.py",
            "def fake() -> int:\n    raise NotImplementedError\n",
        )
        self.commit("work")
        output = self.assert_refused("packages/core/a.py")
        self.assertIn("unimplemented stub", output)
        self.assertNotIn("tests/test_a.py", output)

    def test_a_new_suppress_exception_is_refused(self) -> None:
        self.put("packages/core/a.py", "with suppress(Exception):\n    f()\n")
        self.commit("work")
        self.assertIn(
            "`suppress(Exception)`", self.assert_refused("packages/core/a.py")
        )

    def test_a_new_credential_is_refused(self) -> None:
        # Assembled at runtime, so this file holds no credential-shaped literal.
        token = "gh" + "p_" + "Q7w3" * 9
        self.put("packages/core/settings.py", f'TOKEN = "{token}"\n')
        self.commit("work")
        output = self.assert_refused("packages/core/settings.py")
        self.assertIn("GitHub token", output)
        self.assertNotIn(token, output)

    def test_a_suppression_already_at_the_merge_base_is_not_new(self) -> None:
        self.on_main({"packages/core/a.py": "x = f()  # type: ignore\ny = 1\n"})
        self.put("packages/core/a.py", "x = f()  # type: ignore\ny = 2\n")
        self.commit("work")
        self.assert_clean()

    def test_a_line_citing_an_existing_adr_is_excused(self) -> None:
        self.put("packages/core/a.py", "x = f()  # type: ignore  # ADR-0008\n")
        self.commit("work")
        self.assert_clean()
        self.put("packages/core/b.py", "x = f()  # type: ignore  # ADR-0999\n")
        self.commit("cites a missing ADR")
        self.assertIn("`# type: ignore`", self.assert_refused("packages/core/b.py"))


class ScopeTests(DiffTestCase):
    def test_untracked_files_are_included(self) -> None:
        self.put("packages/core/new.py", "x = f()  # noqa\n")
        self.assertIn("`# noqa`", self.assert_refused("packages/core/new.py"))

    def test_ignored_files_are_not(self) -> None:
        self.put(".gitignore", "local/\n")
        self.commit("work")
        self.put("local/a.py", "x = f()  # noqa\n")
        self.assert_clean()

    def test_vendored_apps_are_out_of_scope(self) -> None:
        # bench/apps holds third-party code (ADR-0021).
        spec = "bench/apps/conduit/frontend/src/a.spec.ts"
        self.on_main({spec: "it('works', () => {\n  expect(f()).toBe(1)\n})\n"})
        self.git("rm", "-q", spec)
        self.put(
            "bench/apps/conduit/frontend/src/b.ts", "// @ts-expect-error upstream\n"
        )
        self.assert_clean()

    def test_a_run_from_a_subdirectory_sees_the_whole_repository(self) -> None:
        self.put("tests/test_a.py", "def test_a():\n    assert f() == 1\n")
        self.commit("work")
        self.put("packages/core/new.py", "x = f()  # noqa\n")
        result = self.guard("--diff", "main", cwd=self.project / "tests")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("packages/core/new.py", result.stdout)

    def test_uncommitted_changes_are_included(self) -> None:
        self.put("packages/core/a.py", "x = 1\n")
        self.commit("work")
        self.put("packages/core/a.py", "x = f()  # noqa\n")
        self.assertIn("`# noqa`", self.assert_refused("packages/core/a.py"))

    def test_changes_on_the_base_after_the_merge_base_are_not_reported(self) -> None:
        # The branch keeps a.py as it was; against main's tip, which has since
        # dropped it, the whole file would look new.
        self.on_main({"packages/core/a.py": "x = f()  # noqa\n"})
        self.git("switch", "-q", "main")
        self.git("rm", "-q", "packages/core/a.py")
        self.commit("main moves on")
        self.git("switch", "-q", "work")
        self.assert_clean()


class TestChangeTests(DiffTestCase):
    TEST_A = "def test_a():\n    assert f(1) == 2\n    assert f(2) == 3\n"
    TESTS = "packages/core/tests"

    def test_a_deleted_test_file_needs_approval(self) -> None:
        # A conftest with fixtures only: no assertion or test drops with it.
        conftest = "import pytest\n\n\n@pytest.fixture\ndef page():\n    return 1\n"
        self.on_main({f"{self.TESTS}/test_a.py": self.TEST_A, "conftest.py": conftest})
        self.git("rm", "-q", f"{self.TESTS}/test_a.py", "conftest.py")
        self.commit("work")
        output = self.assert_needs_approval("conftest.py")
        self.assertIn(f"{self.TESTS}/test_a.py", output)

    def test_a_moved_test_file_reports_as_deleted(self) -> None:
        self.on_main({f"{self.TESTS}/test_a.py": self.TEST_A})
        self.git("mv", f"{self.TESTS}/test_a.py", f"{self.TESTS}/test_b.py")
        self.commit("work")
        self.assert_needs_approval(f"{self.TESTS}/test_a.py")

    def test_fewer_assertions_need_approval(self) -> None:
        self.on_main({f"{self.TESTS}/test_a.py": self.TEST_A})
        self.put(
            f"{self.TESTS}/test_a.py", self.TEST_A.replace("    assert f(2) == 3\n", "")
        )
        output = self.assert_needs_approval(f"{self.TESTS}/test_a.py")
        self.assertIn("1 assertion(s) where there were 2", output)

    def test_a_test_renamed_away_needs_approval(self) -> None:
        self.on_main({f"{self.TESTS}/test_a.py": self.TEST_A})
        self.put(
            f"{self.TESTS}/test_a.py", self.TEST_A.replace("def test_a", "def _test_a")
        )
        output = self.assert_needs_approval(f"{self.TESTS}/test_a.py")
        self.assertIn("0 test(s) where there were 1", output)

    def test_any_change_to_a_bar_test_needs_approval(self) -> None:
        # Flipping an expected outcome keeps every assertion (#60's review).
        bar = "@pytest.mark.parametrize('passes', [True, False])\ndef test_cut(passes):\n    assert audit() is passes\n"
        bar_tests = {
            "tests/test_constraints.py": bar,
            ".claude/hooks/test_guard.py": bar,
        }
        self.on_main(bar_tests)
        for rel, text in bar_tests.items():
            with self.subTest(rel=rel):
                self.put(rel, text.replace("[True, False]", "[True, True]"))
                self.assertIn("proves the bar", self.assert_needs_approval(rel))
                self.git("rm", "-qf", rel)
                self.assert_needs_approval(rel)
                self.put(rel, text)

    def test_a_new_bar_test_needs_no_approval(self) -> None:
        # Adding a test can't lower the floor.
        self.put("tests/test_spec_urls.py", self.TEST_A)
        self.assert_clean()

    def test_more_tests_and_assertions_need_no_approval(self) -> None:
        self.on_main({f"{self.TESTS}/test_a.py": self.TEST_A})
        self.put(f"{self.TESTS}/test_a.py", self.TEST_A + "    assert f(3) == 4\n")
        self.put(f"{self.TESTS}/test_b.py", self.TEST_A)
        self.assert_clean()


class GateTests(DiffTestCase):
    def test_a_gate_config_change_needs_approval(self) -> None:
        self.on_main({"pyproject.toml": PYPROJECT})
        ban = 'constraint-dependencies = ["litellm<0"]'
        changes = {  # what: (old text, new text, the diff line reported)
            "a lowered threshold": (
                "fail_under = 94",
                "fail_under = 80",
                "+fail_under = 80",
            ),
            "the litellm ban dropped": (ban, "", f"-{ban}"),
            "a git source": (MEMBER, f"{MEMBER}\n{GIT_SOURCE}", f"+{GIT_SOURCE}"),
        }
        for what, (old, new, shown) in changes.items():
            with self.subTest(what=what):
                self.put("pyproject.toml", PYPROJECT.replace(old, new))
                self.assertIn(shown, self.assert_needs_approval("pyproject.toml"))

    def test_other_pyproject_changes_need_no_approval(self) -> None:
        self.on_main({"pyproject.toml": PYPROJECT})
        version = PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"')
        self.put("pyproject.toml", version.replace(MEMBER, f"{MEMBER}\n{NEW_MEMBER}"))
        self.assert_clean()

    def test_a_constraints_md_change_needs_approval(self) -> None:
        self.on_main({"CONSTRAINTS.md": CONSTRAINTS_MD})
        self.put("CONSTRAINTS.md", CONSTRAINTS_MD.replace("≥ 94%", "≥ 90%"))
        self.assertIn("≥ 90%", self.assert_needs_approval("CONSTRAINTS.md"))

    def test_an_edit_to_the_guard_or_its_settings_needs_approval(self) -> None:
        guard = {
            ".claude/hooks/policy_guard.py": "RULES = 1\n",
            ".claude/settings.json": '{"hooks": {}}\n',
        }
        self.on_main(guard)
        for rel, text in guard.items():
            with self.subTest(rel=rel):
                self.put(rel, text + "\n")
                self.assert_needs_approval(rel)
                self.put(rel, text)

    def test_workflow_ci_script_and_waiver_changes_need_approval(self) -> None:
        workflow = ".github/workflows/ci.yml"
        script = ".github/scripts/audit-lockfile.sh"
        self.on_main({workflow: WORKFLOW_YML, script: "jq -e 'all(.score < 7)'\n"})
        step = "      - run: uv sync --locked\n"
        self.put(workflow, WORKFLOW_YML.replace(step, "      - run: uv sync\n"))
        self.assert_needs_approval(workflow)
        self.put(workflow, WORKFLOW_YML.replace("name: ci", "name: checks"))
        self.assert_clean()
        self.put(workflow, WORKFLOW_YML.replace("jobs:\n", "# The gates.\njobs:\n"))
        self.assert_clean()
        self.put(workflow, WORKFLOW_YML)
        self.put(script, "jq -e 'all(.score < 11)'\n")
        self.assert_needs_approval(script)
        self.put(script, "jq -e 'all(.score < 7)'\n")
        self.put("osv-scanner.toml", '[[IgnoredVulns]]\nid = "GHSA-fake-0000-0000"\n')
        self.assert_needs_approval("osv-scanner.toml")


class ApprovalTests(DiffTestCase):
    def test_approval_lets_approval_class_findings_pass(self) -> None:
        self.on_main({"CONSTRAINTS.md": CONSTRAINTS_MD})
        self.put("CONSTRAINTS.md", CONSTRAINTS_MD.replace("≥ 94%", "≥ 90%"))
        for value in ("false", "1", "True"):
            with self.subTest(value=value):
                result = self.diff(AQA_FLOOR_CHANGE_APPROVED=value)
                self.assertEqual(result.returncode, 1, result.stdout)
        approved = self.diff(**APPROVED)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertIn("CONSTRAINTS.md", approved.stdout)

    def test_approval_never_excuses_a_refused_finding(self) -> None:
        self.on_main({"CONSTRAINTS.md": CONSTRAINTS_MD})
        self.put("CONSTRAINTS.md", CONSTRAINTS_MD.replace("≥ 94%", "≥ 90%"))
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        result = self.diff(**APPROVED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("packages/core/a.py", result.stdout)
        self.assertIn("CONSTRAINTS.md", result.stdout)


class ExitCodeTests(DiffTestCase):
    def test_a_clean_diff_exits_0(self) -> None:
        self.put("packages/core/a.py", "def f() -> int:\n    return 1\n")
        self.put(
            "packages/core/tests/test_a.py",
            "def test_f():\n    assert f() == 1\n",
        )
        self.put("docs/notes.md", "Never add `# type: ignore` to get green.\n")
        self.commit("work")
        self.put("packages/core/b.py", "y = 2\n")
        self.assert_clean()

    def test_findings_exit_1(self) -> None:
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        self.commit("work")
        result = self.diff()
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_the_escape_hatch_leaves_diff_on(self) -> None:
        # AQA_POLICY_GUARD=off silences the hooks, not a check someone runs.
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        result = self.diff(AQA_POLICY_GUARD="off")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("packages/core/a.py", result.stdout)

    def test_a_base_that_does_not_resolve_exits_2(self) -> None:
        for base in ("no-such-branch", "--output=x", "HEAD~5"):
            with self.subTest(base=base):
                result = self.diff(base)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(base, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_a_base_with_no_history_in_common_exits_2(self) -> None:
        self.git("switch", "-q", "--orphan", "elsewhere")
        self.commit("unrelated")
        self.git("switch", "-q", "work")
        result = self.diff("elsewhere")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("elsewhere", result.stderr)

    def test_no_base_exits_2(self) -> None:
        result = self.guard("--diff")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--diff <base>", result.stderr)

    def test_no_git_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            result = self.diff("main", PATH=empty)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("main", result.stderr)

    def test_a_directory_outside_a_repository_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.guard(
                "--diff",
                "main",
                cwd=Path(elsewhere),
                env={"GIT_CEILING_DIRECTORIES": elsewhere},
            )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_a_file_it_cannot_read_exits_2(self) -> None:
        # Judged as emptied, its new content would go unchecked.
        self.on_main({"packages/core/a.py": "x = 1\n"})
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        (self.project / "packages/core/a.py").chmod(0)
        result = self.diff()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("packages/core/a.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
