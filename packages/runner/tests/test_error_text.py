"""`redaction.error_text`, the shared presenter of an error (#50 A2, ADR-0026's
A2 amendment): three fixed scans, over the complete selected message, the
complete assembly and the escaped first line, then the length cut. Every
expected value is a literal written before the presenter existed; none is
computed by a production call."""

from dataclasses import dataclass

import pytest
from aqa_runner.redaction import NO_SECRETS, REASON_CHARS, Redactor, error_text

from packages.runner.tests.result_redaction_fixtures import fake_secret

WITHHOLDING = "the rest is withheld, since the page was handed a test secret"
CALL_MESSAGE = "Page.goto: fake diagnostic"


class Error(Exception):
    """Named as Playwright's own error class is, so a presented error reads
    `Error: ...`."""


@dataclass(frozen=True)
class Case:
    secret: tuple[str, str] | None
    message: str
    ours: str | None
    withheld: bool
    expected: str


def present(case: Case) -> str:
    redactor = (
        NO_SECRETS
        if case.secret is None
        else Redactor([fake_secret(*case.secret, "https://app.test")])
    )
    return error_text(
        Error(case.message), redactor, ours=case.ours, withheld=case.withheld
    )


PADDING = "p" * 175

CASES = {
    "class_message_join": Case(
        ("FAKE_JOIN", "Error: Page.goto"),
        CALL_MESSAGE,
        None,
        False,
        "[SECRET:FAKE_JOIN]: fake diagnostic",
    ),
    "class_call_join_withheld": Case(
        ("FAKE_JOIN", "Error: Page.goto"),
        CALL_MESSAGE,
        None,
        True,
        f"[SECRET:FAKE_JOIN]: {WITHHOLDING}",
    ),
    "call_withholding_join": Case(
        ("FAKE_JOIN", "Page.goto: the rest is withheld"),
        CALL_MESSAGE,
        None,
        True,
        "Error: [SECRET:FAKE_JOIN], since the page was handed a test secret",
    ),
    "class_component": Case(
        ("FAKE_CLASS", "Error"),
        CALL_MESSAGE,
        None,
        False,
        "[SECRET:FAKE_CLASS]: Page.goto: fake diagnostic",
    ),
    "scanned_call_is_not_recovered": Case(
        ("FAKE_CALL", "Page.goto"),
        CALL_MESSAGE,
        None,
        True,
        f"Error: {WITHHOLDING}",
    ),
    "fixed_wording": Case(
        ("FAKE_FIXED", "the rest is withheld"),
        CALL_MESSAGE,
        None,
        True,
        "Error: Page.goto: [SECRET:FAKE_FIXED], since the page was handed a test secret",
    ),
    "owned_fixed_message": Case(
        ("FAKE_FIXED", "the probe could not be read"),
        CALL_MESSAGE,
        "the probe could not be read",
        True,
        "[SECRET:FAKE_FIXED]",
    ),
    "full_message_scanned_before_first_line": Case(
        ("FAKE_NEWLINE", "fake\nsecret"),
        "Page.goto: fake\nsecret leftover",
        None,
        False,
        "Error: Page.goto: [SECRET:FAKE_NEWLINE] leftover",
    ),
    "prefix_and_newline_span_the_value": Case(
        ("FAKE_NL", "Error: fake\nsecret"),
        "fake\nsecret leftover",
        None,
        False,
        "[SECRET:FAKE_NL] leftover",
    ),
    "escape_created_literal": Case(
        ("FAKE_ESCAPE", "fake\\x01tail"),
        "Page.goto: fake\x01tail",
        None,
        False,
        "Error: Page.goto: [SECRET:FAKE_ESCAPE]",
    ),
    "escaped_scan_precedes_the_cut": Case(
        ("FAKE_ESCAPE", "fake\\x01tail"),
        f"Page.goto: {PADDING}fake\x01tail",
        None,
        False,
        f"Error: Page.goto: {PADDING}[SECRET",
    ),
}

BENIGN = {
    "untrusted": Case(None, CALL_MESSAGE, None, False, f"Error: {CALL_MESSAGE}"),
    "withheld": Case(
        None, CALL_MESSAGE, None, True, f"Error: Page.goto: {WITHHOLDING}"
    ),
    "owned": Case(
        None,
        CALL_MESSAGE,
        "the probe could not be read",
        False,
        "the probe could not be read",
    ),
    "empty_owned_is_not_untrusted": Case(None, CALL_MESSAGE, "", True, ""),
    "escaped": Case(
        None, "Page.goto: fake\x01tail", None, False, r"Error: Page.goto: fake\x01tail"
    ),
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES)
def test_every_join_and_stage_is_scanned(case: Case) -> None:
    assert present(case) == case.expected


@pytest.mark.parametrize("case", BENIGN.values(), ids=BENIGN)
def test_a_benign_diagnostic_is_unchanged(case: Case) -> None:
    assert present(case) == case.expected


def test_the_cut_case_is_exactly_the_reason_bound() -> None:
    assert len(CASES["escaped_scan_precedes_the_cut"].expected) == REASON_CHARS
    assert "fake\\x0" not in present(CASES["escaped_scan_precedes_the_cut"])


def test_a_long_benign_message_is_cut_at_the_reason_bound() -> None:
    long = Case(None, "Page.goto: " + "q" * 400, None, False, "")
    assert present(long) == "Error: Page.goto: " + "q" * (REASON_CHARS - 18)


def test_only_the_first_line_of_an_untrusted_message_is_kept() -> None:
    two = Case(None, "Page.goto: one\ntwo", None, False, "")
    assert present(two) == "Error: Page.goto: one"


def test_a_withheld_message_names_no_detail_past_the_call() -> None:
    page_text = Case(None, "Page.goto: page said hello", None, True, "")
    assert present(page_text) == f"Error: Page.goto: {WITHHOLDING}"
    assert "hello" not in present(page_text)
