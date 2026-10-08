"""Locked, origin-checked observations without retiring refs (ADR-0026)."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from aqa_runner import browser_session as session_module
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
    frame_origin,
)
from playwright.async_api import Frame, Response

from packages.runner.tests.document_fixtures import (
    Sites,
    browsing,
    ref_for,
    serving_sites,
    to,
)


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def test_observing_page_yields_the_page_without_retiring_refs(sites: Sites) -> None:
    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/own")
            ref = ref_for(await session.snapshot(), "textbox", "Top")
            async with session.observing_page() as page:
                assert page is session.page
                assert await page.get_by_label("Top").input_value() == ""
            element = await session.locate(ref)
            assert await element.get_attribute("type") is None

    asyncio.run(scenario())


@pytest.mark.parametrize("when", ["before", "during"])
def test_observing_page_refuses_a_page_off_the_allowed_origins(
    sites: Sites,
    when: str,
) -> None:
    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/own")
            destination = f"{sites.cdn}/doc"
            if when == "before":
                await session.page.goto(f"{sites.app}/redirect?to={to(destination)}")
            entered = False

            async def observe() -> None:
                nonlocal entered
                async with session.observing_page() as page:
                    entered = True
                    await page.goto(f"{sites.app}/redirect?to={to(destination)}")

            with pytest.raises(PolicyEventError) as raised:
                await observe()
            assert entered == (when == "during")
            assert raised.value.event == PolicyEvent("document", destination, sites.cdn)
            assert session.policy_events.kept[-1] == raised.value.event

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["navigate", "detach"])
@pytest.mark.parametrize("when", ["initial_check", "body"])
def test_a_frame_change_while_observing_raises_document_changed(
    sites: Sites,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    when: str,
) -> None:
    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/own")

            async def alter() -> None:
                if change == "navigate":
                    await session.page.goto(f"{sites.app}/own")
                else:
                    await session.page.locator("iframe").evaluate("e => e.remove()")

            original = frame_origin
            changed = False

            async def during_check(frame: Frame) -> str | None:
                nonlocal changed
                if not changed:
                    changed = True
                    await alter()
                return await original(frame)

            if when == "initial_check":
                monkeypatch.setattr(session_module, "frame_origin", during_check)

            async def observe() -> None:
                async with session.observing_page():
                    if when == "body":
                        await alter()

            with pytest.raises(DocumentChangedError):
                await observe()

    asyncio.run(scenario())


def test_observing_page_holds_the_session_lock(
    sites: Sites,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/own")
            original = session.page.goto
            started, dispatched = asyncio.Event(), asyncio.Event()

            async def watched(url: str, **options: Any) -> Response | None:
                dispatched.set()
                return await original(url, **options)

            monkeypatch.setattr(session.page, "goto", watched)

            async def navigate() -> None:
                started.set()
                await session.navigate(f"{sites.app}/kept")

            async with session.observing_page():
                task = asyncio.create_task(navigate())
                await started.wait()
                assert not dispatched.is_set()
            await task
            assert dispatched.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "cancellation"])
def test_an_exception_or_cancellation_while_observing_releases_the_lock(
    sites: Sites,
    failure: str,
) -> None:
    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/own")
            entered = asyncio.Event()
            fault = ValueError("body failure")

            async def observe() -> None:
                async with session.observing_page():
                    entered.set()
                    if failure == "exception":
                        raise fault
                    await asyncio.Event().wait()

            task = asyncio.create_task(observe())
            await entered.wait()
            if failure == "cancellation":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ValueError, match="body failure") as raised:
                    await task
                assert raised.value is fault
            async with asyncio.timeout(5):
                await session.navigate(f"{sites.app}/kept")
            assert session.page.url == f"{sites.app}/kept"

    asyncio.run(scenario())
