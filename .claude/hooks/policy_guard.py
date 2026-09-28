#!/usr/bin/env python3
"""Guard for the AGENTS.md rules that get bent under pressure to go green.

Three entry points, one rule set:

* PreToolUse (default): checks each Bash / Edit / Write / NotebookEdit call
  before it runs. Clear violations are refused; changes that need judgment are
  escalated to the user as a permission prompt.
* ``--stop`` (Stop hook): scans the working tree when Claude ends a turn, to
  catch what the per-call check cannot see: files written by scripts,
  formatters, other agents or people.
* ``--scan`` (CLI, for CI or pre-commit): the same scan; exit 1 on violations.

Refused
-------
1. Silenced checks (AGENTS.md §5 rule 1): newly added inline suppressions
   (``type: ignore``, ``noqa``, ``pyright:`` / ``mypy:`` pragmas, ``@ts-ignore``,
   ``@ts-expect-error``, ``@ts-nocheck``, ``eslint-disable*``, ``fallow-ignore*``,
   ``@expected-unused``), coverage pragmas,
   skipped / focused / rerun-until-green tests, tautological assertions
   (``assert True``, ``expect(true).toBe(true)``), and ``ruff --add-noqa``.
2. Disabling the Chromium sandbox (AGENTS.md §6).
3. Secrets (AGENTS.md §5 rule 9): literal secrets in shell commands (known
   formats, secret-named variables and flags, auth headers, remote database
   URLs with a password) and known-format credentials written into any file,
   docs included.

Escalated to the user
---------------------
4. Any change to a quality-gate config: ruff / mypy / pytest / coverage /
   pyright settings, tsconfig, eslint, vitest and fallow configs, pre-commit, gate
   scripts in package.json, gate steps in CI workflows. Tightening prompts too:
   the bar moves only with a human in the loop. Creating one of these files
   prompts once, which is how the initial bar gets approved.
5. A test-file edit that leaves fewer assertions than before.
6. Edits to this guard or the settings that load it (``.claude/hooks/``,
   ``.claude/settings*.json``), including shell writes that name them and
   installers that rewrite them without naming them (``fallow hooks install``,
   ``fallow agent install``; ``--dry-run`` is fine).

Excuses
-------
* A line citing ``ADR-NNNN`` is excused from rules 1 and 2 when
  ``ADRs/NNNN-*.md`` exists: write the ADR first, then the suppression.
* A credential-shaped value that says it is fake (``fake``, ``dummy``,
  ``example``, ``placeholder``, ``redacted``, ``xxxxxx``) is not a secret.
* ``AQA_POLICY_GUARD=off`` in the environment Claude Code was launched from
  disables every entry point for that session.

Scope
-----
Only files inside the project, minus ``EXEMPT_DIRS``. Rules 1 and 2 skip prose
(``.md``, ``.rst``, ``.txt``, ...) because docs quote the patterns; secret
checks do not. The tree scan also skips dependency and build directories,
lockfiles, binaries and files with a generated-code header. It lists files
with ``git ls-files`` once the project is a repo, and until then walks the
tree honouring the plain (glob-free) entries of the root ``.gitignore``.

Known gaps, stated rather than hidden
-------------------------------------
* A strong assertion swapped for a weaker one (``==`` to ``in``, an exact
  matcher to ``toBeTruthy``) keeps the count level and is not detected.
* In files, only known credential formats are recognised; the generic
  ``PASSWORD=...`` detection runs on shell commands only.
* Shell writes are recognised heuristically. The Stop scan backstops rules
  1-3; rules 4-6 have no backstop for a write through an opaque script.
* This is a guardrail, not a security boundary. Other agents (Codex, Cursor)
  do not run Claude Code hooks: wire ``--scan`` into CI or pre-commit.

Contract (code.claude.com/docs/en/hooks): stdin is the hook payload as JSON.
PreToolUse: exit 2 refuses and shows stderr to the model; a JSON
``permissionDecision: "ask"`` on stdout escalates to the user; exit 0 allows.
Stop: ``{"decision": "block"}`` sends Claude back to fix the tree; on a
repeated stop it only warns, so it can never loop. An internal error exits 1
(non-blocking, shown to the user): fail open, visibly.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import TracebackType
from typing import Any

# --- scope ---------------------------------------------------------------------

# Project-relative directories that are never checked: the guard's own tests
# quote every pattern, .scratch/ is gitignored local scratch space (AGENTS.md
# rule 9), and the dashboard's API client is generated (pnpm gen:client).
EXEMPT_DIRS = (".claude/hooks", ".scratch", "apps/dashboard/src/client")

# Prose quotes the rule 1 and 2 patterns when it describes the rules.
DOC_SUFFIXES = frozenset({".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc"})

FILE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})

# Rule 6: the guard and the settings that load it.
PROTECTED = re.compile(r"^\.claude/(?:hooks/|settings(?:\.local)?\.json$)")

TEST_FILE = re.compile(
    r"(?:^|/)(?:tests?/.*\.(?:py|[cm]?[jt]sx?)"
    r"|test_[^/]*\.py|[^/]*_test\.py|conftest\.py"
    r"|[^/]*\.(?:test|spec)\.[cm]?[jt]sx?)$"
)

# --- rules 1 and 2: content ------------------------------------------------------

SUPPRESSION = "suppression"
SKIPPED_TEST = "skipped test"
WEAK_ASSERTION = "weak assertion"
SANDBOX = "sandbox"

GUIDANCE = {
    SUPPRESSION: (
        "AGENTS.md §5 rule 1: never silence a check to get green. "
        "Fix the underlying error instead."
    ),
    SKIPPED_TEST: (
        "AGENTS.md §5 rule 1: no skipped, focused or rerun-until-green tests. "
        "Fix the test or the code under test instead."
    ),
    WEAK_ASSERTION: (
        "AGENTS.md §5 rule 1: no weakened assertions. "
        "Assert the behaviour the test is named for."
    ),
    SANDBOX: (
        "AGENTS.md §6: Chromium launches with its sandbox enabled "
        "(chromium_sandbox=True); never ship --no-sandbox for hosted runs."
    ),
}

ADR_HINT = (
    "If an accepted ADR genuinely justifies this, write the ADR first "
    "(ADRs/NNNN-*.md), then cite ADR-NNNN on the same line."
)


@dataclass(frozen=True)
class Rule:
    label: str
    kind: str
    pattern: re.Pattern[str]
    tests_only: bool = False


def _rule(label: str, kind: str, regex: str, *, tests_only: bool = False) -> Rule:
    return Rule(label, kind, re.compile(regex), tests_only)


CONTENT_RULES = (
    _rule("`# type: ignore`", SUPPRESSION, r"#\s*type:\s*ignore\b"),
    _rule("`# noqa`", SUPPRESSION, r"#\s*(?:[\w-]+:\s*)?noqa\b"),
    _rule("`# pyright:` pragma", SUPPRESSION, r"#\s*pyright:\s*(?:ignore|basic)\b"),
    _rule(
        "`# mypy:` pragma",
        SUPPRESSION,
        r"#\s*mypy:\s*(?:ignore-errors|disable-error-code|allow-)",
    ),
    _rule(
        "`@ts-ignore` / `@ts-expect-error` / `@ts-nocheck`",
        SUPPRESSION,
        r"@ts-(?:ignore|expect-error|nocheck)\b",
    ),
    _rule("`eslint-disable`", SUPPRESSION, r"\beslint-disable\b"),
    _rule(
        "`fallow-ignore` / `@expected-unused`",
        SUPPRESSION,
        r"\bfallow-ignore(?:-next-line|-file)?\b|@expected-unused\b",
    ),
    _rule(
        "coverage pragma",
        SUPPRESSION,
        r"#\s*pragma:\s*no\s*cover\b|\b(?:istanbul|c8|v8)\s+ignore\b",
    ),
    _rule(
        "pytest skip/xfail",
        SKIPPED_TEST,
        r"\bpytest\.mark\.(?:skip|skipif|xfail)\b"
        r"|\bpytest\.(?:skip|xfail|importorskip)\s*\(",
    ),
    _rule(
        "unittest skip",
        SKIPPED_TEST,
        r"\bunittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b",
    ),
    _rule(
        "skipped or focused JS test",
        SKIPPED_TEST,
        r"\b(?:describe|it|test)\.(?:skip|only|fixme|skipIf|runIf)\b"
        r"|\b(?:xit|xtest|xdescribe)\s*\(",
    ),
    _rule("flaky / rerun marker", SKIPPED_TEST, r"\bpytest\.mark\.flaky\b|--reruns\b"),
    _rule("test retry", SKIPPED_TEST, r"\bretry\s*:\s*[1-9]", tests_only=True),
    _rule(
        "tautological assertion",
        WEAK_ASSERTION,
        r"^\s*assert\s+(?:True|1|not\s+(?:False|None|0))\s*(?:[,#].*)?$"
        r"|^\s*assert\s+(?P<lhs>[\w.]+)\s*==\s*(?P=lhs)\s*(?:[,#].*)?$"
        r"|\bself\.assertTrue\(\s*True\s*\)"
        r"|\bexpect\(\s*(?:true|1)\s*\)\.(?:toBe|toEqual|toStrictEqual)\(\s*(?:true|1)\s*\)"
        r"|\bexpect\(\s*true\s*\)\.toBeTruthy\(\)"
        r"|\bexpect\(\s*false\s*\)\.toBeFalsy\(\)",
    ),
    _rule("`--no-sandbox`", SANDBOX, r"--no-sandbox\b"),
    _rule("`--disable-*sandbox`", SANDBOX, r"--disable-[\w-]*sandbox\b"),
    _rule(
        "`chromium_sandbox` disabled",
        SANDBOX,
        r"""\bchromium_?[sS]andbox["']?\s*[:=]\s*[Ff]alse\b""",
    ),
)
NON_TEST_RULES = tuple(rule for rule in CONTENT_RULES if not rule.tests_only)

ADR_REF = re.compile(r"\bADR-(\d{4})\b")

# --- rule 3: secrets ---------------------------------------------------------------

SECRET_FORMATS = (
    ("Anthropic/OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("Stripe/Clerk secret key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("webhook signing secret", re.compile(r"\bwhsec_[A-Za-z0-9+/=]{20,}")),
    (
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    ),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "JWT",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
)
# A credential-shaped value that says it is fake. No "test": sk_test_ keys are real.
FAKE_MARKER = re.compile(
    r"(?i)fake|dummy|example|placeholder|redacted|not[-_]?real|x{6,}"
)

_VALUE = r"""(?P<value>"[^"]*"|'[^']*'|[^\s;|&]+)"""
SECRET_SLOTS = (
    (
        "secret-named variable",
        re.compile(
            r"(?i)\b(?P<name>[A-Za-z0-9_]*"
            r"(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY)"
            r"[A-Za-z0-9_]*)=" + _VALUE
        ),
    ),
    (
        "secret-named flag",
        re.compile(
            r"(?i)(?<![\w-])--(?P<name>api-?key|token|auth-token|access-token|"
            r"password|passwd|secret|client-secret|secret-string|private-key)"
            r"(?:=|\s+)" + _VALUE
        ),
    ),
    (
        "auth header",
        re.compile(
            r"(?i)\b(?P<name>authorization|x-api-key|api-key|x-auth-token)\s*:\s*"
            r"(?:(?:bearer|basic|token)\s+)?(?P<value>[^\s'\";|&]+)"
        ),
    ),
)
# Names that hold a pointer to a secret, not the secret itself.
NON_SECRET_SUFFIX = re.compile(
    r"(?i)_(?:FILE|PATH|DIR|URL|URI|NAME|ID|ARN|ENV|VAR|HEADER|TTL|SECONDS|"
    r"LENGTH|PREFIX|TYPE|COUNT|LIMIT)$"
)
PLACEHOLDER = re.compile(
    r"(?i)dummy|fake|placeholder|example|changeme|redacted|not[-_]?real|test"
    r"|x{4,}|\*{3,}|<[^>]*>|\.\.\."
)
ENV_REFERENCE = re.compile(r"\$[({A-Za-z_]")
DSN = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|rediss?|mongodb(?:\+srv)?|amqps?)://"
    r"[^\s:/@'\"]+:(?P<value>[^\s@/'\"]+)@(?P<host>[^\s/:@'\"?]+)"
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"})

SHELL_SECRET_GUIDANCE = (
    "AGENTS.md §5 rule 9: no secrets in command-line arguments; they land in the "
    "transcript, shell history and process list. Read the value from an "
    "environment variable, or have the user save it to a gitignored .scratch/ "
    'file and reference it as "$(cat .scratch/<name>)". If this is a real '
    "credential it is already in this transcript: tell the user so they can "
    "rotate it. If it is a false positive, say so and ask the user."
)
FILE_SECRET_GUIDANCE = (
    "AGENTS.md §5 rule 9: no secrets in code, logs or test fixtures. Read real "
    "credentials from the environment at runtime. Test data must say it is fake "
    "(e.g. contain `fake`) or be assembled at runtime. If this is a real "
    "credential it is already in this transcript: tell the user so they can "
    "rotate it."
)

# --- rule 4: quality-gate configs -------------------------------------------------

GATE_WHOLE_FILE = re.compile(
    r"(?:^|/)(?:\.?ruff\.toml|\.?mypy\.ini|pytest\.ini|\.coveragerc|pyrightconfig\.json"
    r"|\.pre-commit-config\.ya?ml|eslint\.config\.[cm]?[jt]s|\.eslintrc(?:\.\w+)?"
    r"|vitest\.(?:config|workspace)\.[cm]?[jt]s|tsconfig[\w.-]*\.json"
    r"|\.fallowrc(?:\.jsonc?)?|\.?fallow\.toml)$"
)
GATE_SECTION_FILES = frozenset({"pyproject.toml", "setup.cfg", "tox.ini"})
SECTION_HEADER = re.compile(r"^\[\[?\s*([A-Za-z_][\w.:\s\"-]*?)\s*\]\]?\s*(?:#.*)?$")
GATE_SECTION = re.compile(
    r"^(?:tool\.(?:ruff|mypy|pytest|coverage|pyright|basedpyright)\b"
    r"|mypy\b|tool:pytest\b|pytest\b|coverage:|flake8\b)"
)
PACKAGE_GATE_SCRIPT = re.compile(
    r'^\s*"(?:lint|typecheck|type-check|tsc|test|check|coverage|ci|verify|format:check)'
    r'(?::[\w:.-]+)?"\s*:'
)
WORKFLOW = re.compile(r"(?:^|/)\.github/workflows/[^/]+\.ya?ml$")
WORKFLOW_GATE_LINE = re.compile(
    r"\b(?:ruff|mypy|pytest|vitest|eslint|tsc|typecheck|lint|coverage)\b"
    r"|--cov|continue-on-error|^\s*-?\s*if\s*:"
)

# --- rule 5: assertions ---------------------------------------------------------------

ASSERTION = re.compile(
    r"^\s*assert\b|\bself\.assert\w+\(|\bpytest\.raises\(|\bexpect\(|\bassert\.\w+\(",
    re.MULTILINE,
)

# --- shell heuristics ---------------------------------------------------------------

SHELL_WRITE = re.compile(
    r"""
      \bsed\b[^|;&\n]*?\s(?:-[a-zA-Z]*i\b|--in-place)   # sed -i
    | \bperl\b[^|;&\n]*?\s-[a-zA-Z]*i                   # perl -i / -pi
    | \btee\b
    | <<-?\s*['"]?[A-Za-z_]                             # heredoc
    | \b(?:python3?|node|ruby)\s+-[ce]\b                # inline interpreter
    | \bpatch\b
    | \bgit\s+apply\b
    | (?<![0-9&=>\-])>>?\s*(?!/dev/null\b|&)[^\s|;&>]   # redirect to a file
    """,
    re.VERBOSE,
)
FILE_MUTATOR = re.compile(
    r"\b(?:cp|mv|rm|rmdir|install|rsync|ln|truncate|chmod|unlink)\s"
    r"|\bruff\s+(?:format|check\b[^|;&\n]*--fix)"
    r"|\b(?:prettier|biome)\b[^|;&\n]*--write"
    r"|\beslint\b[^|;&\n]*--fix"
    r"|\bcurl\b[^|;&\n]*\s(?:-[a-zA-Z]*[oO]\b|--output\b|--remote-name\b)"
    r"|\bwget\b"
)
# Installers that rewrite .claude/settings.json and AGENTS.md without naming them.
AGENT_CONFIG_INSTALLER = re.compile(
    r"\bfallow\s+(?:hooks\s+(?:install|uninstall)|agent\s+(?:install|uninstall)"
    r"|setup-hooks|init\b[^|;&\n]*--(?:agents|hooks))\b"
)
DRY_RUN = re.compile(r"(?<![\w-])--dry-run\b")
BROWSER_LAUNCH = re.compile(
    r"\b(?:chromium(?:-browser)?|google-chrome|chrome|playwright|puppeteer)\b"
)
TEXT_BODY = re.compile(
    r"\bgit\s+commit\b|\bgh\s+(?:pr|issue|release)\s+(?:create|edit|comment)\b"
)
ADD_NOQA = re.compile(r"\bruff\b[^;&|\n]*\s--add-noqa\b")
PROTECTED_IN_SHELL = re.compile(r"\.claude/(?:hooks\b|settings(?:\.local)?\.json\b)")
GATE_FILE_IN_SHELL = re.compile(
    r"(?<![\w-])(?:pyproject\.toml|\.?ruff\.toml|\.?mypy\.ini|pytest\.ini|\.coveragerc"
    r"|setup\.cfg|tox\.ini|pyrightconfig\.json|tsconfig[\w.-]*\.json"
    r"|eslint\.config\.[cm]?[jt]s|\.eslintrc|vitest\.(?:config|workspace)\.[cm]?[jt]s"
    r"|\.pre-commit-config\.ya?ml|package\.json|\.github/workflows/"
    r"|\.fallowrc(?:\.jsonc?)?|\.?fallow\.toml)"
)

# --- tree scan ------------------------------------------------------------------------

SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".next",
        "out",
        "dist",
        "build",
        "coverage",
        "htmlcov",
        ".turbo",
        ".terraform",
        ".pnpm-store",
    }
)
SKIP_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
        ".zip", ".gz", ".tgz", ".tar", ".woff", ".woff2", ".ttf", ".otf",
        ".eot", ".mp3", ".mp4", ".webm", ".mov", ".wasm", ".so", ".dylib",
        ".pyc", ".sqlite", ".db",
    }
)  # fmt: skip
LOCKFILES = frozenset(
    {"pnpm-lock.yaml", "package-lock.json", "yarn.lock", "uv.lock", "poetry.lock"}
)
MAX_SCAN_BYTES = 2_000_000
GENERATED = re.compile(r"@generated|auto-?generated|do not edit", re.IGNORECASE)
SCAN_PREFILTER = re.compile(
    "|".join(
        [f"(?:{rule.pattern.pattern})" for rule in CONTENT_RULES]
        + [f"(?:{pattern.pattern})" for _, pattern in SECRET_FORMATS]
        + [f"(?:{DSN.pattern})"]
    ),
    re.MULTILINE,
)

Finding = tuple[str, str, str]  # (label, kind, example line)


# --- helpers ------------------------------------------------------------------------


def project_dir(cwd: str) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd).resolve()


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def is_exempt(rel: str) -> bool:
    return any(rel == d or rel.startswith(d + "/") for d in EXEMPT_DIRS)


def is_doc(rel: str) -> bool:
    return Path(rel).suffix.lower() in DOC_SUFFIXES


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def shorten(line: str, limit: int = 120) -> str:
    line = line.strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


@cache
def adr_exists(project: Path, number: str) -> bool:
    return any((project / "ADRs").glob(f"{number}-*.md"))


def cites_existing_adr(line: str, project: Path) -> bool:
    return any(adr_exists(project, m.group(1)) for m in ADR_REF.finditer(line))


# --- rules 1 and 2 ------------------------------------------------------------------


def unexcused_hits(
    text: str, project: Path, *, tests: bool
) -> Iterator[tuple[int, Rule, str]]:
    """(line number, rule, line) for each rule match on a line citing no ADR."""
    rules = CONTENT_RULES if tests else NON_TEST_RULES
    for number, line in enumerate(text.splitlines(), 1):
        hits = [rule for rule in rules if rule.pattern.search(line)]
        if hits and not cites_existing_adr(line, project):
            for rule in hits:
                yield number, rule, line


def added_findings(
    before: str, after: str, project: Path, *, tests: bool
) -> list[Finding]:
    """Rules with more unexcused matches in `after` than in `before`."""
    old = Counter(rule for _, rule, _ in unexcused_hits(before, project, tests=tests))
    new_hits = list(unexcused_hits(after, project, tests=tests))
    new = Counter(rule for _, rule, _ in new_hits)
    old_lines = set(before.splitlines())
    findings: list[Finding] = []
    for rule in CONTENT_RULES:
        if new[rule] > old[rule]:
            example = next(
                (
                    shorten(line)
                    for _, hit, line in new_hits
                    if hit is rule and line not in old_lines
                ),
                "",
            )
            findings.append((rule.label, rule.kind, example))
    return findings


# --- rule 3 -------------------------------------------------------------------------


def is_literal_secret(value: str) -> bool:
    value = value.strip("'\"")
    return (
        len(value) >= 8
        and not ENV_REFERENCE.search(value)
        and not value.startswith(("/", "./", "../", "~", ".scratch"))
        and not PLACEHOLDER.search(value)
    )


def credential_hits(text: str) -> list[tuple[int, str]]:
    """(line, description) per known-format credential; never repeats the value."""
    hits: list[tuple[int, str]] = []
    for label, pattern in SECRET_FORMATS:
        for match in pattern.finditer(text):
            if not FAKE_MARKER.search(match.group(0)):
                where = line_of(text, match.start())
                hits.append((where, f"{label} ({match.group(0)[:4]}…)"))
    for match in DSN.finditer(text):
        host = match.group("host").lower()
        value = match.group("value")
        if (
            "." in host
            and host not in LOCAL_HOSTS
            and not ENV_REFERENCE.search(value)
            and not PLACEHOLDER.search(f"{value} {host}")
        ):
            where = line_of(text, match.start())
            hits.append((where, f"database URL with an inline password (host {host})"))
    return hits


def find_secrets(command: str) -> list[str]:
    found = [description for _, description in credential_hits(command)]
    for label, pattern in SECRET_SLOTS:
        for match in pattern.finditer(command):
            name = match.group("name")
            if not NON_SECRET_SUFFIX.search(name) and is_literal_secret(
                match.group("value")
            ):
                found.append(f"{label} `{name}` with a literal value")
    return list(dict.fromkeys(found))


# --- rules 4 and 5 --------------------------------------------------------------------


def gate_lines(rel: str, text: str) -> list[str] | None:
    """The quality-gate part of `rel` as comparable lines, or None if it has none."""
    lines = text.splitlines()
    name = rel.rsplit("/", 1)[-1]
    selected: list[str]
    if GATE_WHOLE_FILE.search(rel):
        selected = lines
    elif name in GATE_SECTION_FILES:
        selected, inside = [], False
        for line in lines:
            header = SECTION_HEADER.match(line)
            if header:
                inside = bool(GATE_SECTION.match(header.group(1)))
            if inside:
                selected.append(line)
    elif name == "package.json":
        selected = [line for line in lines if PACKAGE_GATE_SCRIPT.match(line)]
    elif WORKFLOW.search(rel):
        selected = [line for line in lines if WORKFLOW_GATE_LINE.search(line)]
    else:
        return None
    return [line.rstrip() for line in selected if line.strip()]


def gate_change(rel: str, before: str, after: str) -> str | None:
    old, new = gate_lines(rel, before), gate_lines(rel, after)
    if old is None or new is None or old == new:
        return None
    diff = [
        line
        for line in list(difflib.unified_diff(old, new, lineterm="", n=0))[2:]
        if not line.startswith("@@")
    ]
    shown = diff[:12] + ([f"… {len(diff) - 12} more"] if len(diff) > 12 else [])
    return (
        f"{rel} changes quality-gate settings (the bar moves only with your "
        "approval):\n" + "\n".join(f"    {line}" for line in shown)
    )


def assertion_drop(rel: str, before: str, after: str) -> str | None:
    if not TEST_FILE.search(rel):
        return None
    old, new = len(ASSERTION.findall(before)), len(ASSERTION.findall(after))
    if new >= old:
        return None
    return (
        f"{rel}: this edit leaves {new} assertion(s) where there were {old}. "
        "Approve only if the removed checks are obsolete, not inconvenient."
    )


# --- verdicts -----------------------------------------------------------------------


@dataclass
class Verdict:
    target: str  # project-relative file, or "" for a shell command
    findings: list[Finding] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    asks: list[str] = field(default_factory=list)


def edit_texts(
    tool_name: str, tool_input: dict[str, Any], path: Path
) -> tuple[str, str]:
    """The whole-file (before, after) text a file-editing tool call produces."""
    if tool_name == "NotebookEdit":
        if tool_input.get("edit_mode") == "delete":
            return "", ""
        return "", tool_input.get("new_source") or ""
    before = read_text(path)
    if tool_name == "Write":
        return before, tool_input.get("content") or ""
    edits = (
        (tool_input.get("edits") or []) if tool_name == "MultiEdit" else [tool_input]
    )
    after = before
    for edit in edits:
        old, new = edit.get("old_string") or "", edit.get("new_string") or ""
        if not old or old not in after:
            # The tool will reject this edit; compare the fragments instead.
            return (
                "\n".join(e.get("old_string") or "" for e in edits),
                "\n".join(e.get("new_string") or "" for e in edits),
            )
        count = -1 if edit.get("replace_all") else 1
        after = after.replace(old, new, count)
    return before, after


def check_file_edit(
    tool_name: str, tool_input: dict[str, Any], cwd: str, project: Path
) -> Verdict | None:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        rel = path.resolve().relative_to(project).as_posix()
    except ValueError:
        return None
    verdict = Verdict(target=rel)
    if PROTECTED.search(rel):
        verdict.asks.append(
            f"{rel} is part of the policy guard or the settings that load it."
        )
    if is_exempt(rel):
        return verdict
    before, after = edit_texts(tool_name, tool_input, path)
    if not is_doc(rel):
        tests = bool(TEST_FILE.search(rel))
        verdict.findings = added_findings(before, after, project, tests=tests)
    added = credential_hits(after)
    if len(added) > len(credential_hits(before)):
        verdict.secrets = [f"line {line}: {description}" for line, description in added]
    for ask in (gate_change(rel, before, after), assertion_drop(rel, before, after)):
        if ask:
            verdict.asks.append(ask)
    return verdict


def check_bash(command: str, project: Path) -> Verdict:
    verdict = Verdict(target="", secrets=find_secrets(command))
    if TEXT_BODY.search(command):
        return verdict
    if ADD_NOQA.search(command):
        verdict.findings.append(("`ruff --add-noqa`", SUPPRESSION, ""))
    writes = SHELL_WRITE.search(command) is not None
    mutates = writes or FILE_MUTATOR.search(command) is not None
    launches = BROWSER_LAUNCH.search(command) is not None
    verdict.findings.extend(
        finding
        for finding in added_findings("", command, project, tests=False)
        if writes or (launches and finding[1] == SANDBOX)
    )
    if mutates and PROTECTED_IN_SHELL.search(command):
        verdict.asks.append(
            "this shell command may modify the policy guard or the settings that "
            "load it (.claude/hooks, .claude/settings*.json)."
        )
    if AGENT_CONFIG_INSTALLER.search(command) and not DRY_RUN.search(command):
        verdict.asks.append(
            "this installer rewrites .claude/settings.json and AGENTS.md; the fallow "
            "gate is installed by hand (see AGENTS.md), so review with --dry-run first."
        )
    if mutates and GATE_FILE_IN_SHELL.search(command):
        verdict.asks.append(
            "this shell command may modify a quality-gate config, which the "
            "per-edit diff check cannot see through a shell write."
        )
    return verdict


def render(verdict: Verdict) -> str:
    where = (
        f"this edit adds to {verdict.target}"
        if verdict.target
        else "this shell command writes"
    )
    lines: list[str] = []
    if verdict.findings:
        lines.append(f"BLOCKED by .claude/hooks/policy_guard.py: {where}")
        for label, _, example in verdict.findings:
            lines.append(f"  - {label}" + (f": {example}" if example else ""))
        for kind in dict.fromkeys(kind for _, kind, _ in verdict.findings):
            lines.append(GUIDANCE[kind])
        lines.append(ADR_HINT)
    if verdict.secrets:
        if verdict.target:
            lines.append(
                "BLOCKED by .claude/hooks/policy_guard.py: this edit writes a "
                f"credential into {verdict.target}"
            )
        else:
            lines.append(
                "BLOCKED by .claude/hooks/policy_guard.py: literal secret in a shell command"
            )
        lines.extend(f"  - {secret}" for secret in verdict.secrets)
        lines.append(FILE_SECRET_GUIDANCE if verdict.target else SHELL_SECRET_GUIDANCE)
    return "\n".join(lines) + "\n"


def emit(verdict: Verdict | None) -> int:
    if verdict is None:
        return 0
    if verdict.findings or verdict.secrets:
        sys.stderr.write(render(verdict))
        return 2
    if verdict.asks:
        reason = "policy_guard needs your approval:\n" + "\n".join(
            f"- {ask}" for ask in verdict.asks
        )
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(output))
    return 0


# --- tree scan ----------------------------------------------------------------------


def gitignored(project: Path) -> frozenset[str]:
    """Plain root .gitignore entries; globs and negations are left to git."""
    entries = set()
    for line in read_text(project / ".gitignore").splitlines():
        line = line.strip()
        if (
            line
            and not line.startswith(("#", "!"))
            and not any(c in line for c in "*?[")
        ):
            entries.add(line.strip("/"))
    return frozenset(entries)


def project_files(project: Path) -> Iterator[tuple[Path, str]]:
    try:
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        yield from walk_files(project)
        return
    for rel in listed.split("\0"):
        if rel:
            yield project / rel, rel


def walk_files(project: Path) -> Iterator[tuple[Path, str]]:
    ignored = gitignored(project)

    def skipped(rel: str) -> bool:
        return is_exempt(rel) or any(
            rel == e or rel.startswith(e + "/") for e in ignored
        )

    for root, dirs, files in os.walk(project):
        base = Path(root).relative_to(project).as_posix()
        prefix = "" if base == "." else base + "/"
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not skipped(prefix + d)]
        for name in files:
            if not skipped(prefix + name):
                yield Path(root) / name, prefix + name


def scan_file(path: Path, rel: str, project: Path) -> list[str]:
    if is_exempt(rel) or path.suffix.lower() in SKIP_SUFFIXES or path.name in LOCKFILES:
        return []
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return []
        text = path.read_text(errors="ignore")
    except OSError:
        return []
    if not SCAN_PREFILTER.search(text) or GENERATED.search(
        "\n".join(text.splitlines()[:5])
    ):
        return []
    problems: list[str] = []
    if not is_doc(rel):
        tests = bool(TEST_FILE.search(rel))
        problems.extend(
            f"{rel}:{number}: {rule.label}"
            for number, rule, _ in unexcused_hits(text, project, tests=tests)
        )
    problems.extend(
        f"{rel}:{number}: {description}"
        for number, description in credential_hits(text)
    )
    return problems


def scan(project: Path) -> list[str]:
    return [
        problem
        for path, rel in project_files(project)
        for problem in scan_file(path, rel, project)
    ]


# --- entry points -------------------------------------------------------------------


def pre_tool_use(payload: dict[str, Any]) -> int:
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    project = project_dir(cwd)
    if tool_name == "Bash":
        return emit(check_bash(tool_input.get("command") or "", project))
    if tool_name in FILE_TOOLS:
        return emit(check_file_edit(tool_name, tool_input, cwd, project))
    return 0


def stop(payload: dict[str, Any]) -> int:
    problems = scan(project_dir(payload.get("cwd") or os.getcwd()))
    if not problems:
        return 0
    listing = "\n".join(problems[:20])
    if len(problems) > 20:
        listing += f"\n… and {len(problems) - 20} more"
    if payload.get("stop_hook_active"):
        message = f"policy_guard: {len(problems)} violation(s) remain in the working tree:\n{listing}"
        print(json.dumps({"systemMessage": message}))
        return 0
    reason = (
        f"policy_guard found {len(problems)} violation(s) in the working tree, "
        f"possibly written by a script, a formatter or another agent:\n{listing}\n"
        "Fix them (AGENTS.md §5 rules 1 and 9, §6) or cite an existing ADR-NNNN on "
        "the line. Do not disable the guard; if a finding is wrong, tell the user."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def scan_cli(project: Path) -> int:
    problems = scan(project)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if os.environ.get("AQA_POLICY_GUARD", "").lower() == "off":
        return 0
    mode = argv[1] if len(argv) > 1 else "--pre-tool-use"
    if mode == "--scan":
        return scan_cli(
            Path(argv[2]).resolve() if len(argv) > 2 else project_dir(os.getcwd())
        )
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if mode == "--stop":
        return stop(payload)
    return pre_tool_use(payload)


def fail_open(
    kind: type[BaseException], error: BaseException, trace: TracebackType | None
) -> None:
    """Uncaught errors exit 1, which Claude Code treats as non-blocking: say so."""
    sys.stderr.write(
        "policy_guard.py failed open (the tool call proceeds unchecked):\n"
    )
    sys.__excepthook__(kind, error, trace)


if __name__ == "__main__":
    sys.excepthook = fail_open
    sys.exit(main(sys.argv))
