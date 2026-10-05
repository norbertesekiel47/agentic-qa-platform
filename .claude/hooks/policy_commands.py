"""Command predicates with monotonic searches, preserving the guard's heuristics."""

from __future__ import annotations

import re

from policy_rules import DIRECT_INSTALLER, DIRECT_MUTATOR, DIRECT_WRITE

_SEPARATOR = re.compile(r"[|;&\n]")
_SED = re.compile(r"\bsed\b")
_SED_OPTION = re.compile(r"\s(?:-[a-zA-Z]*i\b|--in-place)")
_PERL = re.compile(r"\bperl\b")
_PERL_OPTION = re.compile(r"\s-[a-zA-Z]*i")

_MUTATOR_OPTIONS = tuple(
    (re.compile(trigger), re.compile(option))
    for trigger, option in (
        (r"\bruff\s+check\b", r"--fix"),
        (r"\b(?:prettier|biome)\b", r"--write"),
        (r"\beslint\b", r"--fix"),
        (r"\bcurl\b", r"\s(?:-[a-zA-Z]*[oO]\b|--output\b|--remote-name\b)"),
    )
)
_FALLOW_INIT = re.compile(r"\bfallow\s+init\b")
_FALLOW_OPTION = re.compile(r"--(?:agents|hooks)\b")
_RUFF = re.compile(r"\bruff\b")
_NOQA = re.compile(r"\s--add-noqa\b")

_GIT = re.compile(r"\bgit$")
_SUBCOMMAND = re.compile(r"(?:checkout|restore|clean|apply)\b")
_OPTION = re.compile(r"-[\w-]+(?:=\S+)?")
_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def _git_mutator(command: str) -> bool:
    words = list(re.finditer(r"\S+", command))
    follows = [False] * (len(words) + 2)
    for index in range(len(words) - 1, -1, -1):
        word = words[index][0]
        follows[index] = _SUBCOMMAND.match(word) is not None
        if word in _VALUE_OPTIONS:
            follows[index] = follows[index + 2]
        elif _OPTION.fullmatch(word):
            follows[index] = follows[index + 1]
        if _GIT.search(word) and follows[index + 1]:
            return True
    return False


def _following(command: str, trigger: re.Pattern[str], option: re.Pattern[str]) -> bool:
    options = option.finditer(command)
    wanted = next(options, None)
    separators = _SEPARATOR.finditer(command)
    boundary = next(separators, None)
    for start in trigger.finditer(command):
        while wanted is not None and wanted.start() < start.end():
            wanted = next(options, None)
        if wanted is None:
            return False
        while boundary is not None and boundary.start() < start.end():
            boundary = next(separators, None)
        # The option's initial whitespace may itself consume a newline.
        if boundary is None or wanted.start() <= boundary.start():
            return True
    return False


def shell_write(command: str) -> bool:
    return (
        DIRECT_WRITE.search(command) is not None
        or _following(command, _SED, _SED_OPTION)
        or _following(command, _PERL, _PERL_OPTION)
    )


def file_mutator(command: str) -> bool:
    return (
        DIRECT_MUTATOR.search(command) is not None
        or any(
            _following(command, trigger, option) for trigger, option in _MUTATOR_OPTIONS
        )
        or _git_mutator(command)
    )


def agent_config_installer(command: str) -> bool:
    return DIRECT_INSTALLER.search(command) is not None or _following(
        command, _FALLOW_INIT, _FALLOW_OPTION
    )


def add_noqa(command: str) -> bool:
    return _following(command, _RUFF, _NOQA)


# A message quotes patterns and paths: blank it, but judge a heredoc's first line.
_MESSAGE = re.compile(
    r"""(?m)\\.|\$'(?:\\.|[^'\\])*'|'[^']*'|"(?:\\.|[^"\\])*"|<<-?[ \t]*(['"]?)(\w+)\1([^\n]*\n)(?:[^\n]*\n)*?[ \t]*\2[ \t]*$"""
)


def mask_messages(command: str) -> str:
    return _MESSAGE.sub(lambda m: m[3] or "''", command)
