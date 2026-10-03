"""A run's test secrets: each one its spec references, with its value from
`AQA_SECRET_<NAME>` and its destination in this run, read before any browser
starts (#49; ADR-0026, Test secrets; SECURITY §5). No browser here: a value
that is missing fails before one could open."""

from pathlib import Path

import pytest
from aqa_core.config import RoleField
from aqa_core.project import SecretDestination, SpecError
from aqa_runner.bound_secrets import (
    MissingSecretError,
    SecretLoggedError,
    bound_secrets,
)

from packages.runner.tests.secret_fixtures import (
    BOUND_AT_START,
    FAKE_VALUE,
    OTHER_FAKE_VALUE,
    secret_spec,
)

START = "http://app.example.test:8080"
OTHER = "http://other.example.test:8080"

# Two secrets: the password on the start origin, and an API token in a
# textbox on the start origin or the second allowed origin.
BOTH = (
    f"{BOUND_AT_START}, "
    f"API_TOKEN: {{ origins: [start, '{OTHER}'], field: {{ role: textbox, name: API token }} }}"
)
BOTH_ACCOUNT = "{ email: { secret: API_TOKEN }, password: { secret: TEST_PASSWORD } }"


@pytest.fixture(autouse=True)
def no_secrets_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """None of the names these tests use, whatever the developer's shell has,
    and no Playwright debug logging."""
    for name in ("TEST_PASSWORD", "API_TOKEN"):
        monkeypatch.delenv(f"AQA_SECRET_{name}", raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("DEBUGP", raising=False)


def test_each_referenced_secret_is_bound_with_its_value_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setenv("AQA_SECRET_API_TOKEN", OTHER_FAKE_VALUE)
    spec = secret_spec(tmp_path, BOTH, account=BOTH_ACCOUNT, allowed_origins=(OTHER,))

    bound = bound_secrets(spec, START)

    assert {
        name: (secret.name, secret.value.get_secret_value(), secret.destination)
        for name, secret in bound.items()
    } == {
        "API_TOKEN": (
            "API_TOKEN",
            OTHER_FAKE_VALUE,
            SecretDestination(
                (START, OTHER), RoleField(role="textbox", name="API token")
            ),
        ),
        "TEST_PASSWORD": (
            "TEST_PASSWORD",
            FAKE_VALUE,
            SecretDestination((START,), "password"),
        ),
    }


def test_a_missing_or_empty_value_fails_naming_its_variable_and_where_the_spec_uses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The secret's bare name holds a value, which is never read: only the
    # prefixed variable is, so a spec can't pull in an unrelated one.
    monkeypatch.setenv("TEST_PASSWORD", FAKE_VALUE)
    # How GitHub Actions passes a repository secret that isn't set.
    monkeypatch.setenv("AQA_SECRET_API_TOKEN", "")
    spec = secret_spec(tmp_path, BOTH, account=BOTH_ACCOUNT, allowed_origins=(OTHER,))

    with pytest.raises(MissingSecretError) as missing:
        bound_secrets(spec, START)

    # Every missing value at once, each saying what to set and why.
    assert missing.value.problems == (
        (
            "AQA_SECRET_API_TOKEN is empty: set it to the value of test secret "
            f"API_TOKEN, which {spec.path} references at preconditions.account.email "
            "(GitHub Actions gives a secret that isn't set as an empty string)"
        ),
        (
            "AQA_SECRET_TEST_PASSWORD is not set: set it to the value of test "
            f"secret TEST_PASSWORD, which {spec.path} references at "
            "preconditions.account.password"
        ),
    )
    # An infrastructure error: the environment's fault, not the spec's.
    assert missing.value.exit_code == 12
    assert str(missing.value) == "\n".join(missing.value.problems)


def test_every_secret_the_spec_references_needs_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even a run that fills only the password needs the token's value.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    spec = secret_spec(tmp_path, BOTH, account=BOTH_ACCOUNT, allowed_origins=(OTHER,))

    with pytest.raises(MissingSecretError) as missing:
        bound_secrets(spec, START)

    assert [problem.split(" ", 1)[0] for problem in missing.value.problems] == [
        "AQA_SECRET_API_TOKEN"
    ]


@pytest.mark.parametrize(
    ("start_url", "allowed_origins"),
    [
        ("/login", ()),
        # A path that decoded would name another host (ADR-0026's start URL
        # amendment), and an origin the spec adds.
        ("/%2f%2fevil.test/login", (OTHER,)),
        ("/elsewhere?next=https://evil.test/", ("https://evil.test",)),
    ],
)
def test_editing_start_url_or_allowed_origins_never_moves_a_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_url: str,
    allowed_origins: tuple[str, ...],
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    spec = secret_spec(tmp_path, start_url=start_url, allowed_origins=allowed_origins)

    bound = bound_secrets(spec, START)

    # `start` is the invocation's start origin, whatever the spec says.
    assert bound["TEST_PASSWORD"].destination.origins == (START,)


def test_a_binding_the_edited_spec_no_longer_allows_fails_before_any_value_is_read(
    tmp_path: Path,
) -> None:
    # Bound to the second origin too, which the spec no longer allows; and
    # no value is set, which is never reached.
    spec = secret_spec(
        tmp_path, f"TEST_PASSWORD: {{ origins: [start, '{OTHER}'], field: password }}"
    )

    with pytest.raises(SpecError) as refused:
        bound_secrets(spec, START)

    assert refused.value.problems == (
        (
            f"{spec.path}: preconditions.account.password: secret TEST_PASSWORD is "
            f"bound to {OTHER}, which is not one of this run's allowed origins: {START}"
        ),
    )


def test_a_bound_secret_never_shows_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)

    secret = bound_secrets(secret_spec(tmp_path), START)["TEST_PASSWORD"]

    # Kept, but shown nowhere it is printed.
    assert secret.value.get_secret_value() == FAKE_VALUE
    for shown in (repr(secret), str(secret), f"{secret.value}", repr(secret.value)):
        assert FAKE_VALUE not in shown


def test_a_secret_referenced_twice_is_reported_where_the_spec_first_uses_it(
    tmp_path: Path,
) -> None:
    spec = secret_spec(
        tmp_path,
        account="{ email: { secret: TEST_PASSWORD }, password: { secret: TEST_PASSWORD } }",
    )

    with pytest.raises(MissingSecretError) as missing:
        bound_secrets(spec, START)

    assert missing.value.problems == (
        (
            "AQA_SECRET_TEST_PASSWORD is not set: set it to the value of test secret "
            f"TEST_PASSWORD, which {spec.path} references at preconditions.account.email"
        ),
    )


@pytest.mark.parametrize(
    ("variable", "value", "says"),
    [
        # Playwright's Python client prints every protocol message.
        ("DEBUGP", "1", "DEBUGP is set"),
        # Its driver logs them under the debug name pw:protocol.
        ("DEBUG", "pw:protocol", "DEBUG turns on pw:protocol"),
        ("DEBUG", "pw:*", "DEBUG turns on pw:protocol"),
        ("DEBUG", "*", "DEBUG turns on pw:protocol"),
        ("DEBUG", "pw:api, pw:protocol", "DEBUG turns on pw:protocol"),
        # The driver prints the browser's stderr, where the headless shell
        # writes the page's console messages, a value the page logs included.
        ("DEBUG", "pw:browser", "DEBUG turns on pw:browser"),
        ("DEBUG", "pw:*,-pw:protocol", "DEBUG turns on pw:browser"),
        # Separated and trimmed as JavaScript's \s, where U+FEFF is space and
        # U+001C is not, so `x\x1c-pw:protocol` is one name, not an off.
        ("DEBUG", "\ufeffpw:protocol", "DEBUG turns on pw:protocol"),
        ("DEBUG", "pw:protocol,x\x1c-pw:protocol", "DEBUG turns on pw:protocol"),
    ],
)
def test_a_secret_isnt_bound_where_playwright_would_log_its_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    says: str,
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setenv(variable, value)

    with pytest.raises(SecretLoggedError) as logged:
        bound_secrets(secret_spec(tmp_path), START)

    assert str(logged.value).startswith(f"{says}: ")
    # An infrastructure error, as a missing value is.
    assert logged.value.exit_code == 12


@pytest.mark.parametrize(
    "debug", ["pw:api", "pw:*,-pw:protocol,-pw:browser", "other:*", "pw:protocolx"]
)
def test_debug_logging_that_leaves_the_protocol_out_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug: str
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setenv("DEBUG", debug)

    assert list(bound_secrets(secret_spec(tmp_path), START)) == ["TEST_PASSWORD"]


def test_a_spec_that_references_no_secret_binds_none_whatever_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEBUGP", "1")
    spec = secret_spec(tmp_path, account="{ email: reader@example.test }")

    assert bound_secrets(spec, START) == {}


@pytest.mark.parametrize(
    "value", [" \t\r\n", "\ufeff\u00a0", "abc", "\ufeffabc\ufeff", "éé", "päx"]
)
def test_a_blank_or_under_four_byte_value_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Deliberately unusable fake values, including the shorter Latin-1 form.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)
    with pytest.raises(MissingSecretError) as raised:
        bound_secrets(secret_spec(tmp_path), START)
    assert raised.value.exit_code == 12
    assert "AQA_SECRET_TEST_PASSWORD" in str(raised.value)
    assert (
        "whitespace" in str(raised.value)
        if not value.strip(" \t\r\n\ufeff\u00a0")
        else "4 bytes" in str(raised.value)
    )
    if value.strip():
        assert value not in str(raised.value)


@pytest.mark.parametrize("value", ["fake", "fäke", "秘密", "\ufeff fake \ufeff"])
def test_a_value_with_four_bytes_in_every_supported_encoding_is_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)
    held = bound_secrets(secret_spec(tmp_path), START)["TEST_PASSWORD"]
    assert held.value.get_secret_value() == value
