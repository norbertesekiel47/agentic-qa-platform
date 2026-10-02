"""What the spec and project-config models share: a strict model base, and the
origins, hosts and names they are written with (DATA_MODEL §6, §9; ADR-0026)."""

import ipaddress
import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr


class StrictModel(BaseModel):
    """Frozen, refuses unknown keys, and never coerces a value's type: a
    quoted "3" is not a count, and `true` is not 1."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _distinct[T](items: tuple[T, ...]) -> tuple[T, ...]:
    repeated = [item for i, item in enumerate(items) if item in items[:i]]
    if repeated:
        raise ValueError(f"lists {repeated[0]} twice")
    return items


def _at_least_one[T](items: tuple[T, ...]) -> tuple[T, ...]:
    if not items:
        raise ValueError("must list at least 1 item")
    return items


# A YAML list, kept as a tuple so a frozen model's lists can't change. Not
# strict on the outside, so the list YAML gives becomes a tuple; each item type
# stays strict.
type ListOf[T] = Annotated[tuple[T, ...], Field(strict=False)]
type DistinctListOf[T] = Annotated[ListOf[T], AfterValidator(_distinct)]
# After the items, so a list whose only item is invalid isn't also reported as
# empty, as Field(min_length=1) would.
AtLeastOne = AfterValidator(_at_least_one)

NonEmpty = Annotated[StrictStr, Field(min_length=1)]
PositiveNumber = Annotated[float, Field(gt=0, allow_inf_nan=False)]

DEFAULT_PORTS = {"http": 80, "https": 443}
# Characters no origin or host contains as written. WHATWG URL parsing reads
# a backslash as a slash and drops tabs and newlines, so either could turn a
# value that reads as one origin into another.
_NOT_IN_AN_ORIGIN = re.compile(r"[\s\x00-\x1f\x7f\\@?#]")
_LABEL = r"(?!-)[a-z0-9_-]{1,63}(?<!-)"
_DNS_NAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
# WHATWG's "ends in a number": a host whose last label is decimal or 0x-hex is
# an IPv4 address to a browser, which reads 0x7f000001 and 127.1 as 127.0.0.1.
_NUMBER = re.compile(r"[0-9]+|0x[0-9a-f]*")


def _host(host: str) -> str | None:
    """`host` (lowercase, IPv6 without brackets) as an origin writes it, or
    None if it is neither an IP address nor a DNS name."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Only dotted decimal, which ip_address takes, is accepted as IPv4.
        if _DNS_NAME.fullmatch(host) and not _NUMBER.fullmatch(host.rsplit(".", 1)[-1]):
            return host
        return None
    if isinstance(address, ipaddress.IPv6Address):
        # A zone index (fe80::1%eth0) names one host's interface, which no
        # browser accepts in a URL; and a browser writes an IPv4-mapped address
        # (::ffff:127.0.0.1) in a form Python doesn't (::ffff:7f00:1).
        if address.scope_id or address.ipv4_mapped is not None:
            return None
        return f"[{address}]"
    return str(address)


def _origin_parts(text: str) -> tuple[str, str, int]:
    """`text`'s scheme, host as an origin writes it, and port, the scheme's
    default when it writes none. Raises ValueError unless `text` is an origin
    as `parse_origin` reads one."""
    problem = ValueError(
        f"'{text}' is not an origin: write a scheme, a host and an optional port, "
        "such as https://shop.example.test"
    )
    if _NOT_IN_AN_ORIGIN.search(text):
        raise problem
    try:
        parts = urlsplit(text)
    except ValueError:  # brackets that don't close
        raise problem from None
    if (
        parts.scheme not in DEFAULT_PORTS
        or parts.path not in ("", "/")
        or not parts.hostname
    ):
        raise problem
    try:
        port = parts.port
    except ValueError:
        raise problem from None
    host = _host(parts.hostname)
    # A bracketed host must be IPv6: urlsplit drops the brackets of any other,
    # such as [v1.example.test], leaving a different host.
    if host is None or port == 0 or ("[" in parts.netloc) != host.startswith("["):
        raise problem
    return parts.scheme, host, DEFAULT_PORTS[parts.scheme] if port is None else port


def parse_origin(text: str) -> str:
    """`text` as an origin: http or https, a host and an optional port, with a
    lone trailing slash allowed. Returned lowercase, without the scheme's
    default port or the slash, so equal origins compare equal."""
    scheme, host, port = _origin_parts(text)
    if port == DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def authority(origin: str) -> tuple[str, int]:
    """The host and port `origin` names, read as `parse_origin` reads it: the
    host lowercase, an IPv6 address in brackets, and the port the scheme's
    default when the origin writes none. Raises ValueError for anything that
    isn't an origin."""
    _, host, port = _origin_parts(origin)
    return host, port


def _parse_host(text: str) -> str:
    bracketed = text.startswith("[") and text.endswith("]")
    host = None
    if not _NOT_IN_AN_ORIGIN.search(text) and (bracketed or ":" not in text):
        host = _host((text[1:-1] if bracketed else text).lower())
    # Only an IPv6 address is written in brackets, and it always is.
    if host is None or bracketed != host.startswith("["):
        raise ValueError(
            f"'{text}' is not a host: write a host name or IP address alone, "
            "such as fonts.example.test, with no scheme, port or wildcard"
        )
    return host


Origin = Annotated[StrictStr, AfterValidator(parse_origin)]
Host = Annotated[StrictStr, AfterValidator(_parse_host)]


def _secret_name(name: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise ValueError(
            f"'{name}' is not a secret name: use capital letters, digits and underscores, "
            "starting with a letter, so AQA_SECRET_<NAME> can be set"
        )
    return name


SecretName = Annotated[StrictStr, AfterValidator(_secret_name)]
