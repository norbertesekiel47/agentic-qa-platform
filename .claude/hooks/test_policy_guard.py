"""Tests for policy_guard.py, driven through the real stdin / stdout / exit-code contract.

Run: python3 -m unittest discover -s .claude/hooks
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

HOOK = Path(__file__).with_name("policy_guard.py")

# Credential-shaped values are assembled at runtime so this file never holds a
# literal token that a secret scanner or push protection would flag.
FAKE_ANTHROPIC = "sk-" + "ant-" + "a1B2" * 10
FAKE_GITHUB = "gh" + "p_" + "Z9y8" * 9
FAKE_AWS_ID = "AK" + "IA" + "ABCDEFGH23456789"
FAKE_BEARER = "abcDEF1234567890ghiJKL"
FAKE_GENERIC_TOKEN = "abcdefghij" + "1234567890"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"
REMOTE_DSN = "postgresql://app:s3cretPassw0rd@db.abc.eu-west-1.rds.amazonaws.com/aqa"

PYPROJECT = """\
[project]
name = "aqa"
version = "0.1.0"

[tool.ruff.lint]
select = ["ALL"]
ignore = [
  "D203",
]

[tool.coverage.report]
fail_under = 90
"""
PYPROJECT_UV = """\
[tool.uv]
package = false
constraint-dependencies = ["litellm<0"]

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
aqa-core = { workspace = true }
"""
CONSTRAINTS_MD = """\
# Constraints

| Dimension | Threshold |
|---|---|
| Coverage, overall | ≥ 94% |
"""
PACKAGE_JSON = """\
{
  "scripts": {
    "dev": "next dev",
    "test": "vitest run"
  }
}
"""
WORKFLOW_YML = """\
name: ci
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: uv run pytest
"""

Result = subprocess.CompletedProcess[str]


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name).resolve()
        self.put("ADRs/0008-runner-image-lambda.md", "# ADR-0008\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def put(self, rel: str, text: str) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_mode(
        self, mode: str, payload: dict[str, Any] | None = None, **env: str
    ) -> Result:
        environ = {k: v for k, v in os.environ.items() if k != "AQA_POLICY_GUARD"}
        environ.update(CLAUDE_PROJECT_DIR=str(self.project), **env)
        args = [sys.executable, str(HOOK)] + ([mode] if mode else [])
        return subprocess.run(
            args,
            input=json.dumps(payload or {}),
            capture_output=True,
            text=True,
            env=environ,
            check=False,
        )

    def run_hook(
        self, tool_name: str, tool_input: dict[str, Any], **env: str
    ) -> Result:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": str(self.project),
        }
        return self.run_mode("", payload, **env)

    def edit(self, rel: str, old: str, new: str) -> Result:
        return self.run_hook(
            "Edit",
            {
                "file_path": str(self.project / rel),
                "old_string": old,
                "new_string": new,
            },
        )

    def write(self, rel: str, content: str) -> Result:
        return self.run_hook(
            "Write", {"file_path": str(self.project / rel), "content": content}
        )

    def bash(self, command: str) -> Result:
        return self.run_hook("Bash", {"command": command})

    def decision(self, result: Result) -> str:
        if result.returncode == 2:
            return "deny"
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return "allow"
        return str(
            json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
        )

    def ask_reason(self, result: Result) -> str:
        self.assert_asks(result)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        return str(output["permissionDecisionReason"])

    def assert_blocked(self, result: Result) -> None:
        self.assertEqual(self.decision(result), "deny", result.stdout)

    def assert_asks(self, result: Result) -> None:
        self.assertEqual(self.decision(result), "ask", result.stderr)

    def assert_allowed(self, result: Result) -> None:
        self.assertEqual(self.decision(result), "allow", result.stderr + result.stdout)


class SuppressionTests(GuardTestCase):
    def test_added_suppressions_are_blocked(self) -> None:
        cases = {
            "a.py": "x = f()  # type: ignore[attr-defined]",
            "b.py": "import os  # noqa: F401",
            "c.py": "# ruff: noqa",
            "d.py": "# pyright: basic",
            "e.py": "# mypy: ignore-errors",
            "f.ts": "// @ts-expect-error legacy",
            "g.tsx": "/* eslint-disable */",
            "h.py": "if x:  # pragma: no cover",
            "i.ts": "/* istanbul ignore next */",
        }
        for rel, line in cases.items():
            with self.subTest(rel=rel):
                result = self.edit(rel, "x = f()", line)
                self.assert_blocked(result)
                self.assertIn("AGENTS.md §5 rule 1", result.stderr)

    def test_touching_an_existing_suppression_is_allowed(self) -> None:
        old = "x = f()  # type: ignore[attr-defined]"
        self.assert_allowed(
            self.edit("a.py", old, "x = f(y)  # type: ignore[attr-defined]")
        )

    def test_removing_a_suppression_is_allowed(self) -> None:
        self.assert_allowed(self.edit("a.py", "x = f()  # type: ignore", "x = f()"))

    def test_edit_is_judged_on_the_whole_resulting_file(self) -> None:
        self.put("a.py", "x = f()  # type: ignore\ny = g()\n")
        self.assert_allowed(self.edit("a.py", "y = g()", "y = g(1)"))
        self.assert_blocked(self.edit("a.py", "y = g()", "y = g()  # type: ignore"))

    def test_citing_an_existing_adr_passes(self) -> None:
        new = "x = f()  # type: ignore[attr-defined]  # ADR-0008"
        self.assert_allowed(self.edit("a.py", "x = f()", new))

    def test_citing_a_missing_adr_is_blocked(self) -> None:
        self.assert_blocked(
            self.edit("a.py", "x = f()", "x = f()  # type: ignore  # ADR-0999")
        )

    def test_docs_outside_and_exempt_paths_are_out_of_scope(self) -> None:
        self.assert_allowed(self.edit("NOTES.md", "", "never add `# type: ignore`"))
        self.assert_allowed(self.edit(".scratch/try.py", "", "# noqa"))
        self.assert_allowed(
            self.edit("apps/dashboard/src/client/sdk.gen.ts", "", "// @ts-nocheck")
        )
        planted = "  // @ts-expect-error upstream\n  expect(true).toBe(true)"
        self.assert_allowed(
            self.edit("bench/apps/conduit/frontend/src/a.spec.ts", "", planted)
        )
        # Only bench/apps is third-party: the benchmark harness around it is ours.
        self.assert_blocked(
            self.edit("bench/harness/run.py", "", "x = f()  # type: ignore")
        )
        outside = {
            "file_path": "/elsewhere/a.py",
            "old_string": "",
            "new_string": "# noqa",
        }
        self.assert_allowed(self.run_hook("Edit", outside))

    def test_write_compares_against_the_file_on_disk(self) -> None:
        self.put("t.py", "@pytest.mark.skip\ndef test_a(): ...\n")
        rewrite = "@pytest.mark.skip\ndef test_a(): ...\ndef test_b(): ...\n"
        self.assert_allowed(self.write("t.py", rewrite))
        self.assert_blocked(
            self.write("u.py", "@pytest.mark.skip\ndef test_a(): ...\n")
        )

    def test_multiedit_is_checked(self) -> None:
        edits = [
            {"old_string": "a = 1", "new_string": "a = 2"},
            {"old_string": "b = g()", "new_string": "b = g()  # noqa"},
        ]
        tool_input = {"file_path": str(self.project / "a.py"), "edits": edits}
        self.assert_blocked(self.run_hook("MultiEdit", tool_input))


class SkippedTestTests(GuardTestCase):
    def test_skipped_focused_and_rerun_tests_are_blocked(self) -> None:
        lines = [
            "@pytest.mark.skipif(sys.platform == 'win32', reason='x')",
            "pytestmark = pytest.mark.xfail",
            "    pytest.skip('flaky')",
            "pytest.importorskip('boto3')",
            "@unittest.skip('later')",
            "it.skip('renders', () => {})",
            "describe.only('suite', () => {})",
            "test.fixme('x', async () => {})",
            "it.skipIf(isCI)('x', () => {})",
            "xit('x', () => {})",
            "@pytest.mark.flaky(reruns=3)",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assert_blocked(self.edit("t.py", "", line))

    def test_retry_is_blocked_in_tests_only(self) -> None:
        self.assert_blocked(
            self.edit("tests/a.test.ts", "", "test('x', { retry: 3 }, () => {})")
        )
        self.assert_allowed(
            self.edit("apps/web/fetch.ts", "", "const opts = { retry: 3 }")
        )

    def test_lookalikes_are_allowed(self) -> None:
        lines = [
            "model.fit(X, y)",
            "test.each([1, 2])('x', () => {})",
            "commit.skip_ci = True",
            "skip = True",
            "it('works', () => {})",
            "browser = pw.chromium.launch(chromium_sandbox=True)",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assert_allowed(self.edit("t.py", "", line))


class AssertionTests(GuardTestCase):
    TEST_A = "def test_a():\n    assert f(1) == 2\n    assert f(2) == 3\n"

    def test_tautological_assertions_are_blocked(self) -> None:
        cases = {
            "tests/test_b.py": "    assert True",
            "tests/test_c.py": "    assert result == result",
            "tests/test_d.py": "        self.assertTrue(True)",
            "apps/web/a.test.ts": "  expect(true).toBe(true)",
        }
        for rel, line in cases.items():
            with self.subTest(rel=rel):
                self.assert_blocked(self.edit(rel, "", line))

    def test_real_assertions_are_allowed(self) -> None:
        self.assert_allowed(
            self.edit("tests/test_b.py", "", "    assert result == expected")
        )
        self.assert_allowed(self.edit("tests/test_b.py", "", "    assert True_value"))

    def test_dropping_assertions_asks(self) -> None:
        self.put("tests/test_a.py", self.TEST_A)
        result = self.edit("tests/test_a.py", "    assert f(2) == 3\n", "")
        self.assert_asks(result)
        self.assertIn("1 assertion(s) where there were 2", result.stdout)

    def test_adding_assertions_is_allowed(self) -> None:
        self.put("tests/test_a.py", self.TEST_A)
        added = "    assert f(2) == 3\n    assert f(3) == 4\n"
        self.assert_allowed(
            self.edit("tests/test_a.py", "    assert f(2) == 3\n", added)
        )


class SandboxTests(GuardTestCase):
    def test_disabling_the_sandbox_is_blocked(self) -> None:
        cases = {
            "runner.py": "browser = pw.chromium.launch(chromium_sandbox=False)",
            "launch.ts": "chromium.launch({ chromiumSandbox: false })",
            "cfg.json": '{"chromium_sandbox": false}',
            "ci.yml": "      args: [--disable-setuid-sandbox]",
            "run.sh": "chrome --headless --no-sandbox about:blank",
        }
        for rel, line in cases.items():
            with self.subTest(rel=rel):
                result = self.edit(rel, "", line)
                self.assert_blocked(result)
                self.assertIn("AGENTS.md §6", result.stderr)

    def test_sandbox_flag_citing_an_adr_passes(self) -> None:
        line = "args = ['--no-sandbox']  # single-tenant CI fallback, ADR-0008"
        self.assert_allowed(self.edit("runner.py", "", line))


class StubTests(GuardTestCase):
    def test_added_stubs_are_blocked(self) -> None:
        cases = {
            "packages/core/src/aqa_core/replay.py": "    raise NotImplementedError",
            "packages/runner/src/aqa_runner/heal.py": "    raise NotImplementedError('M2')",
            "apps/dashboard/src/runs.ts": 'throw new Error("Not implemented");',
            "apps/dashboard/src/specs.tsx": "throw new Error('TODO: not implemented yet');",
        }
        for rel, line in cases.items():
            with self.subTest(rel=rel):
                result = self.edit(rel, "", line)
                self.assert_blocked(result)
                self.assertIn("CONSTRAINTS.md", result.stderr)

    def test_stubs_in_tests_or_citing_an_adr_pass(self) -> None:
        self.assert_allowed(
            self.edit("tests/test_replay.py", "", "        raise NotImplementedError")
        )
        self.assert_allowed(
            self.edit(
                "apps/dashboard/src/a.test.ts", "", 'throw new Error("not implemented")'
            )
        )
        self.assert_allowed(
            self.edit(
                "packages/core/a.py", "", "    raise NotImplementedError  # ADR-0008"
            )
        )

    def test_stub_lookalikes_pass(self) -> None:
        lines = [
            "        return NotImplemented",
            "    except NotImplementedError:",
            'throw new Error("unsupported locator kind");',
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assert_allowed(self.edit("packages/core/a.py", "", line))


class GateConfigTests(GuardTestCase):
    def test_gate_sections_of_pyproject_ask(self) -> None:
        self.put("pyproject.toml", PYPROJECT)
        self.assert_asks(
            self.edit("pyproject.toml", '  "D203",\n', '  "D203",\n  "E501",\n')
        )
        result = self.edit("pyproject.toml", "fail_under = 90", "fail_under = 80")
        self.assert_asks(result)
        self.assertIn("-fail_under = 90", result.stdout)
        self.assertIn("+fail_under = 80", result.stdout)

    def test_other_pyproject_sections_pass(self) -> None:
        self.put("pyproject.toml", PYPROJECT)
        self.assert_allowed(
            self.edit("pyproject.toml", 'version = "0.1.0"', 'version = "0.2.0"')
        )

    def test_whole_file_gate_configs_ask_including_creation(self) -> None:
        self.assert_asks(
            self.write("apps/dashboard/tsconfig.json", '{"compilerOptions": {}}\n')
        )
        self.put("apps/dashboard/eslint.config.js", "export default [];\n")
        self.assert_asks(
            self.edit(
                "apps/dashboard/eslint.config.js",
                "[]",
                "[{ rules: { eqeqeq: 'off' } }]",
            )
        )
        self.assert_asks(self.write("mypy.ini", "[mypy]\nstrict = True\n"))

    def test_package_json_gate_scripts_ask(self) -> None:
        self.put("apps/dashboard/package.json", PACKAGE_JSON)
        rel = "apps/dashboard/package.json"
        self.assert_asks(
            self.edit(rel, '"vitest run"', '"vitest run --passWithNoTests"')
        )
        self.assert_allowed(self.edit(rel, '"next dev"', '"next dev --turbo"'))

    def test_workflow_gate_steps_ask(self) -> None:
        self.put(".github/workflows/ci.yml", WORKFLOW_YML)
        rel = ".github/workflows/ci.yml"
        step = "      - run: uv run pytest\n"
        self.assert_asks(
            self.edit(rel, step, step + "        continue-on-error: true\n")
        )
        self.assert_allowed(self.edit(rel, "name: ci", "name: checks"))

    def test_shell_writes_to_gate_configs_ask(self) -> None:
        self.assert_asks(self.bash("sed -i '' 's/90/50/' pyproject.toml"))
        self.assert_asks(self.bash("git restore --source=main pyproject.toml"))
        self.assert_allowed(self.bash("cat pyproject.toml"))
        self.assert_allowed(self.bash("git commit -m 'bump package.json'"))

    def test_constraints_md_changes_ask(self) -> None:
        self.put("CONSTRAINTS.md", CONSTRAINTS_MD)
        reason = self.ask_reason(self.edit("CONSTRAINTS.md", "| ≥ 94% |", "| ≥ 90% |"))
        self.assertIn("+| Coverage, overall | ≥ 90% |", reason)
        self.assert_asks(self.bash("sed -i '' 's/94/90/' CONSTRAINTS.md"))
        self.assert_asks(self.bash("git checkout main -- CONSTRAINTS.md"))
        self.assert_allowed(self.bash("cat CONSTRAINTS.md"))

    def test_uv_table_changes_ask(self) -> None:
        self.put("pyproject.toml", PYPROJECT_UV)
        ban = 'constraint-dependencies = ["litellm<0"]\n'
        reason = self.ask_reason(self.edit("pyproject.toml", ban, ""))
        self.assertIn('-constraint-dependencies = ["litellm<0"]', reason)

    def test_uv_workspace_tables_pass(self) -> None:
        self.put("pyproject.toml", PYPROJECT_UV)
        self.assert_allowed(
            self.edit("pyproject.toml", '"packages/*"]', '"packages/*", "apps/api"]')
        )
        self.assert_allowed(
            self.edit(
                "pyproject.toml",
                "aqa-core = { workspace = true }",
                "aqa-core = { workspace = true }\naqa-api = { workspace = true }",
            )
        )


class TamperTests(GuardTestCase):
    def test_editing_the_guard_or_its_settings_asks(self) -> None:
        self.assert_asks(self.edit(".claude/hooks/policy_guard.py", "a", "b"))
        self.assert_asks(self.write(".claude/settings.json", "{}"))
        env_off = '{"env": {"AQA_POLICY_GUARD": "off"}}'
        self.assert_asks(self.write(".claude/settings.local.json", env_off))

    def test_shell_writes_to_the_guard_ask(self) -> None:
        self.assert_asks(self.bash("sed -i '' 's/Stop/Nope/' .claude/settings.json"))
        self.assert_asks(self.bash("rm .claude/hooks/policy_guard.py"))

    def test_reading_and_testing_the_guard_is_allowed(self) -> None:
        self.assert_allowed(self.bash("python3 -m unittest discover -s .claude/hooks"))
        self.assert_allowed(self.bash("cat .claude/settings.json"))


class SecretTests(GuardTestCase):
    def test_literal_secrets_in_commands_are_blocked_without_being_echoed(self) -> None:
        cases = {
            f"export ANTHROPIC_API_KEY={FAKE_ANTHROPIC}": FAKE_ANTHROPIC,
            f"gh api user -H 'Authorization: token {FAKE_GITHUB}'": FAKE_GITHUB,
            f"AWS_ACCESS_KEY_ID={FAKE_AWS_ID} aws s3 ls": FAKE_AWS_ID,
            f'curl -H "Authorization: Bearer {FAKE_BEARER}" https://api.example.com': FAKE_BEARER,
            f"GITHUB_TOKEN={FAKE_GENERIC_TOKEN} gh api user": FAKE_GENERIC_TOKEN,
            f"psql {REMOTE_DSN}": "s3cretPassw0rd",
            "aqa login --api-key live_9f8e7d6c5b4a3210": "live_9f8e7d6c5b4a3210",
            f"echo '{PRIVATE_KEY}' > key.pem": "RSA PRIVATE KEY",
        }
        for command, secret in cases.items():
            with self.subTest(command=command[:40]):
                result = self.bash(command)
                self.assert_blocked(result)
                self.assertIn("AGENTS.md §5 rule 9", result.stderr)
                self.assertNotIn(secret, result.stderr)

    def test_secret_references_and_placeholders_in_commands_are_allowed(self) -> None:
        commands = [
            'curl -H "Authorization: Bearer $AQA_TOKEN" https://api.example.com',
            "GITHUB_TOKEN=$(gh auth token) gh api user",
            'export ANTHROPIC_API_KEY="$(cat .scratch/anthropic)"',
            "ANTHROPIC_API_KEY=dummy uv run pytest",
            "ANTHROPIC_API_KEY=test-key-not-real uv run pytest",
            "export ANTHROPIC_API_KEY=sk-ant-fake-" + "a" * 30,
            "TOKEN_FILE=.scratch/gh_token ./scripts/run.sh",
            "psql postgresql://postgres:postgres@localhost:5432/aqa",
            "psql postgresql://postgres:postgres@db:5432/aqa",
            "uv run pytest -k test_token_redaction",
            "pip install scikit-learn",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(self.bash(command))

    def test_credentials_written_into_files_are_blocked(self) -> None:
        cases = {
            "packages/core/settings.py": (
                f'KEY = "{FAKE_ANTHROPIC}"\n',
                FAKE_ANTHROPIC,
            ),
            "docs/setup.md": (f"export GITHUB_TOKEN={FAKE_GITHUB}\n", FAKE_GITHUB),
            "infra/app.yml": (f"url: {REMOTE_DSN}\n", "s3cretPassw0rd"),
        }
        for rel, (content, secret) in cases.items():
            with self.subTest(rel=rel):
                result = self.write(rel, content)
                self.assert_blocked(result)
                self.assertIn("AGENTS.md §5 rule 9", result.stderr)
                self.assertNotIn(secret, result.stderr)

    def test_fake_local_and_preexisting_credentials_in_files_are_allowed(self) -> None:
        fake = 'KEY = "sk-ant-fake-' + "a" * 30 + '"\n'
        self.assert_allowed(self.write("tests/fixtures/keys.py", fake))
        self.assert_allowed(self.write(".scratch/anthropic", FAKE_ANTHROPIC))
        compose = "url: postgresql://postgres:postgres@db:5432/aqa\n"
        self.assert_allowed(self.write("docker-compose.yml", compose))
        self.put("legacy.py", f'KEY = "{FAKE_ANTHROPIC}"\nx = 1\n')
        self.assert_allowed(self.edit("legacy.py", "x = 1", "x = 2"))


class ShellTests(GuardTestCase):
    def test_reads_that_mention_markers_are_allowed(self) -> None:
        commands = [
            'rg -n "# type: ignore" packages',
            'grep -rn -- "--no-sandbox" packages 2>/dev/null',
            "git commit -m 'Remove the last # noqa'",
            "git commit -m \"$(cat <<'EOF'\nfix: drop the last # type: ignore\nEOF\n)\"",
            "uv run pytest --reruns 0 -x",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(self.bash(command))

    def test_shell_writes_of_markers_are_blocked(self) -> None:
        commands = [
            "echo 'x = f()  # type: ignore' >> packages/a.py",
            "sed -i '' 's/f()$/f()  # noqa/' packages/a.py",
            "cat > tests/test_a.py <<'EOF'\n@pytest.mark.skip\ndef test_a(): ...\nEOF",
            "printf 'it.only(\"x\", () => {})' | tee apps/dashboard/a.test.ts",
            "chromium --headless --no-sandbox https://example.com",
            "uv run ruff check --add-noqa .",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assert_blocked(self.bash(command))


class SwallowedExceptionTests(GuardTestCase):
    def test_suppressing_every_exception_is_blocked(self) -> None:
        cases = {
            "packages/core/a.py": "    with contextlib.suppress(Exception):",
            "bench/harness/b.py": "    with suppress(BaseException):",
            "tests/test_c.py": "    with suppress(KeyError, Exception):",
        }
        for rel, line in cases.items():
            with self.subTest(rel=rel):
                result = self.edit(rel, "", line)
                self.assert_blocked(result)
                self.assertIn("CONSTRAINTS.md", result.stderr)

    def test_narrow_or_excused_suppress_passes(self) -> None:
        lines = [
            "    with contextlib.suppress(FileNotFoundError):",
            "    with suppress(KeyError, ExceptionGroup):",
            "    with suppress(Exception):  # plugin hooks may raise anything, ADR-0008",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assert_allowed(self.edit("packages/core/a.py", "", line))


class TestFileShellTests(GuardTestCase):
    def test_deleting_moving_or_rewriting_tests_asks(self) -> None:
        commands = [
            "rm tests/test_a.py",
            "rm -rf packages/cli/tests",
            "git rm -r packages/cli/tests",
            "mv bench/harness/test_toggle.py /tmp/",
            "sed -i '' '/assert/d' tests/test_a.py",
            ": > tests/test_a.py",
            "rm apps/dashboard/src/runs.test.ts",
            "git checkout main -- tests/test_a.py",
            "git restore --source=main packages/cli/tests",
            "git clean -fd tests/",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.bash(command)
                self.assert_asks(result)
                self.assertIn("test file", result.stdout)

    def test_reading_and_running_tests_is_allowed(self) -> None:
        commands = [
            "uv run pytest packages/cli/tests -q 2>&1 | tail -3",
            "cat tests/test_a.py",
            "grep -n assert bench/harness/test_toggle.py",
            "python3 -m unittest discover -s .claude/hooks",
            "rm -rf .pytest_cache htmlcov",
            "rm packages/core/src/aqa_core/pytest_plugin.py",
            "git checkout -b m1/60-tests-in-ci",
            "git restore --staged README.md",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assert_allowed(self.bash(command))


class TreeScanTests(GuardTestCase):
    def stop(self, **payload: Any) -> Result:
        base = {"hook_event_name": "Stop", "cwd": str(self.project)}
        return self.run_mode("--stop", {**base, **payload})

    def test_clean_tree_lets_claude_stop(self) -> None:
        self.put("packages/core/a.py", "x = 1\n")
        result = self.stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_violation_written_outside_the_edit_tools_blocks_stop(self) -> None:
        self.put("packages/core/a.py", "x = 1\ny = f()  # type: ignore\n")
        output = json.loads(self.stop().stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("packages/core/a.py:2: `# type: ignore`", output["reason"])

    def test_repeated_stop_only_warns(self) -> None:
        self.put("packages/core/a.py", "y = f()  # type: ignore\n")
        output = json.loads(self.stop(stop_hook_active=True).stdout)
        self.assertNotIn("decision", output)
        self.assertIn("packages/core/a.py:1", output["systemMessage"])

    def test_scan_skips_dependencies_generated_docs_ignored_and_excused(self) -> None:
        self.put("node_modules/pkg/index.js", "/* eslint-disable */\n")
        self.put(
            "bench/apps/conduit/frontend/src/a.spec.ts", "  expect(true).toBe(true)\n"
        )
        self.put(
            "packages/core/pb.py", "# @generated by protoc\nx = f()  # type: ignore\n"
        )
        self.put("README.md", "never add `# type: ignore`\n")
        self.put(
            "packages/core/b.py", "x = f()  # type: ignore[attr-defined]  # ADR-0008\n"
        )
        self.put(".scratch/anthropic", FAKE_ANTHROPIC)
        self.put(".gitignore", "local/\n")
        self.put("local/a.py", "x = f()  # type: ignore\n")
        result = self.run_mode("--scan")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_scan_flags_a_stub(self) -> None:
        self.put(
            "packages/core/a.py", "def f() -> int:\n    raise NotImplementedError\n"
        )
        self.put(
            "tests/test_a.py", "def fake() -> int:\n    raise NotImplementedError\n"
        )
        result = self.run_mode("--scan")
        self.assertEqual(result.returncode, 1)
        self.assertIn("packages/core/a.py:2: unimplemented stub", result.stdout)
        self.assertNotIn("tests/test_a.py", result.stdout)

    def test_scan_flags_credentials_in_docs_without_echoing_them(self) -> None:
        self.put("docs/setup.md", f"token: {FAKE_GITHUB}\n")
        result = self.run_mode("--scan")
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/setup.md:1: GitHub token", result.stdout)
        self.assertNotIn(FAKE_GITHUB, result.stdout)

    def test_escape_hatch_covers_stop(self) -> None:
        self.put("packages/core/a.py", "y = f()  # type: ignore\n")
        base = {"hook_event_name": "Stop", "cwd": str(self.project)}
        result = self.run_mode("--stop", base, AQA_POLICY_GUARD="off")
        self.assertEqual(result.stdout.strip(), "")


class FallowTests(GuardTestCase):
    def test_fallow_suppressions_are_blocked(self) -> None:
        lines = [
            "// fallow-ignore-next-line unused-export",
            "// fallow-ignore-file unused-export, code-duplication",
            "/** @expected-unused -- kept for plugins */",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assert_blocked(self.edit("apps/dashboard/src/lib.ts", "", line))

    def test_fallow_suppression_citing_an_adr_passes(self) -> None:
        line = "// fallow-ignore-next-line unused-export -- plugin API, ADR-0008"
        self.assert_allowed(self.edit("apps/dashboard/src/lib.ts", "", line))

    def test_fallow_config_changes_ask(self) -> None:
        self.assert_asks(
            self.write(".fallowrc.json", '{"rules": {"unused-exports": "off"}}\n')
        )
        self.assert_asks(
            self.write("apps/dashboard/fallow.toml", "[audit]\ngate = 'new-only'\n")
        )
        self.assert_asks(self.bash("sed -i '' 's/error/off/' .fallowrc.json"))

    def test_agent_config_installers_ask_unless_dry_run(self) -> None:
        self.assert_asks(self.bash("fallow hooks install --target agent"))
        self.assert_asks(self.bash("npx fallow agent install"))
        self.assert_asks(self.bash("fallow init --agents"))
        self.assert_allowed(self.bash("fallow hooks install --target agent --dry-run"))
        self.assert_allowed(self.bash("fallow audit --format json --quiet --explain"))
        self.assert_allowed(self.bash("fallow hooks status --format json"))

    def test_downloads_into_the_guard_ask(self) -> None:
        url = "https://raw.githubusercontent.com/fallow-rs/fallow/v3.30.0/x.sh"
        self.assert_asks(self.bash(f"curl -sfLo .claude/hooks/fallow-gate.sh {url}"))
        self.assert_asks(self.bash(f"curl -sfL --output .claude/hooks/x.sh {url}"))
        self.assert_asks(self.bash(f"wget -O .claude/settings.json {url}"))
        self.assert_allowed(self.bash(f"curl -sfL {url} | head"))


class ContractTests(GuardTestCase):
    def test_session_escape_hatch(self) -> None:
        command = {"command": f"export ANTHROPIC_API_KEY={FAKE_ANTHROPIC}"}
        self.assert_allowed(self.run_hook("Bash", command, AQA_POLICY_GUARD="off"))

    def test_unparseable_payload_fails_open(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assert_allowed(result)

    def test_internal_error_fails_open_visibly(self) -> None:
        # A malformed tool_input crashes the guard: exit 1 is non-blocking, not a refusal.
        payload = {
            "tool_name": "Bash",
            "tool_input": "not a mapping",
            "cwd": str(self.project),
        }
        result = self.run_mode("", payload)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("policy_guard.py failed open", result.stderr)
        self.assertIn("Traceback", result.stderr)

    def test_other_tools_pass_through(self) -> None:
        self.assert_allowed(
            self.run_hook("Read", {"file_path": str(self.project / "a.py")})
        )


if __name__ == "__main__":
    unittest.main()
