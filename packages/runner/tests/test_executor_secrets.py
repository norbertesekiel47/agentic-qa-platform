"""The strict executor's `fill_secret` steps (#49; ADR-0026's fill_secret
amendment, ADR-0024's #46 amendment): the values a replay reads before its
browser starts, the step run through the browser session's `fill_secret`,
and no copy of a value in what the run leaves behind, even when the page
throws it back. The browser tests launch real Chromium on the OS that runs
them: Linux in CI, macOS locally."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.project import SpecError
from aqa_runner import executor
from aqa_runner.bound_secrets import MissingSecretError, SecretLoggedError
from aqa_runner.egress_proxy import EgressBlocks, EgressProxy, RefusedHost
from aqa_runner.executor import RunResult

from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    PAGES,
    App,
    by_role,
    compiled,
    read_steps,
    run,
    serving_app,
)
from packages.runner.tests.secret_fixtures import (
    BOUND_AT_START,
    FAKE_VALUE,
    OTHER_FAKE_VALUE,
    copies_found,
    secret_spec,
)

TARGETS = {
    "password": {
        "semantic": "the sign-in form's password field",
        "locators": [{"css": "input[type=password]"}],
    },
    "name": {
        "semantic": "the sign-in form's name field",
        "locators": [{"css": "input[name=name]"}],
    },
    "sign_in": {
        "semantic": "the sign-in form's submit button",
        "locators": [by_role("button", "Sign in")],
    },
}

FILL_PASSWORD = {
    "seq": 1,
    "action": "fill_secret",
    "target": "password",
    "secret": "TEST_PASSWORD",
    "side_effect": False,
}


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture
def with_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)


def signing_in(
    steps: list[dict[str, Any]], assertions: list[dict[str, Any]] | None = None
) -> CompiledScript:
    return compiled(steps, targets=TARGETS, assertions=assertions)


def no_copy(tmp_path: Path, *texts: str) -> list[str]:
    """Where a copy of the value is: any file under `tmp_path` (the project
    with its compiled script, and the run record), or any of `texts`."""
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    return copies_found(FAKE_VALUE, files=files, texts=texts)


@pytest.mark.usefixtures("with_the_value")
def test_a_secret_filling_runs_egress_record_names_no_host_or_port(
    app: App, tmp_path: Path
) -> None:
    spec = secret_spec(
        tmp_path,
        start_url="/page/fetches?to=http%3A%2F%2Fundeclared.example.test%3A8080%2Fx",
    )

    done = run(app, tmp_path, signing_in([FILL_PASSWORD]), spec=spec)

    assert [step.seq for step in done.result.steps] == [0]
    assert done.result.outcome == "errored"
    assert done.result.error_code == "egress_blocked"
    assert done.result.egress_blocks == EgressBlocks(
        (RefusedHost("undeclared.example.test", 8080),), False
    )
    observed = {invariant.name: invariant for invariant in done.result.invariants}
    assert observed["js_exceptions"].outcome == "violated"
    assert observed["js_exceptions"].total == 1
    for invariant in done.result.invariants:
        assert invariant.seen == ()
    assert json.loads((done.record.path / "egress.json").read_text()) == {
        "error_code": "egress_blocked",
        "refused_count": 1,
        "overflowed": False,
        "hosts_and_ports_withheld": True,
    }


@pytest.mark.usefixtures("with_the_value")
def test_an_overflow_only_block_keeps_a_secret_runs_egress_record_count_only(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocks = EgressBlocks((), overflowed=True)

    def overflowed(
        _proxy: EgressProxy, _expected_blocked: tuple[str, ...]
    ) -> EgressBlocks:
        return blocks

    monkeypatch.setattr(EgressProxy, "egress_blocks", overflowed)
    spec = secret_spec(tmp_path, start_url="/page/signin")

    done = run(app, tmp_path, signing_in([FILL_PASSWORD]), spec=spec)

    assert [step.seq for step in done.result.steps] == [0]
    assert done.result.outcome == "errored"
    assert done.result.error_code == "egress_blocked"
    assert done.result.egress_blocks == blocks
    assert [assertion.outcome for assertion in done.result.assertions] == [
        "not_evaluated"
    ]
    assert json.loads((done.record.path / "egress.json").read_text()) == {
        "error_code": "egress_blocked",
        "refused_count": 0,
        "overflowed": True,
        "hosts_and_ports_withheld": True,
    }


@pytest.mark.usefixtures("with_the_value")
def test_a_value_the_page_logs_and_throws_reaches_nothing_the_run_keeps(
    app: App, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    spec = secret_spec(tmp_path, start_url="/page/signin-echoes")

    result = run(app, tmp_path, signing_in([FILL_PASSWORD]), spec=spec).result
    out, err = capfd.readouterr()

    assert result.outcome == "failed"
    assert result.error_code is None
    assert [assertion.outcome for assertion in result.assertions] == ["pass"]
    observed = {invariant.name: invariant for invariant in result.invariants}
    for name in ("console_errors", "js_exceptions"):
        assert observed[name].outcome == "violated"
        assert observed[name].total > 0
        assert observed[name].seen == ()
    assert no_copy(tmp_path, repr(result), out, err) == []


@pytest.mark.usefixtures("with_the_value")
def test_a_replay_fills_a_secret_into_its_bound_field(app: App, tmp_path: Path) -> None:
    script = signing_in(
        [
            FILL_PASSWORD,
            {
                "seq": 2,
                "action": "click",
                "target": "sign_in",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/signin",
            },
        ],
        [
            {
                "id": "a1",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": "/page/signin",
            }
        ],
    )
    spec = secret_spec(tmp_path, start_url="/page/signin")

    done = run(app, tmp_path, script, spec=spec)

    assert done.result.outcome == "passed"
    # The form took the value, and posted it to the app.
    assert app.bodies == [
        ("/write/signin", f"password={quote_plus(FAKE_VALUE)}&name=".encode())
    ]
    # The step's intent names the secret, never its value.
    assert [line for line in read_steps(done.record.path) if line["seq"] == 1] == [
        {
            "seq": 1,
            "state": "intent",
            "action": {
                "action": "fill_secret",
                "target": "password",
                "secret": "TEST_PASSWORD",
            },
            "side_effect": False,
            "target_used": "password",
        },
        {"seq": 1, "state": "completed", "locator_used": 0, "settled": "idle"},
    ]


@pytest.mark.usefixtures("with_the_value")
def test_after_a_replay_that_filled_a_secret_no_copy_of_its_value_is_stored(
    app: App,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = signing_in(
        [
            FILL_PASSWORD,
            {
                "seq": 2,
                "action": "click",
                "target": "sign_in",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/signin",
            },
        ],
        [
            {
                "id": "a1",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": "/page/signin",
            }
        ],
    )
    # The compiled script as a file, as explore writes one beside its spec.
    compiled_path = tmp_path / "qa" / ".compiled" / "login.json"
    compiled_path.parent.mkdir(parents=True)
    compiled_path.write_text(script.model_dump_json())
    spec = secret_spec(tmp_path, start_url="/page/signin")

    result = run(app, tmp_path, script, spec=spec).result
    out, err = capfd.readouterr()

    assert result.outcome == "passed"
    # The value went in, and out to the app in the form's body, which no
    # record keeps.
    assert [path for path, _ in app.bodies] == ["/write/signin"]
    # The compiled script, the run record, the run's result and its logs.
    assert no_copy(tmp_path, repr(result), out, err, caplog.text) == []


def test_a_missing_value_stops_the_replay_before_the_browser_opens(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AQA_SECRET_TEST_PASSWORD", raising=False)
    opened: list[object] = []

    def no_browser(*args: object, **options: object) -> None:
        opened.append((args, options))
        raise AssertionError("the browser was opened")

    monkeypatch.setattr(executor, "open_browser_session", no_browser)
    spec = secret_spec(tmp_path, start_url="/page/signin")

    with pytest.raises(MissingSecretError) as missing:
        run(app, tmp_path, signing_in([FILL_PASSWORD]), spec=spec)

    assert missing.value.exit_code == 12
    assert missing.value.problems == (
        (
            "AQA_SECRET_TEST_PASSWORD is not set: set it to the value of test secret "
            f"TEST_PASSWORD, which {spec.path} references at preconditions.account.password"
        ),
    )
    assert opened == []
    assert app.paths() == []


@pytest.mark.usefixtures("with_the_value")
def test_a_fill_secret_naming_a_secret_the_spec_doesnt_reference_is_a_spec_error(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []

    def no_browser(*args: object, **options: object) -> None:
        opened.append((args, options))
        raise AssertionError("the browser was opened")

    monkeypatch.setattr(executor, "open_browser_session", no_browser)
    # Declared in the config, so the compiled script loads, but the spec
    # references only TEST_PASSWORD, so the run binds nothing else.
    spec = secret_spec(
        tmp_path,
        f"{BOUND_AT_START}, OTHER_SECRET: {{ origins: [start], field: password }}",
        start_url="/page/signin",
    )
    script = signing_in([{**FILL_PASSWORD, "secret": "OTHER_SECRET"}])

    with pytest.raises(SpecError) as refused:
        run(app, tmp_path, script, spec=spec)

    assert refused.value.problems == (
        (
            "steps[0] (seq 1): fill_secret names OTHER_SECRET, which the spec doesn't "
            "reference, so this run has no binding for it"
        ),
    )
    assert opened == []


@pytest.mark.usefixtures("with_the_value")
def test_a_refused_fill_secret_fails_its_step(app: App, tmp_path: Path) -> None:
    # The target finds the name field, which isn't a password field.
    script = signing_in([{**FILL_PASSWORD, "target": "name"}])

    done = run(
        app, tmp_path, script, spec=secret_spec(tmp_path, start_url="/page/signin")
    )

    step = done.result.steps[1]
    assert (step.outcome, step.error) == (
        "failed",
        'fill_secret refused TEST_PASSWORD: the field isn\'t an <input type="password">',
    )
    assert done.result.outcome == "errored"
    # Its intent stays unresolved, as every failed dispatch's does.
    assert [(line["seq"], line["state"]) for line in read_steps(done.record.path)] == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
    ]
    assert app.bodies == []


@pytest.mark.usefixtures("with_the_value")
def test_a_value_the_page_throws_back_from_the_fill_reaches_nothing_the_run_keeps(
    app: App, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    spec = secret_spec(tmp_path, start_url="/page/signin-throws")

    result = run(app, tmp_path, signing_in([FILL_PASSWORD]), spec=spec).result
    out, err = capfd.readouterr()

    assert (result.steps[1].outcome, result.steps[1].error) == (
        "failed",
        (
            "fill_secret: TEST_PASSWORD wasn't filled: the field takes no text, its "
            "page changed the value, or the page broke or closed during the fill or "
            "its checks"
        ),
    )
    assert no_copy(tmp_path, repr(result), out, err) == []


@pytest.mark.usefixtures("with_the_value")
def test_a_value_the_page_throws_back_from_a_later_action_reaches_nothing_the_run_keeps(
    app: App, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Once filled, the field makes the check before a click on it throw what
    # it holds.
    script = signing_in(
        [
            FILL_PASSWORD,
            {"seq": 2, "action": "click", "target": "password", "side_effect": False},
        ]
    )
    spec = secret_spec(tmp_path, start_url="/page/signin-rethrows")

    result = run(app, tmp_path, script, spec=spec).result
    out, err = capfd.readouterr()

    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
        (2, "failed"),
    ]
    # Playwright's error type and the call it names, never its message.
    assert result.steps[2].error == (
        "Error: ElementHandle.evaluate: the rest is withheld, since the page "
        "was handed a test secret"
    )
    assert no_copy(tmp_path, repr(result), out, err) == []


@pytest.mark.usefixtures("with_the_value")
def test_a_value_the_page_throws_back_from_an_assertion_s_look_reaches_nothing_the_run_keeps(
    app: App, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Once filled, reading the field's text throws what it holds.
    script = signing_in(
        [FILL_PASSWORD],
        [
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "password",
                "text": "nothing",
            }
        ],
    )
    spec = secret_spec(tmp_path, start_url="/page/signin-hides")

    result: RunResult = run(app, tmp_path, script, spec=spec).result
    out, err = capfd.readouterr()

    [assertion] = result.assertions
    assert assertion.outcome == "not_evaluated"
    # Playwright's error type and call, whichever call the page broke.
    assert (assertion.error or "").endswith(
        ": the rest is withheld, since the page was handed a test secret"
    )
    assert result.outcome == "errored"
    assert no_copy(tmp_path, repr(result), out, err) == []


@pytest.mark.usefixtures("with_the_value")
def test_each_fill_secret_step_fills_the_secret_it_names(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_NAME", OTHER_FAKE_VALUE)
    script = signing_in(
        [
            FILL_PASSWORD,
            {
                "seq": 2,
                "action": "fill_secret",
                "target": "name",
                "secret": "TEST_NAME",
                "side_effect": False,
            },
            {
                "seq": 3,
                "action": "click",
                "target": "sign_in",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/signin",
            },
        ],
    )
    spec = secret_spec(
        tmp_path,
        f"{BOUND_AT_START}, "
        "TEST_NAME: { origins: [start], field: { role: textbox, name: Name } }",
        account="{ email: { secret: TEST_NAME }, password: { secret: TEST_PASSWORD } }",
        start_url="/page/signin",
    )

    result = run(app, tmp_path, script, spec=spec).result

    assert result.outcome == "passed"
    posted = f"password={quote_plus(FAKE_VALUE)}&name={quote_plus(OTHER_FAKE_VALUE)}"
    assert app.bodies == [("/write/signin", posted.encode())]


@pytest.mark.usefixtures("with_the_value")
def test_a_run_that_fills_no_secret_keeps_playwrights_reasons(
    app: App, tmp_path: Path
) -> None:
    # The spec references a secret, but the script fills none: the page is
    # never handed a value, so a failed step's reason is as any run's.
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )
    spec = secret_spec(tmp_path, start_url="/page/throws")

    step = run(app, tmp_path, script, spec=spec).result.steps[1]

    assert step.outcome == "failed"
    assert (step.error or "").startswith("Error: ElementHandle.evaluate: Error: ")


@pytest.mark.parametrize(
    ("how", "raised"),
    [
        # Playwright would print the value as the fill sends it.
        ("logged", SecretLoggedError),
        # The secret is bound to an origin the spec no longer allows.
        ("unbound", SpecError),
    ],
)
@pytest.mark.usefixtures("with_the_value")
def test_a_run_that_cant_use_its_secrets_stops_before_the_browser_opens(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    how: str,
    raised: type[Exception],
) -> None:
    opened: list[object] = []

    def no_browser(*args: object, **options: object) -> None:
        opened.append((args, options))
        raise AssertionError("the browser was opened")

    monkeypatch.setattr(executor, "open_browser_session", no_browser)
    secrets = BOUND_AT_START
    if how == "logged":
        monkeypatch.setenv("DEBUGP", "1")
    else:
        secrets = (
            "TEST_PASSWORD: { origins: [start, 'http://other.example.test:1'], "
            "field: password }"
        )
    spec = secret_spec(tmp_path, secrets, start_url="/page/signin")

    with pytest.raises(raised):
        run(app, tmp_path, signing_in([FILL_PASSWORD]), spec=spec)

    assert opened == []
    assert app.paths() == []


@pytest.mark.usefixtures("with_the_value")
@pytest.mark.parametrize("fill_other", [False, True])
def test_a_replay_redacts_a_bound_secret_its_script_never_fills(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fill_other: bool
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_NAME", OTHER_FAKE_VALUE)
    monkeypatch.setitem(
        PAGES,
        "reflect",
        f'<meta charset="utf-8"><p>{FAKE_VALUE}</p><label>Name <input name="name"></label>',
    )
    spec = secret_spec(
        tmp_path,
        f"{BOUND_AT_START}, TEST_NAME: {{ origins: [start], field: {{ role: textbox, name: Name }} }}",
        account="{ email: { secret: TEST_NAME }, password: { secret: TEST_PASSWORD } }",
        start_url="/page/reflect",
    )
    steps = (
        [
            {
                "seq": 1,
                "action": "fill_secret",
                "target": "name",
                "secret": "TEST_NAME",
                "side_effect": False,
            }
        ]
        if fill_other
        else []
    )
    script = signing_in(
        steps,
        [
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_visible",
                "text": "[SECRET:TEST_PASSWORD]",
            }
        ],
    )
    done = run(app, tmp_path, script, spec=spec)
    assert done.result.outcome == "passed", done.result
    assert all(step.outcome == "completed" for step in done.result.steps)


@pytest.mark.parametrize("value", [" \ufeff", "abc", "éé"])
def test_an_unusable_value_stops_replay_before_the_browser_opens(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)

    def no_browser(*_args: object, **_options: object) -> None:
        raise AssertionError("the browser was opened")

    monkeypatch.setattr(executor, "open_browser_session", no_browser)
    spec = secret_spec(tmp_path, start_url="/page/signin")
    with pytest.raises(MissingSecretError) as refused:
        run(app, tmp_path, signing_in([FILL_PASSWORD]), spec=spec)
    assert refused.value.exit_code == 12
    assert app.paths() == []
