"""Best-effort scans of observations against every secret bound for a run.

`Redacted` records that a scan ran; it is not proof against arbitrary encodings
or proof that the caller supplied the current run's secrets (ADR-0026).
"""

import json
import re
from base64 import b64encode, urlsafe_b64encode
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Literal, Self

from aqa_runner.bound_secrets import VALUE_WHITESPACE, BoundSecret

_SCANNED = object()


class Redacted(str):
    """Text scanned by a Redactor. String operations deliberately lose the mark."""

    def __new__(cls, text: str, *, _key: object = None) -> Self:
        if _key is not _SCANNED:
            raise TypeError("only a Redactor can create Redacted text")
        return super().__new__(cls, text)

    def __reduce__(self) -> tuple[type[str], tuple[str]]:
        return str, (str(self),)


type _Groups = tuple[tuple[int, re.Pattern[str]], ...]
type _Transitions = dict[_Groups, dict[int, list[int]]]
type _Multiplicity = Literal["", "?", "*", "+"]


@dataclass(frozen=True)
class _Token:
    groups: _Groups
    multiplicity: _Multiplicity = ""


class Redactor:
    """An immutable set of bound values, with a compiled scan made once per run."""

    def __init__(self, secrets: Iterable[BoundSecret]) -> None:
        self._secrets = tuple(
            sorted(secrets, key=lambda secret: -len(secret.value.get_secret_value()))
        )
        self._paths = tuple(
            (f"[SECRET:{secret.name}]", _paths(secret.value.get_secret_value()))
            for secret in self._secrets
        )
        self._groups = frozenset(
            token.groups for _, paths in self._paths for path in paths for token in path
        )

    def covers(self, secret: BoundSecret) -> bool:
        """Whether both this secret's name and its value belong to this scan."""
        return any(
            held.name == secret.name and held.value == secret.value
            for held in self._secrets
        )

    def redact(self, text: str, *, limit: int | None = None) -> Redacted:
        """Replace matches in any case, then apply the optional character limit."""
        transitions = {groups: _transitions(groups, text) for groups in self._groups}
        matches: dict[int, tuple[int, str]] = {}
        for marker, paths in self._paths:
            for start, end in _endpoints(paths, transitions, len(text)).items():
                matches.setdefault(start, (end, marker))
        pieces: list[str] = []
        cursor = 0
        for start in sorted(matches):
            if start >= cursor:
                end, marker = matches[start]
                pieces.extend((text[cursor:start], marker))
                cursor = end
        pieces.append(text[cursor:])
        return Redacted("".join(pieces)[:limit], _key=_SCANNED)


def _transitions(groups: _Groups, text: str) -> dict[int, list[int]]:
    edges: dict[int, list[int]] = {}
    for width, pattern in groups:
        for found in pattern.finditer(text):
            edges.setdefault(found.start(), []).append(found.start() + width)
    return edges


def _suffix(
    token: _Token,
    edges: dict[int, list[int]],
    following: dict[int, int] | None,
    length: int,
) -> dict[int, int]:
    current: dict[int, int] = {}
    if token.multiplicity in ("?", "*"):
        current = (
            dict(enumerate(range(length + 1)))
            if following is None
            else following.copy()
        )
    repeated = token.multiplicity in ("*", "+")
    starts: Iterable[int] = range(length, -1, -1) if repeated else edges
    for start in starts:
        for end in edges.get(start, ()):
            matched = end if following is None else following.get(end, -1)
            if repeated:
                matched = max(matched, current.get(end, -1))
            if matched >= 0:
                current[start] = max(current.get(start, -1), matched)
    return current


def _endpoints(
    paths: tuple[tuple[_Token, ...], ...],
    transitions: _Transitions,
    length: int,
) -> dict[int, int]:
    best: dict[int, int] = {}
    for path in paths:
        following = None
        for token in reversed(path):
            following = _suffix(token, transitions[token.groups], following, length)
            if not following:
                break
        for start, end in (following or {}).items():
            if end > start:
                best[start] = max(best.get(start, -1), end)
    return best


def _compile(width: int, pattern: str) -> tuple[int, re.Pattern[str]]:
    return width, re.compile(
        pattern if width == 1 else "(?=(?:" + pattern + "))", re.IGNORECASE
    )


def _token(forms: Iterable[str], multiplicity: _Multiplicity = "") -> _Token:
    groups: dict[int, list[str]] = {}
    for form in set(forms):
        groups.setdefault(len(form), []).append(re.escape(form))
    return _Token(
        tuple(
            _compile(width, "|".join(sorted(group)))
            for width, group in sorted(groups.items())
        ),
        multiplicity,
    )


def _percent(text: str) -> str:
    return "".join(f"%{byte:02x}" for byte in text.encode())


def _character_forms(char: str) -> set[str]:
    forms: set[str] = set()
    for case in (char, char.upper(), char.lower(), char.casefold()):
        forms.update((case, _percent(case), json.dumps(case)[1:-1]))
        if len(case) > 1:
            forms.update(
                "".join(written)
                for written in product(*((part, _percent(part)) for part in case))
            )
        if not case.isascii():
            forms.add("".join(f"\\x{byte:02x}" for byte in case.encode()))
    if ord(char) < 32 or 127 <= ord(char) < 160:
        forms.add(f"\\x{ord(char):02x}")
    if char == "'":
        forms.update(("''", "\\'"))
    return forms


_SPACE = _token(
    ["+", *(form for c in VALUE_WHITESPACE for form in _character_forms(c))]
)
_PARTIAL = _Token((_compile(1, "[a-z0-9+/_-]"), _compile(3, "%[0-9a-f]{2}")), "?")
_PADDING = _token(("=", "%3d"), "*")


def _needle(value: str) -> tuple[_Token, ...]:
    parts = []
    for found in re.finditer(f"[{re.escape(VALUE_WHITESPACE)}]+|.", value, re.DOTALL):
        text = found[0]
        if text[0] in VALUE_WHITESPACE:
            # Producers trim ends and collapse interior whitespace runs.
            parts.append(
                _Token(
                    _SPACE.groups,
                    "*" if found.start() == 0 or found.end() == len(value) else "+",
                )
            )
        else:
            parts.append(
                _token(_character_forms(text), "?" if text in "\u200b\u00ad" else "")
            )
    return tuple(parts)


def _paths(value: str) -> tuple[tuple[_Token, ...], ...]:
    encodings = {value.encode()}
    if all(ord(char) < 256 for char in value):
        encodings.add(value.encode("latin-1"))
    producer_value = value.replace("\u200b", "").replace("\u00ad", "")
    paths = [
        _needle(value),
        _needle(value.lower()),
        _needle(producer_value),
        _needle(producer_value.lower()),
    ]
    for raw in encodings:
        for alignment in range(3):
            # Only characters whose six bits lie wholly inside the value.
            first, last = (8 * alignment + 5) // 6, (8 * (alignment + len(raw))) // 6
            for encode in (b64encode, urlsafe_b64encode):
                core = encode(b"\0" * alignment + raw + b"\0\0").decode()[first:last]
                if len(core) >= 4:
                    before = (_PARTIAL,) if alignment else ()
                    after = (_PARTIAL,) if (alignment + len(raw)) * 8 % 6 else ()
                    paths.append(before + _needle(core) + after + (_PADDING,))
    return tuple(dict.fromkeys(paths))


NO_SECRETS = Redactor(())

# The most of an error's presentation kept: the rest of Playwright's message
# can hold what the page chose.
REASON_CHARS = 200

# The call a Playwright error's message names first, such as
# `ElementHandle.evaluate`: Playwright's own words, never the page's, since
# its client puts every API call's name first (1.63's `wrap_api_call`,
# `f"{apiName}: {error}"`). Bounded, so even a line that broke that rule
# could give at most a short name of letters.
PLAYWRIGHT_CALL = re.compile(r"[A-Z][A-Za-z]{0,40}\.[a-z][A-Za-z]{0,40}(?=: )")

_WITHHELD = "the rest is withheld, since the page was handed a test secret"


def error_text(
    error: Exception, redactor: Redactor, *, ours: str | None, withheld: bool
) -> Redacted:
    """`error` as text a result may carry: `ours`, the caller's own complete
    message for an error it owns (even an empty one), or else the error's
    class and the first line of its message, which the page may have chosen.
    When `withheld`, an untrusted message keeps only the call it names.

    Three scans, never a loop, each over the complete text of its stage
    (ADR-0026's A2 amendment): the selected message, before any line, call,
    escape or cut is taken from it; the whole assembly, so a value spanning
    a join is found; and the escaped first line, since escaping can write a
    bound value's characters, before the cut to `REASON_CHARS`."""
    selected = redactor.redact(str(error) if ours is None else ours)
    if ours is not None:
        assembled = str(selected)
    elif withheld:
        call = PLAYWRIGHT_CALL.match(selected)
        named = f"{call[0]}: " if call else ""
        assembled = f"{type(error).__name__}: {named}{_WITHHELD}"
    else:
        assembled = f"{type(error).__name__}: {selected}"
    line = redactor.redact(assembled).split("\n", 1)[0]
    escaped = "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in line
    )
    return redactor.redact(escaped, limit=REASON_CHARS)
