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
