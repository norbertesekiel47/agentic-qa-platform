"""A spec's frontmatter is read strictly (DATA_MODEL §6, §7; ADR-0024; #39)."""

import hashlib
from pathlib import Path

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_spec
from aqa_core.spec import SecretReference, canonical_hash, secret_references

CONFIG = ProjectConfig.model_validate(
    {"secrets": {"TEST_PASSWORD": {"origins": ["start"], "field": "password"}}}
)

LOGIN = """\
id: login
goal: A returning reader signs in.
preconditions:
  start_url: /login
  account: { email: reader@conduit.test, password: { secret: TEST_PASSWORD } }
expect:
  - The home page is shown
"""

# DATA_MODEL §6's example, verbatim.
EXAMPLE = """\
id: checkout-expired-card
goal: A returning user tries to buy a hoodie with an expired saved card and is told clearly why it failed.
preconditions:
  start_url: /                   # a path; the origin comes from the run (`aqa explore --url`), never from the spec
  account: { email: returning@example.test, password: { secret: TEST_PASSWORD } }   # the secret must be declared in the project config (§9)
  reset: { http: "POST /test-api/reset?fixture=returning-user-expired-card" }   # optional; called before every attempt, the first included (ADR-0024)
  probes:                        # optional read-only GETs on the start origin, for observing app state
    orders_count: "GET /test-api/orders/count?email=returning@example.test"
steps:            # optional hints; the agent may deviate
  - Log in
  - Add "Classic Hoodie" (size M) to the cart
  - Check out with the saved card
expect:           # one observable claim per item; every item must compile to ≥ 1 check
  - An error message says the card has expired
  - No order is created for this user
  - The cart still contains the Classic Hoodie (size M)
  - The user remains on the payment step
  - text: The "Pay" button is visible and not covered by any overlay
    visual: deterministic        # deterministic (default) | model (requires verified mode)
invariants:
  inherit: true
  disable: [console_errors]   # this app logs an expected warning here
allowed_origins: [ "https://payments-sandbox.example.test" ]   # extra origins the agent may navigate to and act on (must be verified for hosted runs); CDN and font hosts are subresource hosts in the project config (§9)
browser: { viewport: [1440, 900] }   # optional; overrides the project's browser settings (ADR-0025)
tags: [checkout, payments]
"""


def write(
    tmp_path: Path, frontmatter: str, name: str = "login", body: str = "Notes.\n"
) -> Path:
    path = tmp_path / f"{name}.spec.md"
    path.write_text(f"---\n{frontmatter}---\n\n{body}")
    return path


def problems_for(path: Path) -> tuple[str, ...]:
    with pytest.raises(SpecError) as raised:
        load_spec(path, CONFIG)
    return raised.value.problems


def test_a_valid_spec_loads(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, LOGIN), CONFIG).frontmatter

    assert spec.id == "login"
    assert spec.preconditions.start_url == "/login"
    assert spec.preconditions.account is not None
    # A reference by name; the value comes from AQA_SECRET_TEST_PASSWORD.
    assert isinstance(spec.preconditions.account.password, SecretReference)
    assert list(secret_references(spec)) == [
        ("preconditions.account.password", "TEST_PASSWORD")
    ]
    assert [(e.text, e.visual) for e in spec.expect] == [
        ("The home page is shown", "deterministic")
    ]
    # Every invariant applies unless the spec says otherwise.
    assert (spec.invariants.inherit, spec.invariants.disable) == (True, ())


def test_the_documented_example_loads(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, EXAMPLE, name="checkout-expired-card"), CONFIG)

    assert spec.frontmatter.allowed_origins == (
        "https://payments-sandbox.example.test",
    )
    assert spec.frontmatter.browser.viewport == (1440, 900)
    assert spec.frontmatter.invariants.disable == ("console_errors",)
    assert len(spec.frontmatter.expect) == 5


def test_start_url_may_carry_a_query_and_a_fragment(tmp_path: Path) -> None:
    text = LOGIN.replace("start_url: /login", "start_url: /login?next=%2Fhome#form")

    spec = load_spec(write(tmp_path, text), CONFIG)

    assert spec.frontmatter.preconditions.start_url == "/login?next=%2Fhome#form"


@pytest.mark.parametrize(
    ("old", "new", "key", "problem"),
    [
        # Unknown keys, at every level; preconditions.seed no longer exists (ADR-0024).
        ("goal:", "owner: qa\ngoal:", "owner", "unknown key"),
        (
            "  start_url: /login\n",
            "  start_url: /login\n  seed: fixture\n",
            "preconditions.seed",
            "unknown key",
        ),
        (
            "{ email: reader@conduit.test,",
            "{ username: reader,",
            "preconditions.account.username",
            "unknown key",
        ),
        (
            "{ secret: TEST_PASSWORD }",
            "{ secret: TEST_PASSWORD, origin: start }",
            "preconditions.account.password",
            "must be a string, or { secret: NAME }",
        ),
        (
            "  - The home page is shown\n",
            "  - { text: The home page is shown, weight: 2 }\n",
            "expect[0].weight",
            "unknown key",
        ),
        # Required keys.
        ("id: login\n", "", "id", "missing key"),
        ("goal: A returning reader signs in.\n", "", "goal", "missing key"),
        ("  start_url: /login\n", "", "preconditions.start_url", "missing key"),
        ("expect:\n  - The home page is shown\n", "", "expect", "missing key"),
        (
            "expect:\n  - The home page is shown\n",
            "expect: []\n",
            "expect",
            "at least 1",
        ),
        # start_url is a path; the origin comes from the run (ADR-0026).
        (
            "start_url: /login",
            "start_url: https://evil.test/login",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: //evil.test/login",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: '/\\evil.test/login'",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            'start_url: "/\\t/evil.test"',
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: login",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: '/log in'",
            "preconditions.start_url",
            "not a path",
        ),
        ("start_url: /login", "start_url: ''", "preconditions.start_url", "not a path"),
        (
            "start_url: /login",
            "start_url: /..//evil.test",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: /.//evil.test",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: /%2e%2E//evil.test",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: /%2E%2e/login",
            "preconditions.start_url",
            "not a path",
        ),
        (
            "start_url: /login",
            "start_url: /app//login",
            "preconditions.start_url",
            "not a path",
        ),
        # Test secrets: declared in the project config, named as it names them.
        (
            "{ secret: TEST_PASSWORD }",
            "{ secret: API_TOKEN }",
            "preconditions.account.password.secret",
            "API_TOKEN is not declared",
        ),
        (
            "email: reader@conduit.test",
            "email: { secret: TEST_EMAIL }",
            "preconditions.account.email.secret",
            "TEST_EMAIL is not declared",
        ),
        (
            "{ secret: TEST_PASSWORD }",
            "{ secret: test-password }",
            "preconditions.account.password.secret",
            "not a secret name",
        ),
        (
            "{ secret: TEST_PASSWORD }",
            "[TEST_PASSWORD]",
            "preconditions.account.password",
            "must be a string, or { secret: NAME }",
        ),
        # Expectations: visual: model is a spec error in M1 (ADR-0024).
        (
            "  - The home page is shown\n",
            "  - { text: The home page is shown, visual: model }\n",
            "expect[0].visual",
            "model-assisted",
        ),
        (
            "  - The home page is shown\n",
            "  - { text: The home page is shown, visual: pixel }\n",
            "expect[0].visual",
            "'deterministic' or 'model'",
        ),
        (
            "  - The home page is shown\n",
            "  - { visual: deterministic }\n",
            "expect[0].text",
            "missing key",
        ),
        (
            "  - The home page is shown\n",
            "  - ''\n",
            "expect[0].text",
            "at least 1 character",
        ),
        # Browser overrides are checked like the project's.
        (
            "goal:",
            "browser: { timezone: Mars/Phobos }\ngoal:",
            "browser.timezone",
            "not an IANA time zone",
        ),
        ("goal:", "browser: { zoom: 2 }\ngoal:", "browser.zoom", "unknown key"),
        # Invariants.
        (
            "goal:",
            "invariants: { inherit: yes }\ngoal:",
            "invariants.inherit",
            "valid boolean",
        ),
        (
            "goal:",
            "invariants: { disable: [console_error] }\ngoal:",
            "invariants.disable[0]",
            "'console_errors'",
        ),
        (
            "goal:",
            "invariants: { disable: [http_5xx, http_5xx] }\ngoal:",
            "invariants.disable",
            "http_5xx twice",
        ),
        (
            "goal:",
            "invariants: { inherit: false, disable: [http_5xx] }\ngoal:",
            "invariants",
            "no effect",
        ),
        # Everything else.
        (
            "goal:",
            "allowed_origins: [payments.example.test]\ngoal:",
            "allowed_origins[0]",
            "not an origin",
        ),
        ("goal:", "steps: Sign in\ngoal:", "steps", "must be a list"),
        (
            "  account: { email: reader@conduit.test, password: { secret: TEST_PASSWORD } }\n",
            "  account: reader\n",
            "preconditions.account",
            "must be a mapping of keys",
        ),
        # M11: allowed origins are distinct.
        (
            "goal:",
            "allowed_origins: [https://pay.test, 'HTTPS://Pay.test/']\ngoal:",
            "allowed_origins",
            "https://pay.test twice",
        ),
        ("goal:", "tags: [1]\ngoal:", "tags[0]", "valid string"),
        (
            "  start_url: /login\n",
            "  start_url: /login\n  probes: { count: 3 }\n",
            "preconditions.probes.count",
            "valid string",
        ),
        (
            "  start_url: /login\n",
            "  start_url: /login\n  reset: POST /reset\n",
            "preconditions.reset",
            "must be a mapping of keys",
        ),
    ],
)
def test_an_invalid_spec_names_the_file_the_key_and_the_problem(
    tmp_path: Path, old: str, new: str, key: str, problem: str
) -> None:
    assert old in LOGIN
    path = write(tmp_path, LOGIN.replace(old, new, 1))

    problems = problems_for(path)

    assert any(
        line.startswith(f"{path}: {key}: ") and problem in line for line in problems
    ), problems


def test_an_invalid_expectation_is_reported_once(tmp_path: Path) -> None:
    # Not also as an empty expect list, though no item survived.
    path = write(tmp_path, LOGIN.replace("  - The home page is shown\n", "  - 3\n"))

    assert [line.split(": ")[1] for line in problems_for(path)] == ["expect[0]"]


@pytest.mark.parametrize(
    "value", ["12345678", "{hunter2}", "{ secret: TEST_PASSWORD, hunter2: 1 }"]
)
def test_an_account_value_error_never_repeats_the_value(
    tmp_path: Path, value: str
) -> None:
    # A password typed where a reference goes would otherwise reach a CI log.
    path = write(tmp_path, LOGIN.replace("{ secret: TEST_PASSWORD }", value))

    assert problems_for(path) == (
        f"{path}: preconditions.account.password: must be a string, or {{ secret: NAME }}",
    )


def test_a_start_url_problem_reads_in_full(tmp_path: Path) -> None:
    path = write(tmp_path, LOGIN.replace("start_url: /login", "start_url: login"))

    assert problems_for(path) == (
        (
            f"{path}: preconditions.start_url: 'login' is not a path: write a path "
            "such as /login, with no empty, . or .. segment; the origin comes from "
            "the run (ADR-0026)"
        ),
    )


@pytest.mark.parametrize(
    ("endpoint", "problem"),
    [
        # A probe only reads.
        ("POST /test-api/count", "is not a probe: write GET and a path"),
        ("get /test-api/count", "is not a probe: write GET and a path"),
        ("GET", "is not a probe: write GET and a path"),
        # A path on the start origin, held to start_url's rules: never another
        # origin, written out or as a path a browser reads as one.
        ("GET https://evil.test/count", "is not a path"),
        ("GET //evil.test/count", "is not a path"),
        ("GET /test-api/../count", "is not a path"),
        ("GET  /test-api/count", "is not a path"),
        # The runner sends the path as written, and a request line is ASCII.
        ("GET /café", "write it percent-encoded"),
        # A request carries no fragment, so the probe would read another path
        # than the one written.
        ("GET /test-api/count#total", "a request carries no fragment"),
    ],
)
def test_a_probe_is_get_and_a_path(tmp_path: Path, endpoint: str, problem: str) -> None:
    text = LOGIN.replace(
        "  start_url: /login\n",
        f'  start_url: /login\n  probes: {{ count: "{endpoint}" }}\n',
    )
    path = write(tmp_path, text)

    [line] = problems_for(path)

    assert line.startswith(f"{path}: preconditions.probes.count: '{endpoint}' ")
    assert problem in line


def test_a_probe_problem_reads_in_full(tmp_path: Path) -> None:
    text = LOGIN.replace(
        "  start_url: /login\n",
        '  start_url: /login\n  probes: { count: "POST /test-api/count" }\n',
    )
    path = write(tmp_path, text)

    assert problems_for(path) == (
        (
            f"{path}: preconditions.probes.count: 'POST /test-api/count' is not a "
            "probe: write GET and a path, such as GET /test-api/orders/count: a probe "
            "only reads, from the start origin (DATA_MODEL §6)"
        ),
    )


def test_a_probe_keeps_its_query(tmp_path: Path) -> None:
    text = LOGIN.replace(
        "  start_url: /login\n",
        '  start_url: /login\n  probes: { count: "GET /test-api/count?email=a%40b.test" }\n',
    )

    spec = load_spec(write(tmp_path, text), CONFIG)

    assert spec.frontmatter.preconditions.probes == {
        "count": "GET /test-api/count?email=a%40b.test"
    }


def test_the_id_must_be_the_file_name_without_its_suffix(tmp_path: Path) -> None:
    path = write(tmp_path, LOGIN, name="sign-in")

    assert problems_for(path) == (
        (
            f"{path}: id: 'login' doesn't match the file name: a spec's id is its "
            "file name without .spec.md, here 'sign-in'"
        ),
    )


def test_every_problem_in_the_spec_is_reported_together(tmp_path: Path) -> None:
    text = LOGIN.replace("goal:", "owner: qa\ngoal:").replace(
        "TEST_PASSWORD", "API_TOKEN"
    )
    path = write(tmp_path, text, name="sign-in")

    assert {line.split(": ")[1] for line in problems_for(path)} == {
        "id",
        "owner",
        "preconditions.account.password.secret",
    }


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            "# Login\n\nNo frontmatter.\n",
            "a spec starts with its frontmatter between two --- lines",
        ),
        (
            "---\nid: login\n",
            "a spec starts with its frontmatter between two --- lines",
        ),
        (
            "Notes first.\n---\n" + LOGIN + "---\n",
            "a spec starts with its frontmatter between two --- lines",
        ),
        ("---\n- id\n---\n", "the frontmatter must be a mapping of keys"),
        ("---\n---\n", "the frontmatter must be a mapping of keys"),
    ],
)
def test_a_spec_without_frontmatter_is_rejected(
    tmp_path: Path, text: str, problem: str
) -> None:
    path = tmp_path / "login.spec.md"
    path.write_text(text)

    assert problems_for(path) == (f"{path}: {problem}",)


def test_a_rule_in_the_body_is_not_frontmatter(tmp_path: Path) -> None:
    path = write(tmp_path, LOGIN, body="Notes.\n\n---\n\nMore notes.\n")

    assert load_spec(path, CONFIG).frontmatter.id == "login"


def test_inherit_false_turns_every_invariant_off(tmp_path: Path) -> None:
    path = write(tmp_path, LOGIN + "invariants: { inherit: false }\n")

    invariants = load_spec(path, CONFIG).frontmatter.invariants

    assert (invariants.inherit, invariants.disable) == (False, ())


def test_a_trailing_slash_is_part_of_a_path(tmp_path: Path) -> None:
    path = write(tmp_path, LOGIN.replace("start_url: /login", "start_url: /login/"))

    assert load_spec(path, CONFIG).frontmatter.preconditions.start_url == "/login/"


def test_a_spec_that_is_not_utf8_is_a_spec_error(tmp_path: Path) -> None:
    path = tmp_path / "login.spec.md"
    path.write_bytes(
        ("---\n" + LOGIN + "---\n").replace("reader", "caf\xe9").encode("latin-1")
    )

    assert problems_for(path) == (f"{path}: not UTF-8 text",)


def test_a_spec_that_is_a_directory_is_a_spec_error(tmp_path: Path) -> None:
    path = tmp_path / "login.spec.md"
    path.mkdir()

    assert problems_for(path) == (f"{path}: a directory, not a file",)


def test_a_spec_saved_with_a_byte_order_mark_loads(tmp_path: Path) -> None:
    path = tmp_path / "login.spec.md"
    path.write_text("\ufeff---\n" + LOGIN + "---\n", encoding="utf-8")

    assert load_spec(path, CONFIG).frontmatter.id == "login"


def test_a_duplicate_key_names_its_line_in_the_file(tmp_path: Path) -> None:
    # The frontmatter starts on the file's second line.
    path = write(tmp_path, LOGIN.replace("goal:", "id: sign-in\ngoal:"))

    assert problems_for(path) == (f"{path}: line 3: duplicate key 'id'",)


def test_spec_hash_is_the_sha256_of_the_canonical_frontmatter_without_tags(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path,
        "tags: [smoke]\nid: a\ngoal: Pay at the café\npreconditions: { start_url: / }\nexpect: [Paid]\n",
        name="a",
    )
    # Sorted keys, no whitespace, non-ASCII escaped: Python's json.dumps.
    canonical = '{"expect":["Paid"],"goal":"Pay at the caf\\u00e9","id":"a","preconditions":{"start_url":"/"}}'

    assert (
        load_spec(path, CONFIG).spec_hash
        == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    )


def test_spec_hash_ignores_tags_the_body_and_key_order(tmp_path: Path) -> None:
    first = load_spec(write(tmp_path, LOGIN + "tags: [auth]\n"), CONFIG).spec_hash
    # The id line moved to the end.
    reordered = "\n".join(reversed(LOGIN.split("\n", 1))) + "\n"

    changed = write(
        tmp_path, reordered + "tags: [auth, smoke]\n", body="Rewritten notes.\n"
    )

    assert load_spec(changed, CONFIG).spec_hash == first


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("signs in.", "signs in again."),
        ("start_url: /login", "start_url: /login?next=%2F"),
        ("The home page is shown", "The home page is shown at once"),
        ("expect:", "steps: [Sign in]\nexpect:"),
        ("expect:", "invariants: { disable: [http_5xx] }\nexpect:"),
    ],
)
def test_spec_hash_changes_with_any_other_frontmatter_edit(
    tmp_path: Path, old: str, new: str
) -> None:
    before = load_spec(write(tmp_path, LOGIN), CONFIG).spec_hash

    after = load_spec(write(tmp_path, LOGIN.replace(old, new, 1)), CONFIG)

    assert after.spec_hash != before


@pytest.mark.parametrize("number", [float("nan"), float("inf")])
def test_a_canonical_hash_refuses_a_number_json_cannot_write(number: float) -> None:
    # Python's json would write NaN or Infinity, which no JSON reader takes,
    # so the canonical form would have two spellings of nothing standard.
    with pytest.raises(ValueError, match="not JSON compliant"):
        canonical_hash({"value": number})


def test_a_spec_with_secret_references_dumps_as_it_was_written(tmp_path: Path) -> None:
    # The coverage plan's request is the frontmatter, dumped (#41); a
    # serializer warning there is an error under pytest's filters.
    spec = load_spec(write(tmp_path, LOGIN), CONFIG)

    dumped = spec.frontmatter.model_dump(mode="json", exclude_unset=True)

    assert dumped["preconditions"]["account"] == {
        "email": "reader@conduit.test",
        "password": {"secret": "TEST_PASSWORD"},
    }
