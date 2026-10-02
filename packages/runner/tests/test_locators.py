"""Finding a target's element for the use it is put to (DATA_MODEL §7,
Resolution per use; ADR-0025 and its amendments). These tests load fixture
pages into real Chromium through the browser session."""

import asyncio
import json
import re
import typing
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import playwright
import pytest
from aqa_core.compiled import PLAYWRIGHT_PSEUDO_CLASSES, AriaRole, ByCss, Target
from aqa_core.text import has_pattern, has_text
from aqa_runner.browser_session import open_browser_session
from aqa_runner.locators import (
    Absent,
    Resolved,
    Unresolved,
    rendered_text,
    resolve,
)
from playwright.async_api import ElementHandle, Error, Page, async_playwright
from playwright.async_api import Locator as PlaywrightLocator
from pydantic import ValidationError

from packages.runner.tests.egress_fixtures import egress_proxy

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


def on_page[T](scenario: Callable[[Page], Awaitable[T]], html: str = PAGE) -> T:
    """`scenario`'s result on a fixture page, in a fresh browser session."""

    async def run() -> T:
        async with (
            async_playwright() as playwright,
            egress_proxy() as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            await session.page.set_content(html)
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


def custom_css_names() -> list[str]:
    """The pseudo-class names Playwright's css engine handles itself, as the
    installed driver's selector parser lists them (customCSSNames)."""
    bundle = Path(playwright.__file__).parent / "driver/package/lib/coreBundle.js"
    found = re.search(
        r"customCSSNames = /\* @__PURE__ \*/ new Set\((\[[^\]]*\])\)",
        bundle.read_text(),
    )
    assert found is not None, (
        "Playwright's driver no longer writes customCSSNames as this test reads "
        "it: update the pattern, then check the list"
    )
    names: list[str] = json.loads(found[1])
    return names


def test_the_css_extensions_are_playwrights() -> None:
    # Playwright's list holds the standard pseudo-classes it parses itself,
    # such as :is(), and its own. Its own are the names the browser can't
    # read, which the format refuses (ADR-0025, 2026-10-02 amendment,
    # "generating locators").
    names = custom_css_names()

    async def scenario(page: Page) -> list[str]:
        unread: list[str] = await page.evaluate(
            """(names) => names.filter((name) => [`:${name}`, `:${name}(*)`].every(
                (selector) => {
                    try { document.querySelector(selector); return false; }
                    catch { return true; }
                }))""",
            names,
        )
        return unread

    unread = on_page(scenario)

    assert set(unread) == PLAYWRIGHT_PSEUDO_CLASSES
    assert set(names) - set(unread) == {"not", "is", "where", "has", "scope"}


CSS_PAGE = """<form>
<button class="pay" title=":has-text(Pay)" data-note="a /* b">Pay</button>
<button class="a:visible text visible">Buy</button>
</form><ul><li class="tag">x</li><li>y</li></ul>"""


@pytest.mark.parametrize(
    "css",
    [
        '[title=":has-text(Pay)"]',
        "button /* :visible */",
        ".a\\:visible",
        ".text.visible",
        '[data-note="a /* b"]:not(.x)',
        "button:not(.pay)",
        "li:nth-child(2)",
        ":scope > body",
        "button /* :visible",
    ],
)
def test_a_css_value_the_format_accepts_means_what_css_means(css: str) -> None:
    # Playwright's css engine and the browser's own querySelectorAll agree
    # on it, so no pseudo-class of Playwright's own got through.
    accepted = target({"css": css})

    async def scenario(page: Page) -> tuple[int, int]:
        native: int = await page.evaluate(
            "(css) => document.querySelectorAll(css).length", css
        )
        return await page.locator(f"css={css}").count(), native

    playwrights, browsers = on_page(scenario, CSS_PAGE)

    assert accepted.locators[0] == ByCss(css=css)
    assert browsers > 0
    assert playwrights == browsers


@pytest.mark.parametrize(
    "css",
    ['button:HAS-TEXT("Pay")', 'button:has\\2d text("Pay")', "button:/**/visible"],
)
def test_a_refused_css_value_is_one_only_playwright_reads(css: str) -> None:
    # The control: Playwright reads each spelling as its own pseudo-class,
    # and the browser can't read it at all.
    with pytest.raises(ValidationError, match="Playwright's own pseudo-class"):
        target({"css": css})

    async def scenario(page: Page) -> tuple[bool, bool]:
        native: bool = await page.evaluate(
            """(css) => {
                try { document.querySelectorAll(css); return true; }
                catch { return false; }
            }""",
            css,
        )
        return await page.locator(f"css={css}").count() > 0, native

    assert on_page(scenario, CSS_PAGE) == (True, False)


# Layouts where a click works, each with one button marked "b": the hit test
# must find it there too.
ACTIONABLE_LAYOUTS = {
    "smooth scrolling": (
        "<style>html { scroll-behavior: smooth; }</style>"
        '<div style="height: 3000px"></div><button data-is="b">Go</button>'
    ),
    "clipped by a scrolled box": (
        '<div style="height: 200px; overflow: auto"><div style="height: 300px"></div>'
        '<button data-is="b">Go</button></div><div style="height: 1000px"></div>'
    ),
    "wrapped onto two lines": (
        # The link's two pieces sit at opposite ends of their lines, so the
        # center of its bounding box lands on neither.
        '<p style="width: 200px; font: 16px/30px monospace">xxxxxxxxxxxxxxxxx '
        '<a href="#" data-is="b">yy zz</a></p>'
    ),
    "in an open shadow root": (
        '<div id="host"></div><script>document.getElementById("host")'
        '.attachShadow({mode: "open"}).innerHTML = '
        "'<button data-is=\"b\">Go</button>';</script>"
    ),
    "right of the viewport": (
        '<div style="display: flex"><div style="flex: none; width: 3000px"></div>'
        '<button data-is="b">Go</button></div>'
    ),
    "hit on a child": (
        '<button data-is="b"><span style="display: block; padding: 20px">Go</span></button>'
    ),
}


@pytest.mark.parametrize(
    "html", ACTIONABLE_LAYOUTS.values(), ids=ACTIONABLE_LAYOUTS.keys()
)
def test_an_element_a_click_reaches_is_actionable(html: str) -> None:
    go = target({"role": "button", "name": "Go"}, {"role": "link"})

    async def scenario(page: Page) -> str | None:
        return await marker(await resolve(page, go, "action"))

    assert on_page(scenario, html) == "b"


def test_a_hidden_element_with_a_visible_child_is_not_actionable() -> None:
    # The hit test lands on the visible child, so only the visibility check
    # refuses the hidden button itself.
    html = (
        '<button data-is="b" style="visibility: hidden">'
        '<span style="visibility: visible">Ghost</span></button>'
    )

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, target({"css": "button"}), "action")

    assert on_page(scenario, html) == Unresolved(("not actionable",))


def test_an_element_that_leaves_during_the_checks_is_not_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A page that re-renders between two of the checks makes Playwright raise
    # for the detached element: that is drift, not a broken script.
    seen = ElementHandle.is_visible

    async def visible_then_removed(self: ElementHandle) -> bool:
        visible = await seen(self)
        await self.evaluate("(element) => element.remove()")
        return visible

    monkeypatch.setattr(ElementHandle, "is_visible", visible_then_removed)

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(
            page, target({"role": "button", "name": "Post Comment"}), "action"
        )

    assert on_page(scenario) == Unresolved(("not actionable",))


def test_a_page_that_breaks_the_hit_test_makes_it_not_actionable() -> None:
    # The hit test runs in the page's own world, so it is only the page's word.
    html = (
        "<script>Element.prototype.getClientRects = () => { throw new Error('no'); };"
        "Element.prototype.getBoundingClientRect = Element.prototype.getClientRects;"
        '</script><button data-is="b">Go</button>'
    )

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, target({"role": "button", "name": "Go"}), "action")

    assert on_page(scenario, html) == Unresolved(("not actionable",))


def test_a_page_closed_during_the_checks_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closing: list[Page] = []

    async def closes_the_page(self: ElementHandle) -> bool:
        await closing[0].close()
        return await self.is_enabled()

    monkeypatch.setattr(ElementHandle, "is_visible", closes_the_page)

    async def scenario(page: Page) -> None:
        closing.append(page)
        await resolve(
            page, target({"role": "button", "name": "Post Comment"}), "action"
        )

    with pytest.raises(Error):
        on_page(scenario)


# Near-duplicates and glyphs in odd places, each element marked.
NAMES_PAGE = r"""<!doctype html><style>
  .bmp::before { content: "\e900"; }
  .after::after { content: "\a0\f218"; }
</style>
<button data-is="pay">Pay</button>
<button data-is="pay-later">Pay later</button>
<button data-is="spaced">Post Comment</button>
<button data-is="joined">PostComment</button>
<button data-is="mid-word">Sa<i class="bmp"></i>ve</button>
<button data-is="after-space">Keep <i class="bmp"></i>draft</button>
<a href="#" class="after" data-is="next">Next</a>
<label>Name <input data-is="name"></label>
<label>Username <input data-is="username"></label>
<input placeholder="Search" data-is="search">
<input placeholder="Search orders" data-is="search-orders">
"""


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        # Exact, not a substring: "Pay later" contains "Pay".
        ({"role": "button", "name": "Pay"}, "pay"),
        ({"role": "button", "name": "Post Comment"}, "spaced"),
        ({"role": "button", "name": "PostComment"}, "joined"),
        ({"label": "Name"}, "name"),
        ({"placeholder": "Search"}, "search"),
        # Glyphs inside a word, right after a space, and after the name.
        ({"role": "button", "name": "Save"}, "mid-word"),
        ({"role": "button", "name": "Keep draft"}, "after-space"),
        ({"role": "link", "name": "Next"}, "next"),
    ],
    ids=lambda part: (
        str(part.get("name") or next(iter(part.values())))
        if isinstance(part, dict)
        else None
    ),
)
def test_names_match_exactly_wherever_the_glyphs_sit(
    locator: dict[str, Any], expected: str
) -> None:
    async def scenario(page: Page) -> str | None:
        return await marker(await resolve(page, target(locator), "assertion"))

    assert on_page(scenario, NAMES_PAGE) == expected


def test_a_scope_may_have_a_scope() -> None:
    html = (
        '<div class="a"><div class="s"><button data-is="a-go">Go</button></div></div>'
        '<div class="b"><div class="s"><button data-is="b-go">Go</button></div></div>'
    )
    nested = target(
        {"role": "button", "name": "Go", "scope": {"css": ".s", "scope": {"css": ".a"}}}
    )
    twice = target({"role": "button", "name": "Go", "scope": {"css": ".s"}})
    outer_gone = target(
        {
            "role": "button",
            "name": "Go",
            "scope": {"css": ".s", "scope": {"css": ".gone"}},
        }
    )

    async def scenario(page: Page) -> tuple[Any, ...]:
        return (
            await marker(await resolve(page, nested, "assertion")),
            await resolve(page, twice, "assertion"),
            await resolve(page, outer_gone, "assertion"),
        )

    assert on_page(scenario, html) == (
        "a-go",
        Unresolved(("no scope",)),
        Unresolved(("no scope",)),
    )


def test_a_negative_check_counts_what_is_on_screen() -> None:
    # A visible button under aria-hidden is still on screen, so its role
    # locator finds it and not_visible fails. A display: none button isn't,
    # so it is absent. A hidden button with the target's old name can't stand
    # in for the visible, relabeled one a fallback finds (ADR-0025,
    # "resolving a target per use").
    html = (
        '<div aria-hidden="true"><button data-is="b">Delete</button></div>'
        '<button style="display: none">Archive</button>'
        '<button id="del" data-is="relabeled">Remove</button>'
        '<div style="display: none"><button>Discard</button></div>'
    )
    decoy = target({"role": "button", "name": "Discard"}, {"css": "#del"})

    async def scenario(page: Page) -> tuple[Any, ...]:
        found = await resolve(page, decoy, "negative_check")
        return (
            await marker(
                await resolve(
                    page, target({"role": "button", "name": "Delete"}), "negative_check"
                )
            ),
            await resolve(
                page, target({"role": "button", "name": "Archive"}), "negative_check"
            ),
            found.locator_index if isinstance(found, Resolved) else found,
            await marker(found),
        )

    assert on_page(scenario, html) == ("b", Absent(0), 1, "relabeled")


def test_only_a_negative_check_is_ever_absent() -> None:
    nothing = target({"role": "alert"})
    pay = target({"role": "button", "name": "Pay"})
    publish = target({"role": "button", "name": "Publish"})

    async def scenario(page: Page) -> tuple[Any, ...]:
        return (
            # Unscoped, the page is the locator's scope.
            await resolve(page, nothing, "negative_check"),
            await resolve(page, nothing, "assertion"),
            await resolve(page, nothing, "action"),
            # A negative check needs the element, not that it is actionable.
            await marker(await resolve(page, pay, "negative_check")),
            await marker(await resolve(page, publish, "negative_check")),
        )

    assert on_page(scenario) == (
        Absent(0),
        Unresolved(("no match",)),
        Unresolved(("no match",)),
        "pay",
        "publish",
    )


SMUGGLED = "*/ >> internal:control=enter-frame >> css=input /*"


def test_a_scope_that_leaves_a_quote_open_is_refused() -> None:
    # Its open quote would swallow the separator before the label, which
    # would then chain into the frame (ADR-0025, "reading a compiled script").
    with pytest.raises(ValidationError, match="leaves a quote or escape open"):
        target({"label": SMUGGLED, "scope": {"css": 'iframe /* "'}})


@pytest.mark.parametrize(
    "scope",
    ["body", 'iframe /* \\" */', 'iframe /* " " */', "iframe /* ` ` */"],
    ids=["plain", "escaped quote", "closed quotes", "closed backticks"],
)
def test_a_label_under_a_closed_scope_is_only_a_label(scope: str) -> None:
    # Each scope closes what it opens by Playwright's count, so the label
    # after it is only ever a label, never a chain into the frame.
    html = "<iframe srcdoc='<label>Secret <input type=password></label>'></iframe>"
    closed = target({"label": SMUGGLED, "scope": {"css": scope}})

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(page, closed, "assertion")

    assert on_page(scenario, html) == Unresolved(("no match",))


def test_a_page_that_changes_between_count_and_lookup_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The count says one, but by the lookup a second has appeared: the second
    # look decides, so nothing unique is claimed.
    async def one(_locator: PlaywrightLocator) -> int:
        return 1

    monkeypatch.setattr(PlaywrightLocator, "count", one)

    async def scenario(page: Page) -> Resolved | Absent | Unresolved:
        return await resolve(
            page, target({"role": "button", "name": "Favorite Article"}), "assertion"
        )

    assert on_page(scenario) == Unresolved(("ambiguous",))


def test_what_javascript_trims_is_refused_at_either_end_of_a_css_value() -> None:
    # Playwright trims a selector part before its CSS tokenizer reads it
    # (ADR-0025, "generating locators"). The control: trimmed, a no-break
    # space after :visible leaves Playwright's own pseudo-class, which the
    # browser can't parse.
    async def scenario(page: Page) -> tuple[list[str], int, bool]:
        trimmed: list[str] = await page.evaluate(
            """() => {
                const found = [];
                for (let code = 0; code <= 0x10ffff; code++) {
                    if (code >= 0xd800 && code <= 0xdfff) continue;
                    const char = String.fromCodePoint(code);
                    if ((char + "a" + char).trim() === "a") found.push(char);
                }
                return found;
            }"""
        )
        css = "button.pay:visible\u00a0"
        native: bool = await page.evaluate(
            """(css) => {
                try { document.querySelectorAll(css); return true; }
                catch { return false; }
            }""",
            css,
        )
        return trimmed, await page.locator(f"css={css}").count(), native

    trimmed, playwrights, browsers = on_page(scenario, CSS_PAGE)

    assert (playwrights, browsers) == (1, False)
    assert "\u00a0" in trimmed
    for char in trimmed:
        for css in (f"button{char}", f"{char}button"):
            with pytest.raises(ValidationError, match="write it trimmed"):
                target({"css": css})
