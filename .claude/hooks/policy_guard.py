#!/usr/bin/env python3
"""Guard for the AGENTS.md rules that get bent under pressure to go green.

Four entry points, one rule set (its tables are in ``policy_rules.py``;
``--diff``'s reading of git and its own asks are in ``policy_diff.py``):

* PreToolUse (default): checks each Bash / Edit / Write / NotebookEdit call
  before it runs. Clear violations are refused; changes that need judgment are
  escalated to the user as a permission prompt.
* ``--stop`` (Stop hook): scans the working tree when Claude ends a turn, to
  catch what the per-call check cannot see: files written by scripts,
  formatters, other agents or people.
* ``--scan`` (CLI, for CI or pre-commit): the same scan; exit 1 on violations.
* ``--diff <base>`` (CLI, CI's ``floor`` job): judges each file the working
  tree changes since the merge base of ``<base>`` and HEAD, untracked files
  included, as an edit is judged. Exit 1 on a refused finding, and on one
  that needs approval unless ``AQA_FLOOR_CHANGE_APPROVED=true`` (CI sets it
  from the maintainer's label); exit 2 when the diff can't be taken.

Refused
-------
1. Silenced checks (AGENTS.md §5 rule 1): newly added inline suppressions
   (``type: ignore``, ``noqa``, ``pyright:`` / ``mypy:`` pragmas, ``@ts-ignore``,
   ``@ts-expect-error``, ``@ts-nocheck``, ``eslint-disable*``, ``fallow-ignore*``,
   ``@expected-unused``), coverage pragmas,
   skipped / focused / rerun-until-green tests, tautological assertions
   (``assert True``, ``expect(true).toBe(true)``) and ``ruff --add-noqa``.
   CONSTRAINTS.md's floor adds ``suppress(Exception)`` and, outside tests,
   unimplemented stubs (``raise NotImplementedError``,
   ``throw new Error("Not implemented")``).
2. Disabling the Chromium sandbox (AGENTS.md §6).
3. Secrets (AGENTS.md §5 rule 9): literal secrets in shell commands (known
   formats, secret-named variables and flags, auth headers, remote database
   URLs with a password) and known-format credentials written into any file,
   docs included.

Escalated to the user
---------------------
4. Any change to a quality-gate config: ruff / mypy / pytest / coverage /
   pyright settings, ``[tool.uv]`` and ``[tool.uv.sources]`` (the litellm ban),
   CONSTRAINTS.md, tsconfig, eslint, vitest and fallow configs, osv-scanner
   and gitleaks waivers, Renovate's config, pre-commit, gate scripts in
   package.json, CI workflows (all but comments and the top-level name) and
   every file under ``.github/scripts/``. Tightening prompts too: the
   bar moves only with a human in the loop. Creating one of these files prompts
   once, which is how the initial bar gets approved.
5. A test-file edit that leaves fewer assertions or tests, and a shell
   command that may delete, move or rewrite a test file. ``--diff`` adds a
   deleted test file (a moved one reads as deleted), any change to an
   existing file that proves the bar (``BAR_TESTS``), where a flipped
   expectation keeps every assertion, a gate config file that comes or goes
   (the tools read even an empty one), and a symbolic link.
6. Edits to this guard or the settings that load it (``.claude/hooks/``,
   ``.claude/settings*.json``, in any ``.claude`` directory), including shell
   writes that name them and installers that rewrite them without naming them
   (``fallow hooks install``, ``fallow agent install``; ``--dry-run`` is fine).

Excuses
-------
* A line citing ``ADR-NNNN`` is excused from rules 1 and 2 when
  ``ADRs/NNNN-*.md`` exists: write the ADR first, then the suppression.
* A credential-shaped value that says it is fake (``fake``, ``dummy``,
  ``example``, ``placeholder``, ``redacted``, ``xxxxxx``) is not a secret.
* ``AQA_POLICY_GUARD=off`` in Claude Code's environment (the shell that launched
  it, or a settings file's ``env``) disables the hooks for that session. An
  explicit ``--scan`` or ``--diff`` still runs.

Scope
-----
Only files inside the project, minus ``EXEMPT_DIRS``. Rules 1 and 2 skip prose
(``.md``, ``.rst``, ``.txt``, ...) because docs quote the patterns; secret
checks do not. The tree scan also skips dependency and build directories,
lockfiles, binaries and files with a generated-code header. It lists files
with ``git ls-files`` once the project is a repo, and until then walks the
tree honouring the plain (glob-free) entries of the root ``.gitignore``.
``--diff`` reads each file as git stores it (a link is the name it points to)
and skips the content of lockfiles and binaries. It prints each finding's file
and reason, never a gate config's changed lines, and cuts every credential it
recognises short, so a CI log holds none.

Each kind of watched file (rules 4-6) is defined once, by the shapes of its
names (``NameShapes`` in ``policy_rules.py``), and its edit check and its shell
check are built from that definition. A name may run on past its shape
(``pyproject.toml5``, ``test_a.pyc``) unless it ends as a ``.bak`` backup, and a
directory is named exactly (not ``.claude/hooks-old``). In a shell command a
name also ends at whitespace, a quote or an operator, may follow an attached
short option or a variable (``-o.claude/...``, ``$D.ruff.toml``), its segments
may sit behind quotes, several slashes or ``.`` and ``..`` steps, and a
directory stands for every file under it.

Watched names match in any letter case, as macOS's filesystem compares them,
by full case folding (a ligature can stand for two letters): there
``.claude/Hooks/x.py`` is the guard, pytest reads a new ``Pytest.toml``, and an
edit's path may spell the project's own directory in another case. The
narrowings in a name's shape (a ``.bak`` backup, the shell's ``test`` command)
match in any case too. What the guard skips beyond them is matched as typed:
the exempt directories, and a test file's exemption from the source rules, so a
name that is a test's only in another case (``Test_a.py``, which pytest never
collects) gets both the test and the source rules.

Known gaps, stated rather than hidden
-------------------------------------
* A strong assertion swapped for a weaker one (``==`` to ``in``, an exact
  matcher to ``toBeTruthy``) keeps the count level and is not detected.
* In files, only known credential formats are recognised; the generic
  ``PASSWORD=...`` detection runs on shell commands only.
* An edit's path that reaches the project through another name for one of its
  directories, not a case variant (macOS's ``/System/Volumes/Data`` firmlink,
  ``/.vol/``, ``/.nofollow/``), counts as outside the project and is not
  checked.
* Shell writes are recognised heuristically. The Stop scan backstops rules
  1-3; rules 4-6 have no backstop for a write through an opaque script. A
  watched file goes unseen when a ``find -delete`` or a script deletes it, a
  glob stands in its fixed part (``pyproj*.toml``), a ``cd`` comes before its
  bare name, parameter expansion builds it (``${f%.bak}``), a command
  substitution holding a space stands in for one of its segments, or its name
  holds a space; so does a bare ``test`` directory that starts a word (the
  shell's ``test`` command), and quoted text in a command that commits. A
  mutating command that names a test path asks, even a formatter run or test
  output sent to ``tee``, and a shell write is judged as source (write a test
  fake's stub with Write).
* This is a guardrail, not a security boundary. Other agents (Codex, Cursor)
  do not run Claude Code hooks. CI's ``--diff`` sees their changes, but runs
  the pull request's own copy of this guard and of its workflow, so a pull
  request that weakens either is judged by the weakened copy, compiled
  files included; a link it approved hides later changes behind it; and the
  approval label can outlive a push. ADR-0028's amendment (2026-10-01) lists
  these gaps.

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
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import TracebackType
from typing import Any

from policy_commands import (
    add_noqa,
    agent_config_installer,
    file_mutator,
    mask_messages,
    shell_write,
)
from policy_diff import changed_files, diff_asks, merge_base, texts
from policy_rules import (
    ADR_HINT,
    ADR_REF,
    ASSERTION,
    BROWSER_LAUNCH,
    CONTENT_RULES,
    DOC_SUFFIXES,
    DRY_RUN,
    DSN,
    ENV_REFERENCE,
    FAKE_MARKER,
    FILE_SECRET_GUIDANCE,
    FILE_TOOLS,
    GATE_FILE_IN_SHELL,
    GATE_SECTION,
    GATE_SECTION_FILE,
    GATE_WHOLE_FILE,
    GENERATED,
    GUIDANCE,
    LOCAL_HOSTS,
    MAX_SCAN_BYTES,
    NON_SECRET_SUFFIX,
    PACKAGE_GATE_SCRIPT,
    PACKAGE_MANIFEST,
    PLACEHOLDER,
    PROTECTED,
    PROTECTED_IN_SHELL,
    SANDBOX,
    SCAN_PREFILTER,
    SECRET_FORMATS,
    SECRET_SLOTS,
    SECTION_HEADER,
    SHELL_SECRET_GUIDANCE,
    SKIP_DIRS,
    SOURCE_RULES,
    SUPPRESSION,
    TEST_DEF,
    TEST_FILE,
    TEST_FILE_AS_TYPED,
    TEST_PATH_IN_SHELL,
    TEST_RULES,
    TEXT_BODY,
    WORKFLOW,
    WORKFLOW_FREE_LINE,
    WORKSPACE_MEMBER,
    Finding,
    Rule,
    binary_or_lockfile,
    is_exempt,
    resolved_paths,
)

# --- helpers ------------------------------------------------------------------------


def project_dir(cwd: str) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd).resolve()


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


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


def rules_for(rel: str) -> tuple[Rule, ...]:
    """The test rules for a test file, the source rules for any other, and both
    for a name that is a test's only in another case (`Test_a.py`)."""
    if not TEST_FILE.search(rel):
        return SOURCE_RULES
    return TEST_RULES if TEST_FILE_AS_TYPED.search(rel) else CONTENT_RULES


def unexcused_hits(
    text: str, project: Path, *, rules: tuple[Rule, ...]
) -> Iterator[tuple[int, Rule, str]]:
    """(line number, rule, line) for each rule match on a line citing no ADR."""
    for number, line in enumerate(text.splitlines(), 1):
        hits = [rule for rule in rules if rule.pattern.search(line)]
        if hits and not cites_existing_adr(line, project):
            for rule in hits:
                yield number, rule, line


def added_findings(
    before: str, after: str, project: Path, *, rules: tuple[Rule, ...]
) -> list[Finding]:
    """Rules with more unexcused matches in `after` than in `before`."""
    old = Counter(rule for _, rule, _ in unexcused_hits(before, project, rules=rules))
    new_hits = list(unexcused_hits(after, project, rules=rules))
    new = Counter(rule for _, rule, _ in new_hits)
    old_lines = set(before.splitlines())
    findings: list[Finding] = []
    for rule in CONTENT_RULES:
        if new[rule] > old[rule]:
            example = next(
                (
                    shorten(redacted(line))
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


def redacted(text: str) -> str:
    """`text` with each known-format credential and inline database password cut
    short, as credential_hits() shows them, so a CI log never holds one."""
    for _, pattern in SECRET_FORMATS:
        text = pattern.sub(lambda match: match[0][:4] + "…", text)
    return DSN.sub(lambda match: match[0].replace(match["value"], "…"), text)


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
    selected: list[str]
    if GATE_WHOLE_FILE.search(rel):
        selected = lines
    elif WORKFLOW.search(rel):
        # Ahead of the names a workflow's own name can run on from.
        selected = [line for line in lines if not WORKFLOW_FREE_LINE.match(line)]
    elif GATE_SECTION_FILE.search(rel):
        selected, inside = [], False
        for line in lines:
            header = SECTION_HEADER.match(line)
            if header:
                inside = bool(GATE_SECTION.match(header.group(1)))
            if inside and not WORKSPACE_MEMBER.match(line):
                selected.append(line)
    elif PACKAGE_MANIFEST.search(rel):
        selected = [line for line in lines if PACKAGE_GATE_SCRIPT.match(line)]
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
    for what, pattern in (("assertion", ASSERTION), ("test", TEST_DEF)):
        old, new = len(pattern.findall(before)), len(pattern.findall(after))
        if new < old:
            return (
                f"{rel}: this edit leaves {new} {what}(s) where there were {old}. "
                "Approve only if the removed checks are obsolete, not inconvenient."
            )
    return None


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


def project_relative(path: Path, project: Path) -> str | None:
    """`path` below `project`, or None outside it. The project's own part is
    compared in any letter case, as macOS's filesystem compares it; the rest
    keeps the case typed."""
    count = len(project.parts)
    head = [part.casefold() for part in path.parts[:count]]
    if head != [part.casefold() for part in project.parts]:
        return None
    return Path(*path.parts[count:]).as_posix()


def check_file_edit(
    tool_name: str, tool_input: dict[str, Any], cwd: str, project: Path
) -> Verdict | None:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    rel = project_relative(path.resolve(), project)
    if rel is None:
        return None
    before, after = edit_texts(tool_name, tool_input, path)
    return judge_change(rel, before, after, project)


def judge_change(rel: str, before: str, after: str, project: Path) -> Verdict:
    """What changing `rel` from `before` to `after` breaks or needs approval for:
    one judgment for an edit in Claude Code and for each file `--diff` reads."""
    verdict = Verdict(target=rel)
    if PROTECTED.search(rel):
        verdict.asks.append(
            f"{rel} is part of the policy guard or the settings that load it."
        )
    if is_exempt(rel):
        return verdict
    if not is_doc(rel):
        verdict.findings = added_findings(before, after, project, rules=rules_for(rel))
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
        command = mask_messages(command)
    if add_noqa(command):
        verdict.findings.append(("`ruff --add-noqa`", SUPPRESSION, ""))
    writes = shell_write(command)
    mutates = writes or file_mutator(command)
    launches = BROWSER_LAUNCH.search(command) is not None
    verdict.findings.extend(
        finding
        for finding in added_findings("", command, project, rules=SOURCE_RULES)
        if writes or (launches and finding[1] == SANDBOX)
    )
    # A path can reach a watched file through quotes and . and .. steps.
    resolved = resolved_paths(command) if mutates else command
    named = command if resolved == command else f"{command}\n{resolved}"
    if mutates and PROTECTED_IN_SHELL.search(named):
        verdict.asks.append(
            "this shell command may modify the policy guard or the settings that "
            "load it (.claude/hooks, .claude/settings*.json)."
        )
    if agent_config_installer(command) and not DRY_RUN.search(command):
        verdict.asks.append(
            "this installer rewrites .claude/settings.json and AGENTS.md; the fallow "
            "gate is installed by hand (see AGENTS.md), so review with --dry-run first."
        )
    if mutates and GATE_FILE_IN_SHELL.search(named):
        verdict.asks.append(
            "this shell command may modify a quality-gate config, which the "
            "per-edit diff check cannot see through a shell write."
        )
    if mutates and TEST_PATH_IN_SHELL.search(named):
        verdict.asks.append(
            "this shell command may delete, move or rewrite a test file, which the "
            "per-edit assertion check cannot see."
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
        lines.extend(
            GUIDANCE[kind] for kind in dict.fromkeys(k for _, k, _ in verdict.findings)
        )
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
    for raw in read_text(project / ".gitignore").splitlines():
        line = raw.strip()
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
    if is_exempt(rel) or binary_or_lockfile(rel):
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
        problems.extend(
            f"{rel}:{number}: {rule.label}"
            for number, rule, _ in unexcused_hits(text, project, rules=rules_for(rel))
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


# --- diff: the floor's moves since a base -------------------------------------------


def diff_verdicts(cwd: Path, base: str) -> list[Verdict]:
    """A verdict per file the working tree changes since `base`'s merge base."""
    root, since = merge_base(cwd, base)
    verdicts = []
    for change in changed_files(root, since):
        read = texts(root, since, change)
        before, after = read or (b"", b"")
        verdict = judge_change(
            change.rel,
            before.decode(errors="replace"),
            after.decode(errors="replace"),
            root,
        )
        asks = diff_asks(
            change,
            verdict.asks,
            read=read is not None,
            rewritten=read is None or before != after,
        )
        verdict.asks.extend(asks)
        verdicts.append(verdict)
    return verdicts


def diff_cli(base: str, cwd: Path) -> int:
    """Exit 0 when clean, 1 on findings and 2 when the diff can't be taken."""
    try:
        verdicts = diff_verdicts(cwd, base)
    except subprocess.CalledProcessError as error:
        reason = os.fsdecode(error.stderr).strip() or f"exit {error.returncode}"
        command = " ".join(map(str, error.cmd))
        sys.stderr.write(f"policy_guard --diff {base}: `{command}` failed: {reason}\n")
        return 2
    # No git on PATH, a file it can't read, or git hanging past its timeout.
    except (OSError, subprocess.TimeoutExpired) as error:
        sys.stderr.write(f"policy_guard --diff {base}: {error}\n")
        return 2
    refused = [v for v in verdicts if v.findings or v.secrets]
    for verdict in refused:
        sys.stdout.write(redacted(render(verdict)))
    asks = [ask for verdict in verdicts for ask in verdict.asks]
    # CI sets this from the maintainer's label; it never excuses a refusal.
    approved = os.environ.get("AQA_FLOOR_CHANGE_APPROVED") == "true"
    if asks:
        heading = (
            "approved by the maintainer (AQA_FLOOR_CHANGE_APPROVED=true)"
            if approved
            else "needs the maintainer's approval (in CI, the floor-change-approved label)"
        )
        print(f"policy_guard --diff {base}: {heading}:")
        # The first line names the file; the lines that changed are on the pull
        # request, and a credential among them could span several.
        print(redacted("\n".join(f"- {ask.splitlines()[0]}" for ask in asks)))
    return 1 if refused or (asks and not approved) else 0


# --- entry points -------------------------------------------------------------------


def pre_tool_use(payload: dict[str, Any]) -> int:
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or str(Path.cwd())
    project = project_dir(cwd)
    if tool_name == "Bash":
        return emit(check_bash(tool_input.get("command") or "", project))
    if tool_name in FILE_TOOLS:
        return emit(check_file_edit(tool_name, tool_input, cwd, project))
    return 0


def stop(payload: dict[str, Any]) -> int:
    problems = scan(project_dir(payload.get("cwd") or str(Path.cwd())))
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


def command_line(mode: str, args: list[str]) -> int:
    """--scan and --diff: run by hand or in CI, so the hooks' off switch doesn't apply."""
    if mode == "--scan":
        return scan_cli(
            Path(args[0]).resolve() if args else project_dir(str(Path.cwd()))
        )
    if not args:
        sys.stderr.write("usage: policy_guard.py --diff <base>\n")
        return 2
    # From the working directory, as CI runs it: git finds the repository.
    return diff_cli(args[0], Path.cwd())


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--pre-tool-use"
    if mode in {"--scan", "--diff"}:
        return command_line(mode, argv[2:])
    if os.environ.get("AQA_POLICY_GUARD", "").lower() == "off":
        return 0
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
