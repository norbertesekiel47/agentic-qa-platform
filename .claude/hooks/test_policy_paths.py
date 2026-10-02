"""Tests for how policy_guard reads paths: which kind of file a path or a shell
command names, and which paths it leaves alone. Driven, like test_policy_guard.py,
through the hook's stdin / stdout / exit-code contract.

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

HOOKS = Path(__file__).parent
HOOK = HOOKS / "policy_guard.py"


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
