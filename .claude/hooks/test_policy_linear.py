"""Bounded regressions for the guard's command predicates (#86)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HOOKS = Path(__file__).parent
_MEASURE = """import json, sys, time
import policy_commands
name, command = json.loads(sys.stdin.read())
predicate = getattr(policy_commands, name)
start = time.perf_counter()
matched = predicate(command)
print(json.dumps([matched, time.perf_counter() - start]))
"""


class LinearCommandTests(unittest.TestCase):
    def child(self, args: list[str], payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            input=payload,
            text=True,
            capture_output=True,
            cwd=HOOKS,
            env={
                **{k: v for k, v in os.environ.items() if k != "AQA_POLICY_GUARD"},
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            timeout=5,
            check=False,
        )

    def decision(self, command: str) -> str:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(HOOKS),
        }
        result = self.child([str(HOOKS / "policy_guard.py")], json.dumps(payload))
        if result.returncode == 2:
            return "deny"
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return "allow"
        return str(
            json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
        )

    def test_repeated_write_triggers_finish_without_an_option(self) -> None:
        for trigger in ("sed ", "perl "):
            with self.subTest(trigger=trigger):
                self.assertEqual(self.decision(trigger * 100_000), "allow")

    def test_repeated_option_commands_finish_without_an_option(self) -> None:
        for trigger in ("ruff check ", "prettier ", "eslint ", "curl ", "fallow init "):
            with self.subTest(trigger=trigger):
                self.assertEqual(self.decision(trigger * 100_000), "allow")

    def test_repeated_git_values_finish_without_a_subcommand(self) -> None:
        self.assertEqual(self.decision("git -C " * 100_000), "allow")

    def test_each_changed_predicate_finishes_on_long_adversarial_input(self) -> None:
        cases = (
            ("shell_write", "sed ", "-i"),
            ("shell_write", "perl ", "-pi"),
            ("file_mutator", "ruff check ", "--fix"),
            ("file_mutator", "prettier ", "--write"),
            ("file_mutator", "biome ", "--write"),
            ("file_mutator", "eslint ", "--fix"),
            ("file_mutator", "curl ", "-O"),
            ("file_mutator", "git -C ", "x checkout"),
            ("agent_config_installer", "fallow init ", "--agents"),
            ("add_noqa", "ruff ", "--add-noqa"),
        )
        for name, trigger, option in cases:
            for tail, expected in (("", False), (option, True), ("; " + option, False)):
                with self.subTest(name=name, trigger=trigger, tail=tail):
                    payload = json.dumps([name, trigger * 100_000 + tail])
                    result = self.child(["-c", _MEASURE], payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    matched, seconds = json.loads(result.stdout)
                    self.assertEqual(matched, expected)
                    self.assertLess(seconds, 0.5)

    def test_command_classifications_match_the_preserved_corpus(self) -> None:
        cases = (
            ("sed\n-i tests/test_a.py", "ask"),
            ("sed; -i tests/test_a.py", "allow"),
            ("-i sed; sed -i tests/test_a.py", "ask"),
            ("--fix ruff check tests/test_a.py", "allow"),
            ("sed -ix tests/test_a.py", "allow"),
            ("sed -xi tests/test_a.py", "ask"),
            ("perl -pix tests/test_a.py", "ask"),
            ("ruff\ncheck --fix-more tests/test_a.py", "ask"),
            ("prettier; --write tests/test_a.py", "allow"),
            ("biome --write-more tests/test_a.py", "ask"),
            ("curl\n-O tests/test_a.py", "ask"),
            ("curl -oOUT tests/test_a.py", "allow"),
            ("ruff\n--add-noqa", "deny"),
            ("ruff; --add-noqa", "allow"),
            ("fallow\ninit --agents", "ask"),
            ("fallow init --agents-extra --dry-run", "allow"),
            ("fallow hooks uninstall", "ask"),
            ("fallow hooks uninstall --dry-run", "allow"),
            ("fallow agent uninstall", "ask"),
            ("fallow agent uninstall --dry-run", "allow"),
            ("fallow setup-hooks", "ask"),
            ("fallow setup-hooks --dry-run", "allow"),
            ("git -c a=b;z checkout tests/test_a.py", "ask"),
            ("git -C checkout tests/test_a.py", "allow"),
            ("git --git-dir=x checkout tests/test_a.py", "ask"),
            ("git -é restore tests/test_a.py", "ask"),
            ("git -C git -c x --work-tree y clean tests/test_a.py", "ask"),
            ("git checkouté tests/test_a.py", "allow"),
            ("sed -i pyproject.toml", "ask"),
            ("sed -i .claude/settings.json", "ask"),
            ("sed -i '# noqa' ordinary.py", "deny"),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(self.decision(command), expected)

    def test_long_commands_keep_their_decisions(self) -> None:
        for trigger, tail, expected in (
            ("sed ", "-i tests/test_a.py", "ask"),
            ("curl ", "-O pyproject.toml", "ask"),
            ("ruff ", "--add-noqa", "deny"),
            ("fallow init ", "--hooks", "ask"),
            ("git -C ", "x checkout tests/test_a.py", "ask"),
        ):
            with self.subTest(trigger=trigger):
                self.assertEqual(self.decision(trigger * 100_000 + tail), expected)
