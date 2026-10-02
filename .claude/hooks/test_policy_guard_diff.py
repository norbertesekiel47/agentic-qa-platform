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

    def diff(self, *args: str, **env: str) -> Result:
        """`policy_guard.py --diff`, run from the project as CI runs it."""
        return subprocess.run(
            [sys.executable, str(HOOK), "--diff", *(args or ("main",))],
            cwd=self.project,
            env={**self.environ, **env},
            input="",
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_refused(self, rel: str) -> str:
        """A refused finding fails --diff even with the maintainer's approval."""
        result = self.diff(**APPROVED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(rel, result.stdout)
        return result.stdout

    def assert_clean(self) -> None:
        result = self.diff()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")


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
        self.git("switch", "-q", "main")
        self.put("packages/core/a.py", "x = f()  # type: ignore\ny = 1\n")
        self.commit("old suppression")
        self.git("switch", "-q", "-C", "work")
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

    def test_uncommitted_changes_are_included(self) -> None:
        self.put("packages/core/a.py", "x = 1\n")
        self.commit("work")
        self.put("packages/core/a.py", "x = f()  # noqa\n")
        self.assertIn("`# noqa`", self.assert_refused("packages/core/a.py"))

    def test_changes_on_the_base_after_the_merge_base_are_not_reported(self) -> None:
        # The branch keeps a.py as it was; against main's tip, which has since
        # dropped it, the whole file would look new.
        self.git("switch", "-q", "main")
        self.put("packages/core/a.py", "x = f()  # noqa\n")
        self.commit("an old suppression")
        self.git("switch", "-q", "-C", "work")
        self.git("switch", "-q", "main")
        self.git("rm", "-q", "packages/core/a.py")
        self.commit("main moves on")
        self.git("switch", "-q", "work")
        self.assert_clean()


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
        result = subprocess.run(
            [sys.executable, str(HOOK), "--diff"],
            cwd=self.project,
            env=self.environ,
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--diff <base>", result.stderr)

    def test_no_git_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            result = self.diff("main", PATH=empty)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("main", result.stderr)

    def test_a_directory_outside_a_repository_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            result = subprocess.run(
                [sys.executable, str(HOOK), "--diff", "main"],
                cwd=elsewhere,
                env={**self.environ, "GIT_CEILING_DIRECTORIES": elsewhere},
                input="",
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
