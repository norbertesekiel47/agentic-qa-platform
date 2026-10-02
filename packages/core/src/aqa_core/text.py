"""Text as compiled scripts compare it (DATA_MODEL §7, ADR-0025)."""

import re

# Unicode's private-use areas, where icon fonts put their glyphs: Chromium
# includes a glyph that CSS ::before renders in an element's accessible name
# and rendered text (LAB_NOTES, 2026-09-29).
_PRIVATE_USE = re.compile("[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]")


def normalize(text: str) -> str:
    """`text` with private-use glyphs stripped, then each run of whitespace,
    non-breaking spaces included, made one space, and the ends trimmed."""
    return " ".join(_PRIVATE_USE.sub("", text).split())
