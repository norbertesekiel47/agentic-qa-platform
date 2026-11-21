"""`redaction.one_line`, how a record keeps text the model or the page chose
(#334): scanned, on one line, each non-printable character escaped as
`error_text` escapes a reason's, then scanned again and cut. Every expected
value is a literal written before the function existed; none is computed by a
production call."""

import pytest
from aqa_runner.redaction import NO_SECRETS, REASON_CHARS, Redactor, one_line

from packages.runner.tests.result_redaction_fixtures import fake_secret

PADDING = "p" * 190


def bound(value: str) -> Redactor:
    return Redactor([fake_secret("FAKE_ESCAPE", value, "https://app.test")])


def test_a_bidi_override_and_a_c1_control_are_escaped() -> None:
    kept = one_line("the badge \u202eis green\x9b", NO_SECRETS)

    assert kept == r"the badge \u202eis green\x9b"
    assert {"\u202e", "\x9b"}.isdisjoint(kept)


def test_whitespace_folds_to_single_spaces_and_is_not_escaped() -> None:
    kept = one_line("one\u0085two\u2028three\tfour\n\nfive ", NO_SECRETS)

    assert kept == "one two three four five"


@pytest.mark.parametrize(
    ("value", "said"),
    [
        pytest.param(r"fake\x9btail", "said fake\x9btail", id="c1"),
        pytest.param(r"fake\u202etail", "said fake\u202etail", id="bidi"),
    ],
)
def test_a_value_only_the_escape_spells_is_redacted(value: str, said: str) -> None:
    assert one_line(said, bound(value)) == "said [SECRET:FAKE_ESCAPE]"


def test_a_value_the_fold_makes_whole_is_redacted_before_the_escape() -> None:
    # The text writes the value's space as U+0085, and the value holds U+13430,
    # whose escape no scan reads as the character: only a scan of the folded
    # text, before the escape, finds it.
    kept = one_line("said ab\U00013430\x85cd", bound("ab\U00013430 cd"))

    assert kept == "said [SECRET:FAKE_ESCAPE]"


def test_the_escaped_text_is_scanned_before_the_cut() -> None:
    kept = one_line(f"{PADDING}fake\x9btail", bound(r"fake\x9btail"))

    assert kept == f"{PADDING}[SECRET:FA"
    assert len(kept) == REASON_CHARS
    assert r"fake\x9" not in kept


def test_a_long_escaped_text_is_cut_at_the_reason_bound() -> None:
    kept = one_line("\u202e" * 100, NO_SECRETS)

    assert kept == r"\u202e" * 33 + r"\u"
    assert len(kept) == REASON_CHARS
