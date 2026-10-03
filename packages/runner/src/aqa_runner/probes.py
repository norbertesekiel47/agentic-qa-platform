"""Probes: the read-only GETs a spec declares on the start origin, read for a
compiled check (DATA_MODEL §6, §7; ADR-0024, Settling, and its #48
amendment).

Each read is a runner-side request (`aqa_runner.runner_requests`), so it
goes through the run's egress gate to allowed origins only, under the run's
DNS pins and IP policy, and carries no cookie: neither the browser's nor one
a probe's response set (ADR-0026, Same rules for every request).

The response comes from the app under test, so it is read strictly: status
200, and a body that is one JSON object or array. A body framed by the
connection's close, as a TLS body that loses its close_notify is, can be cut
short without the framing showing it; an object or array cut before its
closing bracket never parses, where a bare `12` cut to `1` would."""

import asyncio
import json
import math
from collections.abc import Sequence
from http import HTTPStatus

from aqa_core.compiled import json_path_steps
from aqa_core.spec import canonical_hash

from aqa_runner.egress import EgressGate
from aqa_runner.runner_requests import runner_request

# How long reading a probe until its value holds still may take, every
# request included, and how long apart its reads are (ADR-0024's #48
# amendment): settling's bound, and its quiet period.
STABLE_SECONDS = 10
READ_SECONDS = 0.5

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)


class ProbeError(Exception):
    """A probe that couldn't be read: it answered other than 200, its body
    isn't one JSON object or array, its JSON path selects nothing, or it
    didn't answer within `STABLE_SECONDS`. The message is ours, and holds
    nothing of the body."""


class ProbeUnstableError(Exception):
    """A probe whose value never held still within `STABLE_SECONDS`: no two
    reads in a row selected the same value. It establishes neither a pass
    nor a failure (DATA_MODEL §7, `check_timed_out`)."""


async def read_stable(gate: EgressGate, url: str, json_path: str) -> JsonValue:
    """The value at `json_path` (an `aqa_core.compiled.JsonPath`) of the
    probe at `url`, an absolute URL on an allowed origin, once two reads in
    a row, `READ_SECONDS` apart, select the same value (`same`).

    `STABLE_SECONDS` bound the whole read, every request inside it, so a
    request the probe never answers is cancelled and its connection closed.
    When the bound passes after at least two reads, the value never held
    still: `ProbeUnstableError`. When it passes before a second read, the
    probe didn't answer twice: `ProbeError`, as for any response the probe
    can't be read from. `runner_request`'s own errors pass through: a refused
    origin or address (`EgressRefusedError`) and an unreachable one
    (`EgressUpstreamError`), both recorded with the gate, and `ValueError`
    for a URL no request line carries, which a spec's probe never is
    (`aqa_core.schema.ProbeEndpoint`)."""
    reads = 0
    previous: JsonValue = None
    limit = asyncio.timeout(STABLE_SECONDS)
    try:
        async with limit:
            while True:
                value = await _read(gate, url, json_path)
                reads += 1
                if reads > 1 and same(previous, value):
                    return value
                previous = value
                await asyncio.sleep(READ_SECONDS)
    except TimeoutError:
        if not limit.expired():
            raise  # not the read's own bound
        if reads < 2:
            raise ProbeError(
                f"the probe didn't answer twice within {STABLE_SECONDS:g} s"
            ) from None
        raise ProbeUnstableError(
            f"the probe's value never held still within {STABLE_SECONDS:g} s: "
            f"{reads} reads, none the same as the one before"
        ) from None


async def _read(gate: EgressGate, url: str, json_path: str) -> JsonValue:
    """One read: the probe's response, its body parsed, `json_path`'s
    value."""
    response = await runner_request(gate, "GET", url)
    if response.status != HTTPStatus.OK:
        raise ProbeError(f"the probe answered {response.status}, not 200")
    return _select(_parsed(response.body), json_path)


def _parsed(body: bytes) -> JsonValue:
    """The body as one JSON object or array, or `ProbeError`."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProbeError("the probe's body isn't UTF-8") from None
    try:
        # https://docs.python.org/3.14/library/json.html#json.loads
        value: JsonValue = json.loads(
            text,
            object_pairs_hook=_unrepeated,
            parse_constant=_constant,
            parse_float=_finite,
        )
    except RecursionError:
        # The decoder's own limit, met by arrays nested a million deep.
        raise ProbeError("the probe's body nests too deep") from None
    except ValueError as error:
        # json.JSONDecodeError, whose message says where, never what; or an
        # integer past Python's limit on digits, whose message says neither.
        raise ProbeError(f"the probe's body isn't JSON: {error}") from None
    if not isinstance(value, dict | list):
        raise ProbeError(
            "the probe's body is one JSON object or array: a bare value cut "
            "short could still read as one"
        )
    return value


def _unrepeated(pairs: Sequence[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    """An object, unless it repeats a key, which a reader would keep only
    the last of."""
    found: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in found:
            raise ProbeError("the probe's body repeats a key")
        found[key] = value
    return found


def _constant(name: str) -> JsonValue:
    """`NaN`, `Infinity` or `-Infinity`, which JSON doesn't have: one of
    those three names, never the body's own text."""
    raise ProbeError(f"the probe's body has {name}, which isn't JSON")


def _finite(text: str) -> float:
    """A number with a fraction or an exponent, which must fit a float. An
    integer is exact in Python, up to its limit on digits, past which
    `json.loads` raises ValueError."""
    number = float(text)
    if not math.isfinite(number):
        raise ProbeError("the probe's body has a number no float holds")
    return number


def _select(body: JsonValue, json_path: str) -> JsonValue:
    """The value `json_path` selects in `body`: each step a key of an object
    or an index of an array."""
    value = body
    for step in json_path_steps(json_path):
        match value, step:
            case dict(), str() if step in value:
                value = value[step]
            case list(), int() if step < len(value):
                value = value[step]
            case _:
                raise ProbeError(f"{json_path} selects nothing in the probe's body")
    return value


def equals(selected: JsonValue, value: int | str) -> bool:
    """Whether a probe's selected value is a `probe_equals` check's `value`:
    the same JSON type and the same value, so neither `2.0` nor `true` is
    the integer 2, and a string compares exactly."""
    return type(selected) is type(value) and selected == value


def same(before: JsonValue, after: JsonValue) -> bool:
    """Whether a probe read the same value twice: the same canonical JSON
    (`aqa_core.spec.canonical_hash`), so `1` is neither `1.0` nor `true`."""
    return canonical_hash(before) == canonical_hash(after)
