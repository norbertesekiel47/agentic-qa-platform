"""A listed subject binds only as its region's part, at every use (ADR-0025's
#53 P7b-II amendment; DATA_MODEL §7): contracted resolution, generation and
replay, on fixture pages and the pilot's captures in real Chromium."""

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import anthropic
import pytest
from aqa_core.compiled import ByCss, Target
from aqa_core.project import (
    contracts_fingerprint,
    load_project,
    parse_compiled,
    subject_contracts,
)
from aqa_core.schema import Contract
from aqa_runner import locator_generation
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.binding import Refused, binding_verdict
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.document_origins import DocumentChangedError
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import AssertionResult, RunResult, RunSetup, replay
from aqa_runner.locator_generation import (
    BindingRefusedError,
    LocatorError,
    Seen,
    TargetUses,
    register_identity_engine,
)
from aqa_runner.locators import Absent, Resolved, Unresolved, rendered_text, resolve
from aqa_runner.model_router import ModelRouter
from aqa_runner.run_record import RunRecord
from langchain_anthropic import ChatAnthropic
from playwright.async_api import ElementHandle, Error, JSHandle, Page, async_playwright
from playwright.async_api import Locator as PlaywrightLocator

from packages.runner.tests.egress_fixtures import egress_proxy, gate
from packages.runner.tests.executor_fixtures import compiled
from packages.runner.tests.explore_fixtures import MODES, App, in_app
from packages.runner.tests.pilot_pages import ORIGIN, in_session, put, show

PILOT = Path(__file__).resolve().parents[3] / "bench" / "apps" / "conduit" / "qa"


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
ADOPTED = (
    'document.implementation.createHTMLDocument("").body'
    '.appendChild(document.querySelector("div.banner"))'
)
FRAMED = (
    'const f = document.createElement("iframe"); document.body.append(f);'
    "f.contentDocument.body.appendChild(r)"
)


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


@pytest.mark.parametrize(
    "change",
    [
        MOVED,
        SECOND,
        ADOPTED,
        f'(() => {{ const r = document.querySelector("div.banner"); {FRAMED} }})()',
    ],
    ids=["moved", "second", "adopted", "adopted into a frame"],
)
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
    escaping_scope = contracted(
        {"css": "span.missing", "scope": {"css": ":scope + div.container.page"}}
    )
    gone = HIDDEN.replace('class="banner"', 'class="hero"')

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, hidden, "negative_check")

    async def shown(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, escaping, "negative_check")

    async def outside(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, escaping_scope, "negative_check")

    assert in_regions(scenario, HIDDEN) == Absent(0)
    assert in_regions(scenario, gone) == Unresolved(("no region", "no region"))
    # The escaping locator finds nothing, but the banner's count shows.
    assert in_regions(shown) == Unresolved(("not the part",))
    # An empty scope outside the region proves nothing, though no part shows.
    assert in_regions(outside, HIDDEN) == Unresolved(("outside region",))


@pytest.mark.parametrize(
    "change",
    [
        "r.replaceWith(r.cloneNode(true))",
        'r.className = "moved"',
        "r.after(r.cloneNode(true))",
        "r.remove()",
        'document.implementation.createHTMLDocument("").body.appendChild(r)',
        FRAMED,
        "",
    ],
    ids=[
        "replaced",
        "relabelled",
        "second",
        "removed",
        "adopted",
        "adopted into a frame",
        "control",
    ],
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


@pytest.mark.parametrize(
    ("spec", "expect", "page"),
    [
        ("read-article", 1, "article-signed-out"),
        ("read-article", 2, "article-signed-out"),
        ("publish-article", 3, "article"),
        ("favorite-article", 0, "article-favorited"),
        ("favorite-article", 1, "article-favorited"),
    ],
)
def test_each_reviewed_conduit_row_binds_only_its_banner_part(
    spec: str, expect: int, page: str
) -> None:
    contract = subject_contracts(load_project(PILOT).config, spec)[expect]
    part = Target(
        semantic=spec, locators=(ByCss(css=contract.part),), contract=contract
    )

    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        await show(session, page)
        found = await resolve(session.page, part, "assertion")
        assert isinstance(found, Resolved), found
        banner = await offered(session, f"div.banner {contract.part}")
        lower = await offered(session, f"div.article-actions {contract.part}")
        same = await found.element.evaluate("(e, b) => e === b", banner.element)
        return same, await binding_verdict(session.page, lower.element, contract)

    assert in_session(scenario) == (True, Refused("outside_region"))


def test_the_published_article_row_binds_its_template_inferred_banner_author() -> None:
    contract = subject_contracts(load_project(PILOT).config, "publish-article")[3]
    author = Target(
        semantic="author", locators=(ByCss(css="a.author"),), contract=contract
    )

    async def scenario(session: BrowserSession) -> str:
        await put(session, MODES["published-template-inferred"])
        found = await resolve(session.page, author, "assertion")
        assert isinstance(found, Resolved), found
        return await rendered_text(found.element)

    assert in_session(scenario) == "jake"


def favorite_project(tmp_path: Path) -> tuple[Any, ...]:
    """The pilot's spec root, its favorite-article spec starting on the
    fixture app instead, so the replay needs no account."""
    root = shutil.copytree(PILOT, tmp_path / "qa")
    (root / "favorite-article.spec.md").write_text(
        "---\nid: favorite-article\ngoal: A reader sees the article favorited.\n"
        "preconditions:\n  start_url: /\nexpect:\n"
        '  - The favorite button reads "Unfavorite Article"\n'
        "  - The article's favorites count shows 1\n---\n"
    )
    project = load_project(root)
    config = project.config
    rows = subject_contracts(config, "favorite-article")
    data = compiled(
        [],
        targets={
            "button": {
                "semantic": "the banner's favorite button",
                "locators": [{"css": "app-favorite-button > button"}],
                "contract": rows[0].model_dump(),
            },
            "count": {
                "semantic": "the banner's favorites count",
                "locators": [{"css": "span.counter"}],
                "contract": rows[1].model_dump(),
            },
        },
        assertions=[
            {
                "id": "a0",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "button",
                "text": "Unfavorite Article",
            },
            {
                "id": "a1",
                "expect_index": 1,
                "check": "text_in_target",
                "target": "count",
                "text": "(1)",
            },
        ],
    ).model_dump(mode="json")
    data["spec_id"] = "favorite-article"
    data["compiled_by"]["subject_contracts"] = contracts_fingerprint(
        config, "favorite-article"
    )
    data["coverage"]["expectations"] = [
        {
            "expect_index": index,
            "subject": "the banner",
            "claim": claim,
            "assertions": [f"a{index}"],
        }
        for index, claim in enumerate(("reads Unfavorite Article", "shows 1"))
    ]
    script = parse_compiled(
        json.dumps(data), config, source=root / "favorite-article.json"
    )
    return project.specs["favorite-article"], config, script


def replayed(tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch) -> RunResult:
    """A strict public replay of the favorite-article script on the fixture
    app in `mode`, with every model client's constructor a trap."""
    built: list[str] = []

    def trap(name: str) -> Callable[..., None]:
        def constructor(*_: object, **__: object) -> None:
            built.append(name)
            raise AssertionError(f"{name} built during a strict replay")

        return constructor

    clients: list[type] = [
        ModelRouter,
        AnthropicClient,
        ChatAnthropic,
        anthropic.Anthropic,
        anthropic.AsyncAnthropic,
    ]
    for client in clients:
        monkeypatch.setattr(client, "__init__", trap(client.__name__))
    for client in clients:
        with pytest.raises(AssertionError):
            client()
    assert len(built) == len(clients)
    built.clear()
    spec, config, script = favorite_project(tmp_path)

    async def run() -> RunResult:
        async with App(mode=mode).serving() as app, async_playwright() as playwright:
            await register_identity_engine(playwright)
            run_gate = gate(allowed=(app.origin,))
            setup = RunSetup(spec, config, app.origin, RunRecord.create(tmp_path))
            async with EgressProxy(run_gate) as proxy, asyncio.timeout(60):
                return await replay(
                    script,
                    setup,
                    chromium=playwright.chromium,
                    proxy=proxy,
                    gate=run_gate,
                )

    result = asyncio.run(run())
    assert built == []
    return result


def test_a_bug_planted_only_in_the_banner_copy_fails_the_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = replayed(tmp_path, "banner-bug", monkeypatch)

    assert result.assertions == (
        AssertionResult("a0", "pass"),
        AssertionResult("a1", "failed"),
    )
    assert result.outcome == "failed"


def test_a_bug_planted_only_in_the_article_actions_copy_passes_the_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = replayed(tmp_path, "lower-bug", monkeypatch)

    assert result.assertions == (
        AssertionResult("a0", "pass"),
        AssertionResult("a1", "pass"),
    )
    assert result.outcome == "passed"


def test_a_count_whose_value_violates_the_check_still_resolves_to_the_banner_copy() -> (
    None
):
    count = contracted({"css": "span.counter"})

    async def scenario(app: App, session: BrowserSession) -> str:
        await session.page.goto(f"{app.origin}/page/banner-bug")
        found = await session.resolve(count, "assertion")
        assert isinstance(found, Resolved), found
        return await rendered_text(found.element)

    assert in_app(scenario) == "(3)"


def test_a_count_whose_attributes_moved_to_the_lower_copy_never_resolves_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The lower copy shows the expected (1) and carries the banner count's
    # class, id and test ID; the banner shows (3) with none of them.
    moved = (
        MODES["banner-bug"]
        .replace('<span class="counter">(3)</span>', "<span>(3)</span>")
        .replace(
            '<span class="counter">(1)</span>',
            '<span class="counter" id="count" data-testid="count">(1)</span>',
        )
    )
    monkeypatch.setitem(MODES, "moved", moved)
    target = contracted({"testid": "count"}, {"css": "#count"}, {"css": "span.counter"})

    async def scenario(
        app: App, session: BrowserSession
    ) -> Resolved | Absent | Unresolved:
        await session.page.goto(f"{app.origin}/page/moved")
        return await session.resolve(target, "assertion")

    assert in_app(scenario) == Unresolved(("no match", "no match", "no match"))


def test_a_contracted_look_releases_its_region_handle_on_every_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The look keeps a main-world handle on the held region as evidence:
    # released after a binding, an absence, a raise and a drift, while the
    # element a binding returns stays the caller's.
    taken: list[JSHandle] = []
    released: list[int] = []
    adopt: list[bool] = []
    evaluate_handle, dispose = ElementHandle.evaluate_handle, JSHandle.dispose
    count = PlaywrightLocator.count

    async def taking(self: ElementHandle, expression: str, arg: Any = None) -> JSHandle:
        handle = await evaluate_handle(self, expression, arg)
        taken.append(handle)
        return handle

    async def releasing(self: JSHandle) -> None:
        released.append(id(self))
        await dispose(self)

    async def adopting(self: PlaywrightLocator) -> int:
        found = await count(self)
        if found == 0 and adopt:
            adopt.clear()
            await self.page.evaluate(f"() => {{ {ADOPTED} }}")
        return found

    monkeypatch.setattr(ElementHandle, "evaluate_handle", taking)
    monkeypatch.setattr(JSHandle, "dispose", releasing)
    monkeypatch.setattr(PlaywrightLocator, "count", adopting)
    count_target = contracted({"css": "span.counter"})

    async def scenario(page: Page) -> tuple[Any, ...]:
        found = await resolve(page, count_target, "assertion")
        assert isinstance(found, Resolved), found
        kept = id(found.element) not in released
        await page.evaluate(
            "() => { document.querySelector('span.counter').hidden = true }"
        )
        absent = await resolve(page, count_target, "negative_check")
        with pytest.raises(Error, match="while parsing css selector"):
            await resolve(page, contracted({"css": "//input"}), "assertion")
        adopt.append(True)
        return kept, absent, await resolve(page, count_target, "negative_check")

    kept, absent, adopted = in_regions(scenario)

    assert (kept, absent, adopted) == (True, Absent(0), Unresolved(("no region",)))
    assert len(taken) == 4
    assert all(id(handle) in released for handle in taken)


def cancelled_look(
    monkeypatch: pytest.MonkeyPatch, blocked: str, failing: bool
) -> tuple[str, bool, bool]:
    """A contracted look cancelled while `blocked` waits (the `bound:` read,
    or the release of the look's evidence handle), that release failing
    when `failing`: the outcome, whether the task counts as cancelled, and
    whether the banner count the look found was released."""
    started = asyncio.Event()
    evidence: list[JSHandle] = []
    counts: list[ElementHandle] = []
    released: list[JSHandle] = []
    evaluate_handle, dispose = ElementHandle.evaluate_handle, JSHandle.dispose
    query, element_handles = (
        ElementHandle.query_selector_all,
        PlaywrightLocator.element_handles,
    )

    async def taking(self: ElementHandle, expression: str, arg: Any = None) -> JSHandle:
        handle = await evaluate_handle(self, expression, arg)
        evidence.append(handle)
        return handle

    async def finding(self: PlaywrightLocator) -> list[ElementHandle]:
        found = await element_handles(self)
        marks = [await each.get_attribute("data-is") for each in found]
        counts.extend(found if marks == ["banner-count"] else [])
        return found

    async def blocking(self: ElementHandle, selector: str) -> list[ElementHandle]:
        if blocked == "bound" and selector.startswith("aqa-binding=bound:"):
            started.set()
            await asyncio.sleep(3600)
        return await query(self, selector)

    async def releasing(self: JSHandle) -> None:
        released.append(self)
        if self in evidence and blocked == "evidence" and not started.is_set():
            started.set()
            await asyncio.sleep(3600)
        if self in evidence and failing:
            raise Error("fake dispose failure")
        await dispose(self)

    monkeypatch.setattr(ElementHandle, "evaluate_handle", taking)
    monkeypatch.setattr(PlaywrightLocator, "element_handles", finding)
    monkeypatch.setattr(ElementHandle, "query_selector_all", blocking)
    monkeypatch.setattr(JSHandle, "dispose", releasing)

    async def scenario(page: Page) -> tuple[str, bool, bool]:
        look = asyncio.create_task(
            resolve(page, contracted({"css": "span.counter"}), "assertion")
        )
        await started.wait()
        look.cancel()
        [outcome] = await asyncio.gather(look, return_exceptions=True)
        return type(outcome).__name__, look.cancelled(), counts[0] in released

    return in_regions(scenario)


def test_a_cancelled_contracted_look_stays_cancelled_when_a_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, cancelled, _ = cancelled_look(monkeypatch, "bound", failing=True)

    assert (outcome, cancelled) == ("CancelledError", True)


def test_a_look_cancelled_while_releasing_its_evidence_still_releases_its_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cancelled_look(monkeypatch, "evidence", failing=False) == (
        "CancelledError",
        True,
        True,
    )


def test_a_page_that_breaks_the_subject_check_binds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_: object) -> None:
        raise Error("fake page break")

    monkeypatch.setattr(locator_generation, "binding_verdict", broken)

    async def scenario(session: BrowserSession) -> tuple[Any, ...]:
        uses = TargetUses(MEANING, checks_text=True, contract=COUNT)
        await put(session, REGIONS)
        count = await offered(session, "div.banner span.counter")
        with pytest.raises(LocatorError) as refused:
            await uses.add(session.page, count, "assertion")
        monkeypatch.setattr(session.page, "is_closed", lambda: True)
        with pytest.raises(Error, match="fake page break"):
            await uses.add(session.page, count, "assertion")
        return str(refused.value), uses.targets

    assert in_session(scenario) == (f"{MEANING}: the page broke the subject check", ())
