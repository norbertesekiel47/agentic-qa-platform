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

    def masked(self, commands: list[str]) -> list[str]:
        script = (
            "import json, sys\nfrom policy_commands import mask_messages\n"
            "print(json.dumps([mask_messages(c) for c in json.load(sys.stdin)]))"
        )
        result = self.child(["-c", script], json.dumps(commands))
        self.assertEqual(result.returncode, 0, result.stderr)
        return list(json.loads(result.stdout))

    def test_message_masking_preserves_quotes_and_heredocs(self) -> None:
        cases = (
            ("-m 'rm tests/a'", "-m ''"),
            ('-m "a \\"b\\" c"', "-m ''"),
            ("-m $'it\\'s' && rm x", "-m '' && rm x"),
            ("echo it\\'s && rm x", "echo it''s && rm x"),
            ('-m "a\\\nb" && rm x', '-m "a\\\nb" && rm x'),
            ("-m 'a && rm x", "-m 'a && rm x"),
            ("<<'EOF'\nfix\nEOF\nrm x", "\n\nrm x"),
            ("<<'EOF' && rm x\nmsg\nEOF", " && rm x\n"),
            ("<<'EOF'x\nmsg\nEOF\nrm x", "x\n\nrm x"),
            ('<<- "EOF"\nmsg\n\tEOF\nrm x', "\n\nrm x"),
            ("<<-EOF\nmsg\n\tEOF\nrm x", "\n\nrm x"),
            ("<<\tEOF\nmsg\nEOF\nrm x", "\n\nrm x"),
            ("<<EOF\nmsg\n \tEOF \t\nrm x", "\n\nrm x"),
            ("<<EOF\nmsg\nEOF\t", "\n"),
            ("<<EOF\nEOF\nrm x", "\n\nrm x"),
            ("<<EOF\nrm x\nEOF\r\n", "<<EOF\nrm x\nEOF\r\n"),
            ("<<EOF\nrm x\n\xa0EOF\n", "<<EOF\nrm x\n\xa0EOF\n"),
            ("<<EOF\nEOFX\nrm x\nEOF", "\n"),
            ("<<ABCDE\nA\nrm x\nABC\n", "DE\n\n"),
            ("<<ABC\nAB\nrm x\n", "C\n\nrm x\n"),
            ("AB\n<<ABC\nA\nrm x\n", "AB\nBC\n\nrm x\n"),
            ("A\n<<AB\nrm x\n", "A\n<<AB\nrm x\n"),
            ("<<AB\nAB\n<<AB\nA\nrm x", "\n\nB\n\nrm x"),
            ("<<'ABC'\nAB\nrm x\n", "<<''\nAB\nrm x\n"),
            ("<<'EOF\"\nmsg\nEOF\nrm x", "<<'EOF\"\nmsg\nEOF\nrm x"),
            ("<<'EOF'\nmsg", "<<''\nmsg"),
            ("<<''\nrm x\n", "<<''\nrm x\n"),
            ("<<\nrm x\n", "<<\nrm x\n"),
            ("<<EOF", "<<EOF"),
            ("<<EOF\nmsg\nrm x", "<<EOF\nmsg\nrm x"),
            ("<<EOF\na\nEOF\nrm x\nEOF\n", "\n\nrm x\nEOF\n"),
            ("<<EOF\na\nEOF\nrm x\ncat <<EOF\nb\nEOF", "\n\nrm x\ncat \n"),
            ("<<A <<B\nx\nB\nA\nrm x", " <<B\n\nrm x"),
            ("<<A <<B\nx\nB\nrm x", "<<A \n\nrm x"),
            ("<<<EOF\nmsg\nEOF\nrm x", "<\n\nrm x"),
            ("\\<<EOF\nmsg\nEOF\nrm x", "''<EOF\nmsg\nEOF\nrm x"),
            ("'<<EOF'\nEOF\nrm x", "''\nEOF\nrm x"),
            ("-m \"$(cat <<'EOF'\nfix\nEOF\n)\" && rm x", "-m '' && rm x"),
            ("<<EOF\nit's\nEOF\nrm 'x'", "\n\nrm ''"),
            ("<<_X1\nmsg\n_X1\nrm x", "\n\nrm x"),
            ("<<ÉOF\nmsg\nÉOF\nrm x", "\n\nrm x"),
            ("<<EOF\nmsg\n  EOF\nrm x", "\n\nrm x"),
            ("<<A\nA\n<<B\nx\nB", "\n\n\n"),
        )
        masked = self.masked([command for command, _ in cases])
        for (command, expected), actual in zip(cases, masked, strict=True):
            with self.subTest(command=command):
                self.assertEqual(actual, expected)

    def test_message_decisions_match_the_preserved_corpus(self) -> None:
        cases = (
            ("git commit -F - <<ABCDE\nA\nrm tests/test_a.py\nABC\n", "allow"),
            ("git commit -F - <<EOF\nrm tests/test_a.py\nEOF\r\n", "ask"),
            ("git commit -F - <<EOF\nrm tests/test_a.py\nEOF\n", "allow"),
            ("git commit -F - <<'ABC'\nAB\nrm tests/test_a.py\n", "ask"),
            ("git commit -F - <<'EOF'\nrm tests/test_a.py\nEOF", "allow"),
            ("git commit -F - <<A <<B\nx\nB\nrm tests/test_a.py", "ask"),
            ("git commit -F - <<A\nrm tests/test_a.py\nA\n", "allow"),
            ("git commit -F - <<EOF\nmsg\nEOF\nrm tests/test_a.py", "ask"),
            ("git commit -F - <<EOF\nrm tests/test_a.py", "ask"),
            ("git commit -m 'x' <<EOF\nmsg\n  EOF\nsed -i pyproject.toml", "ask"),
            ("cat <<EOF\nrm tests/test_a.py\nEOF", "ask"),
            ("git commit -m 'key " + "sk-" + "ant-" + "a1B2" * 10 + "'", "deny"),
            (
                "git commit -F - <<'EOF'\n" + "sk-" + "ant-" + "a1B2" * 10 + "\nEOF",
                "deny",
            ),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(self.decision(command), expected)

    def test_message_masking_finishes_on_long_adversarial_input(self) -> None:
        n = 100_000
        opened = "cat <<EOF\n" * n
        distinct = "<<Z\n" + "".join(f"L{i}\n" for i in range(n))
        cases = (
            (opened, opened),
            (opened + "EOF", "cat \n"),
            (opened + "EOF\r\n", opened + "EOF\r\n"),
            ("<<A " * n, "<<A " * n),
            ("<<A\n" * n, "<<A\n" * n),
            ("<<" + "A" * n + "\n", "<<" + "A" * n + "\n"),
            ("<<" + "A" * n + "\nA", "A" * (n - 1) + "\n"),
            (distinct, distinct),
            ("".join(f"<<L{i}x\nL{i}\n" for i in range(n)), "x\n\n" * n),
            ('"' + '\\"' * n + "\\\n", '"' + "''" * n + "\\\n"),
            ("$'" * n, "''" * (n // 2)),
            ('"\\\n' * n, '"\\\n' * n),
            ("'" * (2 * n + 1), "'" * (2 * n + 1)),
            ("\\" * n, "''" * (n // 2)),
        )
        for index, (command, expected) in enumerate(cases):
            with self.subTest(case=index, start=command[:12]):
                result = self.child(
                    ["-c", _MEASURE], json.dumps(["mask_messages", command])
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                masked, seconds = json.loads(result.stdout)
                self.assertEqual(masked, expected)
                self.assertLess(seconds, 0.5)

    def test_long_messages_keep_their_decisions(self) -> None:
        n = 100_000
        rm = "rm tests/test_a.py"
        for command, expected in (
            ("git commit -F - " + "cat <<EOF\n" * n + rm, "ask"),
            ("git commit -F - " + "cat <<EOF\n" * n + "EOF\n" + rm, "ask"),
            ("git commit -F - <<'EOF'\n" + (rm + "\n") * n + "EOF", "allow"),
            ("git commit -F - " + "<<A " * n + "\n" + rm, "ask"),
            ("git commit -F - " + "<<A\n" * n + rm, "ask"),
            ("git commit -F - <<" + "A" * n + "\n" + rm + "\nA", "allow"),
            (
                "git commit -F - " + "".join(f"<<L{i}x\nL{i}\n" for i in range(n)) + rm,
                "ask",
            ),
            ("git commit -F - <<EOF\n" + "x\n" * n + rm + "\nEOF\r\n", "ask"),
        ):
            with self.subTest(start=command[:24], length=len(command)):
                self.assertEqual(self.decision(command), expected)
