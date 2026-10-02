"""Runner-side requests: the reset hook's and probes' own requests, which the
runner sends through the run's egress gate (ADR-0026 and its 2026-10-01
amendment on the egress proxy; SECURITY.md §7). Test-first (TESTING.md §2).
The cookie test launches real Chromium on the OS that runs it: Linux in CI,
macOS locally."""

import asyncio
import contextlib
import socket
import ssl
import subprocess
import threading
from collections import Counter
from http import HTTPStatus
from ipaddress import ip_address
from pathlib import Path

import pytest
from aqa_runner.browser_session import open_browser_session
from aqa_runner.egress import (
    EgressGate,
    EgressPolicy,
    EgressRefusedError,
    EgressUpstreamError,
)
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.runner_requests import Method, runner_request
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import (
    LOOPBACK,
    Resolver,
    gate,
    header_names,
    raw_upstream,
    serving,
    unused_port,
)

APP = "app.example.test"


def app_run(
    port: int, *, other: tuple[str, ...] = (), subresource: tuple[str, ...] = ()
) -> tuple[EgressGate, Resolver]:
    """The gate of a run that starts at http://app.example.test:`port`, a
    local server, with `other` origins and `subresource` hosts allowed too,
    and its local DNS fixture."""
    start = f"http://{APP}:{port}"
    resolver = Resolver({APP: LOOPBACK})
    policy = EgressPolicy(
        allowed_origins=(start, *other),
        subresource_hosts=subresource,
        private_origins=(start,),
    )
    return EgressGate(policy, resolve=resolver), resolver


def test_a_runner_request_to_an_allowed_origin_gets_its_response() -> None:
    with serving() as origin:
        egress, _ = app_run(origin.port)
        site = f"http://{APP}:{origin.port}"

        async def scenario() -> list[tuple[int, bytes]]:
            got = await runner_request(egress, "GET", f"{site}/count?email=a%40b.test")
            posted = await runner_request(egress, "POST", f"{site}/reset?f=new#top")
            return [(got.status, got.body), (posted.status, posted.body)]

        responses = asyncio.run(scenario())

    assert responses == [
        (
            HTTPStatus.OK,
            f"<!doctype html><title>{APP}:{origin.port}/count?email=a%40b.test</title>".encode(),
        ),
        (HTTPStatus.NO_CONTENT, b""),
    ]
    # The path and query as written, under the origin's own Host.
    assert [(each.host, each.path) for each in origin.seen] == [
        (f"{APP}:{origin.port}", "/count?email=a%40b.test"),
        (f"{APP}:{origin.port}", "/reset?f=new"),
    ]
    assert egress.refusals == []


def test_a_runner_request_reads_a_body_that_arrives_in_parts() -> None:
    with serving() as origin:
        egress, _ = app_run(origin.port)
        # 1 MB, the second half a moment after the first.
        url = f"http://{APP}:{origin.port}/slow"

        response = asyncio.run(runner_request(egress, "GET", url))

    assert response.body == b"x" * 2**20


@pytest.mark.parametrize(
    ("method", "framing"), [("GET", set()), ("POST", {b"content-length"})]
)
def test_a_runner_request_sends_only_its_host_and_framing(
    method: Method, framing: set[bytes]
) -> None:
    async def scenario() -> bytes:
        async with raw_upstream(b"HTTP/1.1 204 No Content\r\n\r\n") as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            await runner_request(gate(allowed=(start,)), method, f"{start}/reset")
            return bytes(upstream.received)

    received = asyncio.run(scenario())

    assert received.startswith(f"{method} /reset HTTP/1.1\r\n".encode())
    # No cookie, no credentials, nothing the browser holds.
    assert header_names(received) == {b"host", b"connection"} | framing


def test_a_runner_request_reads_past_informational_responses() -> None:
    reply = (
        b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
    )

    async def scenario() -> tuple[int, bytes]:
        async with raw_upstream(reply) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            response = await runner_request(gate(allowed=(start,)), "GET", start)
            return response.status, response.body

    assert asyncio.run(scenario()) == (HTTPStatus.OK, b"{}")


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example.test/reset",
        # An allowed origin's host on another port.
        f"http://{APP}:9/reset",
        # Subresource hosts serve the browser's pages, not the runner.
        "http://cdn.example.test/reset",
        "https://cdn.example.test/reset",
    ],
)
def test_a_runner_request_to_a_non_allowed_origin_is_refused(url: str) -> None:
    with serving() as origin:
        egress, resolver = app_run(origin.port, subresource=("cdn.example.test",))

        async def scenario() -> EgressRefusedError:
            with pytest.raises(EgressRefusedError) as refused:
                await runner_request(egress, "POST", url)
            return refused.value

        refused = asyncio.run(scenario())

    assert refused.refusal.kind == "host"
    assert egress.refusals == [refused.refusal]
    # Refused before any lookup, so nothing was dialled.
    assert resolver.lookups == Counter()
    assert origin.seen == []


@pytest.mark.parametrize(
    ("allowed", "requested"), [("https", "http"), ("http", "https")]
)
def test_a_runner_request_on_another_scheme_than_its_origins_is_refused(
    allowed: str, requested: str
) -> None:
    # An origin is its scheme, host and port: a reset hook allowed over https
    # never goes out in plaintext on that port, nor the reverse.
    with serving() as origin:
        start = f"{allowed}://{APP}:{origin.port}"
        resolver = Resolver({APP: LOOPBACK})
        egress = EgressGate(
            EgressPolicy(
                allowed_origins=(start,), subresource_hosts=(), private_origins=(start,)
            ),
            resolve=resolver,
        )
        url = f"{requested}://{APP}:{origin.port}/reset"

        async def scenario() -> EgressRefusedError:
            with pytest.raises(EgressRefusedError) as refused:
                await runner_request(egress, "POST", url)
            return refused.value

        refused = asyncio.run(scenario())

    assert (refused.refusal.host, refused.refusal.port) == (APP, origin.port)
    assert refused.refusal.kind == "host"
    assert egress.refusals == [refused.refusal]
    # Refused before any lookup: nothing was dialled.
    assert resolver.lookups == Counter()
    assert origin.seen == []
    assert egress.infrastructure_events == []


def test_a_runner_request_to_its_exact_allowed_origin_passes() -> None:
    with serving() as origin:
        # Written as a person might write it; read as every origin is read.
        start = f"HTTP://{APP.upper()}:{origin.port}/"
        egress = gate(allowed=(start,), answers={APP: LOOPBACK})
        url = f"http://{APP}:{origin.port}/reset"

        response = asyncio.run(runner_request(egress, "POST", url))

    assert response.status == HTTPStatus.NO_CONTENT
    assert [each.path for each in origin.seen] == ["/reset"]
    assert egress.refusals == []


def test_a_runner_request_follows_the_ip_policy() -> None:
    with serving() as origin:
        # Allowed, but not declared private, so it may not resolve to loopback.
        probes = f"http://probes.example.test:{origin.port}"
        egress, resolver = app_run(origin.port, other=(probes,))
        resolver.answers["probes.example.test"] = LOOPBACK

        async def scenario() -> EgressRefusedError:
            with pytest.raises(EgressRefusedError) as refused:
                await runner_request(egress, "GET", f"{probes}/count")
            return refused.value

        refused = asyncio.run(scenario())

    assert refused.refusal.kind == "address"
    assert egress.refusals == [refused.refusal]
    assert origin.seen == []


def test_runner_requests_keep_the_runs_pinned_address() -> None:
    with serving() as first, serving("::1", first.port) as rebound:
        egress, resolver = app_run(first.port)
        url = f"http://{APP}:{first.port}/count"

        async def scenario() -> list[int]:
            statuses = [(await runner_request(egress, "GET", url)).status]
            # The name rebinds to another address the policy would pass.
            resolver.answers[APP] = ["::1"]
            statuses.append((await runner_request(egress, "GET", url)).status)
            return statuses

        statuses = asyncio.run(scenario())

    assert statuses == [HTTPStatus.OK, HTTPStatus.OK]
    assert len(first.seen) == 2
    assert rebound.seen == []
    assert resolver.lookups == Counter({APP: 1})


def test_runner_requests_carry_none_of_the_browsers_cookies() -> None:
    with serving() as origin:
        egress, _ = app_run(origin.port)
        site = f"http://{APP}:{origin.port}"

        async def scenario() -> None:
            async with (
                async_playwright() as playwright,
                EgressProxy(egress) as proxy,
                open_browser_session(playwright.chromium, egress=proxy) as session,
            ):
                await session.page.goto(f"{site}/set-cookie")
                await session.page.goto(f"{site}/browser")
                await runner_request(egress, "GET", f"{site}/probe")
                await runner_request(egress, "POST", f"{site}/reset")
                # Nor is a cookie a runner-side response sets ever sent.
                await runner_request(egress, "GET", f"{site}/set-cookie")
                await runner_request(egress, "GET", f"{site}/probe-again")

        asyncio.run(scenario())

    assert [(each.path, each.cookie) for each in origin.seen] == [
        ("/set-cookie", None),
        # The browser holds the cookie and sends it back.
        ("/browser", "session=1"),
        ("/probe", None),
        ("/reset", None),
        ("/set-cookie", None),
        ("/probe-again", None),
    ]


def test_a_runner_request_returns_a_redirect_unfollowed() -> None:
    with serving() as origin:
        egress, _ = app_run(origin.port)
        site = f"http://{APP}:{origin.port}"
        # The redirect's target is allowed: following it would reach it.
        redirect = f"/redirect?to={site}/landed"

        response = asyncio.run(runner_request(egress, "GET", f"{site}{redirect}"))

    assert response.status == HTTPStatus.FOUND
    assert [each.path for each in origin.seen] == [redirect]


def certificate(directory: Path, name: str) -> ssl.SSLContext:
    """A server context with a self-signed certificate for `name`, a host
    name or an IP address, made now; its PEM file is `directory`/cert.pem."""
    kind = "DNS" if _ip_literal(name) is None else "IP"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(directory / "key.pem"),
            "-out",
            str(directory / "cert.pem"),
            "-days",
            "1",
            "-subj",
            f"/CN={name}",
            "-addext",
            f"subjectAltName={kind}:{name}",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    return context


def _ip_literal(name: str) -> str | None:
    try:
        return str(ip_address(name))
    except ValueError:
        return None


def test_a_runner_request_over_https_verifies_the_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tls = certificate(tmp_path, APP)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    with serving(tls=tls) as origin:
        start = f"https://{APP}:{origin.port}"
        # The same server under a name its certificate doesn't give.
        other = f"https://other.example.test:{origin.port}"
        egress = EgressGate(
            EgressPolicy(
                allowed_origins=(start, other),
                subresource_hosts=(),
                private_origins=(start, other),
            ),
            resolve=Resolver({APP: LOOPBACK, "other.example.test": LOOPBACK}),
        )

        async def scenario() -> int:
            # The system's trust store doesn't hold the certificate.
            with pytest.raises(EgressUpstreamError):
                await runner_request(egress, "GET", f"{start}/untrusted")
            # https://docs.python.org/3.14/library/ssl.html#ssl.get_default_verify_paths
            monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "cert.pem"))
            with pytest.raises(EgressUpstreamError):
                await runner_request(egress, "GET", f"{other}/misnamed")
            return (await runner_request(egress, "GET", f"{start}/trusted")).status

        status = asyncio.run(scenario())

    assert status == HTTPStatus.OK
    assert [each.path for each in origin.seen] == ["/trusted"]
    events = egress.infrastructure_events
    assert [event.host for event in events] == [APP, "other.example.test"]
    assert all("certificate" in event.cause for event in events)
    assert egress.refusals == []


def test_a_runner_request_to_an_ip_address_verifies_its_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tls = certificate(tmp_path, "::1")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "cert.pem"))
    with serving("::1", tls=tls) as origin:
        start = f"https://[::1]:{origin.port}"

        response = asyncio.run(
            runner_request(gate(allowed=(start,)), "GET", f"{start}/probe")
        )

    assert response.status == HTTPStatus.OK
    assert [(each.host, each.path) for each in origin.seen] == [
        (f"[::1]:{origin.port}", "/probe")
    ]


def test_a_runner_request_never_takes_plaintext_sent_before_the_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An on-path attacker without the certificate answers first, in the
    # clear; the real server's TLS follows.
    tls = certificate(tmp_path, APP)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "cert.pem"))
    listener = socket.create_server(("127.0.0.1", 0))
    start = f"https://{APP}:{listener.getsockname()[1]}"

    def answer() -> None:
        connection, _ = listener.accept()
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nforged")
        # The client gives up on the handshake: an SSLError or a reset.
        with (
            contextlib.suppress(OSError),
            tls.wrap_socket(connection, server_side=True) as private,
        ):
            private.recv(65536)
            private.sendall(b"HTTP/1.1 204 No Content\r\n\r\n")
        connection.close()

    server = threading.Thread(target=answer)
    server.start()
    egress = gate(allowed=(start,), answers={APP: LOOPBACK})
    try:
        with pytest.raises(EgressUpstreamError) as failed:
            asyncio.run(runner_request(egress, "GET", f"{start}/probe"))
    finally:
        server.join(5)
        listener.close()

    assert egress.infrastructure_events == [failed.value.event]


@pytest.mark.parametrize(
    ("method", "path", "listening"),
    [("GET", "/", False), ("GET", "/drop", True), ("POST", "/boom", True)],
    ids=["unreachable", "cut short", "no response"],
)
def test_a_runner_request_that_fails_upstream_is_an_infrastructure_error(
    method: Method, path: str, *, listening: bool
) -> None:
    with serving() as origin:
        port = origin.port if listening else unused_port()
        egress, _ = app_run(port)

        async def scenario() -> EgressUpstreamError:
            with pytest.raises(EgressUpstreamError) as failed:
                await runner_request(egress, method, f"http://{APP}:{port}{path}")
            return failed.value

        failed = asyncio.run(scenario())

    assert egress.infrastructure_events == [failed.event]
    assert (failed.event.host, failed.event.port) == (APP, port)
    assert egress.refusals == []


def test_a_runner_request_closes_its_connection_when_its_caller_times_out() -> None:
    # A runner-side request has no deadline of its own: its caller sets one.
    async def scenario() -> None:
        async with raw_upstream() as silent:
            start = f"http://127.0.0.1:{silent.port}"
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await runner_request(gate(allowed=(start,)), "GET", start)
            # Bounded: a connection left open would hold the upstream forever.
            await asyncio.wait_for(silent.closed.wait(), 5)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "/reset",
        f"ftp://{APP}/reset",
        f"http://user@{APP}/reset",
        f"http://{APP}:99999/reset",
        # Targets no request line can carry.
        f"http://{APP}/a b",
        f"http://{APP}/caf\u00e9",
    ],
)
def test_a_runner_request_needs_an_absolute_http_url(url: str) -> None:
    egress, resolver = app_run(80)

    with pytest.raises(ValueError, match=r"not an origin|not a request target"):
        asyncio.run(runner_request(egress, "POST", url))

    assert resolver.lookups == Counter()
    assert egress.refusals == []
    assert egress.infrastructure_events == []
