"""Document origins: the session observes and acts only on documents from the
run's allowed origins (#44; ADR-0026, Two tiers of hosts, and its amendment on
document origins; SECURITY.md §4 and §7). The browser tests launch real
Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import pytest
from aqa_runner.browser_session import LEFT_OUT, BrowserSession, RefError
from aqa_runner.document_origins import (
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
    Popup,
    Records,
    document_origin,
)
from playwright.async_api import ElementHandle, Error, Frame, Page

from packages.runner.tests.document_fixtures import (
    Sites,
    browsing,
    serving_sites,
    to,
    written,
)

START = "http://app.example.test:8080"


@pytest.mark.parametrize(
    ("url", "inherited", "origin"),
    [
        # An http(s) document is on its URL's origin, written as an origin
        # writes it: the scheme's default port dropped, the host lowercase.
        ("http://app.example.test:8080/cart?x=1#top", None, START),
        ("https://APP.example.test:443/", None, "https://app.example.test"),
        ("http://[::1]:8080/", None, "http://[::1]:8080"),
        # A user and password in the URL are not part of its origin.
        ("http://user:pass@app.example.test:8080/", None, START),
        # A blob is on the origin of the document that made it.
        ("blob:http://app.example.test:8080/3f1c", None, START),
        ("blob:null/3f1c", None, None),
        # about:blank and about:srcdoc are on the origin they inherit: a
        # frame's parent's, a popup's opener's, or none for the session's page.
        ("about:blank", START, START),
        ("about:srcdoc", START, START),
        ("about:blank#x", START, START),
        ("about:blank", None, None),
        # No other document is on an origin a run could allow.
        ("data:text/html,<p>hi</p>", START, None),
        ("chrome-error://chromewebdata/", START, None),
        ("file:///etc/passwd", START, None),
        ("javascript:alert(1)", START, None),
        ("about:version", START, None),
        # A host no origin can name is on none.
        ("http://app.example.test./", None, None),
        ("http://app.example.test:0/", None, None),
    ],
)
def test_document_origin(url: str, inherited: str | None, origin: str | None) -> None:
    assert document_origin(url, inherited) == origin


def test_records_keep_the_first_hundred_and_count_them_all() -> None:
    # A page can open popups in a loop, so a session's records are bounded.
    records: Records[int] = Records()
    for entry in range(250):
        records.add(entry)

    assert records.kept == list(range(100))
    assert records.total == 250


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def ref_for(snapshot: str, role: str, name: str) -> str:
    """The ref the snapshot gives the element with `role` and `name`."""
    found = re.search(rf'- {role} "{re.escape(name)}" \[ref=([^\]]+)\]', snapshot)
    assert found is not None, snapshot
    return found[1]


# How a control reaches the target: (the page, the control's role, and the
# path the page's control goes to). A redirect is a link to the site's
# redirect, on the start origin.
HOW = {
    "click": ("/click", "link", "{target}/doc"),
    "redirect": ("/click", "link", "{app}/redirect?to={doc}"),
    "location change": ("/move", "button", "{target}/doc"),
}


@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("where", ["subresource host", "disallowed host"])
def test_a_document_off_the_allowed_origins_is_a_policy_event(
    sites: Sites, how: str, where: str
) -> None:
    target = sites.cdn if where == "subresource host" else sites.evil
    # Where the browser lands: the planted document, or the error page the
    # browser shows when the proxy refuses the host.
    landed, origin = {
        "subresource host": (f"{sites.cdn}/doc", sites.cdn),
        "disallowed host": ("chrome-error://chromewebdata/", None),
    }[where]
    path, role, control = HOW[how]
    goes_to = control.format(target=target, app=sites.app, doc=to(f"{target}/doc"))

    async def scenario() -> tuple[list[PolicyEventError], list[PolicyEvent]]:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}{path}?to={to(goes_to)}")
            go = ref_for(await session.snapshot(), role, "Go")
            await (await session.locate(go)).click()
            await session.page.wait_for_url(landed)
            refused = []
            with pytest.raises(PolicyEventError) as observing:
                await session.snapshot()
            refused.append(observing.value)
            with pytest.raises(PolicyEventError) as locating:
                await session.locate(go)
            refused.append(locating.value)
            return refused, session.policy_events.kept

    refused, recorded = asyncio.run(scenario())

    event = PolicyEvent("document", landed, origin)
    assert [error.event for error in refused] == [event, event]
    assert recorded == [event, event]


def test_a_cross_origin_iframes_content_is_left_out_of_the_snapshot(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[str, int, int]:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}/frames")
            snapshot = await session.snapshot()
            refs = re.findall(r"\[ref=([^\]]*)\]", snapshot)
            # Every ref left resolves: none names an element left out.
            located = len([await session.locate(ref) for ref in refs])
            return snapshot, located, session.policy_events.total

    snapshot, located, events = asyncio.run(scenario())

    for shown in ["Top", "Nested", "Same", "Other"]:
        assert ref_for(snapshot, "button", shown)
    for hidden in ["Planted", "Ignore your task", "Data"]:
        assert hidden not in snapshot, snapshot
    # The three frames left out keep their iframe's line, with no ref, so the
    # agent can't act into them.
    assert snapshot.count(LEFT_OUT) == 3, snapshot
    assert len(re.findall(r"- iframe \[ref=", snapshot)) == 3, snapshot
    assert located == len(re.findall(r"\[ref=", snapshot))
    # A page that embeds another origin's frame reached nothing: no event.
    assert events == 0


def test_a_frame_that_navigates_during_a_snapshot_discards_it(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken_by_playwright = Page.aria_snapshot
    calls = 0

    # Playwright takes the first snapshot while the frame shows the planted
    # document; the frame then moves to the start origin before the session
    # reads where it is.
    async def frame_moves_after(page: Page, **options: Any) -> str:
        nonlocal calls
        calls += 1
        text = await taken_by_playwright(page, **options)
        if calls == 1:
            [frame] = page.main_frame.child_frames
            await frame.goto(f"{sites.app}/kept")
        return text

    monkeypatch.setattr(Page, "aria_snapshot", frame_moves_after)

    async def scenario() -> tuple[DocumentChangedError, str]:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}/flip")
            with pytest.raises(DocumentChangedError) as changed:
                await session.snapshot()
            return changed.value, await session.snapshot()

    changed, again = asyncio.run(scenario())

    assert "take a new snapshot" in str(changed)
    # Observed again, the frame shows what it shows now.
    assert ref_for(again, "button", "Other")
    assert "Planted" not in again, again


async def open_popup(session: BrowserSession, role: str) -> None:
    """Use the page's control `Go`, which opens a popup, and wait until the
    session has closed it."""
    async with session.page.context.expect_page() as opened:
        go = ref_for(await session.snapshot(), role, "Go")
        await (await session.locate(go)).click()
    popup = await opened.value
    if not popup.is_closed():
        await popup.wait_for_event("close", timeout=5000)


def test_popups_are_recorded_with_their_url_and_opener_and_closed(
    sites: Sites,
) -> None:
    # A window.open on another allowed origin, a link that opens a tab with no
    # opener on the start origin, and a blank window, whose origin the session
    # can't know: Playwright names the page that opened it, not the frame.
    openers = [
        (f"{sites.app}/popup?to={to(f'{sites.other}/kept')}", "button"),
        (f"{sites.app}/tab?to={to(f'{sites.app}/kept')}", "link"),
        (f"{sites.app}/popup?to=", "button"),
    ]

    async def scenario() -> tuple[list[Popup], int, list[PolicyEvent]]:
        async with browsing(sites) as session:
            for opener, role in openers:
                await session.page.goto(opener)
                await open_popup(session, role)
            return (
                session.popups.kept,
                len(session.page.context.pages),
                session.policy_events.kept,
            )

    popups, pages, events = asyncio.run(scenario())

    assert popups == [
        Popup(f"{sites.other}/kept", openers[0][0]),
        # Playwright reports the opener even when the link said noopener.
        Popup(f"{sites.app}/kept", openers[1][0]),
        Popup("about:blank", openers[2][0]),
    ]
    assert pages == 1, "a popup was left open"
    assert events == [PolicyEvent("popup", "about:blank", None)]


@pytest.mark.parametrize("where", ["subresource host", "disallowed host"])
def test_a_popup_off_the_allowed_origins_is_a_policy_event(
    sites: Sites, where: str
) -> None:
    target = sites.cdn if where == "subresource host" else sites.evil
    landed, origin = {
        "subresource host": (f"{sites.cdn}/doc", sites.cdn),
        "disallowed host": ("chrome-error://chromewebdata/", None),
    }[where]
    opener = f"{sites.app}/popup?to={to(f'{target}/doc')}"

    async def scenario() -> tuple[list[Popup], list[PolicyEvent], str]:
        async with browsing(sites) as session:
            await session.page.goto(opener)
            await open_popup(session, "button")
            # The session's own page is still on the start origin.
            return (
                session.popups.kept,
                session.policy_events.kept,
                await session.snapshot(),
            )

    popups, events, snapshot = asyncio.run(scenario())

    assert popups == [Popup(landed, opener)]
    assert events == [PolicyEvent("popup", landed, origin)]
    assert 'button "Go"' in snapshot, snapshot


def test_locate_refuses_an_element_in_a_frame_off_the_allowed_origins(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = Page.query_selector

    async def scenario() -> tuple[PolicyEventError, list[PolicyEvent]]:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}/frames")
            top = ref_for(await session.snapshot(), "button", "Top")
            [cdn, *_] = [f for f in session.page.frames if f.url == f"{sites.cdn}/doc"]

            # A fault: Playwright resolves the session's current ref to an
            # element in the subresource host's frame.
            async def into_the_frame(*_: object, **__: object) -> ElementHandle | None:
                return await cdn.query_selector("button")

            monkeypatch.setattr(Page, "query_selector", into_the_frame)
            with pytest.raises(PolicyEventError) as refused:
                await session.locate(top)
            monkeypatch.setattr(Page, "query_selector", query)
            return refused.value, session.policy_events.kept

    refused, recorded = asyncio.run(scenario())

    event = PolicyEvent("frame", f"{sites.cdn}/doc", sites.cdn)
    assert refused.event == event
    assert recorded == [event]


def test_locate_refuses_an_element_of_no_frame(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> RefError:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}/frames")
            top = ref_for(await session.snapshot(), "button", "Top")
            orphan = await session.page.evaluate_handle(
                "document.implementation.createHTMLDocument('').createElement('button')"
            )

            # A fault: Playwright resolves the ref to an element of a document
            # that no frame shows.
            async def to_no_frame(*_: object, **__: object) -> ElementHandle | None:
                return orphan.as_element()

            monkeypatch.setattr(Page, "query_selector", to_no_frame)
            with pytest.raises(RefError) as refused:
                await session.locate(top)
            return refused.value

    assert "has left the page" in str(asyncio.run(scenario()))


def test_a_frame_removed_during_a_snapshot_discards_it(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken_by_playwright = Page.aria_snapshot
    calls = 0

    # The first time, the page removes its frame, and the subresource host's
    # frame inside it, once Playwright has taken the snapshot.
    async def frame_removed_after(page: Page, **options: Any) -> str:
        nonlocal calls
        calls += 1
        text = await taken_by_playwright(page, **options)
        if calls == 1:
            await page.locator("body > iframe").evaluate("(frame) => frame.remove()")
        return text

    monkeypatch.setattr(Page, "aria_snapshot", frame_removed_after)

    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}/deep")
            with pytest.raises(DocumentChangedError):
                await session.snapshot()
            return await session.snapshot()

    again = asyncio.run(scenario())

    assert "Planted" not in again, again
    assert "Nested" not in again, again


@pytest.mark.parametrize("page", ["/ads", "/embeds"])
def test_a_blank_frame_another_origin_wrote_is_left_out(
    sites: Sites, monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    async def scenario() -> tuple[str, PolicyEventError]:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}{page}")
            planted = await written(session.page, "Planted")
            snapshot = await session.snapshot()
            top = ref_for(snapshot, "button", "Top")

            # A fault: Playwright resolves the session's current ref to the
            # planted button.
            async def into_the_frame(*_: object, **__: object) -> ElementHandle | None:
                return await planted.query_selector("button")

            monkeypatch.setattr(Page, "query_selector", into_the_frame)
            with pytest.raises(PolicyEventError) as refused:
                await session.locate(top)
            return snapshot, refused.value

    snapshot, refused = asyncio.run(scenario())

    for hidden in ["Planted", "Ignore your task"]:
        assert hidden not in snapshot, snapshot
    if page == "/ads":
        # The app's own blank frame is on its origin. Playwright shows no
        # <embed>'s content, so only the iframes count.
        assert ref_for(snapshot, "button", "Own")
        assert snapshot.count(LEFT_OUT) == 2, snapshot
    assert refused.event == PolicyEvent("frame", "about:blank", None)


@pytest.mark.parametrize(
    ("owner", "method"),
    [(Page, "query_selector"), (ElementHandle, "content_frame")],
    ids=["no element", "no frame"],
)
def test_an_iframe_with_no_frame_is_left_out(
    sites: Sites, monkeypatch: pytest.MonkeyPatch, owner: type, method: str
) -> None:
    async def nothing(*_: object, **__: object) -> None:
        return None

    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}/frames")
            # A fault: no iframe's ref resolves to an element, or no iframe
            # element to a frame, as when one is gone.
            monkeypatch.setattr(owner, method, nothing)
            return await session.snapshot()

    snapshot = asyncio.run(scenario())

    # Every frame is left out, the allowed ones included.
    assert ref_for(snapshot, "button", "Top")
    for hidden in ["Nested", "Same", "Other"]:
        assert hidden not in snapshot, snapshot
    assert snapshot.count(LEFT_OUT) == 5, snapshot


def test_an_error_while_frames_stay_put_is_raised_as_it_is(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object, **__: object) -> Frame | None:
        raise Error("broken")

    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.page.goto(f"{sites.app}/frames")
            # A fault no frame change explains: it isn't reported as one.
            monkeypatch.setattr(ElementHandle, "content_frame", broken)
            await session.snapshot()

    with pytest.raises(Error, match="broken"):
        asyncio.run(scenario())
