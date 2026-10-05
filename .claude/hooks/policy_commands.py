"""Command predicates with monotonic searches, preserving the guard's heuristics."""

from __future__ import annotations

import re
from typing import Any

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
# A heredoc opener's closing line comes from _Closings, not a search per opener.
_TOKEN = re.compile(
    r"""\\.|\$'(?:\\.|[^'\\])*'|'[^']*'|"(?:\\.|[^"\\])*"|<<-?[ \t]*(['"]?)(\w+)"""
)
_CLOSING_LINE = re.compile(r"^[ \t]*(\w+)[ \t]*$", re.MULTILINE)


class _Closings:
    """The lines that could close a heredoc, by the one word each holds. Openers
    arrive in order, so a word's next usable line only moves forward."""

    def __init__(self, command: str) -> None:
        self.ends: dict[str, list[int]] = {}
        for line in _CLOSING_LINE.finditer(command):
            self.ends.setdefault(line[1], []).append(line.end())
        self.seen: dict[str, int] = {}
        self.prefixes: dict[str, Any] | None = None

    def after(self, word: str, newline: int) -> int | None:
        """The end of the first line after `newline` that holds just `word`."""
        ends = self.ends.get(word, [])
        index = self.seen.get(word, 0)
        while index < len(ends) and ends[index] <= newline:
            index += 1
        self.seen[word] = index
        return ends[index] if index < len(ends) else None

    def longest(self, word: str, newline: int) -> tuple[int, int] | None:
        """The longest prefix of `word` that closes after `newline`, as its length
        and closing line's end: an unquoted `<<ABC` closes on a later `AB` line."""
        if self.prefixes is None:
            self.prefixes = {}
            for candidate in self.ends:
                node = self.prefixes
                for char in candidate:
                    node = node.setdefault(char, {})
                node[""] = candidate
        node, found = self.prefixes, []
        for char in word:
            child = node.get(char)
            if child is None:
                break
            node = child
            if "" in node:
                found.append(node[""])
        for candidate in reversed(found):
            end = self.after(candidate, newline)
            if end is not None:
                return len(candidate), end
        return None


def _closed(
    token: re.Match[str], newline: int, closings: _Closings
) -> tuple[int, int] | None:
    """Where a heredoc opener's kept text starts and where its heredoc ends, or
    None if no later line closes it."""
    quote, word, after = token[1], token[2], token.end()
    if quote:
        end = closings.after(word, newline) if token.string[after] == quote else None
        return None if end is None else (after + 1, end)
    end = closings.after(word, newline)
    if end is not None:
        return after, end
    prefix = closings.longest(word, newline)
    return None if prefix is None else (token.start(2) + prefix[0], prefix[1])


def mask_messages(command: str) -> str:
    """`command` with each quoted string blanked to `''` and each heredoc's body
    dropped, keeping the rest of its opening line."""
    parts: list[str] = []
    closings: _Closings | None = None
    done = position = 0
    newline = -1
    while token := _TOKEN.search(command, position):
        start = token.start()
        if token[2] is None:
            parts += (command[done:start], "''")
            done = position = token.end()
            continue
        position = start + 1
        if newline < token.end():
            found = command.find("\n", token.end())
            newline = found if found >= 0 else len(command)
        if newline == len(command):
            continue
        closings = closings or _Closings(command)
        closed = _closed(token, newline, closings)
        if closed is not None:
            kept, end = closed
            parts += (command[done:start], command[kept : newline + 1])
            done = position = end
    parts.append(command[done:])
    return "".join(parts)
