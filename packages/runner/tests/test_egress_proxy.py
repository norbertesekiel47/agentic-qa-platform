"""The egress proxy over raw sockets: each request and tunnel asks the run's
egress gate, refusals and failures never look like the app's response, and
upstream failures are infrastructure events (ADR-0026 and its 2026-10-01
amendment on the egress proxy; SECURITY.md §7). Test-first (TESTING.md §2)."""

import asyncio
import socket
import struct
from typing import Any

import pytest
from aqa_runner.egress import Connection, EgressGate
from aqa_runner.egress_proxy import EgressProxy

from packages.runner.tests.egress_fixtures import (
    Seen,
    connect,
    exchange,
    gate,
    get,
    header_names,
    proxy_client,
    raw_upstream,
    reset,
    serving,
    unused_port,
)


def test_a_plain_request_to_an_allowed_origin_is_forwarded() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(proxy, get(f"{start}/page", close=True))

        response = asyncio.run(scenario())

    assert response.startswith(b"HTTP/1.1 200 ")
    assert f"127.0.0.1:{origin.port}/page".encode() in response
    # Forwarded in origin-form, with the target's own Host header.
    assert origin.seen == [Seen(f"127.0.0.1:{origin.port}", "/page")]


def test_a_refused_plain_request_gets_no_response_at_all() -> None:
    # A synthesized 403 would reach the page as the app's own response.
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(
                    proxy, get(f"http://evil.example.test:{origin.port}/", close=True)
                )

        response = asyncio.run(scenario())

    assert response == b""
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("evil.example.test", "host")
    ]
    assert origin.seen == []


def test_a_request_body_is_forwarded() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(
                    proxy,
                    f"POST {start}/form HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    "Content-Length: 9\r\nConnection: close\r\n\r\nname=page".encode(),
                )

        response = asyncio.run(scenario())

    assert response.startswith(b"HTTP/1.1 204 ")
    assert [(each.path, each.body) for each in origin.seen] == [("/form", b"name=page")]


def test_a_browser_connection_carries_one_request() -> None:
    # A failure on a reused connection makes Chromium send the request again;
    # one request per connection means none is ever reused.
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(
                    proxy,
                    get(f"{start}/first")
                    + get(f"http://evil.example.test:{origin.port}/second"),
                )

        response = asyncio.run(scenario())

    head = response.partition(b"\r\n\r\n")[0].lower()
    assert head.startswith(b"http/1.1 200 ")
    assert b"connection: close" in head
    assert response.count(b"HTTP/1.1 ") == 1
    # The second was never read, so never judged.
    assert [each.path for each in origin.seen] == ["/first"]
    assert egress.refusals == []


def test_an_unreachable_upstream_drops_a_plain_request() -> None:
    port = unused_port()
    start = f"http://127.0.0.1:{port}"
    egress = gate(allowed=(start,))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, get(f"{start}/", close=True))

    assert asyncio.run(scenario()) == b""
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", port)
    ]
    assert egress.refusals == []


def test_a_response_cut_short_upstream_is_cut_short_downstream() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(proxy, get(f"{start}/drop", close=True))

        response = asyncio.run(scenario())

    # Whatever arrived is passed on, and the connection drops: the page sees a
    # failed load, never a response the proxy completed.
    assert len(response.partition(b"\r\n\r\n")[2]) < 100
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", origin.port)
    ]


def test_a_tunnel_to_an_allowed_origin_carries_its_bytes() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                inner = "GET /inside HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                return await exchange(
                    proxy, connect(f"127.0.0.1:{origin.port}") + inner.encode()
                )

        response = asyncio.run(scenario())

    established, _, inside = response.partition(b"\r\n\r\n")
    assert established.startswith(b"HTTP/1.1 200 ")
    assert inside.startswith(b"HTTP/1.0 200 ")
    assert [each.path for each in origin.seen] == ["/inside"]


def test_a_refused_tunnel_fails_with_no_bytes_through() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> bytes:
            async with EgressProxy(egress) as proxy:
                return await exchange(
                    proxy, connect(f"evil.example.test:{origin.port}")
                )

        response = asyncio.run(scenario())

    # A non-2xx answer to CONNECT fails the tunnel; Chromium never shows a
    # page a proxy's CONNECT response.
    assert response.startswith(b"HTTP/1.1 403 ")
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("evil.example.test", "host")
    ]
    assert origin.seen == []


def test_an_unreachable_upstream_fails_the_tunnel() -> None:
    port = unused_port()
    start = f"http://127.0.0.1:{port}"
    egress = gate(allowed=(start,))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, connect(f"127.0.0.1:{port}"))

    assert asyncio.run(scenario()).startswith(b"HTTP/1.1 502 ")
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", port)
    ]


def test_a_browser_that_sends_nothing_is_let_go() -> None:
    egress = gate(allowed=("http://127.0.0.1:9",))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            reader, writer = await proxy_client(proxy)
            writer.write_eof()  # connects, and closes its side unused
            try:
                return await asyncio.wait_for(reader.read(), 5)
            finally:
                writer.close()

    assert asyncio.run(scenario()) == b""
    assert (egress.refusals, egress.infrastructure_events) == ([], [])


def test_the_proxy_serves_only_while_open() -> None:
    with pytest.raises(RuntimeError, match="async with"):
        _ = EgressProxy(gate(allowed=("http://127.0.0.1:9",))).url


def test_the_proxy_listens_on_loopback_only() -> None:
    async def scenario() -> str:
        async with EgressProxy(gate(allowed=("http://127.0.0.1:9",))) as proxy:
            return proxy.url

    assert asyncio.run(scenario()).startswith("http://127.0.0.1:")


@pytest.mark.parametrize(
    "request_line",
    [
        b"GET /relative HTTP/1.1",
        b"GET https://a.example.test/ HTTP/1.1",
        b"GET http://a.example.test:99999/ HTTP/1.1",
        b"GET http:///no-host HTTP/1.1",
        b"GET http://user@127.0.0.1:9/ HTTP/1.1",
        b"GET http://[::1/ HTTP/1.1",
        b"GET http://[v1.app.example.test]/ HTTP/1.1",
        b"CONNECT nonsense HTTP/1.1",
        b"CONNECT 127.0.0.1:0x9 HTTP/1.1",
    ],
)
def test_a_request_the_proxy_cannot_read_is_dropped(request_line: bytes) -> None:
    egress = gate(allowed=("http://127.0.0.1:9",))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, request_line + b"\r\nHost: x\r\n\r\n")

    assert asyncio.run(scenario()) == b""
    # Dropped before the gate: nothing was asked for, refused or dialled.
    assert (egress.refusals, egress.infrastructure_events) == ([], [])


def test_headers_a_connection_header_names_stay_behind() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> bytes:
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(proxy, get(f"{start}/hop", close=True))

        names = header_names(asyncio.run(scenario()))

    # RFC 9110 §7.6.1: a header the Connection header names is hop-by-hop.
    assert b"x-kept" in names
    assert b"x-hop" not in names


def test_only_end_to_end_headers_go_upstream_with_the_targets_host() -> None:
    async def scenario() -> tuple[bytes, str]:
        async with raw_upstream(b"HTTP/1.1 204 No Content\r\n\r\n") as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                await exchange(
                    proxy,
                    # A path-less URL, and a Host naming another server.
                    f"GET {start} HTTP/1.1\r\nHost: evil.example.test\r\n"
                    "Cookie: session=1\r\nAuthorization: Basic eA==\r\n"
                    "Keep-Alive: timeout=5\r\nProxy-Authorization: Basic eQ==\r\n"
                    "Proxy-Connection: keep-alive\r\nTE: trailers\r\nUpgrade: h2c\r\n"
                    "Connection: close\r\n\r\n".encode(),
                )
            return bytes(upstream.received), start

    received, start = asyncio.run(scenario())

    request_line, _, _ = received.partition(b"\r\n")
    assert request_line == b"GET / HTTP/1.1"
    assert f"host: {start.removeprefix('http://')}".encode() in received.lower()
    assert header_names(received) == {
        b"host",
        b"cookie",
        b"authorization",
        b"connection",
    }
    assert b"connection: close" in received.lower()


def test_a_browser_that_goes_away_mid_response_is_no_upstream_failure() -> None:
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"
        egress = gate(allowed=(start,))

        async def scenario() -> None:
            async with EgressProxy(egress) as proxy:
                port = int(proxy.url.rsplit(":", 1)[1])
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(get(f"{start}/slow", close=True))
                await reader.readuntil(b"\r\n\r\n")
                # Reset, as a closed tab or a cancelled navigation would.
                raw = writer.get_extra_info("socket")
                raw.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                writer.close()
                await asyncio.sleep(1)  # the rest arrives while nobody reads it

        asyncio.run(scenario())

    assert egress.infrastructure_events == []


@pytest.mark.parametrize(
    ("reply", "body"),
    [
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
            b"hello",
        ),
        (b"HTTP/1.0 200 OK\r\n\r\nhello, until the close", b"hello, until the close"),
        (
            (
                b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
            ),
            b"hello",
        ),
        (
            (
                b"HTTP/1.1 200 OK\r\nConnection: transfer-encoding\r\nContent-Length: 2\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
            ),
            b"hello",
        ),
    ],
    ids=["chunked", "close-delimited", "informational first", "framing named"],
)
def test_a_response_is_framed_for_the_browser(reply: bytes, body: bytes) -> None:
    async def scenario() -> bytes:
        async with raw_upstream(reply) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                return await exchange(proxy, get(f"{start}/", close=True))

    response = asyncio.run(scenario())

    assert b"HTTP/1.1 200 " in response
    assert body in response


def test_a_request_is_framed_one_way_upstream() -> None:
    # Both Content-Length and Transfer-Encoding is the shape of request
    # smuggling: only Transfer-Encoding goes on (RFC 9112 §6.3).
    async def scenario() -> bytes:
        async with raw_upstream(b"HTTP/1.1 204 No Content\r\n\r\n") as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            async with EgressProxy(gate(allowed=(start,))) as proxy:
                await exchange(
                    proxy,
                    f"POST {start}/ HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n"
                    "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                    "4\r\nwiki\r\n0\r\n\r\n".encode(),
                )
            return bytes(upstream.received)

    head = asyncio.run(scenario()).lower()

    assert b"transfer-encoding: chunked" in head
    assert b"content-length" not in head


@pytest.mark.parametrize(
    ("request_bytes", "refused_host"),
    [
        (b"GET http://127.0.0.1:0/ HTTP/1.1", "127.0.0.1"),
        (b"GET http://evil.example.test./ HTTP/1.1", "evil.example.test."),
        (b"GET http://[::ffff:7f00:1]:9/ HTTP/1.1", "[::ffff:7f00:1]"),
        (b"CONNECT evil.example.test.:443 HTTP/1.1", "evil.example.test."),
    ],
)
def test_a_host_not_written_as_an_origin_is_refused_and_recorded(
    request_bytes: bytes, refused_host: str
) -> None:
    # Chromium sends these; the block must reach the run's record (#47).
    egress = gate(allowed=("http://127.0.0.1:9",))

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, request_bytes + b"\r\nHost: x\r\n\r\n")

    response = asyncio.run(scenario())

    assert response in {b"", b"HTTP/1.1 403 \r\ncontent-length: 0\r\n\r\n"}
    assert [(r.host, r.kind) for r in egress.refusals] == [(refused_host, "host")]


def test_an_upstream_reset_is_an_infrastructure_event() -> None:
    async def scenario() -> tuple[bytes, EgressGate, int]:
        async with raw_upstream(resets=True) as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            egress = gate(allowed=(start,))
            async with EgressProxy(egress) as proxy:
                response = await exchange(proxy, get(f"{start}/", close=True))
            return response, egress, upstream.port

    response, egress, port = asyncio.run(scenario())

    assert response == b""
    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", port)
    ]


def test_a_browser_body_cut_short_is_no_upstream_failure() -> None:
    async def scenario() -> tuple[bool, EgressGate]:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            egress = gate(allowed=(start,))
            async with EgressProxy(egress) as proxy:
                _, writer = await proxy_client(proxy)
                writer.write(
                    f"POST {start}/ HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nab".encode()
                )
                await writer.drain()
                writer.close()  # eight bytes short
                try:
                    await asyncio.wait_for(upstream.closed.wait(), 5)
                except TimeoutError:
                    return False, egress
                return True, egress

    closed, egress = asyncio.run(scenario())

    assert closed
    assert egress.infrastructure_events == []


@pytest.mark.parametrize(
    ("target", "kind"),
    [("cdn.example.test:443", "address"), ("cdn.example.test:80", "host")],
)
def test_a_tunnel_reaches_a_subresource_host_on_443_only(
    target: str, kind: str
) -> None:
    # On 443 the allowlist passes it, and the IP policy refuses its answer;
    # on 80, which a plain request would use, the allowlist refuses it.
    egress = gate(
        allowed=("http://127.0.0.1:9",),
        subresource=("cdn.example.test",),
        answers={"cdn.example.test": ["169.254.169.254"]},
    )

    async def scenario() -> bytes:
        async with EgressProxy(egress) as proxy:
            return await exchange(proxy, connect(target))

    assert asyncio.run(scenario()).startswith(b"HTTP/1.1 403 ")
    assert [(r.host, r.kind) for r in egress.refusals] == [("cdn.example.test", kind)]


def test_closing_the_proxy_never_waits_on_a_browser() -> None:
    # Whatever step a new connection's handler has reached when the proxy
    # closes, closing returns, while the browser still holds its socket.
    async def scenario() -> None:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            for steps in range(8):
                proxy = await EgressProxy(gate(allowed=(start,))).__aenter__()
                _, writer = await proxy_client(proxy)
                writer.write(get(f"{start}/charge", close=True))
                for _ in range(steps):
                    await asyncio.sleep(0)
                await asyncio.wait_for(proxy.__aexit__(None, None, None), 2)
                writer.close()

    asyncio.run(scenario())


def test_closing_the_proxy_ends_a_request_still_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A request held at connect must never reach the app once the run's proxy
    # has closed: it could be a side effect.
    with serving() as origin:
        start = f"http://127.0.0.1:{origin.port}"

        async def scenario() -> None:
            proxy = await EgressProxy(gate(allowed=(start,))).__aenter__()
            reader, writer = await proxy_client(proxy)
            connecting, release = asyncio.Event(), asyncio.Event()
            open_connection = asyncio.open_connection

            async def held(host: str, port: int, **tls: Any) -> Connection:
                connecting.set()
                await release.wait()
                return await open_connection(host, port, **tls)

            monkeypatch.setattr(asyncio, "open_connection", held)
            writer.write(
                f"POST {start}/charge HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n"
                "Connection: close\r\n\r\nok".encode()
            )
            await asyncio.wait_for(connecting.wait(), 5)
            # Closing ends the held request at once, not when connect times out.
            await asyncio.wait_for(proxy.__aexit__(None, None, None), 2)
            release.set()
            await asyncio.sleep(0.5)
            writer.close()
            assert await reader.read() == b""

        asyncio.run(scenario())

    assert origin.seen == []


def test_a_browser_that_aborts_lets_a_silent_upstream_go() -> None:
    async def scenario() -> tuple[bool, EgressGate]:
        async with raw_upstream() as upstream:
            start = f"http://127.0.0.1:{upstream.port}"
            egress = gate(allowed=(start,))
            async with EgressProxy(egress) as proxy:
                _, writer = await proxy_client(proxy)
                writer.write(get(f"{start}/long-poll", close=True))
                await writer.drain()
                await asyncio.sleep(0.2)
                writer.write(b"bytes the proxy ignores")
                await asyncio.sleep(0.2)
                writer.close()  # the page aborts the request
                try:
                    await asyncio.wait_for(upstream.closed.wait(), 5)
                except TimeoutError:
                    return False, egress
                return True, egress

    released, egress = asyncio.run(scenario())

    assert released
    assert egress.infrastructure_events == []


def test_an_upstream_reset_inside_a_tunnel_is_an_infrastructure_event() -> None:
    async def scenario() -> EgressGate:
        async with raw_upstream(resets=True) as upstream:
            target = f"127.0.0.1:{upstream.port}"
            egress = gate(allowed=(f"http://{target}",))
            async with EgressProxy(egress) as proxy:
                await exchange(proxy, connect(target) + b"bytes for the server")
            return egress

    egress = asyncio.run(scenario())

    assert [(e.host, e.port) for e in egress.infrastructure_events] == [
        ("127.0.0.1", int(egress.policy.allowed_origins[0].rsplit(":", 1)[1]))
    ]
    assert egress.refusals == []


def test_a_browser_reset_inside_a_tunnel_is_no_upstream_failure() -> None:
    async def scenario() -> tuple[bool, EgressGate]:
        async with raw_upstream() as upstream:
            target = f"127.0.0.1:{upstream.port}"
            egress = gate(allowed=(f"http://{target}",))
            async with EgressProxy(egress) as proxy:
                reader, writer = await proxy_client(proxy)
                writer.write(connect(target) + b"GET / HTTP/1.1\r\n\r\n")
                await reader.readuntil(b"\r\n\r\n")
                reset(writer)
                try:
                    await asyncio.wait_for(upstream.closed.wait(), 5)
                except TimeoutError:
                    return False, egress
                return True, egress

    closed, egress = asyncio.run(scenario())

    assert closed
    assert egress.infrastructure_events == []
