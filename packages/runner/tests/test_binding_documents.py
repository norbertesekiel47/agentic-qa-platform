from unittest.mock import patch

import pytest
from aqa_core.schema import Contract
from aqa_runner import binding
from aqa_runner.binding import (
    Bind,
    HeldRegion,
    Refused,
    _query,
    binding_verdict,
    held_region,
)
from aqa_runner.browser_session import BrowserSession
from playwright.async_api import ElementHandle, Error

from packages.runner.tests.pilot_pages import in_session, put

COUNT = Contract(region="div.banner", part="span.counter", leaf=True)
SHOWN = Contract(region="div.banner", part="form.counter")
LIGHT = '<span class="counter" id="light">1</span>'
BANNER = f'<div class="banner">{LIGHT}</div>'
HIDDEN = '<div class="banner"><span class="counter" hidden>1</span></div>'
WITH_HOST = f'<div class="banner">{LIGHT}<x-host></x-host></div>'
FRAME = '<iframe srcdoc="<p>other</p>"></iframe>'


async def element(session: BrowserSession, selector: str) -> ElementHandle:
    found = await session.page.query_selector(selector)
    assert found is not None
    return found


async def attach(session: BrowserSession, host: str, html: str) -> None:
    await session.page.evaluate(
        "([host, html]) => { document.querySelector(host)"
        '.attachShadow({mode: "open"}).innerHTML = html; }',
        [host, html],
    )


async def adopt(session: BrowserSession) -> None:
    await session.page.evaluate(
        "document.querySelector('iframe').contentDocument.body.append("
        "document.querySelector('div.banner'))"
    )


def test_an_open_shadow_root_gained_inside_the_region_is_never_absent() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, '<div class="banner"><p><x-host></x-host></p></div>')
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            assert await held.root.locator("span.counter").count() == 0
            assert await held.absent() is True
            await attach(session, "x-host", '<span class="counter">1</span>')
            assert await held.root.locator("span.counter").count() == 1
            assert await held.absent() is False

    in_session(scenario)


@pytest.mark.parametrize(
    ("body", "host", "shadow"),
    [
        (WITH_HOST, "x-host", '<span class="counter">2</span>'),
        (WITH_HOST, "x-host", "<i>nothing</i>"),
        (BANNER, "div.banner", '<span class="counter">2</span><slot></slot>'),
        ("<x-host></x-host>", "x-host", BANNER),
    ],
    ids=[
        "beside a shadow part",
        "beside an empty host",
        "as the host",
        "inside a shadow tree",
    ],
)
def test_a_region_with_an_open_shadow_host_never_binds_its_part(
    body: str, host: str, shadow: str
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, body)
        await attach(session, host, shadow)
        offered = await element(session, "#light")
        assert await binding_verdict(session.page, offered, COUNT) == Refused(
            "region_ambiguous"
        )
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            assert await held.bound(offered) is False

    in_session(scenario)


@pytest.mark.parametrize(
    "controls",
    [
        "",
        (
            "<input><select><option>a</option></select><textarea></textarea>"
            "<details><summary>s</summary>d</details><video></video>"
        ),
    ],
    ids=["ordinary", "user-agent shadow roots"],
)
def test_ordinary_and_user_agent_shadow_regions_bind_and_prove_absence(
    controls: str,
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, f'<div class="banner">{LIGHT}{controls}</div>')
        offered = await element(session, "span.counter")
        assert await binding_verdict(session.page, offered, COUNT) == Bind()
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            assert await held.absent() is False
            await offered.evaluate("(e) => { e.hidden = true; }")
            assert await held.absent() is True

    in_session(scenario)


def test_a_region_adopted_into_another_document_after_the_hold_is_drift() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, BANNER + FRAME)
        offered = await element(session, "span.counter")
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            assert await held.bound(offered) is True
            await adopt(session)
            assert await held.bound(offered) is False
            await offered.evaluate("(e) => { e.hidden = true; }")
            assert await held.absent() is False
        assert await binding_verdict(session.page, offered, COUNT) == Refused(
            "region_absent"
        )

    in_session(scenario)


def test_a_region_adopted_between_acquisition_and_hold_is_drift() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, HIDDEN + FRAME)
        offered = await element(session, "span.counter")

        async def racing(handle: ElementHandle, body: str) -> list[ElementHandle]:
            if body.startswith("hold:"):
                await adopt(session)
            return await _query(handle, body)

        with patch.object(binding, "_query", racing):
            async with held_region(session.page, COUNT) as held:
                assert held is not None
                assert await held.bound(offered) is False
                assert await held.absent() is False

    in_session(scenario)


@pytest.mark.parametrize(
    ("name", "owner"), [("plain", "#document"), ("ownerDocument", "FIELDSET")]
)
def test_a_named_form_control_cannot_hide_a_duplicate_region(
    name: str, owner: str
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            f'<fieldset name="{name}" form="f"><form id="f" class="banner">'
            '<span class="counter" hidden>1</span></form></fieldset>',
        )
        assert (
            await session.page.evaluate(
                "document.querySelector('form').ownerDocument.nodeName"
            )
            == owner
        )
        contract = Contract(region="form.banner", part="span.counter", leaf=True)
        async with held_region(session.page, contract) as held:
            assert held is not None
            assert await held.absent() is True
            await session.page.evaluate(
                "document.body.insertAdjacentHTML('beforeend', "
                "'<form class=\"banner\"></form>')"
            )
            assert await held.absent() is False

    in_session(scenario)


@pytest.mark.parametrize(
    ("outer", "inner", "regions"),
    [
        ('<div class="other"></div>', "", 1),
        ('<div class="banner"></div>', "", 2),
        ("<y-host></y-host>", '<div class="banner"></div>', 2),
    ],
    ids=["unrelated host", "duplicate", "nested duplicate"],
)
def test_a_region_duplicated_into_an_open_shadow_root_is_drift(
    outer: str, inner: str, regions: int
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, f"{HIDDEN}<x-host></x-host>")
        offered = await element(session, "span.counter")
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            await session.page.evaluate(
                "([outer, inner]) => {"
                ' const root = document.querySelector("x-host")'
                '.attachShadow({mode: "open"}); root.innerHTML = outer;'
                ' if (inner) root.querySelector("y-host")'
                '.attachShadow({mode: "open"}).innerHTML = inner; }',
                [outer, inner],
            )
            assert await session.page.locator("css=div.banner").count() == regions
            assert await held.absent() is (regions == 1)
            assert await held.bound(offered) is (regions == 1)

    in_session(scenario)


CONTENTS = (
    '<form class="counter" style="display:contents">'
    '<select name="childNodes" hidden></select>'
)
CHILD_NODES = "document.querySelector('.counter').childNodes"
NAMED_TYPE = (
    '<span class="counter" style="display:contents">'
    '<form><input name="nodeType" type="hidden">shown</form></span>'
)


@pytest.mark.parametrize(
    ("part", "contract", "clobbered", "control"),
    [
        (f"{CONTENTS}shown</form>", SHOWN, CHILD_NODES, "SELECT"),
        (f"{CONTENTS}<b>shown</b></form>", SHOWN, CHILD_NODES, "SELECT"),
        (
            NAMED_TYPE,
            COUNT,
            "document.querySelector('.counter form').nodeType",
            "INPUT",
        ),
    ],
    ids=["childNodes over text", "childNodes over an element", "nodeType of a child"],
)
def test_named_controls_cannot_hide_a_visible_contents_part(
    part: str, contract: Contract, clobbered: str, control: str
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, f'<div class="banner">{part}</div>')
        assert await session.page.evaluate(f"{clobbered}.nodeName") == control
        async with held_region(session.page, contract) as held:
            assert held is not None
            assert await held.absent() is False
            await session.page.evaluate(
                "document.querySelector('.counter').style.display = 'none'"
            )
            assert await held.absent() is True

    in_session(scenario)


POISON = """<script>
Node.prototype.contains = () => true;
Element.prototype.querySelectorAll = () => [];
Document.prototype.querySelectorAll = () => [];
DocumentFragment.prototype.querySelectorAll = () => [];
Element.prototype.checkVisibility = () => false;
Element.prototype.getBoundingClientRect = () => ({width: 0, height: 0});
window.getComputedStyle = () => ({display: "none", visibility: "hidden"});
for (const [proto, key, value] of [
    [Node.prototype, "ownerDocument", document], [Node.prototype, "isConnected", true],
    [Node.prototype, "childNodes", []], [Node.prototype, "nodeType", 8],
    [Node.prototype, "parentElement", null], [Element.prototype, "shadowRoot", null],
    [Element.prototype, "children", []], [Element.prototype, "localName", "x"],
    [Element.prototype, "childElementCount", 9],
]) Object.defineProperty(proto, key, {get: () => value});
</script>"""


def test_page_tampering_before_the_first_query_changes_no_answer() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, POISON + BANNER)
        offered = await element(session, "span.counter")
        assert await binding_verdict(session.page, offered, COUNT) == Bind()
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            assert await held.absent() is False
            await session.page.evaluate(
                "document.body.insertAdjacentHTML('beforeend', "
                "'<div class=\"banner\"></div>')"
            )
            assert await held.bound(offered) is False

    in_session(scenario)


def test_a_page_poisoning_its_own_globals_keeps_main_world_actions() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            "<script>Object.getOwnPropertyDescriptor = () => {"
            ' throw new Error("fake poison"); };</script>' + BANNER,
        )
        offered = await element(session, "span.counter")
        await offered.dispatch_event("focus")
        assert await binding_verdict(session.page, offered, COUNT) == Bind()

    in_session(scenario)


def test_a_hold_ends_with_its_document_and_a_fresh_hold_binds() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, BANNER)
        stale: list[bool] = []

        async def navigate_while_held() -> None:
            async with held_region(session.page, COUNT) as old:
                assert old is not None
                await put(session, BANNER)
                stale.append(await old.bound(await element(session, "span.counter")))

        with pytest.raises(Error):
            await navigate_while_held()
        assert stale == [False]
        offered = await element(session, "span.counter")
        async with held_region(session.page, COUNT) as fresh:
            assert fresh is not None
            assert await fresh.bound(offered) is True

    in_session(scenario)


def test_an_iframe_engine_judges_a_hold_in_its_own_document() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, f"{BANNER}<iframe srcdoc='{BANNER}'></iframe>")
        frame = session.page.frames[1]
        region = await frame.query_selector("div.banner")
        inner = await frame.query_selector("span.counter")
        assert region is not None
        assert inner is not None
        held = HeldRegion(region, frame.locator("css=div.banner"), COUNT, "0" * 32)
        await _query(region, f"hold:{held.token}")
        assert await held.bound(inner) is True
        assert await held.bound(await element(session, "span.counter")) is False
        await _query(region, f"drop:{held.token}")
        assert await held.bound(inner) is False

    in_session(scenario)


@pytest.mark.parametrize("name", ["plain", "children"])
def test_a_named_form_control_cannot_hide_unlisted_copies(name: str) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            '<form><x-card class="first"><span class="counter">1</span></x-card>'
            '<x-card class="second"><span class="counter">2</span></x-card>'
            f'<select name="{name}" hidden></select></form>',
        )
        offered = await element(session, "x-card.first span.counter")
        assert await binding_verdict(session.page, offered, None) == Refused(
            "unlisted_copies"
        )

    in_session(scenario)
