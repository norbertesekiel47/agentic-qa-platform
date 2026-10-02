"""Text as compiled scripts compare it: private-use glyphs stripped and
whitespace collapsed (DATA_MODEL §7; ADR-0025; LAB_NOTES 2026-09-29)."""

import pytest
from aqa_core.text import has_pattern, has_text, normalize


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


@pytest.mark.parametrize(
    ("rendered", "literal", "found"),
    [
        # A literal is a whole-word match, so a longer word or number around
        # it doesn't count (ADR-0025).
        ("Unfavorite Article", "Favorite Article", False),
        ("Favorite Article (3)", "Favorite Article", True),
        ("10", "1", False),
        ("Total: 10", "1", False),
        ("1 favorite", "1", True),
        # Case doesn't matter: Chromium's rendered text applies CSS
        # text-transform (LAB_NOTES, 2026-09-29).
        ("POST COMMENT", "Post Comment", True),
        ("STRASSE", "Stra\u00dfe", True),
        # A literal that ends in punctuation still matches at a sentence's end.
        ("Your card has expired.", "card has expired.", True),
        ("Your card has expired.", "has exp", False),
        # The boundary applies only at a literal's edge that is a word
        # character, so punctuation at its edge needs none.
        ("Cart(3)", "(3)", True),
        ("USD$5", "$5", True),
        ("Price: $9.99", "$9.99", True),
        ("A1", "1", False),
        # Text without spaces between words (CJK) has no word boundaries to
        # find, so a claim about part of it needs a pattern.
        ("\u4fdd\u5b58\u3057\u307e\u3057\u305f", "\u4fdd\u5b58", False),
        # Both sides are compared normalized.
        ("Payment due", "Pay\xadment", True),
        ("card \n has   expired", "card has expired", True),
        ("Pay\xadment due", "Payment", True),
    ],
)
def test_text_matches_whole_words_only(
    rendered: str, literal: str, found: bool
) -> None:
    assert has_text(rendered, literal) is found


@pytest.mark.parametrize(
    ("rendered", "pattern", "found"),
    [
        # re.search: a match anywhere, with only the flags the pattern writes.
        ("Classic Hoodie (size M)", "Hoodie", True),
        ("Classic Hoodie", "^Hoodie", False),
        # A pattern without (?i) is case-sensitive, so a claim about case holds.
        ("POST COMMENT", "Post Comment", False),
        ("POST COMMENT", "(?i)post comment", True),
        # The rendered text is normalized first, so . crosses a line break that
        # collapsed into a space.
        ("Classic Hoodie \n  size M", r"Classic Hoodie.*\bM\b", True),
    ],
)
def test_pattern_searches_without_added_flags(
    rendered: str, pattern: str, found: bool
) -> None:
    assert has_pattern(rendered, pattern) is found
