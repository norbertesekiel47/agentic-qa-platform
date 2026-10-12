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
            ("<<A\nA\nA\n<<A\nx\nA\ny rm x", "\n\nA\n\n\ny rm x"),
            ("<<A\nA\nA\nrm x\n<<A\nx\nA", "\n\nA\nrm x\n\n"),
            ("<<AXB\nx\nAB\nrm x", "<<AXB\nx\nAB\nrm x"),
            ("<<'EOF", "<<'EOF"),
            ('<<"EOF', '<<"EOF'),
            ("x <<'A", "x <<'A"),
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
            ("<<A\nA\n" * n, "\n\n" * n),
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

    def test_message_back_off_holds_with_eight_or_nine_closing_lengths(self) -> None:
        cases = (
            ("<<ABCDEFGHIJ\nABC\nrm x\n", "DEFGHIJ\n\nrm x\n"),
            ("<<ABCDEFGHIJ\nAB\nABCD\nrm x\n", "EFGHIJ\n\nrm x\n"),
            ("ABCD\n<<ABCDEFGHIJ\nAB\nrm x\n", "ABCD\nCDEFGHIJ\n\nrm x\n"),
            ("<<ABCDEFGHIJ\nXYZ\nrm x\n", "<<ABCDEFGHIJ\nXYZ\nrm x\n"),
            ("<<ABCDEFGHIJ\nABC\nABCDEFGHIJ\nrm x", "\n\nrm x"),
            (
                "<<ABCDEFGHIJ\nABC\n<<ABCDEFGHIJ\nAB\nrm x\n",
                "DEFGHIJ\n\nCDEFGHIJ\n\nrm x\n",
            ),
            ("ABCDEFGHIJ\n<<ABCDEFGHIJ\nAB\nrm x\n", "ABCDEFGHIJ\nCDEFGHIJ\n\nrm x\n"),
        )
        for lengths in (8, 9):
            decoys = "".join("Q" * k + "\n" for k in range(1, lengths + 1))
            masked = self.masked([decoys + command for command, _ in cases])
            for (command, expected), actual in zip(cases, masked, strict=True):
                with self.subTest(lengths=lengths, command=command):
                    self.assertEqual(actual, decoys + expected)

    def test_long_delimiter_backs_off_quickly_with_nine_closing_lengths(self) -> None:
        n = 100_000
        decoys = "".join("Q" * k + "\n" for k in range(1, 10))
        command = decoys + "<<" + "A" * n + "\nA"
        result = self.child(["-c", _MEASURE], json.dumps(["mask_messages", command]))
        self.assertEqual(result.returncode, 0, result.stderr)
        masked, seconds = json.loads(result.stdout)
        self.assertEqual(masked, decoys + "A" * (n - 1) + "\n")
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


_FIXTURE_ENV = {
    key: os.environ[key]
    for key in (
        "PATH",
        "HOME",
        "TMPDIR",
        "SYSTEMROOT",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "COVERAGE_PROCESS_START",
        "COVERAGE_FILE",
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "COV_CORE_BRANCH",
    )
    if key in os.environ
}
_FIXTURE_ENV["PYTHONDONTWRITEBYTECODE"] = "1"


_CONTRACT_CHECK = r"""import sys, unittest
import policy_rules as RULES

class ContractTests(unittest.TestCase):
    def test_directory(self) -> None:
        r = RULES

        for tail, expected in (("x\n", True), ("x\r\n", True), ("x\r", True),
                               ("x\n\n", False), ("x\ny", False), ("", False)):
            self.assertEqual(r.PROTECTED.search(".claude/hooks/" + tail), expected)
        for path, expected in ((".github/workflows/" * 3 + "a.yml", True),
                               (".github/workflows/a.yml\n", True),
                               (".github/workflows/a.yml\r\n", False),
                               ("tests/x\ny.py", False), ("tests/a/b.py", True)):
            self.assertEqual(r.WORKFLOW.search(path) or r.TEST_FILE.search(path), expected)
        for under, samples in ((r"[^/]+\.ya?ml", (("x.yml", True), ("x.yaml\n", True), ("x.yml\r\n", False), ("x.txt", False), ("", False))),
                               (r".+", (("x", True), ("x.bak", True), ("x\n", True), ("x\n\n", False), ("x\ny", False), ("", True))),
                               (r".*\.(?:py|[cm]?[jt]sx?)", (("x.py", True), ("x.py\n", True), ("x.py\r\n", False), ("x.txt", False)))):
            for dirs in (("a", "a/b"), ("a/b", "a")):
                for tail, wanted in samples:
                    self.assertEqual(r.path_pattern(r.NameShapes(dirs=dirs, under=under)).search("a/b/" + tail), wanted, (dirs, under, tail))
        self.assertTrue(r.path_pattern(r.NameShapes(dirs=("a", "b"))).search("b/x/a/"))
        for shape in ("", "a?", "a//b", "a.*", "a.b", "a+", "a{2}", "a[bc]", "a(?:/b)?", "a(?:/b|)", "a|a/b", "^a", r"a\/b"):
            with self.assertRaises(ValueError):
                r.path_pattern(r.NameShapes(dirs=(shape,)))
        for shape in ("", r"a(?:/b)?{name}\.py", r"z._gate\.toml", r"a.*\.py", r"a[bc]\.py", r"a|b\.py", r"{name}{name}\.py", r"a/{name}{name}\.py", r"^a\.py", r"a\/b\.py", r"{name}Tsconfig{name}\.json"):
            for folded in (True, False):
                with self.assertRaises(ValueError):
                    r.PathCheck(r.NameShapes(files=(shape,)), folded=folded)
        self.assertTrue(r.path_pattern(r.NameShapes(files=(r"q/{name}\.py",))).search("prefix/q/a.py"))
        for folded in (True, False):
            with self.assertRaises(ValueError):
                r.PathCheck(r.NameShapes(), folded=folded)
        for under in (r".*\n", r".*", r"[^/]+.*", r".+\n", r"[^/]+\.json"):
            try:
                r.path_pattern(r.NameShapes(dirs=("test",), under=under))
            except ValueError:
                continue
            self.fail(f"accepted unsupported under: {under}")
        self.assertTrue(r.path_pattern(r.NameShapes(files=("x",), under="unused")).search("x"))

    def test_shell(self) -> None:
        r = RULES

        for width in (764, 765, 766):
            for letter in ("x", "ﬃ", "ΐ"):
                padding = letter * (width // len(letter.casefold())) + "x" * (width % len(letter.casefold()))
                self.assertEqual(len(padding.casefold()), width)
                self.assertEqual(len(padding), width // len(letter.casefold()) + width % len(letter.casefold()))
                for prefix in ("test_", "$Dtest_", "$1test_", "$Dtest_test_", "-otest_", "+test_"):
                    self.assertEqual(r.TEST_PATH_IN_SHELL.search(prefix + padding + ".py"), width <= 765)
                self.assertEqual(r.GATE_FILE_IN_SHELL.search("tsconfig" + padding + ".json"), width <= 765)
        for width in (254, 255, 256):
            for letter in ("x", "ﬃ", "ΐ"):
                padding = letter * (width // len(letter.casefold())) + "x" * (width % len(letter.casefold()))
                self.assertEqual(len(padding.casefold()), width)
                self.assertEqual(len(padding), width // len(letter.casefold()) + width % len(letter.casefold()))
                for name, prefix, end in (("TEST_PATH_IN_SHELL", "test_a", ".py"), ("TEST_PATH_IN_SHELL", "$Dtest_a", ".py"), ("TEST_PATH_IN_SHELL", "$1test_a", ".py"), ("TEST_PATH_IN_SHELL", "-otest_a", ".py"), ("TEST_PATH_IN_SHELL", "+-otest_a", ".py"), ("TEST_PATH_IN_SHELL", "+test_a", ".py"), ("TEST_PATH_IN_SHELL", "$Dtest_test_a", ".py"),
                                          ("GATE_FILE_IN_SHELL", "tsconfig", ".json"), ("GATE_FILE_IN_SHELL", "$Dtsconfig", ".json"), ("GATE_FILE_IN_SHELL", "-otsconfig", ".json")):
                    self.assertEqual(getattr(r, name).search(prefix + end + padding + ".bak"), width == 256 or letter == "ΐ", (name, prefix, width, letter))
        for name, yes, no in (("TEST_FILE", "teﬆ_a.py", "test_a.py.BAK"),
                             ("TEST_FILE_AS_TYPED", "test_a.py.bak\n", "Test_a.py"),
                             ("GATE_WHOLE_FILE", "tsconf\u0131g.json", "tsconfig.json.bak"),
                             ("GATE_WHOLE_FILE", "tsconfig.jsontsconfig", "tsconfig.jsontsconfig.bak"),
                             ("PROTECTED", ".claude/hooks/x.bak", ".claude/hooks/"),
                             ("GATE_WHOLE_FILE", ".g\u0131thub/scripts/x", "tsconfİg.json"),
                             ("TEST_PATH_IN_SHELL", "rm /test", "xtest_a.py"),
                             ("GATE_FILE_IN_SHELL", "xtsconfig.json", "test_a.py")):
            self.assertTrue(getattr(r, name).search(yes), (name, yes))
            self.assertFalse(getattr(r, name).search(no), (name, no))
        self.assertTrue(r.PROTECTED_IN_SHELL.search("-o.claude//hooks"))
        self.assertFalse(r.TEST_PATH_IN_SHELL.search("test x"))
        self.assertTrue(r.shell_pattern(r.TEST_NAMES, r.TEST_NAMES).search("$Dtest_a.py"))
        self.assertTrue(r.shell_pattern(r.NameShapes(files=(r"tsconfig{name}\.json",))).search("rm tsconfig.tsconfig.json"))

    def test_captures(self) -> None:
        r = RULES

        text = "X=TOKEN=fake-one SECRET='fake two' TOKEN=fake-three"
        actual = [(m.span(), m['name'], m['value']) for m in r.SECRET_SLOTS[0][1].finditer(text)]
        self.assertEqual(actual, [((2, 16), 'TOKEN', 'fake-one'), ((17, 34), 'SECRET', "'fake two'"),
                                  ((35, 51), 'TOKEN', 'fake-three')])
        for header, expected in (("[tool.uv]", "tool.uv"), ('[[ a  b ]] # x', 'a  b'),
                                 ('[tool."odd" ]', 'tool."odd"')):
            match = r.SECTION_HEADER.search(header)
            self.assertIsNotNone(match)
            if match is not None:
                self.assertEqual(match[1], expected)
        self.assertIsNone(r.SECTION_HEADER.search("[tool.uv] x"))
        self.assertEqual(len(list(r.ASSERTION.finditer("\n\n assert x\nself.assertTrue(x)\n"))), 2)
        self.assertEqual(len(list(r.TEST_DEF.finditer("\n async def test_a():\n it(x)"))), 2)
        for whitespace in ("\r\n", "\f", "\v", "\x85", "\u2028"):
            self.assertEqual(len(list(r.ASSERTION.finditer("\n" + whitespace + "assert x"))), 1)
            self.assertEqual(len(list(r.TEST_DEF.finditer("\n" + whitespace + "def test_a():"))), 1)
        for text in ("\n assert True\n", "assert x == x", "--disable-x-sandbox", "postgres://u:fake@localhost"):
            self.assertTrue(r.SCAN_PREFILTER.search(text), text)
        self.assertFalse(r.SCAN_PREFILTER.search("\n" * 30 + "ordinary"))

    def test_inventory(self) -> None:
        r = RULES

        cases = (
            ("_WORD", "x"), ("_QUOTING", "'"), ("PROTECTED", ".claude/hooks/x"),
            ("PROTECTED_IN_SHELL", ".claude/hooks"), ("TEST_FILE", "test_a.py"),
            ("TEST_FILE_AS_TYPED", "test_a.py"), ("TEST_PATH_IN_SHELL", "test_a.py"),
            ("GATE_WHOLE_FILE", "ruff.toml"), ("GATE_SECTION_FILE", "pyproject.toml"),
            ("PACKAGE_MANIFEST", "package.json"), ("WORKFLOW", ".github/workflows/x.yml"),
            ("GATE_FILE_IN_SHELL", "pyproject.toml"), ("ADR_REF", "ADR-0028"),
            ("FAKE_MARKER", "fake"), ("NON_SECRET_SUFFIX", "X_FILE"), ("PLACEHOLDER", "<x>"),
            ("ENV_REFERENCE", "$X"), ("DSN", "postgres://u:fake@localhost"),
            ("SECTION_HEADER", "[tool.ruff]"), ("GATE_SECTION", "tool.ruff"),
            ("WORKSPACE_MEMBER", "x = {workspace = true}"), ("PACKAGE_GATE_SCRIPT", '"test":'),
            ("WORKFLOW_FREE_LINE", "# x"), ("ASSERTION", "assert x"), ("TEST_DEF", "def test_x"),
            ("BAR_TESTS", "tests/x"), ("DIRECT_WRITE", "tee"), ("DIRECT_MUTATOR", "rm x"),
            ("DIRECT_INSTALLER", "fallow hooks install"), ("DRY_RUN", "--dry-run"),
            ("BROWSER_LAUNCH", "chromium"), ("TEXT_BODY", "git commit"),
            ("GENERATED", "@generated"), ("SCAN_PREFILTER", "# noqa"),
        )
        direct = {name for name, value in vars(r).items() if isinstance(value, (type(r._WORD), r.AnyCase, r.PathCheck))}
        self.assertEqual({name for name, _ in cases}, direct)
        for name, positive in cases:
            self.assertTrue(getattr(r, name).search(positive), name)
            negative = " " if name == "_WORD" else "ordinary"
            self.assertFalse(getattr(r, name).search(negative), name)
        content = ("# type: ignore", "# noqa", "# pyright: ignore", "# mypy: ignore-errors",
                   "@ts-ignore", "eslint-disable", "fallow-ignore", "# pragma: no cover",
                   "pytest.skip(", "unittest.skip", "it.only(", "--reruns", "retry: 1", "assert True",
                   "raise NotImplementedError", "suppress(Exception)", "--no-sandbox",
                   "--disable-x-sandbox", "chromium_sandbox=False")
        formats = ("sk-" + "fake" * 6, "sk_" + "test_fake" + "x" * 16, "whsec_" + "fake" * 6,
                   "ghp_" + "fake" * 8, "AKIA" + "FAKE" * 4, "xoxb-" + "fake" * 3,
                   "AIza" + "fake" * 8 + "xxx", "-----BEGIN FAKE PRIVATE KEY-----",
                   "eyJ" + "fake" * 3 + ".eyJ" + "fake" * 3 + "." + "fake" * 3)
        self.assertEqual(r.TEST_RULES, tuple(x for x in r.CONTENT_RULES if x.scope != "source"))
        self.assertEqual(r.SOURCE_RULES, tuple(x for x in r.CONTENT_RULES if x.scope != "tests"))
        slots = ("TOKEN=fake", "--token=fake", "authorization: fake")
        for patterns, literals in (([x.pattern for x in r.CONTENT_RULES], content),
                                   ([x[1] for x in r.SECRET_FORMATS], formats),
                                   ([x[1] for x in r.SECRET_SLOTS], slots)):
            self.assertEqual(len(patterns), len(literals))
            for pattern, literal in zip(patterns, literals, strict=True):
                self.assertIsNotNone(pattern.search(literal), literal)
                self.assertIsNone(pattern.search("ordinary"), literal)

unittest.main(argv=[sys.argv[0], sys.argv[1]])
"""

_RULE_MEASURE = r"""import json, sys, time
import policy_rules as r
name, text = json.loads(sys.stdin.read())
pattern = (r.path_pattern(r.NameShapes(dirs=('a','a/b'), under=r'[^/]+\.ya?ml')) if name == 'directory_prefix' else
           dict(r.SECRET_SLOTS)['secret-named variable'] if name == 'variable' else
           {x.label: x.pattern for x in r.CONTENT_RULES}['`--disable-*sandbox`'] if name == 'sandbox' else
           {x.label: x.pattern for x in r.CONTENT_RULES}['tautological assertion'] if name == 'tautology' else getattr(r, name))
start = time.perf_counter()
result = (len(list(pattern.finditer(text))) if name in
          ('ASSERTION', 'TEST_DEF', 'variable') else bool(pattern.search(text)))
print(json.dumps([result, time.perf_counter() - start]))
"""


class LinearPatternTests(unittest.TestCase):
    def child(
        self, args: list[str], payload: str, timeout: float = 5
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            input=payload,
            text=True,
            capture_output=True,
            cwd=HOOKS,
            env=_FIXTURE_ENV,
            timeout=timeout,
            check=False,
        )

    def contract(self, name: str) -> None:
        result = self.child(["-c", _CONTRACT_CHECK, "ContractTests.test_" + name], "")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_directory_endpoints_and_closed_under_grammar(self) -> None:
        self.contract("directory")

    def test_shell_folded_windows_prefixes_flags_and_backups(self) -> None:
        self.contract("shell")

    def test_captures_counts_and_prefilter_keep_their_contracts(self) -> None:
        self.contract("captures")

    def test_each_pattern_family_finishes_on_long_adversarial_input(self) -> None:
        cases = (
            ("sandbox", "--disable-", "sandbox", True),
            ("PLACEHOLDER", "<", ">", True),
            ("variable", "X", "TOKEN=fake", 1),
            ("SECTION_HEADER", " ", "]", True),
            ("ASSERTION", "\n", "assert x", 1),
            ("TEST_DEF", "\n", "def test_a():", 1),
            ("tautology", " ", "assert True", True),
            ("SCAN_PREFILTER", "\n", "assert True", True),
            ("PROTECTED", ".claude/hooks/", "x", True),
            ("TEST_FILE", "tests/", "x.py", True),
            ("TEST_FILE_AS_TYPED", "tests/", "x.py", True),
            ("GATE_WHOLE_FILE", "tsconfig", ".json", True),
            ("GATE_SECTION_FILE", "x/", "pyproject.toml", True),
            ("PACKAGE_MANIFEST", "x/", "package.json", True),
            ("WORKFLOW", ".github/workflows/", "a.yml", True),
            ("directory_prefix", "a/b/", "x.yml", True),
            ("PROTECTED_IN_SHELL", "$D", ".claude/hooks", True),
            ("GATE_FILE_IN_SHELL", "tsconfig", ".json", True),
        )
        for name, trigger, tail, expected in cases:
            for suffix, wanted in (
                ("x\ny" if name == "PROTECTED" else "", False),
                (tail, expected),
            ):
                text = trigger * 100_000 + suffix
                if name == "SECTION_HEADER":
                    text = "[a" + text
                with self.subTest(name=name, tail=suffix):
                    result = self.child(["-c", _RULE_MEASURE], json.dumps([name, text]))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    actual, seconds = json.loads(result.stdout)
                    self.assertEqual(actual, wanted)
                    self.assertLess(seconds, 0.5)
        for trigger in [
            "$Dtest_",
            "$1test_",
            "$Dtest_$1test_",
            "+test_",
            "+-otest_",
            "-otest_",
        ]:
            for tail in ("", ".py", ".py.bak"):
                expected = tail == ".py" and trigger != "-otest_"
                result = self.child(
                    ["-c", _RULE_MEASURE],
                    json.dumps(["TEST_PATH_IN_SHELL", trigger * 100_000 + tail]),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                actual, seconds = json.loads(result.stdout)
                self.assertEqual(actual, expected, (trigger, tail))
                self.assertLess(seconds, 0.5)
        for tail in ("", ".py", ".py.bak"):
            result = self.child(
                ["-c", _RULE_MEASURE],
                json.dumps(["TEST_PATH_IN_SHELL", "$D" + "test_" * 100_000 + tail]),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            actual, seconds = json.loads(result.stdout)
            self.assertEqual(actual, tail == ".py")
            self.assertLess(seconds, 0.5)

    def test_literal_inventory_covers_each_rule_entry(self) -> None:
        self.contract("inventory")

    def test_mixed_long_commands_and_real_write_scan_decisions(self) -> None:
        # One child per long decision, each within the hook's configured 10 s timeout.
        mixed = "git commit <<EOF\n'x' \"y\" <<A \\\n" * 100_000
        for command, wanted in (
            (mixed, "allow"),
            (mixed + "rm tests/test_a.py", "ask"),
        ):
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(HOOKS),
            }
            hook = self.child(
                [str(HOOKS / "policy_guard.py")], json.dumps(payload), timeout=10
            )
            self.assertEqual(hook.returncode, 0, hook.stderr)
            out = hook.stdout.strip()
            verdict = json.loads(out)["hookSpecificOutput"] if out else {}
            self.assertEqual(verdict.get("permissionDecision", "allow"), wanted)
        script = """import json, os, pathlib, subprocess, sys, tempfile
root = pathlib.Path.cwd()
env = dict(os.environ)
env['PYTHONDONTWRITEBYTECODE'] = '1'
with tempfile.TemporaryDirectory() as d:
 p = pathlib.Path(d)
 (p / 'AGENTS.md').write_text('disposable fixture')
 for path, old, new, wanted in [('x.py', '', '--disable-' * 100000 + 'sandbox', 'deny'), ('tests/test_a.py', 'assert x', '\\n' * 100000, 'ask'), ('pyproject.toml', '[tool.ruff]\\nx=1', '[a' + ' ' * 100000 + ']\\nx=1', 'ask'), ('x.py', '', 'X' * 100000 + 'TOKEN=fake', 'allow')]:
  target = p / path
  target.parent.mkdir(parents=True, exist_ok=True)
  target.write_text(old)
  payload = {'hook_event_name':'PreToolUse','tool_name':'Write','tool_input':{'file_path':str(target),'content':new},'cwd':d}
  child = subprocess.run([sys.executable, str(root/'policy_guard.py')], input=json.dumps(payload), text=True, capture_output=True, timeout=10, env=env)
  decision = 'deny' if child.returncode == 2 else json.loads(child.stdout)['hookSpecificOutput']['permissionDecision'] if child.stdout.strip() else 'allow'
  assert decision == wanted, (path, wanted, decision, child.stderr)
 (p/'x.py').write_text('ordinary\\n' * 100000 + '--disable-x-sandbox')
 child = subprocess.run([sys.executable, str(root/'policy_guard.py'), '--scan'], cwd=d, text=True, capture_output=True, timeout=10, env=env)
 assert child.returncode == 1 and 'sandbox' in child.stdout, (child.returncode, child.stdout, child.stderr)
"""
        result = self.child(["-c", script], "")
        self.assertEqual(result.returncode, 0, result.stderr)
