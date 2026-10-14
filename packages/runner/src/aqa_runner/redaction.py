"""Best-effort scans of observations against every secret bound for a run.

`Redacted` records that a scan ran; it is not proof against arbitrary encodings
or proof that the caller supplied the current run's secrets (ADR-0026).
"""

import json
import re
from base64 import b64encode, urlsafe_b64encode
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import product
from typing import Literal, Self

from aqa_runner.bound_secrets import GAP, VALUE_WHITESPACE, BoundSecret

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
# A path's tail: its first token and the number of the tail after it (None
# at the path's end), so paths that share a tail share its number.
type _Tail = tuple["_Token", int | None]


@dataclass(frozen=True)
class _Token:
    groups: _Groups
    multiplicity: _Multiplicity = ""
    # Whether gaps (`GAP`) may come before each of its units: a page can
    # split a value with them (#195).
    gapped: bool = False


class Redactor:
    """An immutable set of bound values, with a compiled scan made once per run."""

    def __init__(self, secrets: Iterable[BoundSecret]) -> None:
        self._secrets = tuple(
            sorted(secrets, key=lambda secret: -len(secret.value.get_secret_value()))
        )
        tails: dict[_Tail, int] = {}
        reads: list[int] = []
        self._paths = tuple(
            (
                f"[SECRET:{secret.name}]",
                tuple(
                    _numbered(path, tails, reads)
                    for path in _paths(secret.value.get_secret_value())
                ),
            )
            for secret in self._secrets
        )
        self._tails = tuple(tails)
        self._reads = tuple(reads)
        self._groups = frozenset(token.groups for token, _ in self._tails)
        self._gapped = any(token.gapped for token, _ in self._tails)

    @property
    def empty(self) -> bool:
        """Whether this scan holds no secret: the run binds none."""
        return not self._secrets

    def covers(self, secret: BoundSecret) -> bool:
        """Whether both this secret's name and its value belong to this scan."""
        return any(
            held.name == secret.name and held.value == secret.value
            for held in self._secrets
        )

    def finds(self, text: str) -> bool:
        """Whether `text` holds any spelling a scan replaces, changing
        nothing: the check for text already scanned, in which a marker can
        still spell a value (its secret's name, say)."""
        return bool(self._matches(text))

    def spells(self, value: object) -> bool:
        """Whether any string in `value`, a JSON value, its keys aside, holds
        a spelling this scan finds, as written or JSON-escaped, as a journal
        line writes it: replay's preflight and the navigator's tools refuse
        or drop by this one rule."""
        return any(
            self.finds(text) or self.finds(json.dumps(text)) for text in _strings(value)
        )

    @property
    def markers_spell_a_value(self) -> bool:
        """Whether a marker this scan writes, `[SECRET:<name>]`, spells a
        bound value (a value that is a secret's name, say), which no scan
        can then take out of what it marked."""
        return any(self.finds(marker) for marker, _ in self._paths)

    def redact(self, text: str, *, limit: int | None = None) -> Redacted:
        """Replace matches in any case, then apply the optional character limit."""
        matches = self._matches(text)
        pieces: list[str] = []
        cursor = 0
        for start in sorted(matches):
            if start >= cursor:
                end, marker = matches[start]
                pieces.extend((text[cursor:start], marker))
                cursor = end
        pieces.append(text[cursor:])
        return Redacted("".join(pieces)[:limit], _key=_SCANNED)

    def _matches(self, text: str) -> dict[int, tuple[int, str]]:
        """Each match's start, with its end and its marker."""
        scan = _Scan(
            {groups: _transitions(groups, text) for groups in self._groups},
            _gaps(text) if self._gapped else {},
            len(text),
            self._tails,
            self._reads,
        )
        matches: dict[int, tuple[int, str]] = {}
        for marker, paths in self._paths:
            for start, end in _endpoints(paths, scan).items():
                matches.setdefault(start, (end, marker))
        return matches


def _strings(value: object) -> list[str]:
    """Every string in `value`, a JSON value, its keys aside; `TypeError` for
    anything else, so no other object passes as holding no string."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return _strings(list(value.values()))
    if isinstance(value, list | tuple):
        return [text for item in value for text in _strings(item)]
    if value is None or isinstance(value, bool | int | float):
        return []
    raise TypeError(f"{type(value).__name__} isn't a JSON value")


def _transitions(groups: _Groups, text: str) -> dict[int, list[int]]:
    edges: dict[int, list[int]] = {}
    for width, pattern in groups:
        for found in pattern.finditer(text):
            edges.setdefault(found.start(), []).append(found.start() + width)
    return edges


def _numbered(
    path: tuple[_Token, ...], tails: dict[_Tail, int], reads: list[int]
) -> tuple[int, ...]:
    """`path` as its tails' numbers, longest first, numbering each new tail.
    `reads` counts the reads of each tail's table in a scan: one by the tail
    a token longer, and one by each path that is the whole tail."""
    numbers: list[int] = []
    rest: int | None = None
    for token in reversed(path):
        if (token, rest) not in tails:
            tails[token, rest] = len(tails)
            reads.append(0)
            if rest is not None:
                reads[rest] += 1
        rest = tails[token, rest]
        numbers.append(rest)
    reads[numbers[-1]] += 1
    return tuple(reversed(numbers))


class _Scan:
    """One text's edges, and the tables computed from them: from each
    offset, the furthest end of a tail. A tail's table is computed once and
    dropped after its last read, so a scan holds only tables still to read."""

    def __init__(
        self,
        transitions: _Transitions,
        gaps: dict[int, list[int]],
        length: int,
        tails: tuple[_Tail, ...],
        reads: tuple[int, ...],
    ) -> None:
        self.transitions = transitions
        self.length = length
        self.gaps = gaps
        # For each offset a gap ends at, where those gaps start.
        self.into: dict[int, list[int]] = {}
        for start, ends in self.gaps.items():
            for end in ends:
                self.into.setdefault(end, []).append(start)
        self.tails = tails
        self.unread = list(reads)
        self.tables: dict[int, dict[int, int]] = {}

    def table(self, path: tuple[int, ...]) -> dict[int, int]:
        """The table of `path`, given as its tails' numbers, longest first:
        each tail's from the next one's, from the longest still held."""
        held = next(
            (i for i, tail in enumerate(path) if tail in self.tables), len(path)
        )
        following = self.tables[path[held]] if held < len(path) else None
        for tail in reversed(path[:held]):
            token, rest = self.tails[tail]
            # Nothing matches before a tail that matches nowhere.
            if following is None or following:
                following = _suffix(token, following, self)
            self.tables[tail] = following
            self._read(rest)
        table = self.tables[path[0]]
        self._read(path[0])
        return table

    def _read(self, tail: int | None) -> None:
        if tail is not None:
            self.unread[tail] -= 1
            if not self.unread[tail]:
                del self.tables[tail]


def _suffix(
    token: _Token, following: dict[int, int] | None, scan: _Scan
) -> dict[int, int]:
    """From each offset, the furthest end of `token` and then `following`
    (the rest of its path; None at its end)."""
    edges = scan.transitions[token.groups]
    gaps = scan.gaps if token.gapped else {}
    current: dict[int, int] = {}
    if token.multiplicity in ("?", "*"):
        current = (
            dict(enumerate(range(scan.length + 1)))
            if following is None
            else following.copy()
        )
    if token.multiplicity in ("*", "+"):
        return _repeated(edges, gaps, following, current)
    through = _once(edges, following)
    if gaps:
        _carry(through, scan.into)
    if not current:
        return through
    for start, best in through.items():
        current[start] = max(current.get(start, -1), best)
    return current


def _once(
    edges: dict[int, list[int]], following: dict[int, int] | None
) -> dict[int, int]:
    """From each offset, the furthest end through one unit, then `following`."""
    through: dict[int, int] = {}
    for start, ends in edges.items():
        for end in ends:
            matched = end if following is None else following.get(end, -1)
            if matched > through.get(start, -1):
                through[start] = matched
    return through


def _carry(through: dict[int, int], into: dict[int, list[int]]) -> None:
    """`through` from the start of each run of gaps before an offset it holds."""
    waiting = list(through)
    while waiting:
        end = waiting.pop()
        for start in into.get(end, ()):
            if through[end] > through.get(start, -1):
                through[start] = through[end]
                waiting.append(start)


def _repeated(
    edges: dict[int, list[int]],
    gaps: dict[int, list[int]],
    following: dict[int, int] | None,
    current: dict[int, int],
) -> dict[int, int]:
    """`current` after one or more units, each after gaps: backward, so a
    repeat or a gap reads ends already final."""
    through: dict[int, int] = {}
    for start in sorted(edges.keys() | gaps.keys(), reverse=True):
        best = -1
        for end in gaps.get(start, ()):
            best = max(best, through.get(end, -1))
        for end in edges.get(start, ()):
            matched = end if following is None else following.get(end, -1)
            best = max(best, matched, current.get(end, -1))
        if best >= 0:
            through[start] = best
            current[start] = max(current.get(start, -1), best)
    return current


def _endpoints(paths: tuple[tuple[int, ...], ...], scan: _Scan) -> dict[int, int]:
    best: dict[int, int] = {}
    for path in paths:
        for start, end in scan.table(path).items():
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


# A sigma's lowercase is final or not by what follows it (Unicode's
# Final_Sigma), which a page's gaps and text change, so each spells all three.
_SIGMAS = frozenset(
    (
        "\N{GREEK CAPITAL LETTER SIGMA}",
        "\N{GREEK SMALL LETTER SIGMA}",
        "\N{GREEK SMALL LETTER FINAL SIGMA}",
    )
)


def _character_forms(char: str) -> set[str]:
    forms: set[str] = set()
    cases = {char, char.upper(), char.lower(), char.casefold()}
    if cases & _SIGMAS:
        cases |= _SIGMAS
    for case in cases:
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


def _utf8(mark: str) -> str:
    """One character's UTF-8 bytes, each written `mark` and two hex digits."""
    byte = mark + "[89ab][0-9a-f]"
    return "|".join(
        (
            mark + "[cd][0-9a-f]" + byte,
            mark + "e[0-9a-f]" + byte * 2,
            mark + "f[0-4]" + byte * 3,
        )
    )


def _from_bytes(escaped: str) -> str:
    return bytes(
        int(pair, 16) for pair in re.findall("[0-9a-f]{2}", escaped, re.IGNORECASE)
    ).decode("utf-8", "replace")


def _from_units(escaped: str) -> str:
    units = re.split(r"\\u", escaped, flags=re.IGNORECASE)[1:]
    joined = "".join(chr(int(unit, 16)) for unit in units)
    return joined.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


# A gap escaped, in each spelling the scan finds a value's own characters in
# or Python's repr writes (a `RefError`'s quoted ref, `error_text`'s
# escapes): UTF-8 bytes percent-encoded or as `\x`, JSON's `\u` and its
# surrogate pairs, repr's `\x`, `\u` and `\U`. Each is decoded and kept
# only when it is one character of `GAP`, so the private-use areas' 137,468
# code points cost no table.
_ESCAPED = tuple(
    (re.compile(f"(?=({pattern}))", re.IGNORECASE), decode)
    for pattern, decode in (
        (_utf8("%"), _from_bytes),
        (_utf8(r"\\x"), _from_bytes),
        (
            r"\\x[0-9a-f]{2}",
            lambda escaped: bytes.fromhex(escaped[2:]).decode("latin-1"),
        ),
        (r"\\ud[89ab][0-9a-f]{2}\\ud[c-f][0-9a-f]{2}|\\u[0-9a-f]{4}", _from_units),
        (r"\\U00(?:0[0-9a-f]|10)[0-9a-f]{4}", _from_units),
    )
)


def _gaps(text: str) -> dict[int, list[int]]:
    """Where a gap starts in `text`, as written or escaped, and where it ends."""
    edges: dict[int, list[int]] = {}
    for found in GAP.finditer(text):
        edges.setdefault(found.start(), []).append(found.end())
    for pattern, decode in _ESCAPED:
        for found in pattern.finditer(text):
            if GAP.fullmatch(decode(found[1])):
                edges.setdefault(found.start(), []).append(found.end(1))
    return edges


def _needle(value: str) -> tuple[_Token, ...]:
    parts = []
    for found in re.finditer(f"[{re.escape(VALUE_WHITESPACE)}]+|.", value, re.DOTALL):
        text = found[0]
        if text[0] in VALUE_WHITESPACE:
            # Producers trim ends and collapse interior whitespace runs, and a
            # page can leave out a run of the value's own gaps (U+FEFF), each
            # run on its own.
            optional = (
                found.start() == 0 or found.end() == len(value) or not GAP.sub("", text)
            )
            parts.append(_Token(_SPACE.groups, "*" if optional else "+"))
        else:
            # A value's own gap can be left out.
            parts.append(_token(_character_forms(text), "?" if GAP.match(text) else ""))
    return tuple(parts)


def _chains(tokens: tuple[_Token, ...]) -> list[tuple[_Token, ...]]:
    """Paths for `tokens`, each unit after the first allowed a gap before
    it. A match starts with a unit, never a gap, so an optional first token
    is either required or left out."""
    if not tokens:
        return []
    first, *rest = tokens
    chain = (first, *(replace(token, gapped=True) for token in rest))
    if first.multiplicity in ("", "+") or not rest:
        return [chain]
    required = replace(first, multiplicity="" if first.multiplicity == "?" else "+")
    return [(required, *chain[1:]), *_chains(tuple(rest))]


def _paths(value: str) -> tuple[tuple[_Token, ...], ...]:
    encodings = {value.encode()}
    if all(ord(char) < 256 for char in value):
        encodings.add(value.encode("latin-1"))
    paths: list[tuple[_Token, ...]] = []
    # Lowercasing or casefolding can lengthen a value (İ, ß): then each
    # character of the result is a unit of its own, which a gap can split.
    for case in (value, value.lower(), value.casefold()):
        # A page can leave out the value's own gaps, U+FEFF (JavaScript
        # whitespace) included.
        for shown in (case, GAP.sub("", case)):
            paths.extend(_chains(_needle(shown)))
    for raw in encodings:
        for alignment in range(3):
            # Only characters whose six bits lie wholly inside the value.
            first, last = (8 * alignment + 5) // 6, (8 * (alignment + len(raw))) // 6
            for encode in (b64encode, urlsafe_b64encode):
                core = encode(b"\0" * alignment + raw + b"\0\0").decode()[first:last]
                if len(core) >= 4:
                    before = (_PARTIAL,) if alignment else ()
                    after = (_PARTIAL,) if (alignment + len(raw)) * 8 % 6 else ()
                    paths.extend(_chains(before + _needle(core) + after + (_PADDING,)))
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
    """`error` as text a result may carry: `ours`, the caller's own message
    for an error it owns (even an empty one), or else the error's class and
    its message, which the page may have chosen. Either is cut to its first
    line and `REASON_CHARS`.
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
