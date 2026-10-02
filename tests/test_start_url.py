"""A run's start URL as Chromium reads it (ADR-0026's start URL amendment): on
the start origin, with a path that doesn't start with //, for the start_urls
named below and for every one the spec parser accepts of up to 4 characters
after the / from ALPHABET. The core's start_url, checked in the runner's
browser."""

import asyncio
import dataclasses
import itertools
import json
from pathlib import Path
from urllib.parse import unquote

from aqa_core.config import ProjectConfig
from aqa_core.project import load_spec, start_url
from aqa_core.spec import Preconditions, Spec
from aqa_runner.browser_session import open_browser_session
from playwright.async_api import async_playwright
from pydantic import ValidationError

# Start origins as start_origin gives them: a DNS name on the scheme's default
# port, an IPv4 address with a port and an IPv6 address with a port.
START_ORIGINS = [
    "https://shop.example.test",
    "http://127.0.0.1:4100",
    "http://[2001:db8::1]:8080",
]

# The start_urls ADR-0026's start URL amendment names.
NAMED_START_URLS = [
    "/",
    "/%2f%2fevil.test",
    "/..;/x",
    "/login?next=//evil.test",
    "/login#//evil.test",
]

# What URL parsing gives a meaning to: separators, dots, % with the hex digits
# of %2e, %2f and %5c in either case, a letter and a non-ASCII letter, and the
# backslash, the tab, LF and CR a URL parser drops, and a space, all of which
# the spec parser must keep refusing.
ALPHABET = "/.%2eEfF5cC;?#@:aé\\\t\n\r "

# The [start origin, URL] pairs, as JSON text, whose URL Chromium reads on
# another origin or with a path starting //, which a parser resolving that
# path again reads as a host; each with the origin and path read.
UNSAFE = """text => JSON.parse(text)
    .map(([start, url]) => [start, url, new URL(url)])
    .filter(([start, , read]) => read.origin !== start || read.pathname.startsWith("//"))
    .map(([start, url, read]) => [start, url, read.origin, read.pathname])"""

# What the check must flag, as it flags it: a start_url that lost its leading
# /, a path of exactly two slashes, and /%2f%2fevil.test decoded first.
SHOP = "https://shop.example.test"
CONTROLS = [
    [SHOP, f"{SHOP}@evil.test", "https://evil.test", "/"],
    [SHOP, f"{SHOP}//evil.test", SHOP, "//evil.test"],
    [SHOP, SHOP + unquote("/%2f%2fevil.test"), SHOP, "///evil.test"],
]

SPEC = """\
---
id: login
goal: A reader signs in.
preconditions: { start_url: /login }
expect: [The home page is shown]
---
"""


def with_start_url(spec: Spec, path: str) -> Spec | None:
    """`spec` with `path` as its start_url, or None when the spec parser
    refuses that start_url."""
    try:
        preconditions = Preconditions.model_validate({"start_url": path})
    except ValidationError:
        return None
    frontmatter = spec.frontmatter.model_copy(update={"preconditions": preconditions})
    return dataclasses.replace(spec, frontmatter=frontmatter)


def test_chromium_reads_every_start_url_on_the_start_origin(tmp_path: Path) -> None:
    (tmp_path / "login.spec.md").write_text(SPEC)
    template = load_spec(tmp_path / "login.spec.md", ProjectConfig())
    named = {path: with_start_url(template, path) for path in NAMED_START_URLS}
    assert [path for path, spec in named.items() if spec is None] == []
    swept = (
        with_start_url(template, "/" + "".join(chars))
        for length in range(5)
        for chars in itertools.product(ALPHABET, repeat=length)
    )
    specs = [spec for spec in (*named.values(), *swept) if spec is not None]
    pairs = [
        [start, start_url(spec, start)] for start in START_ORIGINS for spec in specs
    ]

    async def scenario() -> object:
        async with (
            async_playwright() as playwright,
            open_browser_session(playwright.chromium) as session,
        ):
            # JSON text, since Playwright serializes a list argument item by
            # item: about 6 s for these pairs, against 1 s.
            controls = [row[:2] for row in CONTROLS]
            return await session.page.evaluate(UNSAFE, json.dumps(pairs + controls))

    # Only the controls, which come last.
    assert asyncio.run(scenario()) == CONTROLS
