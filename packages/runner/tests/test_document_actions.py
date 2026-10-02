"""Acting and navigating only on documents from the run's allowed origins
(#44; ADR-0026, Two tiers of hosts, and its amendments on document origins;
SECURITY.md §7; the seam with #46's executor). The browser tests launch real
Chromium on the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import re
from collections.abc import Iterator
from typing import Any, get_args

import pytest
from aqa_core.compiled import ByRole, Target
from aqa_runner import browser_session
from aqa_runner.browser_session import held_keys
from aqa_runner.document_origins import (
    REFUSED_BY_KIND,
    DocumentChangedError,
    PolicyEvent,
    PolicyEventError,
    PolicyEventKind,
    navigable_origin,
)
from aqa_runner.egress import Refusal
from aqa_runner.locators import Absent, Resolved, Unresolved, Use, resolve
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
    ("kind", "says"),
    [
        ("document", "the page is"),
        ("frame", "the element's frame is"),
        ("popup", "a popup is"),
        ("navigation", "the URL to navigate to is"),
    ],
)
def test_each_kind_of_policy_event_says_what_it_refused(
    kind: PolicyEventKind, says: str
) -> None:
    assert set(REFUSED_BY_KIND) == set(get_args(PolicyEventKind.__value__))
    url = "http://cdn.example.test/doc?token=abc"
    on_one = str(PolicyEventError(PolicyEvent(kind, url, "http://cdn.example.test")))
    on_none = str(PolicyEventError(PolicyEvent(kind, url, None)))
    assert on_one.startswith(
        f"{says} on http://cdn.example.test, which isn't one of the run's allowed origins:"
    )
    assert on_none.startswith(f"{says} on no origin a run could allow:")
    # The message names the origin only: the URL's path and query are the page's.
    assert "token" not in on_one + on_none


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
        ("http://app.example.test:8080/\x7f", None),
        ("http://app.example.test:8080/\x00", None),
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


def test_keys_are_never_pressed_into_a_frame_off_the_allowed_origins(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[PolicyEventError, list[str]]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/focus")
            [card] = [f for f in session.page.frames if f.url == f"{sites.cdn}/typing"]
            # The page gives the subresource host's frame the focus.
            await session.page.locator("iframe").first.focus()
            await card.wait_for_function("document.activeElement === card")
            with pytest.raises(PolicyEventError) as refused:
                await session.press("a")
            values = [await card.locator("#card").input_value()]
            # The control: Playwright's own key press, unchecked, lands there.
            await session.page.keyboard.press("x")
            values.append(await card.locator("#card").input_value())
            # A field in the start origin's frame takes keys.
            snapshot = await session.snapshot()
            await session.click(
                await session.locate(ref_for(snapshot, "textbox", "Inner"))
            )
            await session.press("b")
            inner = session.page.frame_locator("iframe >> nth=1").get_by_label("Inner")
            values.append(await inner.input_value())
            return refused.value, values

    refused, values = asyncio.run(scenario())

    assert refused.event == PolicyEvent("frame", f"{sites.cdn}/typing", sites.cdn)
    assert values == ["", "x", "b"]


def test_press_refuses_a_focused_element_around_a_frame_off_the_allowed_origins(
    sites: Sites,
) -> None:
    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/focus")
            # With nothing focused, a key goes to the page, whatever frames
            # it holds.
            await session.press("Shift")
            # The page focuses an element around a subresource host's frame,
            # from which Tab would move the focus into the frame.
            await session.page.get_by_label("Wrapper").focus()
            with pytest.raises(PolicyEventError) as refused:
                await session.press("Tab")
            return refused.value

    refused = asyncio.run(scenario())

    assert refused.event == PolicyEvent("frame", f"{sites.cdn}/doc", sites.cdn)


def test_fill_puts_the_value_into_the_element_even_when_another_frame_takes_the_focus(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[str, str]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/steal")
            [card] = [
                f for f in session.page.frames if f.url == f"{sites.cdn}/stealing"
            ]
            await card.wait_for_function("document.hasFocus()")
            name = await session.locate(
                ref_for(await session.snapshot(), "textbox", "Name")
            )
            await session.fill(name, "Ada")
            return (
                await session.page.get_by_label("Name").input_value(),
                await card.locator("#card").input_value(),
            )

    assert asyncio.run(scenario()) == ("Ada", "")
    assert not sites.saw(CDN, "/typed", within=0.5)


@pytest.mark.parametrize(
    ("role", "name", "value", "reads"),
    [
        ("textbox", "Notes", "new", "new"),
        ("textbox", "Story", "new", "new"),
        ("textbox", "Day", "2026-10-02", "2026-10-02"),
        ("textbox", "Name", "", ""),
        ("spinbutton", "Count", "12", "12"),
    ],
)
def test_fill_fills_each_kind_of_field(
    sites: Sites, role: str, name: str, value: str, reads: str
) -> None:
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/fields")
            field = await session.locate(ref_for(await session.snapshot(), role, name))
            await session.fill(field, value)
            return str(
                await field.evaluate(
                    "(e) => e.isContentEditable ? e.textContent : e.value"
                )
            )

    assert asyncio.run(scenario()) == reads


@pytest.mark.parametrize(
    ("role", "name"), [("spinbutton", "Count"), ("button", "Plain")]
)
def test_fill_refuses_an_element_that_takes_no_such_value(
    sites: Sites, role: str, name: str
) -> None:
    async def scenario() -> Error:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/fields")
            element = await session.locate(
                ref_for(await session.snapshot(), role, name)
            )
            with pytest.raises(Error) as refused:
                await session.fill(element, "not a number")
            return refused.value

    message = asyncio.run(scenario()).message
    assert "didn't take the value" in message
    assert "not a number" not in message


# Elements a click lands through into a subresource host's frame, though
# they don't contain it in the DOM: a region the frame is slotted into, and
# a paragraph in a link the frame is drawn over (Playwright's click takes
# the link as its target).
THROUGH = {
    "slotted": ("/slotted", "region", "Slotted"),
    "in a link": ("/inlink", "paragraph", ""),
}


@pytest.mark.parametrize("how", THROUGH)
def test_a_click_that_would_land_in_a_frame_off_the_allowed_origins_is_refused(
    sites: Sites, how: str
) -> None:
    path, role, name = THROUGH[how]

    async def scenario() -> tuple[PolicyEventError, bool]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}{path}")
            snapshot = await session.snapshot()
            found = re.search(
                rf"- {role}{f' "{name}"' if name else ''} \[ref=(e\d+)\]", snapshot
            )
            assert found is not None, snapshot
            element = await session.locate(found[1])
            with pytest.raises(PolicyEventError) as refused:
                await session.click(element)
            # The control: Playwright's own click, unchecked, lands there.
            await element.click()
            return refused.value, await asyncio.to_thread(
                sites.saw, CDN, "/clicked", within=5
            )

    refused, landed = asyncio.run(scenario())

    assert refused.event == PolicyEvent("frame", f"{sites.cdn}/doc", sites.cdn)
    assert landed, "the unchecked click didn't reach the frame"


@pytest.mark.parametrize(
    ("key", "held"),
    [
        ("a", []),
        ("Enter", []),
        ("Shift+A", ["Shift"]),
        ("Control+Shift+T", ["Control", "Shift"]),
        ("ControlOrMeta+a", ["ControlOrMeta"]),
        ("+", []),
        ("Shift++", ["Shift"]),
        ("Tab+a", ["Tab"]),
        ("a+b+c", ["a", "b"]),
    ],
)
def test_held_keys(key: str, held: list[str]) -> None:
    assert held_keys(key) == held


@pytest.mark.parametrize("key", ["Tab+a", "a+b", "Enter+Shift"])
def test_press_refuses_a_key_held_down_that_isnt_a_modifier(
    sites: Sites, key: str
) -> None:
    # Tab held down would move the focus, maybe into another origin's frame,
    # and the next key would follow it there unchecked.
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/fields")
            name = await session.locate(
                ref_for(await session.snapshot(), "textbox", "Name")
            )
            await session.click(name)
            with pytest.raises(ValueError, match="modifier"):
                await session.press(key)
            return await session.page.get_by_label("Name").input_value()

    assert asyncio.run(scenario()) == "old"


def test_press_refuses_when_another_origins_frame_took_the_focus_back(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[PolicyEventError, str]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/stale")
            [card] = [
                f for f in session.page.frames if f.url == f"{sites.cdn}/stealing"
            ]
            inner = await session.locate(
                ref_for(await session.snapshot(), "textbox", "Inner")
            )
            await session.click(inner)
            await card.wait_for_function("document.hasFocus()")
            with pytest.raises(PolicyEventError) as refused:
                await session.press("a")
            return refused.value, await card.locator("#card").input_value()

    refused, typed = asyncio.run(scenario())

    assert refused.event == PolicyEvent("frame", f"{sites.cdn}/stealing", sites.cdn)
    assert typed == ""


def test_press_refuses_when_the_page_names_a_frame_that_lacks_the_focus(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[PolicyEventError, str]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/stale")
            [card] = [
                f for f in session.page.frames if f.url == f"{sites.cdn}/stealing"
            ]
            await card.wait_for_function("document.hasFocus()")
            # A fault, as Chromium's stale activeElement: the page names its
            # own frame as focused while another origin's frame has the focus.
            await session.page.evaluate(
                """() => Object.defineProperty(document, "activeElement", {
                    get: () => document.querySelector("iframe"),
                })"""
            )
            with pytest.raises(PolicyEventError) as refused:
                await session.press("a")
            return refused.value, await card.locator("#card").input_value()

    refused, typed = asyncio.run(scenario())

    assert refused.event == PolicyEvent("frame", f"{sites.cdn}/stealing", sites.cdn)
    assert typed == ""


def test_press_goes_ahead_when_no_frame_is_off_the_allowed_origins(
    sites: Sites,
) -> None:
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/own")
            top = await session.locate(
                ref_for(await session.snapshot(), "textbox", "Top")
            )
            await session.click(top)
            # The same fault, on a page with no other origin's frame.
            await session.page.evaluate(
                """() => Object.defineProperty(document, "activeElement", {
                    get: () => document.querySelector("iframe"),
                })"""
            )
            await session.press("a")
            return await session.page.get_by_label("Top").input_value()

    assert asyncio.run(scenario()) == "a"


def test_resolve_discards_what_it_saw_when_the_page_changed_meanwhile(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = Target(
        semantic="a button no page here has", locators=(ByRole(role="checkbox"),)
    )
    other = Target(semantic="the other button", locators=(ByRole(role="button"),))

    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            seen: list[Resolved | Absent | Unresolved] = []

            # The lookup runs on the subresource host's page, which then goes
            # back to the start origin before the second check.
            async def away_and_back(page: Page, target: Target, use: Use) -> Any:
                await page.goto(f"{sites.cdn}/doc")
                found = await resolve(page, target, use)
                await page.goto(f"{sites.app}/kept")
                return found

            monkeypatch.setattr(browser_session, "resolve_target", away_and_back)
            with pytest.raises(DocumentChangedError):
                await session.resolve(gone, "negative_check")

            # A same-document change: the element found is still there, and
            # the session lets it go.
            async def then_pushes(page: Page, target: Target, use: Use) -> Any:
                found = await resolve(page, target, use)
                seen.append(found)
                await page.evaluate("history.pushState(null, '', '/kept#moved')")
                return found

            monkeypatch.setattr(browser_session, "resolve_target", then_pushes)
            with pytest.raises(DocumentChangedError):
                await session.resolve(other, "action")
            [found] = seen
            assert isinstance(found, Resolved)
            # Still in its document, but disposed of: it can't be used.
            with pytest.raises(Error):
                await found.element.inner_text()

    asyncio.run(scenario())


def test_actions_refuse_an_element_in_a_frame_off_the_allowed_origins(
    sites: Sites,
) -> None:
    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/flip")
            [cdn] = [f for f in session.page.frames if f.url == f"{sites.cdn}/doc"]
            # An element held in the subresource host's frame, as one could
            # be once its frame had moved there from an allowed origin.
            planted = await cdn.query_selector("button")
            assert planted is not None
            with pytest.raises(PolicyEventError) as refused:
                await session.click(planted)
            return refused.value

    refused = asyncio.run(scenario())

    assert refused.event == PolicyEvent("frame", f"{sites.cdn}/doc", sites.cdn)
    assert not sites.saw(CDN, "/clicked", within=0.5)


def test_press_never_asks_another_origins_frame_where_the_focus_is(
    sites: Sites,
) -> None:
    # The subresource host's frame says it has the focus: asked, it would
    # lead the walk into itself, and the key meant for the start origin's
    # field would be refused.
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/liar")
            inner = await session.locate(
                ref_for(await session.snapshot(), "textbox", "Inner")
            )
            await session.click(inner)
            await session.press("a")
            field = session.page.frame_locator("iframe >> nth=1").get_by_label("Inner")
            return await field.input_value()

    assert asyncio.run(scenario()) == "a"


def test_press_takes_the_root_with_the_focus_for_nothing_focused(sites: Sites) -> None:
    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/wrapped")
            await session.page.evaluate(
                "() => { document.documentElement.tabIndex = -1; document.documentElement.focus(); }"
            )
            await session.press("Shift")

    asyncio.run(scenario())


def test_resolve_looks_nothing_up_on_a_page_off_the_allowed_origins(
    sites: Sites, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked: list[str] = []

    async def spy(page: Page, target: Target, use: Use) -> Any:
        looked.append(page.url)
        return await resolve(page, target, use)

    monkeypatch.setattr(browser_session, "resolve_target", spy)
    other = Target(semantic="a button", locators=(ByRole(role="button"),))

    async def scenario() -> None:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/click?to={to(f'{sites.cdn}/doc')}")
            go = await session.locate(ref_for(await session.snapshot(), "link", "Go"))
            await session.click(go)
            await session.page.wait_for_url(f"{sites.cdn}/doc")
            with pytest.raises(PolicyEventError):
                await session.resolve(other, "assertion")

    asyncio.run(scenario())

    assert looked == []
