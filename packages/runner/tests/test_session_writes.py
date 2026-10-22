"""A browser session `prepared_session` makes, directly or through
`open_browser_session`, hears each write request its egress phase reads, in
the window of its latest action, and one prepared in a later phase hears
only that phase's (ADR-0025's #54 amendment). Real Chromium against the
executor's fixture app."""

import asyncio
from collections.abc import Iterator

import pytest
from aqa_runner.browser_session import (
    BrowserSession,
    launched_browser,
    prepared_session,
)
from aqa_runner.egress_proxy import EgressProxy
from playwright.async_api import async_playwright

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import App, serving_app

type Heard = list[tuple[bool, bool]]


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


async def writes(app: App, session: BrowserSession, path: str | None) -> Heard:
    """Each window's `heard` and `wrote` once the session loads the start page
    and the page writes to `path`, when given."""
    await session.navigate(f"{app.origin}/page/start")
    if path is not None:
        await session.page.evaluate(f"fetch('{path}', {{method: 'POST'}})")
        await until(lambda: ("POST", path) in app.seen)
    return heard(session)


def heard(session: BrowserSession) -> Heard:
    return [(window.heard, window.wrote) for window in session.windows()]


def test_a_session_prepared_directly_hears_each_write_of_its_phase(app: App) -> None:
    async def scenario() -> Heard:
        async with (
            async_playwright() as playwright,
            EgressProxy(gate(allowed=(app.origin,))) as proxy,
            launched_browser(playwright.chromium) as browser,
            prepared_session(browser, egress=proxy) as session,
        ):
            return await writes(app, session, "/write/direct")

    # The window before navigation opened before the session was watched.
    assert asyncio.run(scenario()) == [(False, False), (True, True)]


def test_a_session_prepared_in_a_new_phase_hears_that_phase_and_no_other(
    app: App,
) -> None:
    async def scenario() -> tuple[Heard, Heard]:
        async with (
            async_playwright() as playwright,
            EgressProxy(gate(allowed=(app.origin,))) as proxy,
            launched_browser(playwright.chromium) as browser,
        ):
            async with prepared_session(browser, egress=proxy) as first:
                await writes(app, first, None)
            await proxy.end_phase()
            await proxy.begin_phase()
            async with prepared_session(browser, egress=proxy) as second:
                after = await writes(app, second, "/write/second")
            return heard(first), after

    first, second = asyncio.run(scenario())

    assert (first, second) == (
        [(False, False), (True, False)],
        [(False, False), (True, True)],
    )
