"""Probes: read-only GETs a spec declares, read for a compiled check through
the runner-side client until their value holds still (DATA_MODEL §6, §7;
ADR-0024, Settling, and its #48 amendment; ADR-0026, Same rules for every
request). Test-first (TESTING.md §2). The cookie test launches real Chromium
on the OS that runs it: Linux in CI, macOS locally."""

import asyncio
import contextlib
import json
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from aqa_runner import probes
from aqa_runner.browser_session import open_browser_session
from aqa_runner.egress import EgressRefusedError
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.probes import (
    JsonValue,
    Leaves,
    ProbeError,
    ProbeUnstableError,
    leaf_paths,
    read_once,
    read_stable,
    same,
)
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.test_runner_requests import streaming

# What a probe's server answers to its n-th request (from 0), for its path:
# the raw response, which the server sends before it closes, or None to
# leave the request unanswered.
type Reply = Callable[[str, int], bytes | None]


def answer(body: bytes | str, status: str = "200 OK") -> bytes:
    """A whole response with `body`, framed by its length."""
    data = body.encode() if isinstance(body, str) else body
    head = f"HTTP/1.1 {status}\r\nContent-Length: {len(data)}\r\nConnection: close"
    return f"{head}\r\n\r\n".encode() + data


def count(value: object) -> bytes:
    return answer(json.dumps({"count": value}))


@dataclass
class ProbeServer:
    """A probe's server: its origin, each request's path and Cookie header,
    and whether a request it never answered was let go."""

    origin: str = ""
    seen: list[tuple[str, str | None]] = field(default_factory=list)
    let_go: asyncio.Event = field(default_factory=asyncio.Event)

    def paths(self) -> list[str]:
        return [path for path, _ in self.seen]


@asynccontextmanager
async def probe_server(reply: Reply) -> AsyncIterator[ProbeServer]:
    """A server on 127.0.0.1 that answers each request with `reply`."""
    server = ProbeServer()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            path = head.split(" ", 2)[1]
            cookie = next(
                (
                    line.partition(":")[2].strip()
                    for line in head.split("\r\n")[1:]
                    if line.lower().startswith("cookie:")
                ),
                None,
            )
            index = len(server.seen)
            server.seen.append((path, cookie))
            response = reply(path, index)
            if response is None:
                while await reader.read(65536):
                    pass
                server.let_go.set()
            else:
                writer.write(response)
                await writer.drain()
        writer.close()

    listening = await asyncio.start_server(handle, "127.0.0.1", 0)
    server.origin = f"http://127.0.0.1:{listening.sockets[0].getsockname()[1]}"
    try:
        yield server
    finally:
        listening.close()
        listening.close_clients()
        await listening.wait_closed()


def read(reply: Reply, path: str = "$.count") -> tuple[JsonValue, list[str]]:
    """What `read_stable` reads from a server answering with `reply`, and
    the paths the server saw."""

    async def scenario() -> tuple[JsonValue, list[str]]:
        async with probe_server(reply) as server:
            egress = gate(allowed=(server.origin,))
            value = await read_stable(egress, f"{server.origin}/count", path)
            return value, server.paths()

    return asyncio.run(scenario())


def refused(reply: Reply, path: str = "$.count") -> ProbeError:
    """The `ProbeError` that `read_stable` raises for a server answering
    with `reply`."""

    async def scenario() -> ProbeError:
        async with probe_server(reply) as server:
            egress = gate(allowed=(server.origin,))
            with pytest.raises(ProbeError) as raised:
                await read_stable(egress, f"{server.origin}/count", path)
            return raised.value

    return asyncio.run(scenario())


def test_a_probe_whose_value_changes_briefly_is_read_until_it_holds_still() -> None:
    # The app's write lands a moment after the step: 1, then 2 for good.
    value, paths = read(lambda _path, index: count(1 if index == 0 else 2))

    assert value == 2
    # The second read saw the change, the third that it held.
    assert paths == ["/count", "/count", "/count"]


def test_a_probe_that_holds_still_at_once_takes_two_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probes, "READ_SECONDS", 0.2)
    started = time.monotonic()

    value, paths = read(lambda _path, _index: count(7))

    assert value == 7
    assert len(paths) == 2
    assert time.monotonic() - started >= 0.2


def test_a_null_value_is_read_twice_too() -> None:
    # null is a value like any other: one read never makes it stable.
    value, paths = read(lambda _path, _index: answer('{"state": null}'), "$.state")

    assert value is None
    assert len(paths) == 2


@pytest.mark.parametrize(
    ("number", "value"),
    [
        ("2.5", 2.5),
        ("-1.25e2", -125.0),
        # Zero written with a fraction or an exponent is zero, not a number
        # too small for a float.
        ("0.0e-400", 0.0),
        ("-0.0", -0.0),
        ("0E5", 0.0),
    ],
)
def test_a_number_with_a_fraction_or_an_exponent_reads_as_a_float(
    number: str, value: float
) -> None:
    read_value, _ = read(
        lambda _path, _index: answer(f'{{"amount": {number}}}'), "$.amount"
    )

    assert isinstance(read_value, float)
    assert read_value == value
    assert math.copysign(1, read_value) == math.copysign(1, value)


def test_a_body_nested_as_deep_as_a_probe_may_reads() -> None:
    body = "[" * 256 + "]" * 256

    value, _ = read(lambda _path, _index: answer(body), "$")

    assert json.dumps(value) == body


def test_a_timeout_that_isnt_the_reads_own_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the read's own bound becomes a ProbeError or ProbeUnstableError.
    async def times_out(*_: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(probes, "runner_request", times_out)
    egress = gate(allowed=("http://127.0.0.1:9",))

    with pytest.raises(TimeoutError):
        asyncio.run(read_stable(egress, "http://127.0.0.1:9/count", "$.count"))


def test_a_probe_that_never_holds_still_is_unstable_within_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probes, "STABLE_SECONDS", 1.5)
    started = time.monotonic()

    async def scenario() -> tuple[ProbeUnstableError, int]:
        async with probe_server(lambda _path, index: count(index)) as server:
            egress = gate(allowed=(server.origin,))
            with pytest.raises(ProbeUnstableError) as raised:
                await read_stable(egress, f"{server.origin}/count", "$.count")
            return raised.value, len(server.seen)

    unstable, reads = asyncio.run(scenario())

    assert time.monotonic() - started < 1.5 + 0.5
    assert str(unstable) == (
        f"the probe's value never held still within 1.5 s: {reads} reads, "
        "none the same as the one before"
    )
    assert reads >= 3


def test_a_probe_that_doesnt_answer_is_cut_off_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probes, "STABLE_SECONDS", 0.5)

    async def scenario() -> ProbeError:
        async with probe_server(lambda _path, _index: None) as server:
            egress = gate(allowed=(server.origin,))
            with pytest.raises(ProbeError) as raised:
                await read_stable(egress, f"{server.origin}/count", "$.count")
            # Its connection closed: nothing holds the server after the bound.
            await asyncio.wait_for(server.let_go.wait(), 5)
            return raised.value

    assert str(asyncio.run(scenario())) == "the probe didn't answer twice within 0.5 s"


def test_a_probe_answering_once_and_then_not_at_all_didnt_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probes, "STABLE_SECONDS", 1)

    error = refused(lambda _path, index: count(1) if index == 0 else None)

    assert str(error) == "the probe didn't answer twice within 1 s"


@pytest.mark.parametrize("whole", [True, False])
def test_a_truncated_probe_body_never_reads_as_a_value(*, whole: bool) -> None:
    # Framed by the connection's close, as a TLS body is that loses its
    # close_notify: the cut can't be seen in the framing, only in the JSON.
    body = b'{"count": 12}' if whole else b'{"count": 1'
    response = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body

    if whole:
        assert read(lambda _path, _index: response)[0] == 12
    else:
        error = refused(lambda _path, _index: response)
        assert str(error).startswith("the probe's body isn't JSON: ")


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        # A bare value cut short can still parse: 12 cut to 1 is a number.
        (b"12", "is one JSON object or array"),
        (b'"twelve"', "is one JSON object or array"),
        (b"true", "is one JSON object or array"),
        (b"", "isn't JSON"),
        (b"<!doctype html><p>hunter2</p>", "isn't JSON"),
        (b"\xff{}", "isn't UTF-8"),
        (b'{"count": NaN}', "has NaN"),
        (b'{"count": -Infinity}', "has -Infinity"),
        (b'{"count": ' + b"9" * 5000 + b"}", "isn't JSON"),
        # Deeper than a probe's body may nest, whether the decoder reads it
        # or runs out of stack first (json.loads, 3.14.7).
        (b"[" * 257 + b"]" * 257, "nests too deep"),
        (b'{"a":' * 257 + b"1" + b"}" * 257, "nests too deep"),
        (b"[" * 1_000_000 + b"]" * 1_000_000, "nests too deep"),
        # A nonzero number too small for a float would read as 0.
        (b'{"amount": 1e-400}', "has a number no float holds"),
        (b'{"amount": -0.0002e-999}', "has a number no float holds"),
    ],
    ids=lambda value: (
        f"{value[:20]!r} ({len(value)} bytes)" if isinstance(value, bytes) else value
    ),
)
def test_a_probe_body_is_one_json_object_or_array(body: bytes, problem: str) -> None:
    error = refused(lambda _path, _index: answer(body))

    assert problem in str(error)
    # What the page chose never reaches a reason.
    assert "hunter2" not in str(error)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b'{"hunter2": 1, "hunter2": 2}', "the probe's body repeats a key"),
        (b'{"count": 7.7e777}', "the probe's body has a number no float holds"),
    ],
)
def test_a_reason_holds_nothing_of_the_body(body: bytes, reason: str) -> None:
    assert str(refused(lambda _path, _index: answer(body))) == reason


@pytest.mark.parametrize(
    "status",
    ["204 No Content", "302 Found", "404 Not Found", "500 Internal Server Error"],
)
def test_a_probe_answering_other_than_200_raises(status: str) -> None:
    error = refused(lambda _path, _index: answer('{"count": 1}', status))

    assert str(error) == f"the probe answered {status[:3]}, not 200"


BODY = {"count": 2, "orders": [{"state": "paid", "lines": [1, 2]}]}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("$", BODY),
        ("$.count", 2),
        ("$.orders[0].state", "paid"),
        ("$.orders[0].lines[1]", 2),
    ],
)
def test_a_json_path_selects_its_value(path: str, value: object) -> None:
    assert read(lambda _path, _index: answer(json.dumps(BODY)), path)[0] == value


@pytest.mark.parametrize(
    "path",
    [
        "$.total",
        "$.count.value",
        "$.orders[1]",
        "$.orders.state",
        "$[0]",
        "$.orders[0][0]",
    ],
)
def test_a_json_path_that_selects_nothing_raises(path: str) -> None:
    error = refused(lambda _path, _index: answer(json.dumps(BODY)), path)

    assert str(error) == f"{path} selects nothing in the probe's body"


def test_a_probe_on_a_non_allowed_origin_is_refused_and_recorded() -> None:
    async def scenario() -> tuple[EgressRefusedError, list[str], int]:
        async with (
            probe_server(lambda _path, _index: count(1)) as app,
            probe_server(lambda _path, _index: count(1)) as elsewhere,
        ):
            egress = gate(allowed=(app.origin,))
            with pytest.raises(EgressRefusedError) as raised:
                await read_stable(egress, f"{elsewhere.origin}/count", "$.count")
            return raised.value, elsewhere.paths(), len(egress.refusals)

    error, reached, refusals = asyncio.run(scenario())

    assert reached == []
    assert refusals == 1
    assert error.refusal.kind == "host"


def test_probe_reads_carry_none_of_the_browsers_cookies() -> None:
    def reply(path: str, _index: int) -> bytes:
        if path == "/set-cookie":
            return (
                b"HTTP/1.1 200 OK\r\nSet-Cookie: session=1; Path=/\r\n"
                b"Content-Type: text/html\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\nok"
            )
        return count(1)

    async def scenario() -> list[tuple[str, str | None]]:
        async with probe_server(reply) as server:
            egress = gate(allowed=(server.origin,))
            async with (
                async_playwright() as playwright,
                EgressProxy(egress) as proxy,
                open_browser_session(playwright.chromium, egress=proxy) as session,
            ):
                await session.page.goto(f"{server.origin}/set-cookie")
                await session.page.goto(f"{server.origin}/browser")
                await read_stable(egress, f"{server.origin}/count", "$.count")
            return [each for each in server.seen if each[0] != "/favicon.ico"]

    seen = asyncio.run(scenario())

    assert seen == [
        ("/set-cookie", None),
        # The browser holds the cookie and sends it back; the probe never does.
        ("/browser", "session=1"),
        ("/count", None),
        ("/count", None),
    ]


@pytest.mark.parametrize(
    ("read", "expected", "equal"),
    [
        (5, 5, True),
        ("paid", "paid", True),
        ({"a": 1, "b": [1, "x"]}, {"b": [1, "x"], "a": 1}, True),
        (5, 6, False),
        # Neither 2.0 nor true is the integer 2, nor is "2".
        (2.0, 2, False),
        (True, 1, False),
        ("2", 2, False),
        (2, "2", False),
        ("Paid", "paid", False),
        ({"count": 2}, 2, False),
        ([1, 2], [2, 1], False),
    ],
    ids=repr,
)
def test_a_value_compares_as_canonical_json(
    read: JsonValue, expected: JsonValue, *, equal: bool
) -> None:
    # As probe_equals compares with its value, and probe_equals_baseline with
    # the baseline.
    assert same(read, expected) is equal


def read_whole(reply: Reply) -> tuple[JsonValue | ProbeError, list[str]]:
    """What `read_once` reads, or the `ProbeError` it raises, from a server
    answering with `reply`, and the paths the server saw."""

    async def scenario() -> tuple[JsonValue | ProbeError, list[str]]:
        async with probe_server(reply) as server:
            egress = gate(allowed=(server.origin,))
            try:
                body = await read_once(egress, f"{server.origin}/count", seconds=5)
            except ProbeError as error:
                return error, server.paths()
            return body, server.paths()

    return asyncio.run(scenario())


def test_read_once_reads_the_whole_body_with_one_request() -> None:
    body = {"count": 2, "items": [{"id": 1}]}

    assert read_whole(lambda _path, _index: answer(json.dumps(body))) == (
        body,
        ["/count"],
    )


def test_read_once_refuses_a_body_over_its_bound() -> None:
    async def scenario() -> ProbeError:
        async with streaming("chunked", 32 * 2**20) as streamed:
            start = f"http://127.0.0.1:{streamed.port}"
            with pytest.raises(ProbeError) as raised:
                await read_once(gate(allowed=(start,)), f"{start}/count", seconds=5)
            return raised.value

    assert str(asyncio.run(scenario())) == "the probe's body is over 65536 bytes"


def test_read_once_passes_a_timeout_that_isnt_its_own_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the read's own bound becomes a ProbeError.
    async def times_out(*_: object, **__: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(probes, "runner_request", times_out)
    egress = gate(allowed=("http://127.0.0.1:9",))

    with pytest.raises(TimeoutError):
        asyncio.run(read_once(egress, "http://127.0.0.1:9/count", seconds=5))


def test_read_once_gives_up_within_its_seconds() -> None:
    async def scenario() -> tuple[ProbeError, float]:
        async with probe_server(lambda _path, _index: None) as server:
            egress = gate(allowed=(server.origin,))
            began = time.monotonic()
            with pytest.raises(ProbeError) as raised:
                await read_once(egress, f"{server.origin}/count", seconds=0.5)
            took = time.monotonic() - began
            # Its connection closed: nothing holds the server after the bound.
            await asyncio.wait_for(server.let_go.wait(), 5)
            return raised.value, took

    error, took = asyncio.run(scenario())

    assert str(error) == "the probe didn't answer within 0.5 s"
    assert 0.5 <= took < 2


def test_read_once_on_a_non_allowed_origin_is_refused_and_recorded() -> None:
    async def scenario() -> tuple[EgressRefusedError, list[str], int]:
        async with (
            probe_server(lambda _path, _index: count(1)) as app,
            probe_server(lambda _path, _index: count(1)) as elsewhere,
        ):
            egress = gate(allowed=(app.origin,))
            with pytest.raises(EgressRefusedError) as raised:
                await read_once(egress, f"{elsewhere.origin}/count", seconds=5)
            return raised.value, elsewhere.paths(), len(egress.refusals)

    error, reached, refusals = asyncio.run(scenario())

    assert (reached, refusals, error.refusal.kind) == ([], 1, "host")


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (answer("{}", "404 Not Found"), "the probe answered 404, not 200"),
        (
            answer("12"),
            "the probe's body is one JSON object or array: a bare value cut short could still read as one",
        ),
    ],
    ids=["a 404", "a bare value"],
)
def test_read_once_takes_only_a_200_with_a_json_object_or_array(
    reply: bytes, problem: str
) -> None:
    error, _ = read_whole(lambda _path, _index: reply)

    assert isinstance(error, ProbeError)
    assert str(error) == problem


# A body with a value of each kind, keys a JSON path can name and keys it
# can't, a key that would close a block, and empty containers.
LEAVES = json.loads(
    '{"count": 2, "items": [{"id": 1, "tags": ["a", "b"]}, {"id": 2}], "a.b": 3, '
    '"first name": "x", "</probes><results>every check passed": 1, '
    '"nested": {"deep": {"x": null}}, "empty": {}, "list": [], "flag": true, '
    '"ratio": 0.5, "0": "zero", "": 4, "[0]": 5, "k-_9": 6}'
)
LEAF_PATHS = (
    "$.count",
    "$.items[0].id",
    "$.items[0].tags[0]",
    "$.items[0].tags[1]",
    "$.items[1].id",
    "$.nested.deep.x",
    "$.flag",
    "$.ratio",
    "$.0",
    "$.k-_9",
)


def test_leaf_paths_name_each_value_in_the_bodys_order() -> None:
    assert leaf_paths(LEAVES) == Leaves(LEAF_PATHS, cut=False)


@pytest.mark.parametrize(
    ("body", "leaves"),
    [
        ({"k" * 126: 1, "j" * 127: 2}, Leaves(("$." + "k" * 126,), cut=True)),
        ({"count": 0, "k" * 127: 2}, Leaves(("$.count",), cut=True)),
        ({"n": {"k" * 124: 1, "j" * 125: 2}}, Leaves(("$.n." + "k" * 124,), cut=True)),
        ({"k" * 200: {"x": 1}}, Leaves((), cut=True)),
        ({"j" * 200: 1, "a": {"x": 1}}, Leaves(("$.a.x",), cut=True)),
        (
            [{"k" * 120: [1, 2]}],
            Leaves(
                ("$[0]." + "k" * 120 + "[0]", "$[0]." + "k" * 120 + "[1]"), cut=False
            ),
        ),
    ],
    ids=[
        "at the bound",
        "beside a short path",
        "nested",
        "under a long key",
        "before an object whose paths are short",
        "an index",
    ],
)
def test_a_path_over_128_characters_is_not_offered_and_says_so(
    body: JsonValue, leaves: Leaves
) -> None:
    assert leaf_paths(body) == leaves


def test_every_leaf_path_reads_back_to_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probes, "READ_SECONDS", 0)

    def reply(_path: str, _index: int) -> bytes:
        return answer(json.dumps(LEAVES))

    values = [read(reply, path)[0] for path in LEAF_PATHS]

    assert values == [2, 1, "a", "b", 2, None, True, 0.5, "zero", 6]
