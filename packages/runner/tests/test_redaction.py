"""A run's secret scan, including transformations upstream of observations."""

import copy
import hashlib
import json
import pickle
from base64 import b64encode, urlsafe_b64encode
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
                    # At most one prefix character carries only prefix bits;
                    # the adjoining character's secret bits must disappear.
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
