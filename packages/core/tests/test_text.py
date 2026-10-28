"""Text as compiled scripts compare it: private-use glyphs stripped and
whitespace collapsed (DATA_MODEL §7; ADR-0025; LAB_NOTES 2026-09-29); and
the child process that bounds a search of it in time (ADR-0024)."""

import pytest
from aqa_core.text import (
    any_has_pattern,
    has_pattern,
    has_text,
    normalize,
    search_found,
)


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
        # A letter beyond ASCII is a word character too, as the browser's
        # match of a held value reads one (ADR-0025's #58 amendment).
        ("\u00e9foo", "foo", False),
        ("\u00c9foo", "\u00e9foo", True),
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


# A search child that failed is an error, never a miss: one that exits
# non-zero whatever it printed, and one that exits 0 without a 0 or a 1.
@pytest.mark.parametrize(
    ("returncode", "stdout"), [(1, b"1"), (0, b"found")], ids=["status", "output"]
)
def test_a_search_child_that_failed_raises(returncode: int, stdout: bytes) -> None:
    with pytest.raises(RuntimeError) as raised:
        search_found(
            returncode, stdout, b"Traceback (most recent call last):\nMemoryError\n"
        )

    assert str(raised.value) == f"the search process failed ({returncode}): MemoryError"


# Text that isn't ASCII, and a lone surrogate, reach the bounded search as
# they are.
@pytest.mark.parametrize(
    ("texts", "pattern"),
    [(("cart", "Caf\u00e9 \u2713"), "\u00e9 \u2713$"), (("x\ud800y",), "\\ud800")],
    ids=["not ASCII", "lone surrogate"],
)
def test_a_bounded_pattern_search_reads_each_text_as_it_is(
    texts: tuple[str, ...], pattern: str
) -> None:
    assert any_has_pattern(texts, pattern) is True
