"""Text as compiled scripts compare it: private-use glyphs stripped and
whitespace collapsed (DATA_MODEL §7; ADR-0025; LAB_NOTES 2026-09-29)."""

import pytest
from aqa_core.text import normalize


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        # An icon font's glyph from CSS ::before, then a non-breaking space,
        # as Chromium puts both in a Conduit header link's accessible name.
        ("\uf218\xa0New Article", "New Article"),
        ("\uf218New Article", "New Article"),
        # Private-use glyphs outside the Basic Multilingual Plane too.
        ("\U000f0001 Saved \U0010fffd", "Saved"),
        # Every whitespace run is one space, and the ends are trimmed.
        ("  Post\n\tComment\u2003 ", "Post Comment"),
        # A glyph between two spaces leaves one space, not two.
        ("Favorite \ue900 Article", "Favorite Article"),
        # Each private-use range's first and last code points go, and the
        # character after the BMP range stays.
        ("\ue000a\uf8ff \ue000\uf900", "a \uf900"),
        ("\U000f0000b\U000ffffd", "b"),
        ("\U00100000c\U0010fffd", "c"),
        # A soft hyphen and a zero-width space go, as Playwright drops them
        # from accessible names: "Pay&shy;ment" renders as one word.
        ("Pay\xadment\u200b now", "Payment now"),
        # Case, punctuation and other symbols are kept.
        ("Pay $5.00 \u2014 now \u2605", "Pay $5.00 \u2014 now \u2605"),
        ("\uf218\xa0", ""),
    ],
)
def test_normalize_strips_private_use_glyphs_and_collapses_whitespace(
    text: str, normalized: str
) -> None:
    assert normalize(text) == normalized
