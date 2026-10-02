"""A run's start URL as Chromium reads it (ADR-0026's start URL amendment): its
origin is the start origin and its path doesn't start with //, for the
start_urls named below and for every one the spec's frontmatter accepts of up
to 4 characters after the / from ALPHABET. The core's start_url, checked in the
runner's browser."""

import asyncio
import itertools
import json
from pathlib import Path
from urllib.parse import unquote

from aqa_core.project import start_url
from aqa_core.spec import Spec, SpecContext, SpecFrontmatter, spec_hash
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

# Accepted start_urls that stay on the start origin only when joined as text.
NAMED_START_URLS = [
    "/",
    "/%2f%2fevil.test",
    "/..;/x",
    "/login?next=//evil.test",
    "/login#//evil.test",
]

# What URL parsing gives a meaning to: separators, dots, % with the hex digits
# of %2e, %2f and %5c in either case, a letter and a non-ASCII letter.
ALPHABET = "/.%2eEfF5cC;?#@:aé"

# The [start origin, start URL] pairs, as one JSON text, whose URL Chromium
# reads on another origin or with a path starting //, which a parser resolving
# that path again reads as a host; each with the origin and path read.
OFF_ORIGIN = """text => JSON.parse(text)
    .map(([start, url]) => [start, url, new URL(url)])
    .filter(([start, , read]) => read.origin !== start || read.pathname.startsWith("//"))
    .map(([start, url, read]) => [start, url, read.origin, read.pathname])"""


def accepted_spec(path: str) -> Spec | None:
    """A spec whose start_url is `path`, or None when its frontmatter is
    refused. Validated as load_spec validates it, without a file for each."""
    data = {
        "id": "login",
        "goal": "A reader signs in.",
        "preconditions": {"start_url": path},
        "expect": ["The home page is shown"],
    }
    context = SpecContext(file_id="login", declared_secrets=frozenset())
    try:
        frontmatter = SpecFrontmatter.model_validate(data, context=context)
    except ValidationError:
        return None
    return Spec(Path("login.spec.md"), frontmatter, spec_hash(data))


def test_chromium_reads_every_start_url_on_the_start_origin() -> None:
    named = {path: accepted_spec(path) for path in NAMED_START_URLS}
    assert [path for path, spec in named.items() if spec is None] == []
    swept = (
        accepted_spec("/" + "".join(chars))
        for length in range(5)
        for chars in itertools.product(ALPHABET, repeat=length)
    )
    specs = [spec for spec in (*named.values(), *swept) if spec is not None]
    pairs = [
        [start, start_url(spec, start)] for start in START_ORIGINS for spec in specs
    ]
    # The control: decoded first, /%2f%2fevil.test is ///evil.test.
    decoded = [[start, start + unquote("/%2f%2fevil.test")] for start in START_ORIGINS]

    async def scenario() -> tuple[object, object]:
        async with (
            async_playwright() as playwright,
            open_browser_session(playwright.chromium) as session,
        ):
            # JSON text, since Playwright serializes a list argument item by
            # item: about 6 s for these pairs, against 1 s.
            return (
                await session.page.evaluate(OFF_ORIGIN, json.dumps(pairs)),
                await session.page.evaluate(OFF_ORIGIN, json.dumps(decoded)),
            )

    off_origin, control = asyncio.run(scenario())

    assert off_origin == []
    assert control == [
        [start, f"{start}///evil.test", start, "///evil.test"]
        for start in START_ORIGINS
    ]
