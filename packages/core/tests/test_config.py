"""The project config, qa/config.yaml, is read strictly (DATA_MODEL §9, #39)."""

import json
import typing
from pathlib import Path

import pytest
from aqa_core.config import RoleField
from aqa_core.project import SpecError, load_config
from aqa_core.schema import AriaRole, authority
from pydantic import ValidationError

# DATA_MODEL §9's example, verbatim.
EXAMPLE = """\
base_url: "http://127.0.0.1:4100"   # the start origin when `aqa explore --url` is omitted; `--url` wins
roles:                        # overrides the defaults in TECH_STACK §3
  navigator: { provider: anthropic, model: claude-sonnet-5-5, effort: medium, fallback: claude-opus-5-5 }
browser:                      # settings for every run (ADR-0025); a spec may override them
  timezone: UTC
  locale: en-US
  viewport: [1280, 800]
egress:                       # ADR-0026
  subresource_hosts: [ "fonts.cdn.example.test" ]            # pages may load from these; no navigation, no secrets
  expected_blocked: [ "analytics.example.test" ]             # refused; their direct symptoms don't count against invariants
  private_origins: [ "http://staging.internal.test:8080" ]   # local and CI runs only: may resolve to private addresses
secrets:                      # bindings only; values come from AQA_SECRET_<NAME>
  TEST_PASSWORD: { origins: [ start ], field: password }
  API_TOKEN: { origins: [ start ], field: { role: textbox, name: "API token" } }
models:                       # a model the pinned price map lacks, or a replacement for its entry (ADR-0007 amendment)
  "example-provider/example-model": { capabilities: [tools, structured_output], input_usd_per_mtok: 0.50, output_usd_per_mtok: 1.50 }
budgets:                      # per explore run (ADR-0024)
  attempts: 3
  actions_per_attempt: 40
  model_usd: 3.00
  minutes: 15
  resolve_seconds: 10
"""

BINDING = "{ origins: [ start ], field: password }"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_the_documented_example_loads(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, EXAMPLE))

    assert config.base_url == "http://127.0.0.1:4100"
    assert config.browser.viewport == (1280, 800)
    assert config.egress.subresource_hosts == ("fonts.cdn.example.test",)
    assert config.egress.private_origins == ("http://staging.internal.test:8080",)
    assert config.secrets["TEST_PASSWORD"].origins == ("start",)
    assert config.secrets["TEST_PASSWORD"].field == "password"
    assert config.secrets["API_TOKEN"].field == RoleField(
        role="textbox", name="API token"
    )
    assert config.models["example-provider/example-model"].output_usd_per_mtok == 1.5
    assert config.roles["navigator"].fallback == "claude-opus-5-5"


def test_every_key_is_optional(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "# nothing set yet\n"))

    assert config.base_url is None
    assert config.secrets == {}
    assert config.browser.model_dump(exclude_none=True) == {}
    # ADR-0024's budgets apply when the project sets none.
    assert config.budgets.model_dump() == {
        "attempts": 3,
        "actions_per_attempt": 40,
        "model_usd": 3,
        "minutes": 15,
        "resolve_seconds": 10,
    }


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("HTTP://LocalHost:80/", "http://localhost"),
        ("https://shop.example.test:443", "https://shop.example.test"),
        ("http://[0:0::1]:4100", "http://[::1]:4100"),
        ("http://127.0.0.1:4100", "http://127.0.0.1:4100"),
        # Only the scheme's own default port is dropped.
        ("http://shop.example.test:443", "http://shop.example.test:443"),
        ("https://shop.example.test:80", "https://shop.example.test:80"),
    ],
)
def test_base_url_is_stored_as_a_normalized_origin(
    tmp_path: Path, written: str, stored: str
) -> None:
    assert load_config(write(tmp_path, f"base_url: '{written}'\n")).base_url == stored


@pytest.mark.parametrize(
    ("origin", "host_and_port"),
    [
        ("http://localhost", ("localhost", 80)),
        ("HTTPS://Shop.Example.Test/", ("shop.example.test", 443)),
        ("http://[0:0::1]:4100", ("[::1]", 4100)),
        ("https://127.0.0.1:8443", ("127.0.0.1", 8443)),
        ("http://shop.example.test:443", ("shop.example.test", 443)),
    ],
)
def test_an_origins_authority_is_its_host_and_port(
    origin: str, host_and_port: tuple[str, int]
) -> None:
    # The egress proxy matches requests by host and port (ADR-0026 amendment,
    # 2026-10-01), read as parse_origin reads the origin.
    assert authority(origin) == host_and_port


@pytest.mark.parametrize("text", ["http://a.test/path", "ftp://a.test", "a.test:80"])
def test_authority_refuses_what_is_not_an_origin(text: str) -> None:
    with pytest.raises(ValueError, match="not an origin"):
        authority(text)


@pytest.mark.parametrize(
    ("text", "key", "problem"),
    [
        # Unknown keys, at every level.
        ("timeout: 30\n", "timeout", "unknown key"),
        (
            "egress: { subresource_host: [a.test] }\n",
            "egress.subresource_host",
            "unknown key",
        ),
        (
            "secrets: { TEST_PASSWORD: { origins: [start], field: password, value: x } }\n",
            "secrets.TEST_PASSWORD.value",
            "unknown key",
        ),
        ("browser: { timezon: UTC }\n", "browser.timezon", "unknown key"),
        ("budgets: { attempt: 3 }\n", "budgets.attempt", "unknown key"),
        ("roles: { navigatr: { model: m } }\n", "roles.navigatr", "'navigator'"),
        (
            "roles: { navigator: { temperature: 0 } }\n",
            "roles.navigator.temperature",
            "unknown key",
        ),
        # base_url is an origin: no path, query, user or other scheme.
        ("base_url: http://127.0.0.1:4100/app\n", "base_url", "not an origin"),
        ("base_url: 'http://a.test?x=1'\n", "base_url", "not an origin"),
        ("base_url: 'http://a.test#top'\n", "base_url", "not an origin"),
        ("base_url: http://user@a.test\n", "base_url", "not an origin"),
        ("base_url: 'http://evil.test\\@a.test'\n", "base_url", "not an origin"),
        ("base_url: ftp://a.test\n", "base_url", "not an origin"),
        ("base_url: 127.0.0.1:4100\n", "base_url", "not an origin"),
        ("base_url: //a.test\n", "base_url", "not an origin"),
        ("base_url: http://a.test:99999\n", "base_url", "not an origin"),
        # Origins no browser can use: port 0, and an IPv6 zone index.
        ("base_url: http://a.test:0\n", "base_url", "not an origin"),
        ("base_url: 'http://[fe80::1%25eth0]'\n", "base_url", "not an origin"),
        ("base_url: http://127.000.000.001\n", "base_url", "not an origin"),
        ("base_url: 'http://a b.test'\n", "base_url", "not an origin"),
        # A browser reads a hex last label as IPv4: 0x7f000001 is 127.0.0.1.
        ("base_url: http://0x7f000001\n", "base_url", "not an origin"),
        ("base_url: http://127.0x1\n", "base_url", "not an origin"),
        ("base_url: http://example.0x10\n", "base_url", "not an origin"),
        ("base_url: 'http://[::ffff:127.0.0.1]'\n", "base_url", "not an origin"),
        # A bracketed host that isn't IPv6 would lose its brackets.
        ("base_url: 'https://[v1.attacker.example]'\n", "base_url", "not an origin"),
        ("base_url: http://-a.test\n", "base_url", "not an origin"),
        ("base_url: 4100\n", "base_url", "valid string"),
        # Secret bindings.
        (
            "secrets: { test-password: " + BINDING + " }\n",
            "secrets.test-password",
            "not a secret name",
        ),
        (
            "secrets: { TEST-PASSWORD: " + BINDING + " }\n",
            "secrets.TEST-PASSWORD",
            "not a secret name",
        ),
        (
            "secrets: { test_password: " + BINDING + " }\n",
            "secrets.test_password",
            "not a secret name",
        ),
        (
            "secrets: { A: { origins: [anywhere], field: password } }\n",
            "secrets.A.origins[0]",
            "not an origin",
        ),
        (
            "secrets: { A: { origins: [https://a.test/login], field: password } }\n",
            "secrets.A.origins[0]",
            "not an origin",
        ),
        (
            "secrets: { A: { origins: [], field: password } }\n",
            "secrets.A.origins",
            "at least 1",
        ),
        (
            "secrets: { A: { origins: [start, start], field: password } }\n",
            "secrets.A.origins",
            "start twice",
        ),
        (
            "secrets: { A: { origins: start, field: password } }\n",
            "secrets.A.origins",
            "must be a list",
        ),
        ("secrets: { A: start }\n", "secrets.A", "must be a mapping of keys"),
        (
            "secrets: { A: { origins: [start], field: 3 } }\n",
            "secrets.A.field",
            "password, or a role and an accessible name",
        ),
        (
            "secrets: { A: { origins: [start], field: email } }\n",
            "secrets.A.field",
            "password, or a role and an accessible name",
        ),
        (
            "secrets: { A: { origins: [start], field: { role: textbox } } }\n",
            "secrets.A.field.name",
            "missing key",
        ),
        # A role no field could have would only refuse every fill (#49).
        (
            "secrets: { A: { origins: [start], field: { role: texbox, name: Key } } }\n",
            "secrets.A.field.role",
            "'texbox' is not an ARIA role",
        ),
        (
            "secrets: { A: { origins: [start], field: { role: 5, name: Key } } }\n",
            "secrets.A.field.role",
            "'5' is not an ARIA role",
        ),
        ("secrets: { A: { origins: [start] } }\n", "secrets.A.field", "missing key"),
        # Egress hosts and private origins.
        (
            "egress: { subresource_hosts: ['https://cdn.test'] }\n",
            "egress.subresource_hosts[0]",
            "not a host",
        ),
        (
            "egress: { subresource_hosts: ['*.cdn.test'] }\n",
            "egress.subresource_hosts[0]",
            "not a host",
        ),
        (
            "egress: { expected_blocked: ['stats.test:443'] }\n",
            "egress.expected_blocked[0]",
            "not a host",
        ),
        (
            "egress: { subresource_hosts: ['[127.0.0.1]'] }\n",
            "egress.subresource_hosts[0]",
            "not a host",
        ),
        (
            "egress: { subresource_hosts: ['[fonts.test]'] }\n",
            "egress.subresource_hosts[0]",
            "not a host",
        ),
        (
            "egress: { subresource_hosts: [0x7f000001] }\n",
            "egress.subresource_hosts[0]",
            "not a host",
        ),
        (
            "egress: { expected_blocked: [stats.test, STATS.test] }\n",
            "egress.expected_blocked",
            "stats.test twice",
        ),
        (
            "egress: { private_origins: [staging.test] }\n",
            "egress.private_origins[0]",
            "not an origin",
        ),
        # Models and budgets.
        (
            "models: { m: { capabilities: [tools], input_usd_per_mtok: 1 } }\n",
            "models.m.output_usd_per_mtok",
            "missing key",
        ),
        (
            "models: { m: { capabilities: [telepathy], input_usd_per_mtok: 1, output_usd_per_mtok: 1 } }\n",
            "models.m.capabilities[0]",
            "'tools'",
        ),
        (
            "models: { m: { capabilities: [], input_usd_per_mtok: 1, output_usd_per_mtok: 1 } }\n",
            "models.m.capabilities",
            "at least 1",
        ),
        (
            "models: { m: { capabilities: [vision], input_usd_per_mtok: -1, output_usd_per_mtok: 1 } }\n",
            "models.m.input_usd_per_mtok",
            "greater than or equal to 0",
        ),
        (
            "roles: { navigator: { model: '' } }\n",
            "roles.navigator.model",
            "at least 1 character",
        ),
        ("budgets: { attempts: 0 }\n", "budgets.attempts", "greater than 0"),
        ("budgets: { attempts: true }\n", "budgets.attempts", "valid integer"),
        ("budgets: { attempts: '3' }\n", "budgets.attempts", "valid integer"),
        ("budgets: { model_usd: .nan }\n", "budgets.model_usd", "valid number"),
        # Browser settings, as BrowserSettings validates them.
        (
            "browser: { viewport: [0, 800] }\n",
            "browser.viewport[0]",
            "greater than or equal to 1",
        ),
        (
            "browser: { timezone: Mars/Phobos }\n",
            "browser.timezone",
            "not an IANA time zone",
        ),
    ],
)
def test_an_invalid_config_names_the_file_the_key_and_the_problem(
    tmp_path: Path, text: str, key: str, problem: str
) -> None:
    path = write(tmp_path, text)

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert any(
        line.startswith(f"{path}: {key}: ") and problem in line
        for line in raised.value.problems
    ), raised.value.problems


@pytest.mark.parametrize(
    ("text", "line", "problem"),
    [
        (
            "base_url: http://a.test\nbase_url: http://b.test\n",
            2,
            "duplicate key 'base_url'",
        ),
        (
            "secrets:\n  A: " + BINDING + "\n  A: " + BINDING + "\n",
            3,
            "duplicate key 'A'",
        ),
        ("1: x\n", 1, "keys must be strings"),
        # An alias is reported at its anchor's line.
        ("budgets: &b { attempts: 3 }\nroles: *b\n", 1, "aliases"),
        ("budgets: { attempts: !!int '3' }\n", 1, "tags"),
        ("browser: [\n", 2, "expected"),
        # A tag on a collection: refused, never read as a plain mapping or list.
        ("browser: !!set {timezone}\n", 1, "tags"),
        ("browser: !custom {timezone: UTC}\n", 1, "tags"),
        ("browser: !!null {timezone: UTC}\n", 1, "tags"),
        ("browser: !!map [UTC]\n", 1, "tags"),
        ("browser: !!bool [x]\n", 1, "tags"),
        ("budgets: { attempts: !!int {a: 1} }\n", 1, "tags"),
        # Characters YAML refuses, and a number Python won't read.
        (
            "budgets:\n  attempts: 3\nbase_url: 'http://a.test\x1b'\n",
            3,
            "unacceptable character",
        ),
        ("budgets: { attempts: " + "9" * 5000 + " }\n", 1, "can't read this int"),
    ],
)
def test_invalid_yaml_names_the_file_and_the_line(
    tmp_path: Path, text: str, line: int, problem: str
) -> None:
    path = write(tmp_path, text)

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert any(
        entry.startswith(f"{path}: line {line}: ") and problem in entry
        for entry in raised.value.problems
    ), raised.value.problems


def test_every_problem_in_the_file_is_reported_together(tmp_path: Path) -> None:
    path = write(tmp_path, "base_url: http://a.test/app\ntimeout: 30\n")

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert {line.split(": ")[1] for line in raised.value.problems} == {
        "base_url",
        "timeout",
    }


def test_a_config_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "- base_url\n")

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert raised.value.problems == (
        f"{path}: the project config must be a mapping of keys",
    )


def test_a_missing_config_is_a_spec_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert raised.value.problems == (
        f"{path}: no such file: a project's spec root is the directory that holds its config.yaml",
    )


def test_a_config_that_is_a_directory_is_a_spec_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.mkdir()

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert raised.value.problems == (f"{path}: a directory, not a file",)


def test_yaml_that_changes_no_value_is_accepted(tmp_path: Path) -> None:
    # ADR-0030: an anchor with no alias, and a tag naming the type a value
    # already has; `no` stays Norwegian's language tag.
    path = write(
        tmp_path,
        "browser: !!map &b { locale: !!str no, viewport: !!seq [800, 600] }\n",
    )

    assert load_config(path).browser.locale == "no"


def test_integers_are_decimal(tmp_path: Path) -> None:
    # YAML 1.1 reads 017 as octal 15 (ADR-0030).
    path = write(tmp_path, "budgets: { attempts: 017 }\n")

    assert load_config(path).budgets.attempts == 17


def test_yaml_nested_too_deeply_is_a_spec_error(tmp_path: Path) -> None:
    path = write(tmp_path, "browser: " + "[" * 5000 + "]" * 5000 + "\n")

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert raised.value.problems == (f"{path}: nested too deeply",)


def test_a_config_that_is_not_utf8_is_a_spec_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"roles: { navigator: { model: caf\xe9 } }\n")

    with pytest.raises(SpecError) as raised:
        load_config(path)

    assert raised.value.problems == (f"{path}: not UTF-8 text",)


def test_a_loaded_config_cannot_be_changed(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "base_url: http://127.0.0.1:4100\n"))

    with pytest.raises(ValidationError):
        config.base_url = "https://elsewhere.example.test"


def test_a_field_binding_takes_every_role_playwright_knows() -> None:
    roles = typing.get_args(AriaRole)

    assert [RoleField(role=role, name="Key").role for role in roles] == list(roles)


def test_a_subject_contract_row_takes_a_spec_an_expectation_a_region_a_part_and_leaf(
    tmp_path: Path,
) -> None:
    config = load_config(
        write(
            tmp_path,
            "subjects: [{spec: login, expect: 0, region: div.banner, part: a.author, leaf: true}]\n",
        )
    )

    row = config.subjects[0]
    assert (row.spec, row.expect) == ("login", 0)
    assert row.contract.model_dump() == {
        "region": "div.banner",
        "part": "a.author",
        "leaf": True,
    }


@pytest.mark.parametrize(
    "region",
    [
        "div .banner",
        "div > a",
        "div+a",
        "div~a",
        "div:scope",
        "div[x]",
        "div'",
        'div"',
        "div,a",
        "div|a",
        "div>>a",
        "Div.banner",
        "div" + ".a" * 9,
        "div." + "a" * 197,
    ],
)
def test_a_region_with_a_combinator_pseudo_class_attribute_quote_or_space_is_refused_naming_the_row(
    tmp_path: Path,
    region: str,
) -> None:
    data = {
        "subjects": [
            {"spec": "login", "expect": 0, "region": region, "part": "a.author"}
        ]
    }
    with pytest.raises(SpecError) as raised:
        load_config(write(tmp_path, json.dumps(data)))
    assert "subjects[0].region:" in str(raised.value)
    assert "not a region" in str(raised.value)


@pytest.mark.parametrize(
    "part",
    [
        ":scope > a",
        "a + i",
        "a ~ i",
        "a[x]",
        "a:hover",
        "a|i",
        "a>>i",
        "a,b",
        "a'",
        'a"',
        "a >a",
        "a  i",
        "a b c d e",
        "a." + "b" * 199,
    ],
)
def test_a_part_selector_with_scope_a_sibling_combinator_an_attribute_a_pseudo_class_or_a_pipe_is_refused_naming_the_row(
    tmp_path: Path,
    part: str,
) -> None:
    data = {
        "subjects": [
            {"spec": "login", "expect": 0, "region": "div.banner", "part": part}
        ]
    }
    with pytest.raises(SpecError) as raised:
        load_config(write(tmp_path, json.dumps(data)))
    assert "subjects[0].part:" in str(raised.value)
    assert "not a part selector" in str(raised.value)


def test_a_row_without_a_part_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match=r"subjects\[0\].part: missing key"):
        load_config(
            write(tmp_path, "subjects: [{spec: login, expect: 0, region: div.banner}]")
        )


@pytest.mark.parametrize("second", ["div.banner", "section.other"])
def test_a_subject_listed_twice_is_refused_naming_it(
    tmp_path: Path, second: str
) -> None:
    text = f"subjects: [{{spec: login, expect: 0, region: div.banner, part: a.author}}, {{spec: login, expect: 0, region: {second}, part: span.date}}]"
    with pytest.raises(SpecError, match="subjects lists login expect 0 twice"):
        load_config(write(tmp_path, text))


def test_subjects_are_optional_and_leaf_defaults_to_false(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, "{}")).subjects == ()
    config = load_config(
        write(
            tmp_path,
            "subjects: [{spec: login, expect: 0, region: app-favorite-button#main.primary, part: div.meta > a.author span.name}]",
        )
    )
    assert config.subjects[0].leaf is False
    assert config.subjects[0].contract.leaf is False
    assert config.subjects[0].part == "div.meta > a.author span.name"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec", ""),
        ("expect", -1),
        ("expect", True),
        ("expect", "0"),
        ("leaf", "false"),
        ("leaf", 0),
        ("extra", True),
    ],
)
def test_a_subject_rows_fields_are_strict(
    tmp_path: Path, field: str, value: object
) -> None:
    row: dict[str, object] = {
        "spec": "login",
        "expect": 0,
        "region": "div.banner",
        "part": "a.author",
    }
    row[field] = value
    with pytest.raises(SpecError) as raised:
        load_config(write(tmp_path, json.dumps({"subjects": [row]})))
    assert f"subjects[0].{field}:" in str(raised.value)
