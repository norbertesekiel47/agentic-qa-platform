import copy
import hashlib
import json
import pickle
import subprocess
import sys
from base64 import b64encode, urlsafe_b64encode
from itertools import product
from pathlib import Path
from urllib.parse import quote, quote_plus

import h11
import pytest
from aqa_core.project import SecretDestination
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.redaction import NO_SECRETS, Redacted, Redactor
from pydantic import SecretStr

from packages.runner.tests.secret_fixtures import FAKE_VALUE

MARKER = "[SECRET:FAKE]"


def secret(value: str = FAKE_VALUE, name: str = "FAKE") -> BoundSecret:
    return BoundSecret(
        name, SecretStr(value), SecretDestination(("https://fake.test",), "password")
    )


def test_a_value_is_redacted_before_a_cut() -> None:
    redactor = Redactor([secret()])
    assert redactor.redact(f"before {FAKE_VALUE} after") == f"before {MARKER} after"
    assert redactor.redact(FAKE_VALUE.upper()) == MARKER
    assert redactor.redact(FAKE_VALUE, limit=8) == MARKER[:8]
    assert FAKE_VALUE not in repr(redactor)


def test_only_a_redactor_makes_redacted_text() -> None:
    with pytest.raises(TypeError):
        Redacted("unscanned")
    scanned = NO_SECRETS.redact("hello")
    assert isinstance(scanned, Redacted)
    for ordinary in (
        scanned[:],
        scanned + " world",
        copy.copy(scanned),
        copy.deepcopy(scanned),
    ):
        assert type(ordinary) is str
    assert NO_SECRETS.redact("untouched") == "untouched"
    factory, args = scanned.__reduce__()
    assert type(factory(*args)) is str
    assert b"Redacted" not in pickle.dumps(scanned)


def test_coverage_requires_both_the_name_and_value() -> None:
    redactor = Redactor([secret()])
    assert redactor.covers(secret())
    assert not redactor.covers(secret(name="OTHER_FAKE"))
    assert not redactor.covers(secret("another-fake-value"))
    assert not NO_SECRETS.covers(secret())


@pytest.mark.parametrize(
    "value",
    [
        FAKE_VALUE,
        "fake-ß-ﬁ-😀",
        "fake-\x1b-\x85-\b-\u200b\u00ad-\"-\\-'",
        "fake\ufeff  \r\nsecret",
    ],
)
def test_raw_percent_and_producer_escaped_forms_are_redacted(value: str) -> None:
    redactor = Redactor([secret(value)])
    forms = [
        value,
        value.upper(),
        value.lower(),
        value.casefold(),
        quote(value, safe=""),
        quote_plus(value),
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        value.replace("'", "''"),
        repr(value.encode())[2:-1],
    ]
    forms.append(
        "".join(quote(c, safe="") if i % 2 else c for i, c in enumerate(value))
    )
    forms.append(value.replace("\u200b", "").replace("\u00ad", ""))
    forms.append(
        value.replace("\x1b", r"\x1b").replace("\x85", r"\x85").replace("\b", r"\b")
    )
    for form in forms:
        assert redactor.redact(form) == MARKER, repr(form)


@pytest.mark.parametrize("space", ["\n", "\r\n", "\t", "\u00a0", "\ufeff", "\u2028"])
def test_percent_encoded_whitespace_is_redacted(space: str) -> None:
    redactor = Redactor([secret("  fake secret\ufeff")])
    for form in (space, quote(space), quote(space).lower(), "+", " "):
        assert redactor.redact(f"fake{form}secret") == MARKER


@pytest.mark.parametrize("value", [FAKE_VALUE, "fake", "fäke", "秘密", "fake-😀"])
@pytest.mark.parametrize("alignment", range(3))
def test_base64_is_redacted_at_each_byte_alignment(value: str, alignment: int) -> None:
    encodings = [value.encode()]
    if all(ord(c) < 256 for c in value):
        encodings.append(value.encode("latin-1"))
    redactor = Redactor([secret(value)])
    for raw in encodings:
        for encode in (b64encode, urlsafe_b64encode):
            for suffix in (b"", b"tail"):
                encoded = encode(b"x" * alignment + raw + suffix).decode()
                for shown in (
                    encoded,
                    encoded.rstrip("="),
                    quote(encoded, safe=""),
                    encoded.lower(),
                    encoded.upper(),
                ):
                    scanned = redactor.redact(shown)
                    assert MARKER in scanned, (value, alignment, shown)
                    assert scanned.index(MARKER) == (alignment * 8 // 6)
    standalone = b64encode(value.encode()).decode()
    assert (
        redactor.redact(f"https://{standalone.lower()}.example.test/")
        == f"https://{MARKER}.example.test/"
    )


def test_h11s_quoted_bytes_are_redacted() -> None:
    value = "fake-pässword"
    connection = h11.Connection(h11.CLIENT)
    connection.receive_data(value.encode() + b"\r\n\r\n")
    with pytest.raises(h11.RemoteProtocolError) as raised:
        connection.next_event()
    assert (
        Redactor([secret(value)]).redact(str(raised.value))
        == f"illegal status line: bytearray(b'{MARKER}')"
    )


def test_generated_values_leave_no_copy() -> None:
    alphabet = "abäß😀 /+%\"'\\\t"
    for index in range(40):
        seed = hashlib.sha256(f"fake-seed-50-{index}".encode()).digest()
        value = "fake-" + "".join(alphabet[byte % len(alphabet)] for byte in seed[:12])
        redactor = Redactor([secret(value)])
        for form in (
            value,
            quote_plus(value),
            json.dumps(value)[1:-1],
            b64encode(value.encode()).decode(),
        ):
            assert redactor.redact(form) == MARKER


def test_full_case_expansion_can_mix_raw_and_percent_encoded_characters() -> None:
    assert Redactor([secret("fake-ß-ﬁ")]).redact("fake-S%53-F%49") == MARKER


@pytest.mark.parametrize("case", ["ascii", "optional", "spaces"])
def test_ambiguous_near_matches_finish_in_a_bounded_process(case: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("redaction_probe.py")),
            case,
        ],
        capture_output=True,
        text=True,
        env={},
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"{case}: near-matches unchanged; matches redacted\n"


def test_contextual_lowercase_is_encoded_after_the_whole_value_changes() -> None:
    redactor = Redactor([secret("FAKE-ΟΣ")])
    for shown in (
        "FAKE-ΟΣ",
        "fake-\u03bfς",
        "fake-%CE%BF%CF%82",
        "fake-\u03bf%CF%82",
        "FAKE-%CE%9F%CE%A3",
    ):
        assert redactor.redact(f"<{shown}>") == f"<{MARKER}>"


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("fake-%25-tail", "fake-%25-tail"),
        ("fake-%25-tail", "fake-%2525-tail"),
        ("fake-%%25-tail", "fake-%25%25-tail"),
        ("fake-%25%25-tail", "fake-%2525%25-tail"),
        ("fake-\\'\"-tail", "fake-\\\\''\\\"-tail"),
    ],
)
def test_all_local_spelling_widths_remain_available(value: str, shown: str) -> None:
    assert Redactor([secret(value)]).redact(f"<{shown}>") == f"<{MARKER}>"


def test_secret_priority_precedes_observed_span_length() -> None:
    redactor = Redactor([secret("fake-ßß", "SHORT"), secret("fake-SSS", "LONG")])
    assert redactor.redact("fake-SSSS") == "[SECRET:LONG]S"
    assert (
        Redactor([secret("fake", "FIRST"), secret("fake", "SECOND")]).redact("fake")
        == "[SECRET:FIRST]"
    )


def test_selection_is_leftmost_longest_within_a_secret_and_nonoverlapping() -> None:
    redactor = Redactor([secret("fake-tail", "LONG"), secret("fake", "SHORT")])
    assert redactor.redact("fake fake-tailfake-tail") == (
        "[SECRET:SHORT] [SECRET:LONG][SECRET:LONG]"
    )
    assert Redactor([secret("fakefake")]).redact("fakefakefake") == f"{MARKER}fake"
    assert Redactor([secret("fake\u200b")]).redact("fake\u200b") == MARKER
    assert Redactor([secret("fake ")]).redact("fake   ") == MARKER
    assert Redactor([secret("fake' ")]).redact("fake''   ") == MARKER


@pytest.mark.parametrize("value", ["\u200b\u00ad", "\u200b\u200b", "\u00ad" * 4])
def test_zero_length_matches_do_not_replace_text(value: str) -> None:
    redactor = Redactor([secret(value)])
    assert redactor.redact("") == ""
    assert redactor.redact("ordinary") == "ordinary"
    assert redactor.redact(value) == MARKER
    assert redactor.redact(quote(value, safe="")) == MARKER
    assert redactor.redact(json.dumps(value)[1:-1]) == MARKER
    assert redactor.redact(f"<{value[0]}>") == f"<{MARKER}>"


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("fake-I", "fake-\u0131"),
        ("fake-i", "fake-İ"),
        ("fake-İ", "fake-i"),
        ("fake-s", "fake-\u017f"),
        ("fake-k", "fake-\u212a"),
        ("fake-\u03c3", "fake-ς"),
    ],
)
def test_simple_unicode_case_equivalence_is_preserved(value: str, shown: str) -> None:
    assert Redactor([secret(value)]).redact(shown) == MARKER


def test_markers_are_not_rescanned_and_limits_follow_replacement() -> None:
    redactor = Redactor([secret("fake", "FAKE"), secret("SECRET", "OTHER")])
    assert redactor.redact("fakeSECRET") == "[SECRET:FAKE][SECRET:OTHER]"
    assert redactor.redact("fake", limit=0) == ""
    assert redactor.redact("fake", limit=-1) == "[SECRET:FAKE"


def reference_forms(parts: tuple[str, ...]) -> set[str]:
    spellings = {
        "%": ("%", "%25"),
        "2": ("2", "%32"),
        "\u200b": ("", "\u200b", "%E2%80%8B"),
        " ": (" ", "  ", "   ", "+", "%20", " +"),
    }
    return {
        "fake-" + "".join(choice) + "!"
        for choice in product(*(spellings[part] for part in parts))
    }


def test_short_paths_match_an_independent_finite_language() -> None:
    for parts in product(("%", "2", "\u200b", " "), repeat=2):
        forms = reference_forms(parts)
        redactor = Redactor([secret("fake-" + "".join(parts) + "!")])
        for shown in forms:
            assert redactor.redact(f"<{shown}>{shown}") == f"<{MARKER}>{MARKER}"
            near_match = shown[:-1] + "?"
            assert redactor.redact(near_match) == near_match


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("\u200b fake-value", "fake-value"),
        ("fake-value \u200b", "fake-value"),
        ("\u00ad fake-value", "fake-value"),
        ("fake-value \u00ad", "fake-value"),
        ("fake \u200b value", "fake value"),
        ("fake \u00ad value", "fake value"),
        ("\u200b fake-value \u00ad", "fake-value"),
        ("\u200b \u00ad\tfake-value \u200b \u00ad", "fake-value"),
        ("fake \u200b \u00ad\tvalue", "fake value"),
        ("\u200b \u00ad\tfake \u200b \u00ad\tvalue \u00ad \u200b", "fake value"),
        ("\u200b\u00a0fake\u00a0\u200b\ufeffvalue\u00a0\u00ad", "fake value"),
    ],
)
def test_producer_removal_precedes_whitespace_normalization(
    value: str, shown: str
) -> None:
    redactor = Redactor([secret(value)])
    assert redactor.redact(shown) == MARKER
    assert redactor.redact(f"<{shown}>") == f"<{MARKER}>"
    assert redactor.redact(f"<{value}>") == f"<{MARKER}>"
    assert redactor.redact(quote(value, safe="")) == MARKER
    assert redactor.redact(quote(shown, safe="")) == MARKER


@pytest.mark.parametrize("value", ["fake \u200b value", "fake \u00ad value"])
def test_producer_normalization_preserves_required_interior_whitespace(
    value: str,
) -> None:
    redactor = Redactor([secret(value)])
    assert redactor.redact("fake value") == MARKER
    assert redactor.redact("fake%20value") == MARKER
    assert redactor.redact("fakevalue") == "fakevalue"


def test_removed_producer_path_keeps_contextual_lowercase() -> None:
    redactor = Redactor([secret("FAKE-\u039f\u200bΣ")])
    assert redactor.redact("fake-%CE%BF%CF%82") == MARKER
    assert redactor.redact("fake-ος") == MARKER
    assert redactor.redact("FAKE-\u039f\u200bΣ") == MARKER


def test_producer_paths_keep_original_secret_priority() -> None:
    redactor = Redactor([secret("fake", "SHORT"), secret("\u200b fake", "LONG")])
    assert redactor.redact("fake") == "[SECRET:LONG]"
    tied = Redactor([secret("fake\u200b", "FIRST"), secret("\u200bfake", "SECOND")])
    assert tied.redact("fake") == "[SECRET:FIRST]"


def test_producer_paths_do_not_expand_base64_generation() -> None:
    value = "\u200b fake-value"
    redactor = Redactor([secret(value)])
    assert redactor.redact(b64encode(value.encode()).decode()) == MARKER
    assert redactor.redact("ZmFrZS12YWx1ZQ==") == "ZmFrZS12YWx1ZQ=="


@pytest.mark.parametrize("value", ["\u200b\u00ad\u200b", "\u00ad" * 4, "\u200b \u00ad"])
def test_empty_producer_paths_do_not_insert_markers(value: str) -> None:
    redactor = Redactor([secret(value)])
    assert redactor.redact("") == ""
    assert redactor.redact("ordinary") == "ordinary"
    assert redactor.redact(value) == MARKER


def test_whitespace_only_producer_paths_still_consume_positive_spans() -> None:
    redactor = Redactor([secret("\u200b \u00ad")])
    assert redactor.redact("ordinary words") == f"ordinary{MARKER}words"
    assert redactor.redact("<  >") == f"<{MARKER}>"
    assert redactor.redact("") == ""


def test_short_producer_remnants_can_redact_unrelated_text() -> None:
    redactor = Redactor([secret("\u200b f\u00ad")])
    assert redactor.redact("f") == MARKER
    assert redactor.redact("leaf") == f"lea{MARKER}"
    assert redactor.redact("") == ""
