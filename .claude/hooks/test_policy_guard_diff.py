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
from collections.abc import Mapping
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
        self.assertIn("needs the maintainer's approval", result.stdout)
        approved = self.diff(**APPROVED)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertIn(rel, approved.stdout)
        self.assertIn("approved by the maintainer", approved.stdout)
        return result.stdout

    def assert_clean(self) -> None:
        result = self.diff()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def on_main(self, files: Mapping[str, str | bytes]) -> None:
        """Commit `files` to the base, so the branch starts from them."""
        self.git("switch", "-q", "main")
        for rel, content in files.items():
            if isinstance(content, bytes):
                path = self.project / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            else:
                self.put(rel, content)
        self.commit("base files")
        self.git("switch", "-q", "-C", "work")

    def link(self, rel: str, target: str) -> None:
        """Make `rel` a symbolic link to `target`, replacing any file there."""
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to(target)


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

    def test_a_committed_change_to_an_existing_file_is_judged(self) -> None:
        # CI checks out the pull request's head: HEAD and the working tree agree.
        self.on_main({"packages/core/a.py": "x = f()\n"})
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        self.commit("work")
        self.assertIn("`# type: ignore`", self.assert_refused("packages/core/a.py"))

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

    def test_untracked_files_with_non_ascii_names_are_included(self) -> None:
        self.put("packages/core/café.py", "x = f()  # noqa\n")
        self.assertIn("`# noqa`", self.assert_refused("packages/core/café.py"))

    def test_a_file_dropped_from_the_index_but_kept_on_disk_is_unchanged(self) -> None:
        # The working tree still matches the merge base (#66's blind verifier).
        self.on_main({"tests/test_a.py": TestChangeTests.TEST_A})
        self.git("rm", "-q", "--cached", "tests/test_a.py")
        self.assert_clean()

    def test_binaries_and_lockfiles_are_not_read(self) -> None:
        self.on_main({"docs/a.png": b"\x89PNG\n", "uv.lock": "version = 1\n"})
        (self.project / "docs/a.png").write_bytes(b"\x89PNG\nx = f()  # noqa\n")
        self.put("uv.lock", "version = 1\nx = f()  # noqa\n")
        self.assert_clean()

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
        # One ask per file: dropping its assertions already says it.
        self.assertEqual(output.count(f"- {self.TESTS}/test_a.py"), 1, output)

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

    # Flipping an expected outcome keeps every assertion (#60's review).
    BAR = (
        "@pytest.mark.parametrize('passes', [True, False])\n"
        "def test_cut(passes):\n    assert audit() is passes\n"
    )

    def assert_a_flipped_outcome_needs_approval(self, rel: str) -> None:
        self.on_main({rel: self.BAR})
        self.put(rel, self.BAR.replace("[True, False]", "[True, True]"))
        self.assertIn("proves the bar", self.assert_needs_approval(rel))

    def test_a_changed_bar_test_needs_approval(self) -> None:
        self.assert_a_flipped_outcome_needs_approval("tests/test_constraints.py")

    def test_a_changed_bar_test_in_a_subdirectory_needs_approval(self) -> None:
        self.assert_a_flipped_outcome_needs_approval("tests/isolation/test_runs.py")

    def test_a_bar_test_changed_only_in_non_utf8_bytes_needs_approval(self) -> None:
        # Both bytes decode to U+FFFD; the file still changed.
        rel = "tests/test_bar.py"
        self.on_main({rel: b"# coding: latin-1\nSTRICT = '\xe9' == '\xe9'\n"})
        (self.project / rel).write_bytes(
            b"# coding: latin-1\nSTRICT = '\xe9' == '\xe8'\n"
        )
        self.assertIn("proves the bar", self.assert_needs_approval(rel))

    def test_a_changed_binary_under_tests_needs_approval(self) -> None:
        # Its content isn't read, so a changed fixture is taken as rewritten.
        self.on_main({"tests/fixtures/page.png": b"\x89PNG\none\n"})
        (self.project / "tests/fixtures/page.png").write_bytes(b"\x89PNG\ntwo\n")
        self.assertIn("proves the bar", self.assert_needs_approval("tests/fixtures"))

    def test_a_case_variant_test_path_needs_approval(self) -> None:
        # On macOS's filesystem these are tests/test_constraints.py and a
        # conftest.py: a flipped outcome keeps every assertion, and a deleted
        # fixtures-only conftest drops none.
        conftest = "import pytest\n\n\n@pytest.fixture\ndef page():\n    return 1\n"
        self.on_main(
            {"Tests/test_constraints.py": self.BAR, "Tests/CONFTEST.py": conftest}
        )
        flipped = self.BAR.replace("[True, False]", "[True, True]")
        self.put("Tests/test_constraints.py", flipped)
        self.git("rm", "-q", "Tests/CONFTEST.py")
        output = self.assert_needs_approval("Tests/CONFTEST.py: this change deletes")
        self.assertIn("Tests/test_constraints.py proves the bar", output)

    def test_a_deleted_bar_test_needs_approval(self) -> None:
        self.on_main({"tests/test_constraints.py": self.BAR})
        self.git("rm", "-q", "tests/test_constraints.py")
        self.assert_needs_approval("tests/test_constraints.py")

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
        changes = {  # what: (old text, new text)
            "a lowered threshold": ("fail_under = 94", "fail_under = 80"),
            "the litellm ban dropped": (ban, ""),
            "a git source": (MEMBER, f"{MEMBER}\n{GIT_SOURCE}"),
        }
        for what, (old, new) in changes.items():
            with self.subTest(what=what):
                self.put("pyproject.toml", PYPROJECT.replace(old, new))
                output = self.assert_needs_approval("pyproject.toml")
                self.assertIn("pyproject.toml changes quality-gate settings", output)

    def test_other_pyproject_changes_need_no_approval(self) -> None:
        self.on_main({"pyproject.toml": PYPROJECT})
        version = PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"')
        self.put("pyproject.toml", version.replace(MEMBER, f"{MEMBER}\n{NEW_MEMBER}"))
        self.assert_clean()

    def test_a_constraints_md_change_needs_approval(self) -> None:
        self.on_main({"CONSTRAINTS.md": CONSTRAINTS_MD})
        self.put("CONSTRAINTS.md", CONSTRAINTS_MD.replace("≥ 94%", "≥ 90%"))
        output = self.assert_needs_approval("CONSTRAINTS.md")
        self.assertIn("CONSTRAINTS.md changes quality-gate settings", output)

    def test_pytest_config_files_need_approval(self) -> None:
        # pytest 9 reads these ahead of pyproject.toml's [tool.pytest].
        for rel in ("pytest.toml", ".pytest.toml", ".pytest.ini"):
            with self.subTest(rel=rel):
                self.put(rel, "[pytest]\naddopts = --cov-fail-under=0\n")
                self.assert_needs_approval(rel)
                (self.project / rel).unlink()

    def test_a_gate_config_file_that_appears_or_goes_needs_approval(self) -> None:
        # The tools read an empty one ahead of pyproject.toml's settings.
        for rel in ("pytest.toml", ".coveragerc", "packages/core/ruff.toml"):
            with self.subTest(rel=rel):
                self.put(rel, "")
                self.assert_needs_approval(rel)
                (self.project / rel).unlink()
        self.on_main({".coveragerc": ""})
        self.git("rm", "-q", ".coveragerc")
        self.assert_needs_approval(".coveragerc")

    def test_a_gate_file_it_does_not_read_needs_approval(self) -> None:
        (self.project / ".github/scripts").mkdir(parents=True)
        (self.project / ".github/scripts/audit.tgz").write_bytes(b"\x1f\x8b\x08\x00")
        self.assert_needs_approval(".github/scripts/audit.tgz")

    def test_an_edit_to_the_guard_or_its_settings_needs_approval(self) -> None:
        # The guard's own tests prove the bar too; they ask as part of the guard.
        guard = {
            ".claude/hooks/policy_guard.py": "RULES = 1\n",
            ".claude/hooks/test_guard.py": "def test_a():\n    assert f() == 1\n",
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

    def test_a_renovate_config_that_appears_needs_approval(self) -> None:
        # Renovate reads the first config file it finds: a new root one
        # shadows .github/renovate.json.
        self.on_main({".github/renovate.json": '{"automerge": false}\n'})
        self.put("renovate.json", '{"automerge": true}\n')
        self.assert_needs_approval("renovate.json")

    def test_a_workflow_named_like_another_gate_file_needs_approval(self) -> None:
        # Judged as a package manifest, it would have no gate lines to change.
        self.put(".github/workflows/package.json.yml", WORKFLOW_YML)
        self.assert_needs_approval(".github/workflows/package.json.yml")

    def test_a_case_variant_gate_config_that_appears_needs_approval(self) -> None:
        # On macOS's filesystem, a checkout's pytest reads it as pytest.toml.
        self.put("Pytest.toml", "[pytest]\naddopts = --cov-fail-under=0\n")
        self.assert_needs_approval("Pytest.toml")


class OutputTests(DiffTestCase):
    # Assembled at runtime, so this file holds no credential-shaped literal.
    TOKEN = "gh" + "p_" + "R8x2" * 9
    WORKFLOW = ".github/workflows/ci.yml"

    def test_a_credential_added_to_a_gate_file_is_never_printed(self) -> None:
        self.on_main({self.WORKFLOW: WORKFLOW_YML})
        self.put(
            self.WORKFLOW, WORKFLOW_YML + f"        env: {{TOKEN: {self.TOKEN}}}\n"
        )
        result = self.diff(**APPROVED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(self.WORKFLOW, result.stdout)
        self.assertNotIn(self.TOKEN, result.stdout + result.stderr)

    def test_a_credential_removed_from_a_gate_file_is_never_printed(self) -> None:
        line = f"        env: {{TOKEN: {self.TOKEN}}}\n"
        self.on_main({self.WORKFLOW: WORKFLOW_YML + line})
        self.put(self.WORKFLOW, WORKFLOW_YML)
        self.assertNotIn(self.TOKEN, self.assert_needs_approval(self.WORKFLOW))

    def test_an_unchanged_non_utf8_gate_line_is_not_a_change(self) -> None:
        # Both sides decode alike, so only the new comment differs.
        workflow = b"name: ci\non: pull_request\njobs:\n  a:\n    steps:\n      - run: echo caf\xe9\n"
        self.on_main({self.WORKFLOW: workflow})
        (self.project / self.WORKFLOW).write_bytes(workflow + b"# A comment.\n")
        self.assert_clean()

    def test_a_gate_change_lists_the_file_not_its_lines(self) -> None:
        # A credential can span lines (a key's body), so none is printed.
        begin = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2g"
        key = (
            f"        env:\n          KEY: |\n            {begin}\n            {body}\n"
        )
        self.on_main({self.WORKFLOW: WORKFLOW_YML})
        self.put(self.WORKFLOW, WORKFLOW_YML + key)
        result = self.diff(**APPROVED)
        self.assertIn(self.WORKFLOW, result.stdout)
        self.assertNotIn(body, result.stdout + result.stderr)

    def test_a_refused_line_is_shortened_after_its_credential_is_cut(self) -> None:
        # A key that the 120-character cut would split no longer matches.
        key = "AI" + "za" + "Kp4T" * 8 + "Q7wR"
        line = "x = f()  # type: ignore  # ".ljust(98, "k") + " " + key
        self.put("packages/core/a.py", line + "\n")
        output = self.assert_refused("packages/core/a.py")
        self.assertNotIn(key[4:20], output)


class LinkTests(DiffTestCase):
    def test_a_gate_file_turned_into_a_link_needs_approval(self) -> None:
        # Step one of a two-step bypass: later edits would go to a name no rule
        # watches, behind the link.
        self.on_main({"pyproject.toml": PYPROJECT})
        self.put("config/project.toml", PYPROJECT)
        self.link("pyproject.toml", "config/project.toml")
        self.assertIn("symbolic link", self.assert_needs_approval("pyproject.toml"))

    def test_a_link_is_judged_by_its_target_name_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "outside.txt"
            outside.write_text("OUTSIDE-THE-REPOSITORY\n")
            self.link(".github/scripts/peek", str(outside))
            output = self.assert_needs_approval(".github/scripts/peek")
        self.assertIn("symbolic link", output)
        self.assertNotIn("OUTSIDE-THE-REPOSITORY", output)

    def test_a_links_target_content_is_never_read(self) -> None:
        # The target holds a suppression already at the merge base; read
        # through the link, it would count as new in the link.
        self.on_main({"packages/core/real.py": "x = f()  # type: ignore\n"})
        self.link("packages/core/alias.py", "real.py")
        output = self.assert_needs_approval("packages/core/alias.py")
        self.assertIn("symbolic link", output)
        self.assertNotIn("BLOCKED", output)


class CommandLineTests(DiffTestCase):
    def test_scan_takes_an_explicit_path(self) -> None:
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.guard("--scan", str(self.project), cwd=Path(elsewhere))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("packages/core/a.py:1", result.stdout)


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
                self.assertIn("rev-parse --verify", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_a_base_with_no_history_in_common_exits_2(self) -> None:
        self.git("switch", "-q", "--orphan", "elsewhere")
        self.commit("unrelated")
        self.git("switch", "-q", "work")
        result = self.diff("elsewhere")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("merge-base", result.stderr)

    def test_no_base_exits_2(self) -> None:
        result = self.guard("--diff")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--diff <base>", result.stderr)

    def test_no_git_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            result = self.diff("main", PATH=empty)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("'git'", result.stderr)

    def test_a_directory_outside_a_repository_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            result = self.guard(
                "--diff",
                "main",
                cwd=Path(elsewhere),
                env={"GIT_CEILING_DIRECTORIES": elsewhere},
            )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("rev-parse --show-toplevel", result.stderr)

    def test_a_file_it_cannot_read_exits_2(self) -> None:
        # Judged as emptied, its new content would go unchecked. chmod can't
        # stop root from reading; CI's runner isn't root.
        self.on_main({"packages/core/a.py": "x = 1\n"})
        self.put("packages/core/a.py", "x = f()  # type: ignore\n")
        (self.project / "packages/core/a.py").chmod(0)
        result = self.diff()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("packages/core/a.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
