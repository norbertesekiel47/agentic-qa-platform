"""Acting and navigating only on documents from the run's allowed origins
(#44; ADR-0026, Two tiers of hosts, and its amendments on document origins;
SECURITY.md §7; the seam with #46's executor). The browser tests launch real
Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
from collections.abc import Iterator

import pytest
from aqa_runner.document_origins import (
    PolicyEvent,
    PolicyEventError,
    navigable_origin,
)
from aqa_runner.egress import Refusal

from packages.runner.tests.document_fixtures import (
    Sites,
    browsing,
    run_gate,
    serving_sites,
)

START = "http://app.example.test:8080"


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://app.example.test:8080/cart?x=1#top", START),
        ("HTTP://APP.example.test:8080/", START),
        ("https://app.example.test/", "https://app.example.test"),
        ("http://user@app.example.test:8080/", START),
        # Not an absolute http(s) URL: nothing navigate goes to.
        ("/cart", None),
        ("//app.example.test:8080/", None),
        ("http:app.example.test:8080/", None),
        ("javascript:location='http://app.example.test:8080/'", None),
        ("data:text/html,<p>hi</p>", None),
        ("about:blank", None),
        ("blob:http://app.example.test:8080/3f1c", None),
        ("file:///etc/passwd", None),
        # Text a browser reads differently from Python, wherever it is.
        ("http://evil.example.test\\@app.example.test:8080/", None),
        ("http://app.example.test:8080\\evil.example.test/", None),
        ("http://app.exa\tmple.test:8080/", None),
        ("http://app.example.test:8080/\nx", None),
        (" http://app.example.test:8080/", None),
        ("http://[::1/", None),
    ],
)
def test_navigable_origin(url: str, origin: str | None) -> None:
    assert navigable_origin(url) == origin


# URLs navigate refuses from the start origin, by what is wrong with them,
# and the origin each is on, if any.
REFUSED = {
    "disallowed host": ("{evil}/doc", "{evil}"),
    "subresource host": ("{cdn}/doc", "{cdn}"),
    "another scheme": (
        "https://app.example.test:{port}/kept",
        "https://app.example.test:{port}",
    ),
    "javascript": ("javascript:location = '{app}/kept'", None),
    "data": ("data:text/html,<p>hi</p>", None),
    "blank": ("about:blank", None),
    "relative": ("/kept", None),
    "backslash": ("{evil}\\@app.example.test:{port}/", None),
}


@pytest.mark.parametrize("why", REFUSED)
def test_navigate_refuses_a_url_off_the_allowed_origins(sites: Sites, why: str) -> None:
    template, origin = REFUSED[why]
    names = {"app": sites.app, "evil": sites.evil, "cdn": sites.cdn, "port": sites.port}
    url = template.format(**names)
    egress = run_gate(sites)

    async def scenario() -> tuple[PolicyEventError, list[PolicyEvent], str]:
        async with browsing(sites, egress) as session:
            await session.navigate(f"{sites.app}/kept")
            with pytest.raises(PolicyEventError) as refused:
                await session.navigate(url)
            return refused.value, session.policy_events.kept, await session.url()

    before = len(sites.seen)
    refused, recorded, still = asyncio.run(scenario())

    event = PolicyEvent(
        "navigation", url, None if origin is None else origin.format(**names)
    )
    assert refused.event == event
    assert recorded == [event]
    # Refused before anything left: the gate judged nothing, the site saw
    # only the first navigation, and the page stayed where it was.
    assert egress.refusals == list[Refusal]()
    assert sites.seen[before:] == [("app.example.test", "/kept")]
    assert still == f"{sites.app}/kept"
