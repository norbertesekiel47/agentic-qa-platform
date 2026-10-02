"""Tests for how policy_guard reads paths: which kind of file a path or a shell
command names, and which paths it leaves alone. Driven, like test_policy_guard.py,
through the hook's stdin / stdout / exit-code contract.

Run: python3 -m unittest discover -s .claude/hooks
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, NamedTuple

HOOKS = Path(__file__).parent
HOOK = HOOKS / "policy_guard.py"

TEST_A = "def test_a():\n    assert f(1) == 2\n    assert f(2) == 3\n"
GATE_SECTIONS = "[tool.coverage.report]\nfail_under = 94\n"
PACKAGE_JSON = '{\n  "scripts": {\n    "test": "vitest run"\n  }\n}\n'
WORKFLOW_YML = "on: push\njobs:\n  test:\n    steps:\n      - run: uv run pytest\n"

Result = subprocess.CompletedProcess[str]


class PathTestCase(unittest.TestCase):
    """A throwaway project, and a guard run as Claude Code runs it. This file
    keeps its own helpers: pytest's importlib mode can't import
    test_policy_guard.py's."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name).resolve()
        self.hook = HOOK

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def put(self, rel: str, text: str) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_guard(self, *args: str, payload: dict[str, Any] | None = None) -> Result:
        environ = {k: v for k, v in os.environ.items() if k != "AQA_POLICY_GUARD"}
        environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        return subprocess.run(
            [sys.executable, str(self.hook), *args],
            input=json.dumps(payload or {}),
            capture_output=True,
            text=True,
            env=environ,
            check=False,
        )

    def decide(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """The hook's decision on one tool call: deny, ask or allow."""
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": str(self.project),
        }
        result = self.run_guard(payload=payload)
        if result.returncode == 2:
            return "deny"
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return "allow"
        output = json.loads(result.stdout)["hookSpecificOutput"]
        return str(output["permissionDecision"])

    def bash(self, command: str) -> str:
        return self.decide("Bash", {"command": command})

    def edit(self, rel: str, old: str, new: str) -> str:
        edit = {"file_path": str(self.project / rel), "old_string": old}
        return self.decide("Edit", {**edit, "new_string": new})

    def write(self, rel: str, content: str) -> str:
        return self.decide(
            "Write", {"file_path": str(self.project / rel), "content": content}
        )

    def assert_bash(self, expected: str, *commands: str) -> None:
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), expected)


class Added(NamedTuple):
    """A shape added to one shared definition, an edit that its kind of file
    is judged on (no `before`: a Write of `new`), and a shell command naming it."""

    definition: str
    field: str
    shape: str
    rel: str
    before: str | None
    old: str
    new: str
    command: str


class SharedDefinitionTests(PathTestCase):
    ADDED = (
        Added(
            definition="GUARD_NAMES",
            field="files",
            shape=r"\.claude/zz\.json",
            rel=".claude/zz.json",
            before=None,
            old="",
            new="{}\n",
            command="rm .claude/zz.json",
        ),
        Added(
            definition="TEST_NAMES",
            field="files",
            shape=r"zz_{name}\.py",
            rel="pkg/zz_a.py",
            before=TEST_A,
            old="    assert f(2) == 3\n",
            new="",
            command="rm pkg/zz_a.py",
        ),
        Added(
            definition="GATE_WHOLE_NAMES",
            field="files",
            shape=r"zz-gate\.toml",
            rel="zz-gate.toml",
            before=None,
            old="",
            new="strict = false\n",
            command="sed -i '' s/a/b/ zz-gate.toml",
        ),
        Added(
            definition="GATE_SECTION_NAMES",
            field="files",
            shape=r"zz-sections\.toml",
            rel="zz-sections.toml",
            before=GATE_SECTIONS,
            old="fail_under = 94",
            new="fail_under = 50",
            command="sed -i '' s/94/50/ zz-sections.toml",
        ),
        Added(
            definition="PACKAGE_NAMES",
            field="files",
            shape=r"zz-package\.json",
            rel="zz-package.json",
            before=PACKAGE_JSON,
            old='"vitest run"',
            new='"true"',
            command="rm zz-package.json",
        ),
        Added(
            definition="WORKFLOW_NAMES",
            field="dirs",
            shape=r"\.zz/workflows",
            rel=".zz/workflows/ci.yml",
            before=WORKFLOW_YML,
            old="uv run pytest",
            new="true",
            command="rm -rf .zz/workflows",
        ),
    )

    def guard_with(self, added: Added) -> Path:
        """A copy of the guard with `added.shape` in its definition."""
        copies = tempfile.TemporaryDirectory()
        self.addCleanup(copies.cleanup)
        copy = Path(copies.name)
        for module in HOOKS.glob("policy_*.py"):
            shutil.copy(module, copy / module.name)
        rules = (copy / "policy_rules.py").read_text()
        start = f"{added.definition} = NameShapes("
        self.assertEqual(rules.count(start), 1, start)
        begin = rules.index(start)
        end = rules.find(" = NameShapes(", begin + len(start))
        field = f"{added.field}=("
        opening = rules.index(field, begin) + len(field)
        if end != -1:
            self.assertLess(opening, end, f"{field} isn't in {added.definition}")
        rules = rules[:opening] + f'r"{added.shape}", ' + rules[opening:]
        (copy / "policy_rules.py").write_text(rules)
        return copy / "policy_guard.py"

    def decisions(self, added: Added) -> tuple[str, str]:
        """The decisions on the edit and on the shell command."""
        if added.before is None:
            edit = self.write(added.rel, added.new)
        else:
            self.put(added.rel, added.before)
            edit = self.edit(added.rel, added.old, added.new)
        return edit, self.bash(added.command)

    def test_a_name_added_to_a_definition_is_caught_by_an_edit_and_a_shell_command(
        self,
    ) -> None:
        for added in self.ADDED:
            with self.subTest(definition=added.definition):
                # Not caught without the new shape, so the copy's catch is its.
                self.hook = HOOK
                self.assertEqual(self.decisions(added), ("allow", "allow"))
                self.hook = self.guard_with(added)
                self.assertEqual(self.decisions(added), ("ask", "ask"))


class ShellNameTests(PathTestCase):
    def test_shell_writes_to_pytest_config_files_ask(self) -> None:
        self.assert_bash(
            "ask",
            "sed -i '' s/a/b/ pytest.toml",
            "sed -i '' s/a/b/ .pytest.toml",
            "sed -i '' s/a/b/ .pytest.ini",
        )

    def test_shell_writes_to_section_gate_files_ask(self) -> None:
        self.assert_bash(
            "ask", "sed -i '' s/a/b/ setup.cfg", "sed -i '' s/a/b/ tox.ini"
        )

    def test_shell_test_names_take_any_name_character(self) -> None:
        self.assert_bash(
            "ask",
            "rm packages/a/test_a+b.py",
            "rm packages/a/test_*.py",
            "rm 'packages/a/test_[ab].py'",
            "rm {test_a,test_b}.py",
        )

    def test_shell_names_after_any_run_of_name_characters(self) -> None:
        # Whatever comes before _test.py belongs to the name, however long.
        self.assert_bash(
            "ask",
            "rm " + "*" * 256 + "_test.py",
            "rm {" + "x" * 256 + ",a}.spec.ts",
            "sed -i '' s/a/b/ " + "*" * 256 + "tsconfig.json",
        )

    def test_shell_names_a_test_directory(self) -> None:
        self.assert_bash("ask", "rm -rf apps/web/test", "rm -rf ./test")
        # A bare `test` that starts a word is the shell's test command.
        self.assert_bash("allow", "test -f x && rm -f y", "test;rm -f y")

    def test_shell_names_a_gate_directory(self) -> None:
        self.assert_bash("ask", "rm -rf .github/workflows", "rm -rf .github/scripts")

    def test_doubled_slashes_still_name_a_watched_file(self) -> None:
        self.assert_bash(
            "ask",
            "sed -i '' s/7/11/ .github//scripts/audit-lockfile.sh",
            "rm .claude//hooks/policy_guard.py",
        )

    def test_dot_steps_still_name_a_watched_file(self) -> None:
        self.assert_bash(
            "ask",
            "mkdir -p .claude/hooks-old && cp x .claude/hooks-old/../hooks/policy_guard.py",
            "rm .claude/settings.json.bak/../settings.json",
            "rm .claude/./hooks/policy_guard.py",
            "rm .github/scripts-old/../scripts/audit-lockfile.sh",
            # Quotes and substitutions beside a step don't hide it.
            "cp x '.claude/hooks-old'/../hooks/policy_guard.py",
            'cp x ".claude/settings.json.bak"/../settings.json',
            "cp x .claude/hooks-old$(true)/../hooks/policy_guard.py",
            "cp x .claude/hooks-old`true`/../hooks/policy_guard.py",
        )

    def test_quotes_inside_a_name_still_name_it(self) -> None:
        self.assert_bash("ask", "rm te''st_a.py", "rm .claude/hoo''ks/policy_guard.py")

    def test_a_variable_before_a_name_may_be_empty(self) -> None:
        self.assert_bash(
            "ask",
            "prefix=; rm $prefix.claude/hooks/policy_guard.py",
            "D=./; cp x $D.ruff.toml",
            "rm $1.claude/hooks/policy_guard.py",
            "rm $" + "A" * 80 + ".ruff.toml",
        )

    def test_a_name_attached_to_a_short_option_is_named(self) -> None:
        self.assert_bash(
            "ask",
            "wget -qO.claude/hooks/policy_guard.py https://example.com/x",
            "cp -t.claude/hooks policy_guard.py",
            "curl -o.ruff.toml https://example.com/x",
            "cp -ttests helpers.py",
            "wget -qnvcNkxEO.claude/hooks/policy_guard.py https://example.com/x",
            "cp \\-t.claude/hooks policy_guard.py",
            "cp {-t.claude/hooks,policy_guard.py}",
        )

    def test_shell_names_may_run_on_past_their_shape(self) -> None:
        self.assert_bash(
            "ask",
            "rm .claude/settings.jsonc",
            "rm .claude/settings.json5",
            "rm apps/web/__snapshots__/a.test.ts.snap",
        )

    def test_shell_keeps_asking_about_names_a_tool_may_read(self) -> None:
        self.assert_bash(
            "ask",
            "rm apps/dashboard/package.json5",
            "rm packages/a/a_test.pyc",
            "rm packages/a/conftest.pyc",
            "rm packages/a/test_a.pyi",
            "rm .claude/hooks/__pycache__/policy_rules.cpython-314.pyc",
            "rm .claude/hooks/policy_rules.pyi",
            "rm .claude/settings.json.backup",
            "rm pyproject.toml.bak2",
        )

    def test_shell_skips_a_bak_backup(self) -> None:
        # No tool reads one.
        self.assert_bash(
            "allow",
            "cp x .claude/settings.json.bak",
            "cp pyproject.toml.bak /tmp/",
            "rm -rf .claude/hooks.bak",
            "rm .eslintrc.bak",
            "rm .eslintrc.json.bak",
        )

    def test_shell_skips_a_directory_other_than_the_one_named(self) -> None:
        self.assert_bash("allow", "rm -rf .claude/hooks-old", "rm -rf my.claude/hooks")

    def test_shell_skips_a_name_another_runs_into(self) -> None:
        self.assert_bash("allow", "rm x.ruff.toml", "rm x.pyproject.toml")

    def test_tsconfig_variants_with_a_prefix_are_gate_config(self) -> None:
        # A tsconfig's `extends` can read any of them, as it reads
        # tsconfig.base.json.
        self.assert_bash("ask", "rm base.tsconfig.json")
        self.assertEqual(
            self.write("apps/web/base.tsconfig.json", '{"strict": false}\n'), "ask"
        )

    def test_long_commands_are_checked_quickly(self) -> None:
        # Well inside the hook's 10 s timeout, after which it fails open.
        runs = ("*", "a*", "/", "test_", "*test_", "x_tes", "tsconfig", "a/../")
        for run in runs:
            with self.subTest(run=run):
                start = time.perf_counter()
                decision = self.bash("rm " + run * (100_000 // len(run)))
                self.assertLess(time.perf_counter() - start, 5.0)
                self.assertIn(decision, {"allow", "ask"})


class EditNameTests(PathTestCase):
    def test_renovate_config_changes_ask(self) -> None:
        # Renovate reads the first of its config files it finds, so a new root
        # renovate.json would shadow .github/renovate.json.
        self.put(".github/renovate.json", '{"automerge": false}\n')
        self.assertEqual(self.edit(".github/renovate.json", "false", "true"), "ask")
        names = (
            "renovate.json",
            "renovate.jsonc",
            "renovate.json5",
            ".github/renovate.json",
            ".github/renovate.jsonc",
            ".github/renovate.json5",
            ".gitlab/renovate.json",
            ".gitlab/renovate.jsonc",
            ".gitlab/renovate.json5",
            ".renovaterc",
            ".renovaterc.json",
            ".renovaterc.jsonc",
            ".renovaterc.json5",
        )
        for rel in names:
            with self.subTest(rel=rel):
                self.assertEqual(self.write(rel, '{"automerge": true}\n'), "ask")
                self.assert_bash("ask", f"rm {rel}")

    def test_gitleaksignore_changes_ask(self) -> None:
        # An entry there passes the secret scan.
        entry = "src/a.py:generic-api-key:1\n"
        self.assertEqual(self.write(".gitleaksignore", entry), "ask")
        self.assert_bash("ask", f"echo '{entry.strip()}' >> .gitleaksignore")

    def test_section_gate_file_changes_ask(self) -> None:
        for rel, section in (("setup.cfg", "tool:pytest"), ("tox.ini", "pytest")):
            with self.subTest(rel=rel):
                self.put(rel, f"[{section}]\naddopts = -x\n")
                self.assertEqual(self.edit(rel, "-x", "-x -p no:warnings"), "ask")

    def test_nested_ci_scripts_are_gate_config(self) -> None:
        rel = ".github/scripts/lib/helper.sh"
        self.put(rel, "#!/usr/bin/env bash\nexit 0\n")
        self.assertEqual(self.edit(rel, "exit 0", "exit 1"), "ask")

    def test_a_workflow_named_like_another_gate_file_is_a_workflow(self) -> None:
        for name in ("package.json.yml", "setup.cfg.yaml", "pyproject.toml-lint.yml"):
            with self.subTest(name=name):
                rel = f".github/workflows/{name}"
                self.assertEqual(self.write(rel, WORKFLOW_YML), "ask")

    def test_other_files_in_the_workflows_directory_are_not_workflows(self) -> None:
        # GitHub reads only the YAML files directly in it.
        self.put(".github/workflows/README.md", "The gates.\n")
        self.assertEqual(
            self.edit(".github/workflows/README.md", "gates", "checks"), "allow"
        )

    def test_nested_claude_directories_are_guard_files(self) -> None:
        self.assertEqual(self.write("apps/x/.claude/settings.json", "{}\n"), "ask")
        self.assertEqual(self.write("apps/x/.claude/hooks/a.py", "x = 1\n"), "ask")

    def test_a_name_that_runs_on_past_its_shape_is_judged_as_that_kind(self) -> None:
        self.put("pyproject.toml.orig", GATE_SECTIONS)
        self.assertEqual(
            self.edit("pyproject.toml.orig", "fail_under = 94", "fail_under = 50"),
            "ask",
        )
        self.put("apps/dashboard/package.json5", PACKAGE_JSON)
        self.assertEqual(
            self.edit("apps/dashboard/package.json5", '"vitest run"', '"true"'),
            "ask",
        )
        self.put("packages/a/test_a.py.orig", TEST_A)
        self.assertEqual(
            self.edit("packages/a/test_a.py.orig", "    assert f(2) == 3\n", ""),
            "ask",
        )
        self.assertEqual(self.write(".claude/settings.json.tmp", "{}\n"), "ask")

    def test_a_json5_manifest_script_with_a_bare_key_asks(self) -> None:
        rel = "apps/dashboard/package.json5"
        self.put(rel, "{\n  scripts: {\n    test: 'vitest run',\n  },\n}\n")
        self.assertEqual(self.edit(rel, "'vitest run'", "'true'"), "ask")

    def test_edits_to_a_bak_backup_need_no_approval(self) -> None:
        self.put("pyproject.toml.bak", GATE_SECTIONS)
        self.assertEqual(
            self.edit("pyproject.toml.bak", "fail_under = 94", "fail_under = 50"),
            "allow",
        )
        self.assertEqual(self.write(".claude/settings.json.bak", "{}\n"), "allow")


class TestNameScopeTests(PathTestCase):
    # A test-only rule tells a test file from any other: a retry in a test.
    RETRY = "const options = { retry: 3 }\n"

    def test_a_json_file_beside_a_spec_name_is_not_a_test_file(self) -> None:
        self.assertEqual(self.write("apps/web/a.spec.json", self.RETRY), "allow")
        self.assertEqual(self.bash("rm apps/web/a.spec.json"), "allow")
        self.assertEqual(self.write("apps/web/a.spec.ts", self.RETRY), "deny")

    def test_a_data_file_under_a_test_directory_is_not_a_test_file(self) -> None:
        self.assertEqual(self.write("tests/fixtures/data.yml", self.RETRY), "allow")
        self.assertEqual(self.write("tests/fixtures/helpers.ts", self.RETRY), "deny")


class ScopeTests(PathTestCase):
    SUPPRESSED = "x = f()  # type: ignore\n"

    def test_a_sibling_of_an_exempt_directory_is_checked(self) -> None:
        for rel in ("bench/apps-extra/a.py", ".scratchpad/a.py"):
            with self.subTest(rel=rel):
                self.assertEqual(self.write(rel, self.SUPPRESSED), "deny")

    def test_a_path_equal_to_an_exempt_directory_name_is_exempt(self) -> None:
        self.assertEqual(self.write(".scratch", self.SUPPRESSED), "allow")

    def test_a_binary_suffix_in_upper_case_is_not_read(self) -> None:
        self.put("docs/logo.PNG", self.SUPPRESSED)
        result = self.run_guard("--scan", str(self.project))
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
