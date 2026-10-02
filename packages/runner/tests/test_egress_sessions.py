"""Browser sessions through the egress proxy: Chromium's every request goes
through it, loopback included, and redirects, WebSockets and failures are
judged at the proxy (ADR-0026 and its 2026-10-01 amendment on the egress
proxy; SECURITY.md §7). These tests launch real Chromium on the OS that runs
them: Linux in CI, macOS locally."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import pytest
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.egress import Connection, EgressGate, EgressPolicy
from aqa_runner.egress_proxy import EgressProxy
from playwright.async_api import Error, async_playwright

from packages.runner.tests.egress_fixtures import (
    LOOPBACK,
    Resolver,
    gate,
    serving,
    unused_port,
)

APP = "app.example.test"


EVIL = "evil.example.test"


# A WebSocket's outcome, as the page sees it.
OPEN_SOCKET = """(url) => new Promise((done) => {
    const socket = new WebSocket(url);
    socket.onopen = () => done("open");
    socket.onerror = () => done("error");
})"""


@asynccontextmanager
async def browsing(egress: EgressGate) -> AsyncIterator[BrowserSession]:
    """A browser session whose traffic goes through a proxy for `egress`."""
    async with (
        async_playwright() as playwright,
        EgressProxy(egress) as proxy,
        open_browser_session(playwright.chromium, egress=proxy) as session,
    ):
        yield session


def app_gate(port: int, **answers: list[str]) -> EgressGate:
    """A run that starts at http://app.example.test:`port`, a local server."""
    return gate(allowed=(f"http://{APP}:{port}",), answers={APP: LOOPBACK, **answers})


async def load(session: BrowserSession, url: str) -> int | str:
    """The status a navigation got, or the network error it failed with."""
    try:
        response = await session.page.goto(url)
    except Error as error:
        return error.message.split()[1]
    assert response is not None
    return response.status


@pytest.mark.parametrize("playwright_opts_out", [False, True])
def test_loopback_goes_through_the_proxy_too(
    *, playwright_opts_out: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Playwright stops sending loopback through a proxy when its driver's
    # environment, the runner's, sets this variable; the session names the
    # bypass list itself.
    if playwright_opts_out:
        monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
    # The run's start origin is elsewhere, so a direct connection would load
    # these pages, and the proxy refuses them.
    with serving() as origin:
        egress = gate(allowed=("http://127.0.0.1:9",))

        async def scenario() -> list[int | str]:
            async with browsing(egress) as session:
                return [
                    await load(session, f"http://127.0.0.1:{origin.port}/"),
                    await load(session, f"http://localhost:{origin.port}/"),
                ]

        outcomes = asyncio.run(scenario())

    assert outcomes == ["net::ERR_EMPTY_RESPONSE"] * 2
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("127.0.0.1", "host"),
        ("localhost", "host"),
    ]
    assert origin.seen == []


def test_a_redirect_to_a_disallowed_host_is_refused_at_the_hop() -> None:
    with serving() as origin:
        egress = app_gate(origin.port)
        hop = f"http://{EVIL}:{origin.port}/landed"

        async def scenario() -> tuple[int | str, list[str]]:
            async with browsing(egress) as session:
                responses: list[str] = []
                session.page.on(
                    "response", lambda response: responses.append(response.url)
                )
                outcome = await load(
                    session, f"http://{APP}:{origin.port}/redirect?to={hop}"
                )
                return outcome, responses

        outcome, responses = asyncio.run(scenario())

    # Refused at the hop, with no response the page could take for the app's.
    assert outcome == "net::ERR_EMPTY_RESPONSE"
    assert [url for url in responses if urlsplit(url).hostname == EVIL] == []
    assert origin.hosts() == [APP]
    assert [(r.host, r.port, r.kind) for r in egress.refusals] == [
        (EVIL, origin.port, "host")
    ]


def test_a_redirect_to_a_subresource_host_passes_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A subresource host passes on its scheme's default port only: 80 for
    # http. The gate's dial of port 80 goes to the fixture's port instead.
    with serving() as origin:
        open_connection = asyncio.open_connection

        async def to_fixture(host: str, port: int, **tls: Any) -> Connection:
            return await open_connection(
                host, origin.port if port == 80 else port, **tls
            )

        monkeypatch.setattr(asyncio, "open_connection", to_fixture)
        egress = gate(
            allowed=(f"http://{APP}:{origin.port}",),
            subresource=("cdn.example.test",),
            # Declared private only so it may resolve to loopback here.
            private=("http://cdn.example.test",),
            answers={APP: LOOPBACK, "cdn.example.test": LOOPBACK},
        )
        hop = "http://cdn.example.test/asset"

        async def scenario() -> int | str:
            async with browsing(egress) as session:
                return await load(
                    session, f"http://{APP}:{origin.port}/redirect?to={hop}"
                )

        outcome = asyncio.run(scenario())

    # Which document may use which origin is the session's check (#44).
    assert outcome == HTTPStatus.OK
    assert [(each.host, each.path) for each in origin.seen][-1] == (
        "cdn.example.test",
        "/asset",
    )
    assert egress.refusals == []


def test_a_post_that_fails_upstream_is_sent_once() -> None:
    # Chromium sends a request again when a reused connection fails, side
    # effects included; the proxy never lets it reuse one.
    with serving() as origin:
        egress = app_gate(origin.port)

        async def scenario() -> None:
            async with browsing(egress) as session:
                await session.page.goto(f"http://{APP}:{origin.port}/")
                await session.page.evaluate(
                    """async () => {
                        await fetch("/warm");
                        await fetch("/boom", {method: "POST", body: "once"})
                            .catch(() => "failed");
                    }"""
                )

        asyncio.run(scenario())

    assert [each.path for each in origin.seen].count("/boom") == 1


def test_a_websocket_to_a_disallowed_host_is_refused_at_connect() -> None:
    with serving() as origin:
        egress = app_gate(origin.port)

        async def scenario() -> list[str]:
            async with browsing(egress) as session:
                await session.page.goto(f"http://{APP}:{origin.port}/")
                return [
                    await session.page.evaluate(
                        OPEN_SOCKET, f"ws://{EVIL}:{origin.port}/ws"
                    ),
                    await session.page.evaluate(
                        OPEN_SOCKET, f"ws://{APP}:{origin.port}/ws"
                    ),
                ]

        outcomes = asyncio.run(scenario())

    # Neither opens: the fixture answers 400 to the upgrade it does receive.
    assert outcomes == ["error", "error"]
    assert [(r.host, r.kind) for r in egress.refusals] == [(EVIL, "host")]
    # The allowed one went through the tunnel; the refused one never left.
    assert [(each.host.rsplit(":", 1)[0], each.upgrade) for each in origin.seen] == [
        (APP, None),
        (APP, "websocket"),
    ]


def test_an_unreachable_upstream_never_becomes_a_response() -> None:
    port = unused_port()
    egress = app_gate(port)

    async def scenario() -> tuple[int | str, str]:
        async with browsing(egress) as session:
            # The blank first page opens the socket; a failed navigation would
            # leave an error page behind.
            socket_outcome = await session.page.evaluate(
                OPEN_SOCKET, f"ws://{APP}:{port}/ws"
            )
            return await load(session, f"http://{APP}:{port}/"), socket_outcome

    outcome, socket_outcome = asyncio.run(scenario())

    # The navigation fails as a network error, never as an HTTP status, and
    # the tunnel fails; each is an infrastructure event.
    assert outcome == "net::ERR_EMPTY_RESPONSE"
    assert socket_outcome == "error"
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        (APP, port),
        (APP, port),
    ]
    assert egress.refusals == []


def test_the_start_origin_and_a_private_origin_load_and_other_loopback_doesnt() -> None:
    with serving() as origin:
        egress = gate(
            allowed=(
                f"http://127.0.0.1:{origin.port}",
                f"http://staging.example.test:{origin.port}",
                f"http://elsewhere.example.test:{origin.port}",
            ),
            private=(f"http://staging.example.test:{origin.port}",),
            answers={
                "staging.example.test": LOOPBACK,
                "elsewhere.example.test": LOOPBACK,
            },
        )

        async def scenario() -> list[int | str]:
            async with browsing(egress) as session:
                return [
                    await load(session, f"http://127.0.0.1:{origin.port}/"),
                    await load(session, f"http://staging.example.test:{origin.port}/"),
                    await load(
                        session, f"http://elsewhere.example.test:{origin.port}/"
                    ),
                ]

        outcomes = asyncio.run(scenario())

    # An allowed origin that isn't declared private may not resolve to one.
    assert outcomes == [HTTPStatus.OK, HTTPStatus.OK, "net::ERR_EMPTY_RESPONSE"]
    assert origin.hosts() == ["127.0.0.1", "staging.example.test"]
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("elsewhere.example.test", "address")
    ]


def test_every_session_of_a_run_keeps_the_pinned_address() -> None:
    with serving() as first, serving("::1", first.port) as rebound:
        resolver = Resolver({APP: LOOPBACK})
        start = f"http://{APP}:{first.port}"
        egress = EgressGate(
            EgressPolicy(
                allowed_origins=(start,), subresource_hosts=(), private_origins=(start,)
            ),
            resolve=resolver,
        )

        async def scenario() -> list[int | str]:
            async with (
                async_playwright() as playwright,
                EgressProxy(egress) as proxy,
            ):
                outcomes = []
                for answer in (["127.0.0.1"], ["::1"]):
                    resolver.answers[APP] = answer  # rebinds after the first
                    async with open_browser_session(
                        playwright.chromium, egress=proxy
                    ) as session:
                        outcomes.append(
                            await load(session, f"http://{APP}:{first.port}/")
                        )
                return outcomes

        outcomes = asyncio.run(scenario())

    assert outcomes == [HTTPStatus.OK, HTTPStatus.OK]
    assert len(first.seen) == 2
    assert rebound.seen == []
    assert resolver.lookups == Counter({APP: 1})


def test_a_closed_proxy_leaves_the_browser_no_way_out() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> int | str:
            async with async_playwright() as playwright:
                proxy = await EgressProxy(gate(allowed=(start,))).__aenter__()
                async with open_browser_session(
                    playwright.chromium, egress=proxy
                ) as session:
                    # The proxy closes while the session still runs.
                    await proxy.__aexit__(None, None, None)
                    return await load(session, f"{start}/")

        outcome = asyncio.run(scenario())

    # Chromium has no other route: it doesn't fall back to a direct connection.
    assert outcome == "net::ERR_PROXY_CONNECTION_FAILED"
    assert origin.seen == []
