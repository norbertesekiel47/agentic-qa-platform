"""A listed subject binds only as its region's part, at every use (ADR-0025's
#53 P7b-II amendment; DATA_MODEL §7): contracted resolution, generation and
replay, on fixture pages and the pilot's captures in real Chromium."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import pytest
from aqa_core.compiled import ByCss, Target
from aqa_core.schema import Contract
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.document_origins import DocumentChangedError
from aqa_runner.locator_generation import (
    BindingRefusedError,
    LocatorError,
    Seen,
    TargetUses,
    register_identity_engine,
)
from aqa_runner.locators import Absent, Resolved, Unresolved, resolve
from playwright.async_api import ElementHandle, Page, async_playwright
from playwright.async_api import Locator as PlaywrightLocator

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.pilot_pages import ORIGIN, in_session, put, show


async def marker(resolution: Resolved | Absent | Unresolved) -> str | None:
    """The data-is marker of the element a resolution found."""
    assert isinstance(resolution, Resolved), resolution
    return await resolution.element.get_attribute("data-is")


# A banner and a lower copy of the article meta, with no class shared above
# them (ADR-0025, the #53 P7b-II amendment).
REGIONS = (
    '<div class="banner"><div class="article-meta"><div class="info">'
    '<a class="author" data-is="banner-author">anna</a></div><app-favorite-button>'
    '<button data-is="banner-button">Favorite <span class="counter" data-testid="count"'
    ' data-is="banner-count">(3)</span></button></app-favorite-button></div></div>'
    '<div class="container page"><div class="article-meta"><app-favorite-button>'
    '<button data-is="lower-button">Favorite <span class="counter" data-is="lower-count"'
    ">(1)</span></button></app-favorite-button></div></div>"
)
HIDDEN = REGIONS.replace('data-is="banner-count"', 'data-is="banner-count" hidden')
COUNT = Contract(region="div.banner", part="span.counter", leaf=True)
BUTTON = Contract(region="div.banner", part="app-favorite-button > button")
MOVED = (
    'document.querySelector("div.banner").className = "moved";'
    'document.querySelector("div.container").classList.add("banner")'
)
SECOND = 'document.querySelector("div.container").classList.add("banner")'


def contracted(*locators: dict[str, Any], contract: Contract = COUNT) -> Target:
    return Target.model_validate(
        {
            "semantic": "the favorites count in the banner",
            "locators": locators,
            "contract": contract.model_dump(),
        }
    )


def in_regions[T](scenario: Callable[[Page], Awaitable[T]], html: str = REGIONS) -> T:
    """`scenario`'s result on `html`, with the binding engine registered."""

    async def run() -> T:
        async with async_playwright() as playwright:
            await register_identity_engine(playwright)
            async with (
                egress_proxy() as egress,
                open_browser_session(playwright.chromium, egress=egress) as session,
            ):
                await session.page.set_content(html)
                return await scenario(session.page)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "css", [":scope + div.container.page span.counter", ":scope ~ div span.counter"]
)
def test_a_sibling_selector_from_the_region_root_never_resolves_outside_it(
    css: str,
) -> None:
    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, contracted({"css": css}), "assertion")

    assert in_regions(scenario) == Unresolved(("outside region",))


def test_an_escaping_scope_is_outside_the_region_and_a_missing_one_is_no_scope() -> (
    None
):
    escaping = contracted(
        {"css": "span.counter", "scope": {"css": ":scope + div.container.page"}}
    )
    missing = contracted({"css": "span.counter", "scope": {"css": "section.gone"}})
    inside = contracted({"css": "span.counter", "scope": {"css": "div.article-meta"}})

    async def scenario(page: Page) -> tuple[Any, ...]:
        return (
            await resolve(page, escaping, "assertion"),
            await resolve(page, missing, "assertion"),
            await marker(await resolve(page, inside, "assertion")),
        )

    assert in_regions(scenario) == (
        Unresolved(("outside region",)),
        Unresolved(("no scope",)),
        "banner-count",
    )


@pytest.mark.parametrize("change", [MOVED, SECOND], ids=["moved", "second"])
def test_a_region_changed_between_counting_and_retrieving_is_drift(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # The page changes once, after the region is held and the banner's count
    # retrieved, so only the held-region postcondition can catch it.
    original = PlaywrightLocator.element_handles
    changed: list[str] = []

    async def element_handles(self: PlaywrightLocator) -> list[ElementHandle]:
        found = await original(self)
        marks = [await each.get_attribute("data-is") for each in found]
        if marks == ["banner-count"] and not changed:
            changed.append(change)
            await self.page.evaluate(change)
        return found

    monkeypatch.setattr(PlaywrightLocator, "element_handles", element_handles)

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, contracted({"css": "span.counter"}), "assertion")

    assert in_regions(scenario) == Unresolved(("no region",))
    assert changed == [change]


def test_a_region_relabelled_while_an_action_is_checked_is_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The part is judged after actionability, so a page that relabels its
    # region meanwhile can't hand the click another region's button.
    original = ElementHandle.is_enabled
    relabel: list[bool] = []

    async def is_enabled(self: ElementHandle) -> bool:
        enabled = await original(self)
        if relabel:
            relabel.clear()
            await self.evaluate(f"() => {{ {MOVED} }}")
        return enabled

    monkeypatch.setattr(ElementHandle, "is_enabled", is_enabled)
    button = contracted({"css": "button"}, contract=BUTTON)

    async def scenario(page: Page) -> tuple[Any, ...]:
        control = await marker(await resolve(page, button, "action"))
        relabel.append(True)
        return control, await resolve(page, button, "action")

    assert in_regions(scenario) == ("banner-button", Unresolved(("no region",)))


@pytest.mark.parametrize("use", ["action", "assertion", "negative_check"])
@pytest.mark.parametrize(
    "html",
    [
        REGIONS.replace('class="banner"', 'class="hero"'),
        REGIONS.replace('class="container page"', 'class="banner"'),
    ],
    ids=["missing", "two"],
)
def test_a_contracted_target_whose_region_is_missing_or_ambiguous_is_drift(
    html: str, use: Literal["action", "assertion", "negative_check"]
) -> None:
    both = contracted({"css": "span.counter"}, {"testid": "count"})

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, both, use)

    assert in_regions(scenario, html) == Unresolved(("no region", "no region"))


def test_a_contracted_target_on_another_element_than_its_part_is_drift() -> None:
    button = contracted({"css": "app-favorite-button > button"})
    grown = REGIONS.replace(">(3)</span>", "><b>(3)</b></span>")

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, button, "assertion")

    async def leaf(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, contracted({"css": "span.counter"}), "assertion")

    assert in_regions(scenario) == Unresolved(("not the part",))
    assert in_regions(leaf, grown) == Unresolved(("not a leaf",))


def test_a_contracted_negative_check_is_absent_only_inside_its_present_region() -> None:
    hidden = contracted(
        {"css": "span.counter", "scope": {"css": "app-favorite-button"}},
        {"testid": "count", "scope": {"css": "div.article-meta"}},
    )
    escaping = contracted({"css": ":scope + div.container.page span.missing"})
    gone = HIDDEN.replace('class="banner"', 'class="hero"')

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, hidden, "negative_check")

    async def shown(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, escaping, "negative_check")

    assert in_regions(scenario, HIDDEN) == Absent(0)
    assert in_regions(scenario, gone) == Unresolved(("no region", "no region"))
    # The escaping locator finds nothing, but the banner's count shows.
    assert in_regions(shown) == Unresolved(("not the part",))


@pytest.mark.parametrize(
    "change",
    [
        "r.replaceWith(r.cloneNode(true))",
        'r.className = "moved"',
        "r.after(r.cloneNode(true))",
        "r.remove()",
        'document.implementation.createHTMLDocument("").body.appendChild(r)',
        "",
    ],
    ids=["replaced", "relabelled", "second", "removed", "adopted", "control"],
)
def test_a_region_change_after_a_zero_result_is_drift_not_absence(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    original = PlaywrightLocator.count
    changed: list[str] = []

    async def count(self: PlaywrightLocator) -> int:
        found = await original(self)
        if found == 0 and not changed:
            changed.append(change)
            await self.page.evaluate(
                f'() => {{ const r = document.querySelector("div.banner"); {change} }}'
            )
        return found

    monkeypatch.setattr(PlaywrightLocator, "count", count)
    hidden = contracted(
        {"css": "span.counter", "scope": {"css": "app-favorite-button"}}
    )

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, hidden, "negative_check")

    assert in_regions(scenario, HIDDEN) == (
        Absent(0) if not change else Unresolved(("no region",))
    )
    assert changed == [change]


def test_every_locators_absence_is_judged_by_one_region_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ElementHandle.query_selector_all
    reads: list[str] = []

    async def query_selector_all(self: ElementHandle, selector: str) -> Any:
        if selector.startswith("aqa-binding="):
            reads.append(selector.removeprefix("aqa-binding="))
        return await original(self, selector)

    monkeypatch.setattr(ElementHandle, "query_selector_all", query_selector_all)
    scoped = {"css": "span.counter", "scope": {"css": "app-favorite-button"}}
    by_id = {"testid": "count", "scope": {"css": "div.article-meta"}}
    in_button = {"css": "span", "scope": {"css": "button"}}
    escaping = {"css": "span.counter", "scope": {"css": ":scope + div.container.page"}}

    async def scenario(page: Page) -> tuple[Any, ...]:
        absent = await resolve(
            page, contracted(scoped, by_id, in_button), "negative_check"
        )
        absent_reads = list(reads)
        reads.clear()
        drift = await resolve(
            page, contracted(scoped, escaping, by_id), "negative_check"
        )
        return absent, absent_reads, drift, list(reads)

    absent, absent_reads, drift, drift_reads = in_regions(scenario, HIDDEN)

    held = [
        read.removeprefix("hold:") for read in absent_reads if read.startswith("hold:")
    ]
    assert absent == Absent(0)
    assert [read for read in absent_reads if read.startswith("absent:")] == [
        f"absent:{held[0]}|div.banner|span.counter"
    ]
    assert drift == Unresolved(("no match", "outside region", "no match"))
    assert not [read for read in drift_reads if read.startswith("absent:")]


def test_a_navigation_inside_a_held_region_is_a_document_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The page navigates after a locator's zero result, while the region is
    # held: the session's frame-change count, never the drop's message, says
    # the look saw another document, so the executor looks again.
    original = PlaywrightLocator.count
    navigate: list[bool] = []

    async def count(self: PlaywrightLocator) -> int:
        found = await original(self)
        if found == 0 and navigate:
            navigate.clear()
            await self.page.goto(f"{ORIGIN}/")
        return found

    monkeypatch.setattr(PlaywrightLocator, "count", count)
    hidden = contracted(
        {"css": "span.counter", "scope": {"css": "app-favorite-button"}}
    )

    async def scenario(session: BrowserSession) -> Resolved | Absent | Unresolved:
        await put(session, HIDDEN)
        control = await session.resolve(hidden, "negative_check")
        navigate.append(True)
        with pytest.raises(DocumentChangedError):
            await session.resolve(hidden, "negative_check")
        return control

    assert in_session(scenario) == Absent(0)


MEANING = "the favorites count in the banner"
REGION_GONE = f"{MEANING}: its region is not on the page exactly once"


async def offered(session: BrowserSession, css: str) -> Seen:
    element = await session.page.query_selector(css)
    assert element is not None, css
    return Seen(element)


def test_target_uses_runs_the_contract_verdict_at_every_later_use() -> None:
    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        uses = TargetUses(MEANING, checks_text=True, contract=COUNT)
        await show(session, "article")
        first = await uses.add(
            session.page, await offered(session, "div.banner span.counter"), "assertion"
        )
        before = uses.targets
        await show(session, "article-favorited")
        lower = await offered(session, "div.article-actions span.counter")
        with pytest.raises(BindingRefusedError) as refused:
            await uses.add(session.page, lower, "assertion")
        await put(session, REGIONS.replace('class="container page"', 'class="banner"'))
        banner = await offered(session, "div.banner span.counter")
        with pytest.raises(BindingRefusedError) as moved:
            await uses.add(session.page, banner, "assertion")
        return first, before, uses.targets, refused.value, moved.value

    first, before, after, refused, moved = in_session(scenario)

    assert first == 0
    assert (
        before
        == after
        == (
            Target(
                semantic=MEANING, locators=(ByCss(css="span.counter"),), contract=COUNT
            ),
        )
    )
    assert (refused.reason, str(refused)) == (
        "outside_region",
        f"{MEANING}: outside_region",
    )
    assert moved.reason == "region_ambiguous"


def test_a_sighting_outside_the_region_keeps_no_sighting() -> None:
    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        uses = TargetUses(MEANING, checks_text=False, contract=COUNT)
        await put(session, REGIONS)
        await uses.see_for_negative_check(
            session.page, await offered(session, "div.banner span.counter")
        )
        lower = await offered(session, "div.container span.counter")
        with pytest.raises(BindingRefusedError) as refused:
            await uses.see_for_negative_check(session.page, lower)
        await put(session, HIDDEN)
        with pytest.raises(LocatorError) as unseen:
            await uses.add_negative_check(session.page)
        return refused.value.reason, str(unseen.value), uses.targets

    assert in_session(scenario) == (
        "outside_region",
        f"{MEANING}: it wasn't seen before its negative check",
        (),
    )


def test_unlisted_uses_are_refused_only_where_copies_are_detected() -> None:
    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        author = TargetUses("the article's author", checks_text=True)
        await show(session, "article")
        with pytest.raises(BindingRefusedError) as refused:
            await author.add(
                session.page, await offered(session, "div.banner a.author"), "assertion"
            )
        field = TargetUses("the login form's email field", checks_text=False)
        await show(session, "login")
        email = await offered(session, 'input[name="email"]')
        return (
            refused.value.reason,
            author.targets,
            await field.add(session.page, email, "action"),
            field.targets[0].contract,
        )

    assert in_session(scenario) == ("unlisted_copies", (), 0, None)


def test_page_world_facts_cannot_drop_the_region_from_a_contracted_targets_locators() -> (
    None
):
    lower = REGIONS.replace(
        'data-is="lower-count"', 'data-is="lower-count" id="lower" data-testid="lower"'
    )
    lie = """() => {
        const read = Element.prototype.getAttribute;
        Element.prototype.getAttribute = function (name) {
            if (read.call(this, "data-is") !== "banner-count") return read.call(this, name);
            if (name === "id" || name === "data-testid") return "lower";
            return read.call(this, name);
        };
    }"""

    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        uses = TargetUses(MEANING, checks_text=True, contract=COUNT)
        await put(session, lower)
        await session.page.evaluate(lie)
        banner = await offered(session, "div.banner span.counter")
        said = await banner.element.evaluate("(e) => e.getAttribute('data-testid')")
        await uses.add(session.page, banner, "assertion")
        return said, uses.targets

    assert in_session(scenario) == (
        "lower",
        (
            Target(
                semantic=MEANING, locators=(ByCss(css="span.counter"),), contract=COUNT
            ),
        ),
    )


def test_a_banner_count_stripped_of_its_class_is_refused_and_a_span_part_still_generates() -> (
    None
):
    # The banner count's class, id and test ID moved to the lower copy: under
    # the reviewed row the banner has no part, so nothing binds; under a
    # test-local row whose part is the span, only region-rooted locators stay.
    moved = REGIONS.replace(
        'class="counter" data-testid="count" data-is="banner-count"',
        'data-is="banner-count"',
    ).replace(
        'class="counter" data-is="lower-count"',
        'class="counter" id="count" data-testid="count" data-is="lower-count"',
    )
    span = Contract(region="div.banner", part="span")

    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        row = TargetUses(MEANING, checks_text=True, contract=COUNT)
        local = TargetUses(MEANING, checks_text=True, contract=span)
        await put(session, moved)
        banner = await offered(session, "div.banner app-favorite-button span")
        with pytest.raises(BindingRefusedError) as refused:
            await row.add(session.page, banner, "assertion")
        await local.add(session.page, banner, "assertion")
        return refused.value.reason, row.targets, local.targets

    assert in_session(scenario) == (
        "part_absent",
        (),
        (Target(semantic=MEANING, locators=(ByCss(css="span"),), contract=span),),
    )


CHANGES = {
    "replaced": "r.replaceWith(r.cloneNode(true))",
    "relabelled": 'r.className = "moved"',
    "second": "r.after(r.cloneNode(true))",
    "removed": "r.remove()",
}


@pytest.mark.parametrize("path", ["new", "joining"])
@pytest.mark.parametrize("change", CHANGES)
def test_a_region_change_after_a_zero_result_fails_a_contracted_negative_check(
    monkeypatch: pytest.MonkeyPatch, change: str, path: str
) -> None:
    # Armed, the page changes after a zero result: the first one on the new
    # target's path, the first repeated look (the join's) on the joining path.
    original = PlaywrightLocator.count
    armed: list[str] = []
    looked: list[str] = []

    async def count(self: PlaywrightLocator) -> int:
        found = await original(self)
        if found == 0 and armed:
            repeated = repr(self) in looked
            looked.append(repr(self))
            if path == "new" or repeated:
                await self.page.evaluate(
                    f'() => {{ const r = document.querySelector("div.banner"); {armed.pop()} }}'
                )
        return found

    monkeypatch.setattr(PlaywrightLocator, "count", count)

    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        uses = TargetUses(MEANING, checks_text=False, contract=COUNT)
        await put(session, REGIONS)
        await uses.see_for_negative_check(
            session.page, await offered(session, "div.banner span.counter")
        )
        await put(session, HIDDEN)
        indexes = (
            [await uses.add_negative_check(session.page)] if path == "joining" else []
        )
        before = uses.targets
        await put(session, HIDDEN)
        armed.append(CHANGES[change])
        with pytest.raises(LocatorError) as failed:
            await uses.add_negative_check(session.page)
        return indexes, before, uses.targets, str(failed.value), armed

    indexes, before, after, message, left = in_session(scenario)

    assert indexes == ([0] if path == "joining" else [])
    assert after == before
    assert all(target.contract == COUNT for target in after)
    assert message == REGION_GONE
    assert left == []


def test_a_contracted_negative_check_whose_region_is_gone_names_it() -> None:
    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        uses = TargetUses(MEANING, checks_text=False, contract=COUNT)
        await put(session, REGIONS)
        await uses.see_for_negative_check(
            session.page, await offered(session, "div.banner span.counter")
        )
        await put(session, HIDDEN.replace('class="banner"', 'class="hero"'))
        with pytest.raises(LocatorError) as gone:
            await uses.add_negative_check(session.page)
        return str(gone.value), uses.targets

    assert in_session(scenario) == (REGION_GONE, ())
