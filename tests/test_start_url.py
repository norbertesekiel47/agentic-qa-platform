"""A run's start URL as Chromium reads it: for every start_url the spec parser
accepts, its origin is the run's start origin (ADR-0026's 2026-10-01
amendment). The core's start_url, checked in the runner's browser."""

import asyncio
import itertools
from pathlib import Path

from aqa_core.project import start_url
from aqa_core.spec import Spec, SpecContext, SpecFrontmatter, spec_hash
from aqa_runner.browser_session import open_browser_session
from playwright.async_api import async_playwright
from pydantic import ValidationError

# Start origins as start_origin gives them: a DNS name on the scheme's default
# port, an IPv4 address with a port and an IPv6 address with a port.
STARTS = [
    "https://shop.example.test",
    "http://127.0.0.1:4100",
    "http://[2001:db8::1]:8080",
]

# Accepted start_urls a careless join could move to another origin: decoded,
# /%2f%2fevil.test is //evil.test; ..; is no dot segment to a browser; and //
# in a query or fragment names no host.
NAMED = [
    "/",
    "/%2f%2fevil.test",
    "/..;/x",
    "/login?next=//evil.test",
    "/login#//evil.test",
]

# What URL parsing gives a meaning to: separators, dots, % with the hex digits
# of %2e, %2f and %5c, a letter and a non-ASCII letter.
ALPHABET = "/.%2efF5c;?#@:aé"


def spec_starting_at(path: str) -> Spec | None:
    """A spec whose start_url is `path`, or None when the parser refuses it."""
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
    named = {path: spec_starting_at(path) for path in NAMED}
    assert [path for path, spec in named.items() if spec is None] == []
    # Every start_url the parser accepts, up to 4 characters after the /.
    candidates = (
        spec_starting_at("/" + "".join(chars))
        for length in range(5)
        for chars in itertools.product(ALPHABET, repeat=length)
    )
    specs = [spec for spec in (*named.values(), *candidates) if spec is not None]
    urls = [[start, start_url(spec, start)] for start in STARTS for spec in specs]

    async def scenario() -> object:
        async with (
            async_playwright() as playwright,
            open_browser_session(playwright.chromium) as session,
        ):
            # Each URL whose origin isn't its start origin, with the origin read.
            return await session.page.evaluate(
                """urls => urls
                    .map(([start, url]) => [start, url, new URL(url).origin])
                    .filter(([start, , origin]) => origin !== start)""",
                urls,
            )

    assert asyncio.run(scenario()) == []
