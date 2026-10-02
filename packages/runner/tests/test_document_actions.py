"""Acting and navigating only on documents from the run's allowed origins
(#44; ADR-0026, Two tiers of hosts, and its amendments on document origins;
SECURITY.md §7; the seam with #46's executor). The browser tests launch real
Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import pytest
from aqa_core.compiled import ByRole, Target
from aqa_runner import browser_session
from aqa_runner.document_origins import (
    PolicyEvent,
    PolicyEventError,
    navigable_origin,
)
from aqa_runner.egress import Refusal
from aqa_runner.locators import Resolved, Use, resolve
from playwright.async_api import Error, Page

from packages.runner.tests.document_fixtures import (
    CDN,
    Sites,
    browsing,
    ref_for,
    run_gate,
    serving_sites,
    to,
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


def test_navigate_refuses_an_allowed_url_that_redirects_to_a_subresource_host(
    sites: Sites,
) -> None:
    landed = f"{sites.cdn}/doc"

    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            with pytest.raises(PolicyEventError) as refused:
                await session.navigate(f"{sites.app}/redirect?to={to(landed)}")
            return refused.value

    # The proxy passes the hop; the session refuses the document it reached.
    assert asyncio.run(scenario()).event == PolicyEvent("document", landed, sites.cdn)


def test_navigate_to_an_allowed_url_that_redirects_to_a_disallowed_host_fails_at_the_proxy(
    sites: Sites,
) -> None:
    egress = run_gate(sites)

    async def scenario() -> tuple[Error, PolicyEventError]:
        async with browsing(sites, egress) as session:
            with pytest.raises(Error) as failed:
                await session.navigate(
                    f"{sites.app}/redirect?to={to(f'{sites.evil}/doc')}"
                )
            # The browser then shows its error page, on no origin.
            await session.page.wait_for_url("chrome-error://chromewebdata/")
            with pytest.raises(PolicyEventError) as refused:
                await session.snapshot()
            return failed.value, refused.value

    failed, refused = asyncio.run(scenario())

    assert "net::ERR_EMPTY_RESPONSE" in failed.message
    assert [(r.host, r.kind) for r in egress.refusals] == [
        ("evil.example.test", "host")
    ]
    assert refused.event == PolicyEvent(
        "document", "chrome-error://chromewebdata/", None
    )


def test_navigate_follows_a_redirect_between_allowed_origins(sites: Sites) -> None:
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(
                f"{sites.app}/redirect?to={to(f'{sites.other}/kept')}"
            )
            return await session.url()

    assert asyncio.run(scenario()) == f"{sites.other}/kept"


def test_navigating_back_to_an_allowed_origin_after_a_policy_event_works(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[list[PolicyEvent], str]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/click?to={to(f'{sites.cdn}/doc')}")
            go = ref_for(await session.snapshot(), "link", "Go")
            await session.click(await session.locate(go))
            await session.page.wait_for_url(f"{sites.cdn}/doc")
            with pytest.raises(PolicyEventError):
                await session.snapshot()
            await session.navigate(f"{sites.app}/kept")
            snapshot = await session.snapshot()
            await session.click(
                await session.locate(ref_for(snapshot, "button", "Other"))
            )
            return session.policy_events.kept, await session.url()

    events, url = asyncio.run(scenario())

    assert events == [PolicyEvent("document", f"{sites.cdn}/doc", sites.cdn)]
    assert url == f"{sites.app}/kept"


def test_actions_act_on_an_allowed_page(sites: Sites) -> None:
    async def scenario() -> tuple[str, str, str | None]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/form?to=")
            snapshot = await session.snapshot()
            await session.fill(
                await session.locate(ref_for(snapshot, "textbox", "Name")), "Ada"
            )
            await session.select(
                await session.locate(ref_for(snapshot, "combobox", "Size")), "M"
            )
            await session.click(
                await session.locate(ref_for(snapshot, "button", "Save"))
            )
            page = session.page
            return (
                await page.get_by_label("Name").input_value(),
                await page.get_by_label("Size").input_value(),
                await page.get_by_role("button").text_content(),
            )

    assert asyncio.run(scenario()) == ("Ada", "M", "Saved")


def test_nothing_is_acted_on_in_an_off_origin_document(sites: Sites) -> None:
    landed = f"{sites.cdn}/doc"

    async def scenario() -> list[PolicyEventError]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/form?to={to(landed)}")
            snapshot = await session.snapshot()
            held = {
                name: await session.locate(ref_for(snapshot, role, name))
                for role, name in [
                    ("textbox", "Name"),
                    ("combobox", "Size"),
                    ("button", "Save"),
                ]
            }
            await session.click(await session.locate(ref_for(snapshot, "link", "Go")))
            await session.page.wait_for_url(landed)
            target = Target(
                semantic="the planted button", locators=(ByRole(role="button"),)
            )
            refused = []
            for attempt in [
                session.click(held["Save"]),
                session.fill(held["Name"], "Ada"),
                session.select(held["Size"], "M"),
                session.press("Enter"),
                session.reload(),
                session.url(),
                session.resolve(target, "action"),
            ]:
                with pytest.raises(PolicyEventError) as refusal:
                    await attempt
                refused.append(refusal.value)
            return refused

    before = len(sites.seen)
    refused = asyncio.run(scenario())

    assert [error.event for error in refused] == [
        PolicyEvent("document", landed, sites.cdn)
    ] * 7
    # The reload never left, and no click reached the planted document.
    assert sites.seen[before:].count(("cdn.example.test", "/doc")) == 1
    assert ("cdn.example.test", "/clicked") not in sites.seen


def around(snapshot: str, element: str) -> str:
    """The ref of an element of /wrapped that contains a frame from the
    subresource host: the page's root, a region around one, a region whose
    open shadow root holds one, or a frame of the start origin with one
    nested inside it."""
    if element == "root":
        first = re.search(r"\[ref=(e\d+)\]", snapshot)
        assert first is not None, snapshot
        return first[1]
    if element in ("region", "shadow"):
        return ref_for(
            snapshot, "region", "Offers" if element == "region" else "Shadow"
        )
    *_, nested = re.findall(r"- iframe \[ref=(e\d+)\]", snapshot)
    return str(nested)


@pytest.mark.parametrize("element", ["root", "region", "shadow", "nested frame"])
def test_acting_on_an_element_around_a_frame_off_the_allowed_origins_is_refused(
    sites: Sites, element: str
) -> None:
    async def scenario() -> list[PolicyEventError]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/wrapped")
            snapshot = await session.snapshot()
            target = await session.locate(around(snapshot, element))
            refused = []
            for attempt in [
                session.click(target),
                session.fill(target, "Ada"),
                session.select(target, "M"),
            ]:
                with pytest.raises(PolicyEventError) as refusal:
                    await attempt
                refused.append(refusal.value)
            # Controls: an element around no such frame, and one inside the
            # start origin's frame, beside the one nested there.
            for name in ["Plain", "Nested"]:
                await session.click(
                    await session.locate(ref_for(snapshot, "button", name))
                )
            return refused

    refused = asyncio.run(scenario())

    event = PolicyEvent("frame", f"{sites.cdn}/doc", sites.cdn)
    assert [error.event for error in refused] == [event] * 3
    assert ("cdn.example.test", "/clicked") not in sites.seen


def test_a_click_on_an_element_around_a_frame_lands_in_the_frame(sites: Sites) -> None:
    # The control for the test above: Playwright's own click, unchecked,
    # lands in the subresource host's frame inside the region.
    async def scenario() -> bool:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/wrapped")
            region = await session.locate(
                ref_for(await session.snapshot(), "region", "Offers")
            )
            await region.click()
            return await asyncio.to_thread(sites.saw, CDN, "/clicked", within=5)

    assert asyncio.run(scenario()), "the click didn't reach the frame"


def test_actions_refuse_an_element_of_no_frame(sites: Sites) -> None:
    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            orphan = await session.page.evaluate_handle(
                "document.implementation.createHTMLDocument('').createElement('button')"
            )
            element = orphan.as_element()
            assert element is not None
            with pytest.raises(PolicyEventError) as refused:
                await session.click(element)
            return refused.value

    assert asyncio.run(scenario()).event == PolicyEvent("frame", "", None)


def test_resolve_finds_a_target_on_an_allowed_page(sites: Sites) -> None:
    other = Target(semantic="the other button", locators=(ByRole(role="button"),))

    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            found = await session.resolve(other, "action")
            assert isinstance(found, Resolved)
            return await found.element.inner_text()

    assert asyncio.run(scenario()) == "Other"


def test_resolve_refuses_a_page_that_navigated_while_it_looked(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = Target(semantic="the other button", locators=(ByRole(role="button"),))
    landed = f"{sites.cdn}/doc"

    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")

            # The page moves to the subresource host after the lookup.
            async def then_moves(page: Page, target: Target, use: Use) -> Any:
                found = await resolve(page, target, use)
                await page.goto(landed)
                return found

            monkeypatch.setattr(browser_session, "resolve_target", then_moves)
            with pytest.raises(PolicyEventError) as refused:
                await session.resolve(other, "action")
            return refused.value

    refused = asyncio.run(scenario())

    assert refused.event == PolicyEvent("document", landed, sites.cdn)


def test_reload_checks_the_page_it_lands_on(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    landed = f"{sites.cdn}/doc"
    reloads = Page.reload

    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            await session.reload()

            # The next reload lands on the subresource host, as a reload a
            # server redirects would.
            async def lands_elsewhere(page: Page, **options: Any) -> Any:
                await page.goto(landed)
                return await reloads(page, **options)

            monkeypatch.setattr(Page, "reload", lands_elsewhere)
            with pytest.raises(PolicyEventError) as refused:
                await session.reload()
            return refused.value

    before = len(sites.seen)
    refused = asyncio.run(scenario())

    assert sites.seen[before:][:2] == [("app.example.test", "/kept")] * 2
    assert refused.event == PolicyEvent("document", landed, sites.cdn)
