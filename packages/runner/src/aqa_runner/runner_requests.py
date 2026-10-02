"""Runner-side requests: the requests the runner sends itself, the reset
hook's and probes', never the browser's (ADR-0026 and its 2026-10-01
amendment on the egress proxy; SECURITY.md §7). Each goes through the run's
egress gate, so it reaches allowed origins only, under the same DNS pins and
IP policy as the browser's traffic, and is recorded like it. It carries no
cookie: there is no cookie jar, so neither the browser's cookies nor one a
runner-side response set can be sent."""

import ssl
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import h11
from aqa_core.schema import authority, parse_origin

from aqa_runner.egress import EgressGate
from aqa_runner.egress_peers import Upstream

# A probe reads with GET, and the reset hook posts (DATA_MODEL §6).
type Method = Literal["GET", "POST"]


@dataclass(frozen=True)
class RunnerResponse:
    """A runner-side request's response: its final status and its body. A
    redirect comes back as it is, never followed."""

    status: int
    body: bytes


async def runner_request(gate: EgressGate, method: Method, url: str) -> RunnerResponse:
    """Send `method` to the absolute http or https `url` through `gate`, and
    read the response. Raises ValueError, before any connection, for a URL
    whose origin isn't one or whose path and query no request line carries;
    `EgressRefusedError` for an origin the run doesn't allow or an address
    the IP policy refuses; and `EgressUpstreamError`, an infrastructure
    error, when the origin can't be reached, its certificate doesn't verify,
    or its response breaks off. The gate records both.

    It has no deadline of its own past the gate's for connecting, its TLS
    handshake included: a caller bounds it with `asyncio.timeout`."""
    parts = urlsplit(url)
    origin = parse_origin(f"{parts.scheme}://{parts.netloc}")
    host, port = authority(origin)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    headers = [
        # The origin's authority, without its scheme's default port.
        ("host", origin.partition("://")[2]),
        ("connection", "close"),
    ]
    if method == "POST":
        headers.append(("content-length", "0"))
    try:
        # Built before connecting, so a target h11 refuses is never sent.
        request = h11.Request(method=method, target=target, headers=headers)
    except (h11.LocalProtocolError, UnicodeEncodeError) as error:
        raise ValueError(
            f"'{url}': '{target}' is not a request target: {error}"
        ) from error
    # https speaks TLS from the first byte, with the certificate checked for
    # the URL's host on the connection to its pinned address: nothing the
    # server sends before the handshake can pass for the response.
    tls = ssl.create_default_context() if parts.scheme == "https" else None
    reader, writer = await gate.connect(host, port, "runner", tls=tls)
    upstream = Upstream(h11.Connection(h11.CLIENT), reader, writer, gate, host, port)
    try:
        await upstream.send(request)
        await upstream.send(h11.EndOfMessage())
        return await _response(upstream)
    finally:
        writer.close()


async def _response(upstream: Upstream) -> RunnerResponse:
    """The final response, past any 1xx, with its body."""
    head = await upstream.next()
    while not isinstance(head, h11.Response):
        head = await upstream.next()
    body = bytearray()
    # After a response head, h11 gives Data until the EndOfMessage.
    while isinstance(event := await upstream.next(), h11.Data):
        body += event.data
    return RunnerResponse(head.status_code, bytes(body))
