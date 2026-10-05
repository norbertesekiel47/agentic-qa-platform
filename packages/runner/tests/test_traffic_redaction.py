"""What the browser session records of the page's traffic, scanned for every
bound test secret (#50 A2; ADR-0026's A2 amendment): request and response
methods and URLs, scanned before their bound, while network checks still
read the complete metadata. The browser tests launch real Chromium on the OS
that runs them: Linux in CI, macOS locally."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import pytest
from aqa_core.compiled import NetworkNone, NetworkSeen
from aqa_runner import settling
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.settling import Window

from packages.runner.tests import executor_fixtures
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    App,
    compiled,
    run,
    serving_app,
)
from packages.runner.tests.result_redaction_fixtures import (
    PLAIN,
    browsing,
    fake_secret,
    leaves,
)
from packages.runner.tests.secret_fixtures import copies_found, secret_spec


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def requests(window: Window) -> list[tuple[str, str]]:
    return [
        (request.method, urlsplit(request.url).path) for request in window.requests.kept
    ]


def responses(window: Window) -> list[tuple[str, str, int]]:
    return [
        (response.method, urlsplit(response.url).path, response.status)
        for response in window.responses.kept
    ]


async def fetched(
    app: App, redactor: Redactor, script: str, argument: object = None
) -> Window:
    """The window of a key press on the start page, after the page ran
    `script`, an async function of `argument`, and the window settled."""
    async with browsing(app, redactor) as session:
        await session.navigate(f"{app.origin}/page/start")
        window = await session.press("a")
        await session.page.evaluate(script, argument)
        assert await session.settle(window) == "idle"
        return window


def test_a_page_chosen_method_is_redacted_in_requests_and_responses(app: App) -> None:
    redactor = Redactor([fake_secret("FAKE_METHOD", "fake-secret-method", app.origin)])

    window = asyncio.run(
        fetched(
            app,
            redactor,
            """async () => {
                await fetch('/did/custom', {method: 'fake-secret-method'});
                await fetch('/did/plain');
            }""",
        )
    )

    assert requests(window) == [
        ("[SECRET:FAKE_METHOD]", "/did/custom"),
        ("GET", "/did/plain"),
    ]
    # Python's server answers a method it has no handler for with 501.
    assert responses(window) == [
        ("[SECRET:FAKE_METHOD]", "/did/custom", 501),
        ("GET", "/did/plain", 204),
    ]
    assert ("GET", "/did/plain") in app.seen


URL_VALUE = "fake-url-secret-50"
# The value with every character percent-encoded, in mixed case, and in base64.
PERCENT = "%66%61%6B%65%2D%75%72%6c%2D%73%65%63%72%65%74%2d%35%30"
BASE64 = "ZmFrZS11cmwtc2VjcmV0LTUw"
FETCH_EACH = "async (paths) => { for (const path of paths) await fetch(path); }"


def test_recorded_urls_are_scanned_before_their_bound(app: App) -> None:
    redactor = Redactor([fake_secret("FAKE_URL", URL_VALUE, app.origin)])
    # The value starts 5 characters before the 2048th and ends after it.
    pad = 2043 - len(f"{app.origin}/did/straddle?pad=")
    paths = [
        f"/did/raw?x={URL_VALUE}",
        f"/did/percent?x={PERCENT}",
        f"/did/base64?x={BASE64}",
        f"/did/straddle?pad={'p' * pad}{URL_VALUE}",
        f"/did/benign?pad={'b' * 3000}",
    ]

    window = asyncio.run(fetched(app, redactor, FETCH_EACH, paths))

    expected = [
        f"{app.origin}/did/raw?x=[SECRET:FAKE_URL]",
        f"{app.origin}/did/percent?x=[SECRET:FAKE_URL]",
        f"{app.origin}/did/base64?x=[SECRET:FAKE_URL]",
        f"{app.origin}/did/straddle?pad={'p' * pad}[SECR",
        f"{app.origin}/did/benign?pad={'b' * 3000}"[:2048],
    ]
    assert [request.url for request in window.requests.kept] == expected
    assert [response.url for response in window.responses.kept] == expected
    assert [response.status for response in window.responses.kept] == [204] * 5


type NetworkKind = Literal["network_seen", "network_none"]
KINDS: tuple[NetworkKind, NetworkKind] = ("network_seen", "network_none")


def network(kind: NetworkKind, pattern: str) -> NetworkSeen | NetworkNone:
    model = NetworkSeen if kind == "network_seen" else NetworkNone
    return model.model_validate(
        {
            "id": "a1",
            "expect_index": 0,
            "check": kind,
            "method": "POST",
            "url_pattern": pattern,
            "status_class": "2xx",
        }
    )


@pytest.mark.parametrize(
    ("value", "path", "pattern", "exported"),
    [
        (
            "POST",
            "/did/orders",
            "/did/orders$",
            ("[SECRET:FAKE_COLLIDING]", "/did/orders"),
        ),
        (
            "fake-order-path",
            "/did/fake-order-path",
            "/did/fake-order-path$",
            ("POST", "/did/[SECRET:FAKE_COLLIDING]"),
        ),
        (None, f"/did/long?pad={'p' * 3000}&end=fake-suffix", "end=fake-suffix$", None),
    ],
    ids=["method", "url", "suffix-past-bound"],
)
def test_network_checks_match_complete_metadata_while_exports_are_scanned(
    app: App,
    value: str | None,
    path: str,
    pattern: str,
    exported: tuple[str, str] | None,
) -> None:
    secrets = (
        [] if value is None else [fake_secret("FAKE_COLLIDING", value, app.origin)]
    )

    async def scenario() -> tuple[Window, list[bool], list[bool]]:
        async with browsing(app, Redactor(secrets)) as session:
            await session.navigate(f"{app.origin}/page/start")
            window = await session.press("a")
            await session.page.evaluate(
                "async (path) => { await fetch(path, {method: 'POST'}); }", path
            )
            assert await session.settle(window) == "idle"
            held = [
                await session.network_held(network(kind, pattern)) for kind in KINDS
            ]
            absent = [
                await session.network_held(network(kind, "/did/absent$"))
                for kind in KINDS
            ]
            return window, held, absent

    window, held, absent = asyncio.run(scenario())

    assert held == [True, False]
    assert absent == [False, True]
    recorded = window.responses.kept[-1]
    if exported is None:
        assert (recorded.method, recorded.url) == ("POST", f"{app.origin}{path}"[:2048])
    else:
        method, exported_path = exported
        assert (recorded.method, recorded.url) == (
            method,
            f"{app.origin}{exported_path}",
        )


SAVE: dict[str, Any] = {
    "seq": 1,
    "action": "click",
    "target": "save",
    "side_effect": True,
    "side_effect_basis": "network: POST /write/save",
}
SAVED: dict[str, Any] = {
    "id": "seen",
    "expect_index": 0,
    "check": "network_seen",
    "method": "POST",
    "url_pattern": "/write/save$",
    "status_class": "2xx",
}


@pytest.mark.parametrize(
    ("value", "exported"),
    [
        ("POST", ("[SECRET:FAKE_COLLIDING]", "/write/save")),
        ("write/save", ("POST", "/[SECRET:FAKE_COLLIDING]")),
    ],
    ids=["method", "url"],
)
def test_a_replays_network_checks_hold_when_a_bound_value_collides(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    exported: tuple[str, str],
) -> None:
    monkeypatch.setenv("AQA_SECRET_FAKE_COLLIDING", value)
    spec = secret_spec(
        tmp_path,
        "FAKE_COLLIDING: { origins: [start], field: password }",
        account="{ password: { secret: FAKE_COLLIDING } }",
        start_url="/page/form",
    )
    script = compiled(
        [SAVE],
        targets=FORM_TARGETS,
        assertions=[SAVED, SAVED | {"id": "none", "check": "network_none"}],
    )

    result = run(app, tmp_path, script, spec=spec).result

    assert [assertion.outcome for assertion in result.assertions] == ["pass", "failed"]
    assert result.outcome == "failed"
    window = result.steps[-1].window
    assert window is not None
    method, path = exported
    assert [(r.method, r.url, r.status) for r in window.responses.kept] == [
        (method, f"{app.origin}{path}", 204)
    ]
    assert app.seen.count(("POST", "/write/save")) == 1


class ScanFailedError(Exception):
    pass


class FailingScan(Redactor):
    """A scan that fails on any text naming `fail-scan`, and notes it."""

    def __init__(self) -> None:
        super().__init__([])
        self.failed = False

    def redact(self, text: str, *, limit: int | None = None) -> Redacted:
        if "fail-scan" in text:
            self.failed = True
            raise ScanFailedError("fake callback scan failure")
        return super().redact(text, limit=limit)


def test_a_failed_scan_records_nothing_of_its_request(app: App) -> None:
    scan = FailingScan()

    async def scenario() -> tuple[Window, list[bool], list[bool]]:
        async with browsing(app, scan) as session:
            await session.navigate(f"{app.origin}/page/start")
            window = await session.press("a")
            await session.page.evaluate(
                """async () => {
                    await fetch('/did/plain', {method: 'POST'});
                    fetch('/did/fail-scan', {method: 'POST'});
                }"""
            )
            for _ in range(250):
                if scan.failed:
                    break
                await asyncio.sleep(0.02)
            assert scan.failed
            plain = [
                await session.network_held(network(k, "/did/plain$")) for k in KINDS
            ]
            failed = [
                await session.network_held(network(k, "/did/fail-scan$")) for k in KINDS
            ]
            return window, plain, failed

    window, plain, failed = asyncio.run(scenario())

    assert plain == [True, False]
    assert failed == [False, True]
    assert requests(window) == [("POST", "/did/plain")]
    assert responses(window) == [("POST", "/did/plain", 204)]
    assert window.open == set()
    # The routing's continuation of that request met the error and dropped it.
    assert ("POST", "/did/fail-scan") not in app.seen


HELD_VALUE = "fake-held-secret-50"


def test_a_held_request_stays_scanned_from_open_to_idle(app: App) -> None:
    redactor = Redactor([fake_secret("FAKE_HELD", HELD_VALUE, app.origin)])
    held = f"{app.origin}/held/forever?x=[SECRET:FAKE_HELD]"

    async def scenario() -> tuple[list[str], int, str, Window]:
        async with browsing(app, redactor) as session:
            await session.navigate(f"{app.origin}/page/start")
            window = await session.press("a")
            await session.page.evaluate(
                "value => { fetch('/held/forever?x=' + value); }", HELD_VALUE
            )
            for _ in range(250):
                if window.open:
                    break
                await asyncio.sleep(0.02)
            sent = [request.url for request in window.requests.kept]
            opened = len(window.open)
            app.release("forever")
            return sent, opened, await session.settle(window), window

    sent, opened, settled, window = asyncio.run(scenario())

    assert sent == [held]
    assert opened == 1
    assert settled == "idle"
    assert [(r.url, r.status) for r in window.responses.kept] == [(held, 204)]
    assert {type(leaf) for leaf in leaves(window)} <= set(PLAIN)


def test_an_open_request_leaves_only_scanned_plain_data_in_the_result(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)
    monkeypatch.setitem(
        executor_fixtures.PAGES,
        "holds",
        f"<p>Holding</p><script>fetch('/held/forever?x={HELD_VALUE}')</script>",
    )
    monkeypatch.setenv("AQA_SECRET_FAKE_HELD", HELD_VALUE)
    spec = secret_spec(
        tmp_path,
        "FAKE_HELD: { origins: [start], field: password }",
        account="{ password: { secret: FAKE_HELD } }",
        start_url="/page/holds",
    )

    result = run(app, tmp_path, compiled([]), spec=spec).result

    assert result.outcome == "passed"
    window = result.steps[0].window
    assert window is not None
    assert result.steps[0].settled == "timeout"
    assert len(window.open) == 1
    assert f"{app.origin}/held/forever?x=[SECRET:FAKE_HELD]" in [
        request.url for request in window.requests.kept
    ]
    reachable = leaves(result)
    assert {type(leaf) for leaf in reachable} <= set(PLAIN)
    texts = [leaf for leaf in reachable if isinstance(leaf, str)]
    assert copies_found(HELD_VALUE, texts=[*texts, repr(result)]) == []
