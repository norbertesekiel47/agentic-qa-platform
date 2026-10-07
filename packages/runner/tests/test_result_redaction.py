"""A replay's errors and infrastructure events, scanned for every bound test
secret (#50 A2, ADR-0026's A2 amendment): what a failed step, an unevaluated
assertion and an egress failure carry into a `RunResult`, through the real
transport and the real browser. The pure presenter's literals are in
test_error_text.py."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner.executor import RunResult
from aqa_runner.redaction import REASON_CHARS

from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    PAGES,
    App,
    a_spec,
    compiled,
    run,
    serving_app,
)
from packages.runner.tests.result_redaction_fixtures import (
    PLAIN,
    leaves,
    probing_account,
    raw_http_server,
)
from packages.runner.tests.secret_fixtures import (
    BOUND_AT_START,
    FAKE_VALUE,
    OTHER_FAKE_VALUE,
    secret_spec,
)

# What the fixture app's malformed probe sends as a status line, and the
# fake test secret that text is bound as (#48's regression, A2's repair).
MALFORMED = "fake-sensitive"
PROBE: dict[str, Any] = {
    "id": "a1",
    "expect_index": 0,
    "check": "probe_equals",
    "probe": "count",
    "json_path": "$.count",
    "value": 2,
}
BASELINE: dict[str, Any] = {
    "id": "a1",
    "expect_index": 0,
    "check": "probe_equals_baseline",
    "probe": "count",
}
CLICK = {
    "seq": 9,
    "action": "click",
    "target": "save",
    "side_effect": True,
    "side_effect_basis": "network: POST /write/save",
}
TWO_SECRETS = (
    f"{BOUND_AT_START}, TEST_NAME: "
    "{ origins: [start], field: { role: textbox, name: Name } }"
)
TWO_ACCOUNTS = "{ email: { secret: TEST_NAME }, password: { secret: TEST_PASSWORD } }"


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def everything(result: RunResult) -> str:
    """The result's `repr`, and every string and number reachable in it,
    which `repr` alone can leave out; plain data only."""
    found = leaves(result)
    assert all(isinstance(leaf, PLAIN) for leaf in found)
    return repr(result) + "\n".join(str(leaf) for leaf in found)


@pytest.mark.parametrize("baseline", [False, True])
def test_a_malformed_status_line_leaves_only_h11s_error_class_in_the_event(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: bool
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", MALFORMED)
    spec = secret_spec(
        tmp_path, start_url="/page/form", account=probing_account("/probe/malformed")
    )
    script = compiled(
        [CLICK] if baseline else [],
        targets=FORM_TARGETS,
        assertions=[BASELINE if baseline else PROBE],
        probe_baselines={"count": {"capture_before_seq": 9, "json_path": "$.count"}}
        if baseline
        else {},
    )

    result = run(app, tmp_path, script, spec=spec).result

    (event,) = result.infrastructure_events
    assert event.host == "127.0.0.1"
    assert event.port == int(app.origin.rsplit(":", 1)[1])
    assert event.cause == "the exchange broke off: h11.RemoteProtocolError"
    assert result.outcome == "errored"
    if not baseline:
        assert result.assertions[0].error == (
            "the probe request could not reach its allowed origin"
        )
    assert MALFORMED not in everything(result)
    # The baseline's capture failed before the click was dispatched.
    assert ("POST", "/write/save") not in app.seen


def test_an_unbound_malformed_status_line_keeps_only_h11s_error_class(
    app: App, tmp_path: Path
) -> None:
    spec = a_spec(
        tmp_path,
        ProjectConfig(),
        start_url="/page/form",
        probes={"count": "GET /probe/malformed"},
    )

    result = run(app, tmp_path, compiled([], assertions=[PROBE]), spec=spec).result

    (event,) = result.infrastructure_events
    assert event.cause == "the exchange broke off: h11.RemoteProtocolError"


@pytest.mark.parametrize(
    ("encoding", "escaped"), [("utf-8", "\\xc3\\xa4"), ("latin-1", "\\xe4")]
)
def test_a_bound_value_in_a_malformed_status_line_leaves_no_trace(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    escaped: str,
) -> None:
    # h11 quotes the line's bytes with each non-ASCII one escaped, so a value
    # the upstream sends as Latin-1 is a spelling no scan looks for.
    value = "fäke-sénsitive"
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)
    reply = f"HTTP/1.1 {value}\r\n\r\n".encode(encoding)
    with raw_http_server(reply) as origin:
        spec = secret_spec(tmp_path, start_url="/")
        result = run(app, tmp_path, compiled([]), spec=spec, origins=(origin,)).result

    (event,) = result.infrastructure_events
    assert (event.host, event.port) == ("127.0.0.1", int(origin.rsplit(":", 1)[1]))
    assert event.cause == "the exchange broke off: h11.RemoteProtocolError"
    assert result.outcome == "errored"
    text = everything(result)
    assert value not in text
    assert escaped not in text


def test_an_unfilled_bound_value_is_scanned_in_a_step_reason(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setenv("AQA_SECRET_TEST_NAME", OTHER_FAKE_VALUE)
    monkeypatch.setitem(
        PAGES,
        "throws-bound",
        "<label>Name <input></label><script>document.execCommand = () => "
        f'{{ throw new Error("{OTHER_FAKE_VALUE}"); }};</script>',
    )
    spec = secret_spec(
        tmp_path, TWO_SECRETS, account=TWO_ACCOUNTS, start_url="/page/throws-bound"
    )
    fill = {
        "seq": 1,
        "action": "fill",
        "target": "name",
        "value": "Ada",
        "side_effect": False,
    }

    result = run(
        app, tmp_path, compiled([fill], targets=FORM_TARGETS), spec=spec
    ).result

    step = result.steps[1]
    assert step.outcome == "failed"
    # No fill_secret step, so Playwright's own reason shows: scanned.
    assert step.error == "Error: ElementHandle.evaluate: Error: [SECRET:TEST_NAME]"
    assert OTHER_FAKE_VALUE not in everything(result)


def test_an_assertion_reason_holding_a_bound_value_is_scanned(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reason after a stopped look names the assertion that raised it.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "not evaluated after")
    spec = secret_spec(tmp_path, start_url="/page/leaves")
    script = compiled(
        [],
        targets={
            "never": {
                "semantic": "text that never comes",
                "locators": [{"css": "#never"}],
            }
        },
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "never",
                "text": "never",
            },
            {"id": "a2", "expect_index": 0, "check": "url_matches", "pattern": "/"},
        ],
    )

    result = run(app, tmp_path, script, spec=spec).result

    assert (
        result.assertions[1].error
        == "[SECRET:TEST_PASSWORD] a1's look at the page raised"
    )


def test_a_join_spanning_bound_value_is_scanned_after_a_secret_fill(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TEST_NAME is never filled, but the page was handed TEST_PASSWORD, so a
    # later reason keeps only Playwright's class and call, which together
    # are TEST_NAME's value.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setenv("AQA_SECRET_TEST_NAME", "Error: ElementHandle.evaluate")
    spec = secret_spec(
        tmp_path, TWO_SECRETS, account=TWO_ACCOUNTS, start_url="/page/signin-rethrows"
    )
    fill = {
        "seq": 1,
        "action": "fill_secret",
        "target": "password",
        "secret": "TEST_PASSWORD",
        "side_effect": False,
    }
    click = {"seq": 2, "action": "click", "target": "password", "side_effect": False}
    targets = {
        "password": {
            "semantic": "the password field",
            "locators": [{"css": "input[type=password]"}],
        }
    }

    result = run(
        app, tmp_path, compiled([fill, click], targets=targets), spec=spec
    ).result

    assert result.steps[2].outcome == "failed"
    assert result.steps[2].error == (
        "[SECRET:TEST_NAME]: the rest is withheld, since the page was handed a "
        "test secret"
    )
    assert FAKE_VALUE not in everything(result)


def test_a_valid_status_line_reaches_the_normal_result(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "fäke-sénsitive")
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    with raw_http_server(reply) as origin:
        spec = secret_spec(tmp_path, start_url="/")
        result = run(app, tmp_path, compiled([]), spec=spec, origins=(origin,)).result

    assert result.infrastructure_events == ()


def test_a_join_spanning_bound_value_is_scanned_in_a_run_with_no_fill(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setenv("AQA_SECRET_TEST_NAME", "Error: ElementHandle.evaluate")
    monkeypatch.setitem(
        PAGES,
        "throws-bound",
        "<label>Name <input></label><script>document.execCommand = () => "
        '{ throw new Error("fake page text"); };</script>',
    )
    spec = secret_spec(
        tmp_path, TWO_SECRETS, account=TWO_ACCOUNTS, start_url="/page/throws-bound"
    )
    fill = {
        "seq": 1,
        "action": "fill",
        "target": "name",
        "value": "Ada",
        "side_effect": False,
    }

    result = run(
        app, tmp_path, compiled([fill], targets=FORM_TARGETS), spec=spec
    ).result

    # Playwright's message shows in a run that fills no secret: its class
    # and call are the bound value, scanned across their join.
    assert result.steps[1].error == "[SECRET:TEST_NAME]: Error: fake page text"


def test_an_owned_message_is_one_line_of_at_most_the_reason_bound(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refused fill_secret names the page's origin and the field, and the
    # origins the secret allows after it: what runs past the bound is cut.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    origins = ", ".join(f"https://long-{n}.example.test" for n in range(12))
    spec = secret_spec(
        tmp_path,
        f"TEST_PASSWORD: {{ origins: [{origins}], field: password }}",
        start_url="/page/form",
        allowed_origins=tuple(f"https://long-{n}.example.test" for n in range(12)),
    )
    fill = {
        "seq": 1,
        "action": "fill_secret",
        "target": "name",
        "secret": "TEST_PASSWORD",
        "side_effect": False,
    }

    result = run(
        app, tmp_path, compiled([fill], targets=FORM_TARGETS), spec=spec
    ).result

    error = result.steps[1].error or ""
    assert error.startswith("fill_secret refused TEST_PASSWORD: the page is on ")
    assert len(error) == REASON_CHARS
    assert "\n" not in error


def test_a_bound_value_in_an_infrastructure_events_host_is_scanned(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fixture app's loopback address is both the allowed host and, here,
    # a bound value.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "127.0.0.1")
    spec = secret_spec(
        tmp_path, start_url="/page/form", account=probing_account("/probe/malformed")
    )

    result = run(app, tmp_path, compiled([], assertions=[PROBE]), spec=spec).result

    (event,) = result.infrastructure_events
    assert event.host == "[SECRET:TEST_PASSWORD]"
    assert "127.0.0.1" not in event.cause


def test_the_action_timeout_reason_is_scanned(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "didn't finish within")
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    spec = secret_spec(tmp_path, start_url="/page/stall")
    fill = {
        "seq": 1,
        "action": "fill",
        "target": "name",
        "value": "Ada",
        "side_effect": False,
    }

    result = run(
        app, tmp_path, compiled([fill], targets=FORM_TARGETS), config=config, spec=spec
    ).result

    # The run's one second and the executor's one second of margin.
    assert result.steps[1].error == "the action [SECRET:TEST_PASSWORD] 2 s"
