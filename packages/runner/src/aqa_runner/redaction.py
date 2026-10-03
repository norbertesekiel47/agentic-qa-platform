"""Best-effort scans of observations against every secret bound for a run.

`Redacted` records that a scan ran; it is not proof against arbitrary encodings
or proof that the caller supplied the current run's secrets (ADR-0026).
"""

import json
import re
from base64 import b64encode, urlsafe_b64encode
from collections.abc import Iterable
from typing import Self

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


class Redactor:
    """An immutable set of bound values, with a compiled scan made once per run."""

    def __init__(self, secrets: Iterable[BoundSecret]) -> None:
        self._secrets = tuple(
            sorted(secrets, key=lambda secret: -len(secret.value.get_secret_value()))
        )
        self._markers = {
            f"s{i}": f"[SECRET:{secret.name}]" for i, secret in enumerate(self._secrets)
        }
        self._pattern = (
            re.compile(
                "|".join(
                    f"(?P<s{i}>{_patterns(secret.value.get_secret_value())})"
                    for i, secret in enumerate(self._secrets)
                ),
                re.IGNORECASE,
            )
            if self._secrets
            else None
        )

    def covers(self, secret: BoundSecret) -> bool:
        """Whether both this secret's name and its value belong to this scan."""
        return any(
            held.name == secret.name and held.value == secret.value
            for held in self._secrets
        )

    def redact(self, text: str, *, limit: int | None = None) -> Redacted:
        """Replace matches in any case, then apply the optional character limit."""
        if self._pattern is not None:
            text = self._pattern.sub(self._replacement, text)
        return Redacted(text[:limit], _key=_SCANNED)

    def _replacement(self, match: re.Match[str]) -> str:
        return self._markers[match.lastgroup or ""] if match[0] else ""


def _alternatives(forms: Iterable[str]) -> str:
    return (
        "(?:" + "|".join(sorted(set(forms), key=lambda form: (-len(form), form))) + ")"
    )


def _written(char: str) -> str:
    encoded = "".join(f"%{byte:02x}" for byte in char.encode())
    return _alternatives((re.escape(char), re.escape(encoded)))


def _character(char: str) -> str:
    cases = (char, char.upper(), char.lower(), char.casefold())
    # Expand case first, then allow each resulting character's percent form.
    patterns = [
        "".join(_written(part) for part in case) for case in cases if len(case) > 1
    ]
    forms: set[str] = set()
    for case in cases:
        forms.update(
            (
                case,
                "".join(f"%{byte:02x}" for byte in case.encode()),
                json.dumps(case)[1:-1],
            )
        )
        if not case.isascii():
            forms.add("".join(f"\\x{byte:02x}" for byte in case.encode()))
    if ord(char) < 32 or 127 <= ord(char) < 160:
        forms.add(f"\\x{ord(char):02x}")
    if char == "'":
        forms.update(("''", "\\'"))
    # Longer producer/percent forms win over literal prefixes, such as
    # a doubled quote or %25; expanded case forms come before single letters.
    patterns.append(_alternatives(re.escape(form) for form in forms))
    pattern = "(?:" + "|".join(patterns) + ")"
    return pattern + "?" if char in "\u200b\u00ad" else pattern


_SPACE = _alternatives(
    [re.escape("+"), *(_character(char) for char in VALUE_WHITESPACE)]
)
_PARTIAL = "(?:[a-z0-9+/_-]|%[0-9a-f]{2})?"
_PADDING = "(?:=|%3d)*"


def _needle(value: str) -> str:
    parts = []
    for found in re.finditer(f"[{re.escape(VALUE_WHITESPACE)}]+|.", value, re.DOTALL):
        text = found[0]
        if text[0] in VALUE_WHITESPACE:
            # Producers trim ends and collapse interior whitespace runs.
            parts.append(
                _SPACE
                + ("*" if found.start() == 0 or found.end() == len(value) else "+")
            )
        else:
            parts.append(_character(text))
    return "".join(parts)


def _patterns(value: str) -> str:
    encodings = {value.encode()}
    if all(ord(char) < 256 for char in value):
        encodings.add(value.encode("latin-1"))
    forms = [_needle(value)]
    for raw in encodings:
        for alignment in range(3):
            # Only characters whose six bits lie wholly inside the value.
            first, last = (8 * alignment + 5) // 6, (8 * (alignment + len(raw))) // 6
            for encode in (b64encode, urlsafe_b64encode):
                core = encode(b"\0" * alignment + raw + b"\0\0").decode()[first:last]
                if len(core) >= 4:
                    before = _PARTIAL if alignment else ""
                    after = _PARTIAL if (alignment + len(raw)) * 8 % 6 else ""
                    forms.append(before + _needle(core) + after + _PADDING)
    return _alternatives(forms)


NO_SECRETS = Redactor(())
