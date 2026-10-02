"""The rule tables that policy_guard.py checks against.

policy_guard.py holds the checks and the entry points, and its docstring
describes the rules. This module holds their data: what each rule matches,
where it applies and what it tells the agent. Like the guard, it runs on
whatever python3 Claude Code finds, so it needs only the standard library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# --- scope ---------------------------------------------------------------------

# Project-relative directories that are never checked: the guard's own tests
# quote every pattern, .scratch/ is gitignored local scratch space (AGENTS.md
# rule 9), the dashboard's API client is generated (pnpm gen:client), and
# bench/apps/ holds vendored third-party apps whose planted bugs are the point
# (ADR-0021; gitleaks still scans it for secrets).
EXEMPT_DIRS = (".claude/hooks", ".scratch", "apps/dashboard/src/client", "bench/apps")

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
STUB = "stub"
SWALLOWED = "swallowed exception"
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
    STUB: (
        "CONSTRAINTS.md floor: no unimplemented stubs. Implement it, or leave it "
        "out until a ticket needs it; abstract and Protocol methods use `...`."
    ),
    SWALLOWED: (
        "CONSTRAINTS.md floor: no swallowed exceptions. Suppress only the "
        "narrowest exception type, and say why in a comment."
    ),
    SANDBOX: (
        "AGENTS.md §6: Chromium launches with its sandbox enabled "
        "(chromium_sandbox=True); never ship --no-sandbox for hosted runs."
    ),
}

# Where a rule applies: every checked file, test files only, or all but tests.
Scope = Literal["any", "tests", "source"]

ADR_HINT = (
    "If an accepted ADR genuinely justifies this, write the ADR first "
    "(ADRs/NNNN-*.md), then cite ADR-NNNN on the same line."
)


@dataclass(frozen=True)
class Rule:
    label: str
    kind: str
    pattern: re.Pattern[str]
    scope: Scope = "any"


def _rule(label: str, kind: str, regex: str, *, scope: Scope = "any") -> Rule:
    return Rule(label, kind, re.compile(regex), scope)


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
        r"#\s*pragma:\s*no\s*(?:cover|branch)\b|\b(?:istanbul|c8|v8)\s+ignore\b",
    ),
    _rule(
        "pytest skip/xfail",
        SKIPPED_TEST,
        r"\bpytest\.mark\.(?:skip|skipif|xfail)\b"
        r"|\bpytest\.(?:skip|xfail|importorskip)\s*\("
        r"|\b__test__\s*=\s*False\b|\bcollect_ignore(?:_glob)?\b",
    ),
    _rule(
        "unittest skip",
        SKIPPED_TEST,
        r"\bunittest\.(?:skip|skipIf|skipUnless|expectedFailure|SkipTest)\b"
        r"|\bself\.skipTest\(",
    ),
    _rule(
        "skipped or focused JS test",
        SKIPPED_TEST,
        r"\b(?:describe|it|test)\.(?:skip|only|fixme|skipIf|runIf)\b"
        r"|\b(?:xit|xtest|xdescribe)\s*\(",
    ),
    _rule("flaky / rerun marker", SKIPPED_TEST, r"\bpytest\.mark\.flaky\b|--reruns\b"),
    _rule("test retry", SKIPPED_TEST, r"\bretry\s*:\s*[1-9]", scope="tests"),
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
    _rule(
        "unimplemented stub",
        STUB,
        r"\braise\s+NotImplementedError\b"
        r"|(?i:\bthrow\s+new\s+Error\(\s*[\"'`][^\"'`]*\bnot\s+implemented)",
        scope="source",
    ),
    _rule(
        "`suppress(Exception)`",
        SWALLOWED,
        r"\bsuppress\(\s*(?:[\w.]+\s*,\s*)*(?:Base)?Exception\b",
    ),
    _rule("`--no-sandbox`", SANDBOX, r"--no-sandbox\b"),
    _rule("`--disable-*sandbox`", SANDBOX, r"--disable-[\w-]*sandbox\b"),
    _rule(
        "`chromium_sandbox` disabled",
        SANDBOX,
        r"""\bchromium_?[sS]andbox["']?\s*[:=]\s*[Ff]alse\b""",
    ),
)
TEST_RULES = tuple(rule for rule in CONTENT_RULES if rule.scope != "source")
SOURCE_RULES = tuple(rule for rule in CONTENT_RULES if rule.scope != "tests")

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
    r"|vitest\.(?:config|workspace)\.[cm]?[jt]s|tsconfig[\w.-]*\.json|\.github/scripts/[^/]+"
    r"|\.fallowrc(?:\.jsonc?)?|\.?fallow\.toml|osv-scanner\.toml|CONSTRAINTS\.md)$"
)
GATE_SECTION_FILES = frozenset({"pyproject.toml", "setup.cfg", "tox.ini"})
SECTION_HEADER = re.compile(r"^\[\[?\s*([A-Za-z_][\w.:\s\"-]*?)\s*\]\]?\s*(?:#.*)?$")
# [tool.uv] holds the litellm ban, which a [tool.uv.sources] entry can override;
# a workspace member's entry can't, so it doesn't gate.
GATE_SECTION = re.compile(
    r"^(?:tool\.(?:ruff|mypy|pytest|coverage|pyright|basedpyright)\b|tool\.uv(?:$|\.sources\b)"
    r"|mypy\b|tool:pytest\b|pytest\b|coverage:|flake8\b)"
)
WORKSPACE_MEMBER = re.compile(r"^\s*[\w-]+\s*=\s*\{\s*workspace\s*=\s*true\s*\}\s*$")
PACKAGE_GATE_SCRIPT = re.compile(
    r'^\s*"(?:lint|typecheck|type-check|tsc|test|check|coverage|ci|verify|format:check)'
    r'(?::[\w:.-]+)?"\s*:'
)
WORKFLOW = re.compile(r"(?:^|/)\.github/workflows/[^/]+\.ya?ml$")
# Any workflow line can weaken a gate; only comments and the top-level name
# can't, unless the name holds a YAML anchor or alias a step could run.
WORKFLOW_FREE_LINE = re.compile(r"^\s*#|^name\s*:[^&*]*$")

# --- rule 5: assertions ---------------------------------------------------------------

ASSERTION = re.compile(
    r"^\s*assert\b|\bself\.assert\w+\(|\bpytest\.raises\(|\bexpect\(|\bassert\.\w+\(",
    re.MULTILINE,
)
# Counted too: renaming a test away drops it without touching an assertion.
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test|\b(?:it|test)\(", re.MULTILINE)
# The tests that prove the bar: the root tests/ (CONSTRAINTS.md's threshold
# checks, AGENTS.md §4's command checks) and the guard's own. Flipping an
# expected outcome there weakens the bar and keeps every assertion, so --diff
# asks about any change to an existing one. A new one can't lower the floor.
BAR_TESTS = re.compile(r"^(?:tests/|\.claude/hooks/test_[^/]*\.py$)")

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
# git's global options, which may stand between `git` and its subcommand.
GIT_OPTIONS = r"(?:\s+(?:-[Cc]|--(?:git-dir|work-tree|namespace))\s+\S+|\s+-(?!(?:[Cc]|-(?:git-dir|work-tree|namespace))\s)[\w-]+(?:=\S+)?)*"
FILE_MUTATOR = re.compile(
    r"\b(?:cp|mv|rm|rmdir|install|rsync|ln|truncate|chmod|unlink)\s"
    r"|\bruff\s+(?:format|check\b[^|;&\n]*--fix)"
    r"|\b(?:prettier|biome)\b[^|;&\n]*--write"
    r"|\beslint\b[^|;&\n]*--fix"
    r"|\bcurl\b[^|;&\n]*\s(?:-[a-zA-Z]*[oO]\b|--output\b|--remote-name\b)"
    r"|\bwget\b"
    r"|\bgit" + GIT_OPTIONS + r"\s+(?:checkout|restore|clean|apply)\b"
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
# A message quotes patterns and paths: blank it, but judge a heredoc's first line.
MESSAGE = re.compile(
    r"""(?m)\\.|\$'(?:\\.|[^'\\])*'|'[^']*'|"(?:\\.|[^"\\])*"|<<-?[ \t]*(['"]?)(\w+)\1([^\n]*\n)(?:[^\n]*\n)*?[ \t]*\2[ \t]*$"""
)
ADD_NOQA = re.compile(r"\bruff\b[^;&|\n]*\s--add-noqa\b")
PROTECTED_IN_SHELL = re.compile(r"\.claude/(?:hooks\b|settings(?:\.local)?\.json\b)")
GATE_FILE_IN_SHELL = re.compile(
    r"(?<![\w-])(?:pyproject\.toml|\.?ruff\.toml|\.?mypy\.ini|pytest\.ini|\.coveragerc"
    r"|setup\.cfg|tox\.ini|pyrightconfig\.json|tsconfig[\w.-]*\.json"
    r"|eslint\.config\.[cm]?[jt]s|\.eslintrc|vitest\.(?:config|workspace)\.[cm]?[jt]s"
    r"|\.pre-commit-config\.ya?ml|package\.json|\.github/(?:workflows|scripts)/"
    r"|\.fallowrc(?:\.jsonc?)?|\.?fallow\.toml|osv-scanner\.toml|CONSTRAINTS\.md)"
)
# A test file or directory named in a shell command: TEST_FILE's shapes.
TEST_PATH_IN_SHELL = re.compile(
    r"(?<![\w.-])(?:tests?/|tests(?![\w.-])|test_[\w.-]*\.py|[\w.-]*_test\.py"
    r"|conftest\.py|[\w.-]*\.(?:test|spec)\.[cm]?[jt]sx?(?![\w.-]))"
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
