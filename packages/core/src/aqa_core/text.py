"""Text as compiled scripts compare it (DATA_MODEL §7, ADR-0025)."""

import re

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
