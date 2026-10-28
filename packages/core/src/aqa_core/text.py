"""Text as compiled scripts compare it (DATA_MODEL §7, ADR-0025), and the
child process that bounds a search of it in time (ADR-0024, "Bounded text
searches")."""

import json
import re
import subprocess
import sys
from collections.abc import Sequence

# Stripped before comparing:
# - Unicode's private-use areas, where icon fonts put their glyphs. Chromium
#   puts a glyph that CSS ::before renders into the element's accessible name
#   (LAB_NOTES, 2026-09-29); rendered text holds one only when the page
#   writes the character itself.
# - The soft hyphen and the zero-width space, which Playwright 1.63 drops from
#   accessible names and which rendered text keeps, so "Pay&shy;ment" still
#   reads as "Payment".
_STRIPPED = re.compile(
    "[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd\xad\u200b]"
)


def normalize(text: str) -> str:
    """`text` with private-use glyphs, soft hyphens and zero-width spaces
    stripped, then each run of whitespace, non-breaking spaces included, made
    one space, and the ends trimmed."""
    return " ".join(_STRIPPED.sub("", text).split())


def has_text(rendered: str, literal: str) -> bool:
    """Whether `literal` is in `rendered` as whole words, ignoring case: both
    normalized and casefolded. Where the literal starts or ends with a word
    character, no word character may touch it there, so "1" isn't found in
    "10" but "(3)" is found in "Cart(3)" (ADR-0025). Text with no spaces
    between words, such as Japanese, has no boundaries to find, so a claim
    about part of it needs a `pattern`."""
    words = normalize(literal).casefold()
    before = r"(?<!\w)" if re.match(r"\w", words) else ""
    after = r"(?!\w)" if re.search(r"\w\Z", words) else ""
    found = re.search(
        f"{before}{re.escape(words)}{after}", normalize(rendered).casefold()
    )
    return found is not None


def has_pattern(rendered: str, pattern: str) -> bool:
    """Whether the Python regex `pattern` matches anywhere in `rendered`,
    normalized, with only the flags the pattern writes, such as (?i)
    (ADR-0025)."""
    return re.search(pattern, normalize(rendered)) is not None


# The most one bounded search may take, in seconds, starting the process
# included.
SEARCH_SECONDS = 2

# What a bounded search's child runs: Python's `re` can backtrack for
# exponential time on a script's pattern, and a search in progress can't be
# interrupted, not even from another thread, so the child is killed at the
# deadline. It runs this fixed code in isolated mode with an empty
# environment, reading the search's kind, needle and haystack from stdin.
# Matching is this module's, so it means what the format says; a URL is
# searched as it is, never normalized.
SEARCH_CHILD = """\
import json, re, sys
from aqa_core.text import has_pattern, has_text
kind, needle, haystack = json.loads(sys.stdin.buffer.read())
if kind == "text":
    found = has_text(haystack, needle)
elif kind == "pattern":
    found = has_pattern(haystack, needle)
elif kind == "patterns":
    found = any(has_pattern(text, needle) for text in haystack)
elif kind == "url":
    found = re.search(needle, haystack) is not None
elif kind == "urls":
    pattern = re.compile(needle)
    found = any(pattern.search(url) is not None for url in haystack)
else:
    sys.exit(f"no search of kind {kind!r}")
sys.stdout.write("1" if found else "0")
"""


class SearchTimeoutError(Exception):
    """A search ran past `SEARCH_SECONDS`, and was stopped: it established
    neither a match nor its absence."""


def search_found(returncode: int | None, stdout: bytes, stderr: bytes) -> bool:
    """Whether a finished search child found its needle. A child that failed
    raises RuntimeError, so a failure never reads as a miss."""
    if returncode != 0 or stdout not in (b"0", b"1"):
        last = stderr.decode(errors="replace").strip().rsplit("\n", 1)[-1]
        raise RuntimeError(f"the search process failed ({returncode}): {last}")
    return stdout == b"1"


def any_has_pattern(texts: Sequence[str], pattern: str) -> bool:
    """Whether `has_pattern` finds `pattern` in any of `texts`, searched in
    one child under one `SEARCH_SECONDS` bound. No texts start no child.
    Raises `SearchTimeoutError` when the search runs out of time."""
    if not texts:
        return False
    # ASCII only, with every other character escaped, so the bytes say the
    # same to the child whatever its locale, a lone surrogate included.
    payload = json.dumps(["patterns", pattern, list(texts)]).encode("ascii")
    try:
        # https://docs.python.org/3.14/library/subprocess.html#subprocess.run:
        # run() kills the child when the timeout expires.
        done = subprocess.run(
            [sys.executable, "-I", "-c", SEARCH_CHILD],
            input=payload,
            capture_output=True,
            env={},
            timeout=SEARCH_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SearchTimeoutError(
            f"the search didn't finish within {SEARCH_SECONDS} s"
        ) from None
    return search_found(done.returncode, done.stdout, done.stderr)
