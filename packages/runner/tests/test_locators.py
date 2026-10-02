"""Finding a target's element for the use it is put to (DATA_MODEL §7,
Resolution per use; ADR-0025 and its amendments). These tests load fixture
pages into real Chromium through the browser session."""

import asyncio
import typing
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aqa_core.compiled import AriaRole, Target
from aqa_core.text import has_pattern, has_text
from aqa_runner.browser_session import open_browser_session
from aqa_runner.locators import (
    Absent,
    Resolved,
    Unresolved,
    rendered_text,
    resolve,
)
from playwright.async_api import Error, Page, async_playwright

# Every element a test finds carries a data-is marker naming it, so a test
# can tell which element resolved.
PAGE = r"""<!doctype html><title>Locators</title>
<style>
  /* An icon font's glyph and a non-breaking space, from CSS (LAB_NOTES,
     2026-09-29). */
  .icon::before { content: "\f218\a0"; }
  /* A glyph outside the Basic Multilingual Plane, where Material Design
     Icons' webfont puts its glyphs. */
  .mdi::before { content: "\F0001\a0"; }
  .upper { text-transform: uppercase; }
  .cover { position: relative; display: inline-block; }
  .overlay { position: absolute; inset: 0; }
</style>
<header class="banner">
  <a href="#" data-is="new-article"><i class="icon"></i>New Article</a>
  <button data-is="banner-favorite">Favorite Article</button>
  <a href="#" data-is="saved"><i class="mdi"></i>Saved</a>
</header>
<main>
  <label>Email <input id="email" data-is="email" placeholder="you@example.test"></label>
  <span data-testid="count" data-is="count">3</span>
  <button class="upper" data-is="post-comment">Post Comment</button>
  <button disabled data-is="publish">Publish</button>
  <button aria-disabled="true" data-is="archive">Archive</button>
  <button data-is="odd-name">C++ / "quoted" &gt;&gt; 'names'</button>
  <button data-is="cafe">Caf&eacute; &#x2713;</button>
  <button data-is="hot-deals">&#x1F525; Hot deals</button>
  <button class="delete" data-is="remove">Remove</button>
  <span class="cover"><button data-is="pay">Pay</button><span class="overlay"></span></span>
  <section class="empty"></section>
  <div style="height: 3000px"></div>
  <button data-is="below">Below the fold</button>
</main>
<footer><button data-is="footer-favorite">Favorite Article</button></footer>
"""


def target(*locators: dict[str, Any]) -> Target:
    return Target.model_validate(
        {"semantic": "an element on the fixture page", "locators": locators}
    )


async def marker(resolution: Resolved | Absent | Unresolved) -> str | None:
    """The data-is marker of the element a resolution found."""
    assert isinstance(resolution, Resolved), resolution
    return await resolution.element.get_attribute("data-is")


def on_page[T](scenario: Callable[[Page], Awaitable[T]]) -> T:
    """`scenario`'s result on the fixture page, in a fresh browser session."""

    async def run() -> T:
        async with (
            async_playwright() as playwright,
            open_browser_session(playwright.chromium) as session,
        ):
            await session.page.set_content(PAGE)
            return await scenario(session.page)

    return asyncio.run(run())


def test_role_and_name_match_a_name_with_an_icon_glyph() -> None:
    new_article = target({"role": "link", "name": "New Article"})

    async def scenario(page: Page) -> tuple[int, list[str | None], list[int]]:
        # The control: the glyph is in the accessible name, so Playwright's
        # own exact match misses it.
        exact = await page.get_by_role("link", name="New Article", exact=True).count()
        found = [
            await resolve(page, new_article, use) for use in ("action", "assertion")
        ]
        return (
            exact,
            [await marker(each) for each in found],
            [each.locator_index for each in found if isinstance(each, Resolved)],
        )

    exact, markers, locators = on_page(scenario)

    assert exact == 0
    assert markers == ["new-article", "new-article"]
    assert locators == [0, 0]


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ({"role": "textbox"}, "email"),
        ({"role": "button", "name": "Post Comment"}, "post-comment"),
        ({"role": "button", "name": "C++ / \"quoted\" >> 'names'"}, "odd-name"),
        ({"role": "button", "name": "Caf\u00e9 \u2713"}, "cafe"),
        ({"role": "button", "name": "\U0001f525 Hot deals"}, "hot-deals"),
        ({"role": "link", "name": "Saved"}, "saved"),
        ({"label": "Email"}, "email"),
        ({"placeholder": "you@example.test"}, "email"),
        ({"testid": "count"}, "count"),
        ({"css": "#email"}, "email"),
        ({"css": "button", "scope": {"css": "header.banner"}}, "banner-favorite"),
    ],
    ids=lambda part: next(iter(part)) if isinstance(part, dict) else part,
)
def test_each_locator_kind_resolves(locator: dict[str, Any], expected: str) -> None:
    async def scenario(page: Page) -> str | None:
        return await marker(await resolve(page, target(locator), "assertion"))

    assert on_page(scenario) == expected


def test_an_element_rendered_twice_resolves_inside_its_scope() -> None:
    scoped = target(
        {
            "role": "button",
            "name": "Favorite Article",
            "scope": {"css": "header.banner"},
        }
    )
    unscoped = target({"role": "button", "name": "Favorite Article"})

    async def scenario(page: Page) -> tuple[str | None, Resolved | Absent | Unresolved]:
        return (
            await marker(await resolve(page, scoped, "assertion")),
            await resolve(page, unscoped, "assertion"),
        )

    inside, without = on_page(scenario)

    assert inside == "banner-favorite"
    assert without == Unresolved(("ambiguous",))


def test_a_scope_must_be_one_element() -> None:
    # Both buttons sit in a scope that matches twice, header and footer.
    twice = target(
        {
            "role": "button",
            "name": "Favorite Article",
            "scope": {"css": "header, footer"},
        }
    )
    gone = target({"role": "button", "scope": {"css": "nav.missing"}})

    async def scenario(page: Page) -> list[Resolved | Absent | Unresolved]:
        return [await resolve(page, each, "assertion") for each in (twice, gone)]

    assert on_page(scenario) == [Unresolved(("no scope",)), Unresolved(("no scope",))]


def test_a_covered_element_resolves_for_an_assertion_not_an_action() -> None:
    pay = target({"role": "button", "name": "Pay"})

    async def scenario(
        page: Page,
    ) -> tuple[bool, str | None, Resolved | Absent | Unresolved]:
        # The control: the button is visible, only covered.
        visible = await page.get_by_role("button", name="Pay").is_visible()
        return (
            visible,
            await marker(await resolve(page, pay, "assertion")),
            await resolve(page, pay, "action"),
        )

    visible, for_assertion, for_action = on_page(scenario)

    assert visible
    assert for_assertion == "pay"
    assert for_action == Unresolved(("not actionable",))


@pytest.mark.parametrize(
    "name", ["Publish", "Archive"], ids=["disabled", "aria-disabled"]
)
def test_a_disabled_button_is_not_actionable(name: str) -> None:
    button = target({"role": "button", "name": name})

    async def scenario(page: Page) -> tuple[Resolved | Absent | Unresolved, str | None]:
        return (
            await resolve(page, button, "action"),
            await marker(await resolve(page, button, "assertion")),
        )

    for_action, for_assertion = on_page(scenario)

    assert for_action == Unresolved(("not actionable",))
    assert for_assertion == name.lower()


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Post Comment", "post-comment"), ("Below the fold", "below")],
    ids=["in view", "below the fold"],
)
def test_an_enabled_uncovered_element_is_actionable(name: str, expected: str) -> None:
    # Below the fold, the element is scrolled into view first, as a click
    # would scroll it.
    button = target({"role": "button", "name": name})

    async def scenario(page: Page) -> str | None:
        return await marker(await resolve(page, button, "action"))

    assert on_page(scenario) == expected


def test_zero_matches_inside_a_negative_checks_scope_is_a_result() -> None:
    absent = target({"role": "alert", "scope": {"css": "section.empty"}})
    scope_gone = target({"role": "alert", "scope": {"css": "section.gone"}})
    present = target({"role": "button", "name": "Remove"})

    async def scenario(page: Page) -> tuple[Any, ...]:
        return (
            await resolve(page, absent, "negative_check"),
            await resolve(page, scope_gone, "negative_check"),
            await marker(await resolve(page, present, "negative_check")),
        )

    zero, gone, found = on_page(scenario)

    assert zero == Absent(0)
    assert gone == Unresolved(("no scope",))
    assert found == "remove"


def test_absence_must_be_unanimous() -> None:
    # The button was relabeled from "Delete" to "Remove": its role and name
    # find nothing, but its structural locator still finds it, so a
    # not_visible check must see it rather than pass (ADR-0025, "resolving a
    # target per use").
    relabeled = target({"role": "button", "name": "Delete"}, {"css": "button.delete"})
    # Nothing and two things: absence can't be established.
    unclear = target(
        {"css": "button.missing"}, {"role": "button", "name": "Favorite Article"}
    )
    # A scope that is gone says nothing, so another locator's empty scope
    # still establishes absence.
    partly_gone = target(
        {"css": "p", "scope": {"css": "nav.missing"}},
        {"role": "alert", "scope": {"css": "section.empty"}},
    )

    async def scenario(page: Page) -> tuple[Any, ...]:
        found = await resolve(page, relabeled, "negative_check")
        return (
            found.locator_index if isinstance(found, Resolved) else found,
            await marker(found),
            await resolve(page, unclear, "negative_check"),
            await resolve(page, partly_gone, "negative_check"),
        )

    locator, found, unclear_result, partly_gone_result = on_page(scenario)

    assert (locator, found) == (1, "remove")
    assert unclear_result == Unresolved(("no match", "ambiguous"))
    assert partly_gone_result == Absent(1)


def test_resolution_reports_the_locator_that_resolved() -> None:
    fallback = target({"testid": "missing"}, {"label": "Email"}, {"css": "#email"})

    async def scenario(page: Page) -> tuple[int, str | None]:
        found = await resolve(page, fallback, "action")
        assert isinstance(found, Resolved)
        return found.locator_index, await marker(found)

    assert on_page(scenario) == (1, "email")


def test_an_unresolved_target_reports_why_each_locator_missed() -> None:
    missing = target(
        {"testid": "missing"},
        {"role": "button", "name": "Favorite Article"},
        {"css": "button", "scope": {"css": "nav.missing"}},
        {"role": "button", "name": "Pay"},
    )

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, missing, "action")

    assert on_page(scenario) == Unresolved(
        ("no match", "ambiguous", "no scope", "not actionable")
    )


def test_text_matches_a_button_styled_uppercase() -> None:
    post_comment = target({"role": "button", "name": "Post Comment"})

    async def scenario(page: Page) -> str:
        found = await resolve(page, post_comment, "assertion")
        assert isinstance(found, Resolved)
        return await rendered_text(found.element)

    rendered = on_page(scenario)

    # The control: CSS text-transform shows in the rendered text.
    assert rendered == "POST COMMENT"
    assert has_text(rendered, "Post Comment")
    # A case-dependent pattern stays case-sensitive.
    assert not has_pattern(rendered, "Post Comment")
    assert has_pattern(rendered, "(?i)^post comment$")


@pytest.mark.parametrize(
    "css", ["xpath=//input", "text=Pay", "//input", "internal:control=enter-frame"]
)
def test_a_css_value_is_only_ever_css(css: str) -> None:
    # Resolution sends a css value as css=<value>, so another engine's syntax
    # is a parse error, never a match (ADR-0025, 2026-10-02 amendment).
    other_engine = target({"css": css})

    async def scenario(page: Page) -> None:
        await resolve(page, other_engine, "assertion")

    with pytest.raises(Error, match="while parsing css selector"):
        on_page(scenario)


def test_the_roles_are_playwrights() -> None:
    # A locator's role goes straight to get_by_role, so the format's list is
    # Playwright's (https://playwright.dev/python/docs/api/class-page#page-get-by-role).
    playwrights = typing.get_type_hints(Page.get_by_role)["role"]

    assert set(typing.get_args(AriaRole)) == set(typing.get_args(playwrights))
