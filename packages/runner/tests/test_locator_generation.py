"""Building a target's locators from the element a use put it to (ADR-0025,
"Locator grammar", and its 2026-10-02 amendment, "generating locators";
DATA_MODEL §7, Locators). The rules are tested on the pilot's pages
(`pilot_pages.py`) where the pilot has an example, and on fixture pages where
it has none, in real Chromium through the browser session."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import pytest
from aqa_core.compiled import (
    ByCss,
    ByLabel,
    ByPlaceholder,
    ByRole,
    ByTestId,
    Locator,
    Target,
    TextInTarget,
)
from aqa_core.text import normalize
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.locator_generation import (
    LocatorError,
    Seen,
    TargetUses,
    generate_for_action,
    generate_for_assertion,
    seen_element,
    snapshot_elements,
)
from aqa_runner.locators import Absent, Resolved, Unresolved, rendered_text, resolve
from aqa_runner.text_search import text_matches
from playwright.async_api import ElementHandle, Error, Page, async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.pilot_pages import PAGES, RENDERINGS, in_session, put, show


def ref_of(snapshot: str, role: str, name: str | None = None, nth: int = 0) -> str:
    """The ref of the `nth` element, in page order, that the snapshot gives
    `role` and, if given, a name that normalizes to `name`."""
    refs = [
        ref
        for ref, (each_role, each_name) in snapshot_elements(snapshot).items()
        if each_role == role and (name is None or normalize(each_name or "") == name)
    ]
    return refs[nth]


def ref_with_text(snapshot: str, role: str, text: str) -> str:
    """The ref of the element the snapshot gives `role` and, as its only
    content, `text`, such as `- paragraph [ref=e30]: Body text`."""
    found = re.search(
        rf"^ *- {role}(?: \[[^\]\n]*\])* \[ref=(e[0-9]+)\]: {re.escape(text)}$",
        snapshot,
        re.MULTILINE,
    )
    assert found is not None, f"no {role} with the text {text!r}"
    return found[1]


def test_the_headers_icon_links_get_role_and_name_without_their_glyphs() -> None:
    async def scenario(
        session: BrowserSession,
    ) -> list[tuple[str | None, tuple[Locator, ...]]]:
        await show(session, "home")
        snapshot = await session.snapshot()
        found = []
        for name in ("New Article", "Settings"):
            used = await seen_element(session, snapshot, ref_of(snapshot, "link", name))
            found.append((used.name, await generate_for_action(session.page, used)))
        return found

    [(new_article, new_article_locators), (settings, settings_locators)] = in_session(
        scenario
    )

    # The control: the snapshot's names carry the icon font's glyphs.
    assert new_article is not None
    assert new_article != "New Article"
    assert settings is not None
    assert settings != "Settings"
    assert new_article_locators[0] == ByRole(role="link", name="New Article")
    assert settings_locators[0] == ByRole(role="link", name="Settings")


def test_a_container_is_never_found_by_its_text() -> None:
    # Headings, paragraphs and list items take their names from their text,
    # so only controls get a name, as an action's target or an assertion's
    # (ADR-0025: never text-only locators for containers).
    async def scenario(session: BrowserSession) -> list[tuple[Locator, ...]]:
        await show(session, "article-signed-out")
        snapshot = await session.snapshot()
        found = []
        for ref in (
            ref_of(snapshot, "heading", "Testing without flakes"),
            ref_with_text(snapshot, "paragraph", "Deterministic data helped us most."),
            ref_with_text(snapshot, "listitem", "testing"),
        ):
            used = await seen_element(session, snapshot, ref)
            found.append(await generate_for_action(session.page, used))
            found.append(
                await generate_for_assertion(session.page, used, checks_text=False)
            )
        return found

    for locators in in_session(scenario):
        assert locators
        assert not [each for each in locators if isinstance(each, ByRole) and each.name]


FORM = """<form class="signup">
  <label>Email <input id="email" class="field" name="email" type="email"
    placeholder="you@example.test" data-testid="email-field"></label>
  <input class="field" name="nickname" placeholder="Nickname">
</form>"""


def test_an_action_targets_locators_come_in_the_grammars_order() -> None:
    # Role and name, then label, placeholder, test ID and stable id, then
    # structure (ADR-0025). Conduit has no labels or test IDs, so the order
    # is shown on a fixture form, and on the pilot's login form.
    async def scenario(session: BrowserSession) -> list[tuple[Locator, ...]]:
        found = []
        await put(session, FORM)
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_of(snapshot, "textbox", "Email")
        )
        found.append(await generate_for_action(session.page, used))
        await show(session, "login")
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_of(snapshot, "textbox", "Email")
        )
        found.append(await generate_for_action(session.page, used))
        return found

    fixture, login = in_session(scenario)

    assert fixture == (
        ByRole(role="textbox", name="Email"),
        ByLabel(label="Email"),
        ByPlaceholder(placeholder="you@example.test"),
        ByTestId(testid="email-field"),
        ByCss(css="#email"),
        ByCss(css="input[name=email]"),
    )
    assert login == (
        ByRole(role="textbox", name="Email"),
        ByPlaceholder(placeholder="Email"),
        ByCss(css="input[name=email]"),
    )


def test_a_structural_locator_may_name_a_custom_element_ancestor() -> None:
    # The favorite button's own classes don't tell it from the follow button
    # beside it, but its custom element does (ADR-0025:
    # app-favorite-button).
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await show(session, "article")
        snapshot = await session.snapshot()
        ref = ref_of(snapshot, "button", "Favorite Article (0)")
        used = await seen_element(session, snapshot, ref)
        return await generate_for_action(session.page, used)

    structural = [each for each in in_session(scenario) if isinstance(each, ByCss)]

    assert structural == [
        ByCss(css="app-favorite-button button.btn", scope=ByCss(css="div.banner"))
    ]


TOKENS = """<button id="r0x9k2m4" class="sc-AxjAm jsx-123456 active pay">Pay</button>
<x-a1b2c3d4e5 class="css-1x2y3z ng-star-inserted Button_root__x7Kd2 open">
  <button class="ng-untouched save">Go</button>
</x-a1b2c3d4e5>
<button class="save">Save</button>"""


def test_generated_and_state_tokens_are_never_used() -> None:
    # CSS-in-JS and hash-like classes, Angular's runtime classes, a
    # generated id, a generated custom-element tag and state classes: only
    # the stable classes are used, and "Go" can't be told from "Save" by
    # anything stable but its name.
    async def scenario(session: BrowserSession) -> list[tuple[Locator, ...]]:
        await put(session, TOKENS)
        snapshot = await session.snapshot()
        found = []
        for name in ("Pay", "Go"):
            used = await seen_element(
                session, snapshot, ref_of(snapshot, "button", name)
            )
            found.append(await generate_for_action(session.page, used))
        return found

    assert in_session(scenario) == [
        (ByRole(role="button", name="Pay"), ByCss(css="button.pay")),
        (ByRole(role="button", name="Go"),),
    ]


def test_an_element_only_position_tells_apart_has_no_locator() -> None:
    # Never positional (ADR-0025): the second of two identical buttons can't
    # be found, so generating fails by name.
    html = "<ul><li><button>Edit</button></li><li><button>Edit</button></li></ul>"

    async def scenario(session: BrowserSession) -> None:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_of(snapshot, "button", "Edit", 1)
        )
        await generate_for_action(session.page, used)

    with pytest.raises(LocatorError, match="finds the button element alone"):
        in_session(scenario)


SECTIONS = """<section id="cart" class="cart"><div class="line">
  <button class="remove">Remove</button></div></section>
<section class="wishlist"><div class="line">
  <button class="remove">Remove</button></div></section>"""


def test_a_scope_is_the_nearest_ancestor_unique_on_the_page() -> None:
    # div.line is nearer but on the page twice, so it can't pick out a place;
    # the section's stable id can.
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, SECTIONS)
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_of(snapshot, "button", "Remove")
        )
        return await generate_for_action(session.page, used)

    assert in_session(scenario) == (
        ByRole(role="button", name="Remove", scope=ByCss(css="#cart")),
        ByCss(css="button.remove", scope=ByCss(css="#cart")),
    )


def test_a_locator_that_finds_another_element_is_dropped() -> None:
    # The page lies about the button's id, which it can: its facts are read
    # in its own world. Resolution finds the decoy, so #decoy is dropped.
    html = """<script>
      const real = Element.prototype.getAttribute;
      Element.prototype.getAttribute = function (name) {
        return name === "id" && this.classList.contains("mine")
          ? "decoy" : real.call(this, name);
      };
    </script>
    <button id="decoy">Other</button><button class="mine">Mine</button>"""

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Mine"))
        return await generate_for_action(session.page, used)

    assert in_session(scenario) == (
        ByRole(role="button", name="Mine"),
        ByCss(css="button.mine"),
    )


def test_the_snapshot_gives_each_refs_role_and_name() -> None:
    # A name with a colon makes Playwright quote the line's key for YAML, and
    # its quotes are written as JSON inside it.
    html = """<h2>Plans</h2><button>Note: "quoted" it's</button>
    <a href="#">Next</a><div style="display: block">Plain</div>"""

    async def scenario(session: BrowserSession) -> dict[str, tuple[str, str | None]]:
        await put(session, html)
        return snapshot_elements(await session.snapshot())

    assert list(in_session(scenario).values()) == [
        ("generic", None),
        ("heading", "Plans"),
        ("button", 'Note: "quoted" it\'s'),
        ("link", "Next"),
        ("generic", None),
    ]


def test_the_banner_date_has_a_ref_only_when_styled() -> None:
    # The control for the generator taking an element: whether the snapshot
    # gives an element a ref depends on its styling, so the pages are styled
    # as the app styles them (LAB_NOTES, 2026-10-02).
    date = re.compile(r"- generic \[ref=e[0-9]+\]: January 11, 2026$", re.MULTILINE)

    async def scenario(session: BrowserSession) -> list[bool]:
        refs = []
        for styled in (False, True):
            if styled:
                await show(session, "article")
            else:
                await put(session, (RENDERINGS / "article.html").read_text())
            refs.append(date.search(await session.snapshot()) is not None)
        return refs

    assert in_session(scenario) == [False, True]


GARBLED = {
    "a tag of the wrong type": (
        "Object.defineProperty(Element.prototype, 'localName', { get: () => 7 });",
        "described the element as no element is",
    ),
    "too many classes": (
        (
            "Object.defineProperty(Element.prototype, 'classList', "
            "{ get: () => Array.from({ length: 300 }, (_, each) => 'c' + each) });"
        ),
        "described the element as no element is",
    ),
    "facts of the wrong type": (
        "Element.prototype.getAttribute = function () { return 7; };",
        "described the element as no element is",
    ),
    "a reading that throws": (
        "DOMTokenList.prototype[Symbol.iterator] = () => { throw new Error('no'); };",
        "scripts broke the reading",
    ),
}


@pytest.mark.parametrize(("script", "message"), GARBLED.values(), ids=GARBLED.keys())
def test_a_page_that_garbles_the_elements_facts_gets_no_locator(
    script: str, message: str
) -> None:
    # The facts are read in the page's own world, where its scripts run.
    html = f"<script>{script}</script><button class='go'>Go</button>"

    async def scenario(session: BrowserSession) -> None:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        await generate_for_action(session.page, used)

    with pytest.raises(LocatorError, match=message):
        in_session(scenario)


@pytest.mark.parametrize(
    ("method", "when"),
    [("evaluate", ""), ("query_selector", "=hold:"), ("query_selector", "=is:")],
    ids=["reading the facts", "holding the element", "judging a candidate"],
)
def test_a_page_closed_during_generating_still_raises(
    monkeypatch: pytest.MonkeyPatch, method: str, when: str
) -> None:
    # Only the page's own doing becomes LocatorError; a closed page is not,
    # whenever it closes.
    real = getattr(ElementHandle, method)
    closing: list[Page] = []

    async def closes_the_page(self: ElementHandle, *args: Any) -> Any:
        if when in str(args[0]):
            await closing[0].close()
        return await real(self, *args)

    async def scenario(session: BrowserSession) -> None:
        closing.append(session.page)
        await put(session, "<button class='go'>Go</button>")
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        monkeypatch.setattr(ElementHandle, method, closes_the_page)
        await generate_for_action(session.page, used)

    with pytest.raises(Error):
        in_session(scenario)


def test_the_page_cannot_fake_which_element_a_locator_found() -> None:
    # The page lies about the button's id, says every element carries the
    # button's mark, and rewrites how arrays iterate, so a pair of two
    # elements reads as the first one twice. Comparing in the page's own world
    # would take the decoy for the button; Playwright's utility world doesn't.
    html = """<script>
      const real = Element.prototype.getAttribute;
      Element.prototype.getAttribute = function (name) {
        if (name === "data-aqa-generating") {
          return real.call(document.querySelector(".mine"), name);
        }
        return name === "id" && this.classList.contains("mine")
          ? "decoy" : real.call(this, name);
      };
      Array.prototype[Symbol.iterator] = function* () {
        if (this.length === 2 && this[0] instanceof Element) {
          yield this[0];
          yield this[0];
          return;
        }
        for (let index = 0; index < this.length; index++) yield this[index];
      };
    </script>
    <button id="decoy">Other</button><button class="mine">Mine</button>"""

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Mine"))
        return await generate_for_action(session.page, used)

    assert ByCss(css="#decoy") not in in_session(scenario)


# Two buttons, and a page that says the one used has the other's test ID. The
# facts are the page's word, so a candidate resolves to the other button.
# The page also hooks what it can see of a trial: Playwright's own marking
# events, and the hit test that runs in its world for an action.
TAMPERING = """<div id="a"><button data-testid="keep">Cancel</button></div>
<div id="b"><button data-testid="evil">Delete account</button></div>
<script>
  const keep = document.querySelector("[data-testid=keep]");
  const real = Element.prototype.getAttribute;
  Element.prototype.getAttribute = function (name) {
    return this === keep && name === "data-testid" ? "evil" : real.call(this, name);
  };
  window.seen = [];
  document.addEventListener("__playwright_mark_target__", (event) => {
    window.seen.push(event.composedPath()[0].textContent);
  }, true);
  const rects = Element.prototype.getClientRects;
  Element.prototype.getClientRects = function () {
    window.seen.push(this.textContent);
    return rects.call(this);
  };
</script>"""


def test_a_page_that_tampers_with_the_trial_gets_no_locator_for_another_element() -> (
    None
):
    # Which element a candidate found is judged in Playwright's utility
    # world, from no DOM state, so the lie about the test ID only costs that
    # candidate (#52's security review).
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, TAMPERING)
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_of(snapshot, "button", "Cancel")
        )
        return await generate_for_action(session.page, used)

    locators = in_session(scenario)

    assert ByTestId(testid="evil") not in locators
    assert locators[0] == ByRole(role="button", name="Cancel")


MOVE_INTO_TEMPLATE = (
    "(button) => document.querySelector('template').content.appendChild(button)"
)


@pytest.mark.parametrize("when", ["before", "during"])
def test_an_element_moved_into_another_document_gets_no_locator(when: str) -> None:
    # Playwright can't hold it in its own world any more, before the trial
    # or from inside the trial's first hit test, and a check that waited for
    # it would wait forever.
    hook = (
        "const rects = Element.prototype.getClientRects;"
        "Element.prototype.getClientRects = function () {"
        "  const result = rects.call(this);"
        "  document.querySelector('template').content.appendChild(this);"
        "  return result; };"
    )
    html = "<button class='go'>Go</button><template></template>" + (
        f"<script>{hook}</script>" if when == "during" else ""
    )

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        if when == "before":
            await used.element.evaluate(MOVE_INTO_TEMPLATE)
        return await generate_for_action(session.page, used)

    with pytest.raises(LocatorError):
        in_session(scenario)


def test_generating_without_the_identity_engine_says_so() -> None:
    async def scenario() -> tuple[Locator, ...]:
        async with (
            async_playwright() as playwright,
            egress_proxy() as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            await put(session, "<button>Go</button>")
            snapshot = await session.snapshot()
            used = await seen_element(
                session, snapshot, ref_of(snapshot, "button", "Go")
            )
            return await generate_for_action(session.page, used)

    with pytest.raises(RuntimeError, match="register_identity_engine"):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("css", "used"),
    [
        ("pay", True),
        ("year-20240", True),
        ("ab12", True),
        ("ab12c", False),
        ("Button_root__x7Kd2", False),
        ("css-pay", False),
        ("sc-pay", False),
        ("jsx-pay", False),
        ("emotion-pay", False),
        ("svelte-pay", False),
        ("ng-pay", False),
        ("active", False),
        ("1pay", False),
        ("a\\:b", False),
    ],
)
def test_what_counts_as_a_stable_class(css: str, used: bool) -> None:
    # A plain identifier, with no generated prefix, no part of five or more
    # characters that mixes letters and digits, and no state.
    html = f"<button class='{css}'>Go</button><button>Stop</button>"

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        seen = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        return await generate_for_action(session.page, seen)

    structural = [each for each in in_session(scenario) if isinstance(each, ByCss)]

    assert bool(structural) is used


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # A bare tag names every element of its kind, so it is no scope.
        (
            "<article><p class='x'>One</p></article><aside><p class='x'>Two</p></aside>",
            None,
        ),
        # A custom element's tag is one.
        (
            "<app-cart><p class='x'>One</p></app-cart><app-list><p class='x'>Two</p></app-list>",
            ByCss(css="p.x", scope=ByCss(css="app-cart")),
        ),
    ],
    ids=["bare tag", "custom tag"],
)
def test_a_scope_is_never_a_bare_standard_tag(
    html: str, expected: Locator | None
) -> None:
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_with_text(snapshot, "paragraph", "One")
        )
        return await generate_for_action(session.page, used)

    if expected is None:
        with pytest.raises(LocatorError):
            in_session(scenario)
    else:
        assert in_session(scenario) == (expected,)


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            "<input type='email' class='f' placeholder=''><input type='text' class='f'>",
            ByCss(css="input[type=email]"),
        ),
        (
            "<x-chip class='chip'>One</x-chip><span class='chip'>Two</span>",
            ByCss(css="x-chip"),
        ),
    ],
    ids=["a type attribute", "a custom tag first"],
)
def test_structure_takes_the_most_particular_form(html: str, expected: Locator) -> None:
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        first = await session.page.query_selector("body > *")
        assert first is not None
        return await generate_for_action(session.page, Seen(first))

    assert in_session(scenario)[-1] == expected


@pytest.mark.parametrize(("depth", "found"), [(15, True), (16, False)])
def test_only_the_nearest_ancestors_are_tried_as_scopes(
    depth: int, found: bool
) -> None:
    # The section that tells the two buttons apart is the 16th ancestor, or
    # the 17th, past the ones tried.
    nest = "<div>" * depth + "<button class='x'>Go</button>" + "</div>" * depth
    html = f"<section class='a'>{nest}</section><section class='b'>{nest}</section>"

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        return await generate_for_action(session.page, used)

    if found:
        assert ByCss(css="button.x", scope=ByCss(css="section.a")) in in_session(
            scenario
        )
    else:
        with pytest.raises(LocatorError):
            in_session(scenario)


def test_the_work_one_element_costs_is_bounded() -> None:
    # Sixteen nested ancestors, each unique on the page by any of its sixteen
    # stable classes, around two identical elements: every scope is worth
    # trying and none tells them apart, so generating stops at its budget of
    # round trips rather than try all 4,000 or so.
    letters = "abcdefghijklmnop"
    opening = "".join(
        f"<div class='{' '.join(f'l{level}{each}' for each in letters)}'>"
        for level in range(16)
    )
    inner = " ".join(f"c{each}" for each in letters)
    html = opening + f"<b class='{inner}'>x</b><b class='{inner}'>x</b>" + "</div>" * 16

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        element = await session.page.query_selector("b")
        assert element is not None
        return await generate_for_action(session.page, Seen(element))

    with pytest.raises(LocatorError, match="within 400 tries"):
        in_session(scenario)


@pytest.mark.parametrize(
    "label",
    ['"Save \\uD800"', '"x".repeat(2000)'],
    ids=["a lone surrogate", "too long"],
)
def test_a_name_no_locator_can_carry_is_no_name(label: str) -> None:
    # A lone surrogate can't be written, and the snapshot gives a name of
    # thousands of characters as none, so the button is found by structure.
    html = f"""<button class="save">x</button><button>y</button><script>
      document.querySelector(".save").setAttribute("aria-label", {label});
    </script>"""

    async def scenario(
        session: BrowserSession,
    ) -> tuple[str | None, tuple[Locator, ...]]:
        await put(session, html)
        snapshot = await session.snapshot()
        ref = ref_of(snapshot, "button", nth=0)
        used = await seen_element(session, snapshot, ref)
        return used.name, await generate_for_action(session.page, used)

    name, locators = in_session(scenario)

    assert name is None
    assert locators == (ByCss(css="button.save"),)


def test_a_page_that_navigates_mid_trial_gets_no_locator() -> None:
    # The page reloads from inside the first hit test, which destroys the
    # element's world: whatever the trial was doing then, it ends in
    # LocatorError, never a raw error.
    html = """<button class="go">Go</button><script>
      const rects = Element.prototype.getClientRects;
      Element.prototype.getClientRects = function () {
        location.reload();
        return rects.call(this);
      };
    </script>"""

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        return await generate_for_action(session.page, used)

    with pytest.raises(LocatorError):
        in_session(scenario)


def test_tries_running_out_keep_the_locators_found() -> None:
    # The button's name finds it on the first try; its structure, shared
    # with its twin under sixteen ancestors that each look worth a try,
    # spends the rest. What was found stands.
    letters = "abcdefghijklmnop"
    opening = "".join(
        f"<div class='{' '.join(f'l{level}{each}' for each in letters)}'>"
        for level in range(16)
    )
    inner = " ".join(f"c{each}" for each in letters)
    html = (
        opening
        + f"<button class='{inner}'>Go</button><button class='{inner}'>Stop</button>"
        + "</div>" * 16
    )

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        return await generate_for_action(session.page, used)

    assert in_session(scenario) == (ByRole(role="button", name="Go"),)


async def generated(
    page: Page,
    used: Seen,
    use: Literal["action", "assertion"],
    check: TextInTarget | None,
) -> tuple[Locator, ...]:
    """The locators for `use`, as its caller asks for them: a text check
    locates its target by no name."""
    if use == "action":
        return await generate_for_action(page, used)
    return await generate_for_assertion(page, used, checks_text=check is not None)


def test_a_text_assertions_target_is_found_by_no_name() -> None:
    # The author link's name is what read-article's check claims, so it is
    # found by structure instead (ADR-0025).
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await show(session, "article-signed-out")
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "link", "anna"))
        return await generate_for_assertion(session.page, used, checks_text=True)

    assert in_session(scenario) == (
        ByCss(css="a.author", scope=ByCss(css="div.banner")),
    )


@pytest.mark.parametrize("checks_text", [True, False])
def test_a_name_that_shares_words_with_the_text_is_no_way_to_check_it(
    checks_text: bool,
) -> None:
    # "Save" doesn't accept "Save changes" nor the other way round, yet a
    # change to the text would change both. With nothing else to tell the
    # buttons apart, a text check gets no locator; a check of no text keeps
    # the name.
    html = """<button aria-label="Save" class="act">Save changes</button>
    <button aria-label="Cancel" class="act">Cancel changes</button>"""

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Save"))
        return await generate_for_assertion(session.page, used, checks_text=checks_text)

    if checks_text:
        with pytest.raises(LocatorError):
            in_session(scenario)
    else:
        assert in_session(scenario) == (ByRole(role="button", name="Save"),)


def test_an_assertion_targets_locators_come_in_the_grammars_order() -> None:
    # Test ID, stable id, role, then structure; no label or placeholder.
    html = """<label>Pay <input data-testid="pay" id="pay-field" class="pay"
      placeholder="Amount"></label>"""

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "textbox", "Pay"))
        return await generate_for_assertion(session.page, used, checks_text=True)

    assert in_session(scenario) == (
        ByTestId(testid="pay"),
        ByCss(css="#pay-field"),
        ByRole(role="textbox"),
        ByCss(css="input.pay"),
    )


def test_an_assertion_that_checks_no_text_keeps_role_and_name() -> None:
    # login's visible_unoccluded check claims nothing about the label, so the
    # header link keeps its name.
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await show(session, "home")
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_of(snapshot, "link", "New Article")
        )
        return await generate_for_assertion(session.page, used, checks_text=False)

    assert ByRole(role="link", name="New Article") in in_session(scenario)


def text_check(**fields: Any) -> TextInTarget:
    """A text_in_target check, as the coverage plan's would be compiled."""
    return TextInTarget.model_validate(
        {"id": "a1", "expect_index": 0, "check": "text_in_target", "target": "t"}
        | fields
    )


async def is_same(page: Page, found: ElementHandle, used: ElementHandle) -> bool:
    same: bool = await page.evaluate("([a, b]) => a === b", [found, used])
    return same


async def narrowed(element: ElementHandle, css: str) -> Seen:
    """The element under a ref'd one that the caller points at, as the
    navigator will for one the snapshot gives no ref (#53). The favorites
    count is one: an inline span inside the favorite button, whose text the
    snapshot folds into the button's (LAB_NOTES, 2026-10-02)."""
    inner = await element.query_selector(css)
    assert inner is not None, css
    return Seen(inner)


async def banner_part(
    session: BrowserSession, part: str
) -> tuple[Seen, Literal["action", "assertion"], TextInTarget | None]:
    """One of the article banner's date, author, favorite button and
    favorites count, with its use and its check, as the pilot uses it:
    read-article checks the date and author, and favorite-article clicks the
    button and checks the count."""
    signed_out = part in ("date", "author")
    await show(session, "article-signed-out" if signed_out else "article")
    snapshot = await session.snapshot()
    if signed_out:
        author = await seen_element(session, snapshot, ref_of(snapshot, "link", "anna"))
        if part == "author":
            return author, "assertion", text_check(text="anna")
        # Styled, the date is a block, so the snapshot gives it a ref.
        ref = ref_with_text(snapshot, "generic", "January 4, 2026")
        return (
            await seen_element(session, snapshot, ref),
            "assertion",
            text_check(text="January 4, 2026"),
        )
    button = await seen_element(
        session, snapshot, ref_of(snapshot, "button", "Favorite Article (0)")
    )
    if part == "favorite button":
        return button, "action", None
    count = await narrowed(button.element, "span.counter")
    return count, "assertion", text_check(pattern=r"\b0\b")


@pytest.mark.parametrize("part", ["date", "author", "favorite button", "count"])
def test_the_banners_meta_is_scoped_to_the_banner(part: str) -> None:
    # Each is rendered twice, in the banner and again under the article, so
    # each locator is scoped to the banner, and is ambiguous without it.
    async def scenario(
        session: BrowserSession,
    ) -> list[tuple[Locator, Resolved | Unresolved]]:
        used, use, check = await banner_part(session, part)
        found = []
        for locator in await generated(session.page, used, use, check):
            alone = locator.model_copy(update={"scope": None})
            target = Target(semantic="the banner's part", locators=(alone,))
            found.append((locator, await resolve(session.page, target, use)))
        return found

    found = in_session(scenario)

    assert found
    for locator, unscoped in found:
        assert locator.scope == ByCss(css="div.banner")
        assert unscoped == Unresolved(("ambiguous",))


def test_the_date_is_found_by_structure_so_a_wrong_date_fails_its_check() -> None:
    # conduit-bug-001 (bench/manifest.v1.json) renders article dates one day
    # early: its one change is the zone of article-meta's date pipe
    # (bench/apps/conduit/frontend/src/app/features/article/components/
    # article-meta.component.ts, dateZone), so it is applied here to both of
    # the clean page's article dates. The date's target still resolves, and
    # read-article's check fails rather than drifting (ADR-0025).
    bug_001 = """(dates) => dates.forEach((date) => {
        date.textContent = " January 3, 2026 ";
    })"""

    async def scenario(
        session: BrowserSession,
    ) -> tuple[tuple[Locator, ...], list[bool]]:
        used, _, check = await banner_part(session, "date")
        assert check is not None
        locators = await generate_for_assertion(session.page, used, checks_text=True)
        target = Target(semantic="the article's date in its banner", locators=locators)
        passes = []
        for change in (None, bug_001):
            if change is not None:
                await session.page.eval_on_selector_all(
                    "app-article-meta span.date", change
                )
            found = await resolve(session.page, target, "assertion")
            assert isinstance(found, Resolved), found
            passes.append(await text_matches(check, await rendered_text(found.element)))
        return locators, passes

    locators, passes = in_session(scenario)

    assert all(isinstance(each, ByCss) for each in locators)
    assert passes == [True, False]


def test_the_favorites_count_is_the_count_not_the_button() -> None:
    # #20: conduit-benign-002 moves the count out of the button, so its
    # target is the count itself. After favoriting, its check passes.
    async def scenario(
        session: BrowserSession,
    ) -> tuple[tuple[Locator, ...], bool, bool]:
        await show(session, "article-favorited")
        snapshot = await session.snapshot()
        ref = ref_of(snapshot, "button", "Unfavorite Article (1)")
        button = await seen_element(session, snapshot, ref)
        count = await narrowed(button.element, "span.counter")
        check = text_check(pattern=r"\b1\b")
        locators = await generate_for_assertion(session.page, count, checks_text=True)
        target = Target(
            semantic="the favorites count in the article banner", locators=locators
        )
        found = await resolve(session.page, target, "assertion")
        assert isinstance(found, Resolved), found
        return (
            locators,
            await is_same(session.page, found.element, count.element),
            await text_matches(check, await rendered_text(found.element)),
        )

    locators, is_the_count, passes = in_session(scenario)

    assert is_the_count
    assert passes
    assert not [each for each in locators if isinstance(each, ByRole)]


# What a locator must never be: positional, or built from a generated name,
# and the controls whose name may locate them (ADR-0025). The test's own
# reading, independent of the generator's.
POSITIONAL = re.compile(r":(?:nth|first|last|only)-|\bnth=")
GENERATED = re.compile(
    r"(?:^|[\s.#\[=])(?:css|sc|jsx|emotion|svelte|ng)-|_ng"
    r"|(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])\b[A-Za-z0-9]{5,}\b"
)
CONTROLS = {
    "button",
    "link",
    "textbox",
    "checkbox",
    "radio",
    "combobox",
    "option",
    "tab",
}


def broken_rules(locator: Locator) -> list[str]:
    """Which of the grammar's never-rules `locator` or its scope breaks."""
    broken = []
    if isinstance(locator, ByCss):
        if POSITIONAL.search(locator.css):
            broken.append("positional")
        if GENERATED.search(locator.css):
            broken.append("generated")
    if isinstance(locator, ByRole) and locator.name and locator.role not in CONTROLS:
        broken.append("text for a container")
    if isinstance(locator, ByRole) and locator.role in {
        "generic",
        "none",
        "presentation",
    }:
        broken.append("no role of its own")
    if locator.scope is not None:
        broken.extend(broken_rules(locator.scope))
    return broken


@pytest.mark.parametrize("page", PAGES)
def test_every_pilot_locator_keeps_the_grammar_and_finds_its_element(page: str) -> None:
    # Every element the snapshot gives a ref, as an action and as an
    # assertion: each locator generated keeps the never-rules and, alone,
    # resolves to that element. An element no locator finds is left out.
    async def scenario(session: BrowserSession) -> list[tuple[Locator, bool]]:
        await show(session, page)
        snapshot = await session.snapshot()
        found = []
        for ref in snapshot_elements(snapshot):
            used = await seen_element(session, snapshot, ref)
            for use in ("action", "assertion"):
                try:
                    locators = await generated(session.page, used, use, None)
                except LocatorError:
                    continue
                for locator in locators:
                    target = Target(semantic="a pilot element", locators=(locator,))
                    resolved = await resolve(session.page, target, use)
                    same = isinstance(resolved, Resolved) and await is_same(
                        session.page, resolved.element, used.element
                    )
                    found.append((locator, same))
        return found

    found = in_session(scenario)

    assert len(found) >= 10
    assert [locator for locator, same in found if not same] == []
    assert [
        (locator, broken_rules(locator))
        for locator, _ in found
        if broken_rules(locator)
    ] == []


async def resolves_alone_to(
    page: Page, target: Target, use: Literal["action", "assertion"], used: Seen
) -> list[bool]:
    """Whether each of `target`'s locators, alone, resolves for `use` to the
    element `used`."""
    found = []
    for locator in target.locators:
        alone = Target(semantic=target.semantic, locators=(locator,))
        resolved = await resolve(page, alone, use)
        found.append(
            isinstance(resolved, Resolved)
            and await is_same(page, resolved.element, used.element)
        )
    return found


FAVORITE_BUTTON = "the favorite button in the article banner"
BANNER = ByCss(css="div.banner")
FAVORITING: list[tuple[str, str, Literal["action", "assertion"]]] = [
    ("article", "Favorite Article (0)", "action"),
    ("article-favorited", "Unfavorite Article (1)", "assertion"),
]


def test_the_favorite_toggle_is_split_between_its_click_and_its_check() -> None:
    # favorite-article clicks the banner's favorite button, reloads, and
    # checks the button's text. The click finds the button by a name that
    # favoriting changes, and a text check's target has no name, so the check
    # starts a second target. Each locator resolves alone to the element its
    # use put it to, at that use (AC5).
    async def scenario(
        session: BrowserSession,
    ) -> tuple[tuple[Target, ...], list[tuple[int, list[bool]]]]:
        uses = TargetUses(FAVORITE_BUTTON, checks_text=True)
        got = []
        for page, name, use in FAVORITING:
            await show(session, page)
            snapshot = await session.snapshot()
            ref = ref_of(snapshot, "button", name)
            used = await seen_element(session, snapshot, ref)
            index = await uses.add(session.page, used, use)
            target = uses.targets[index]
            got.append(
                (index, await resolves_alone_to(session.page, target, use, used))
            )
        return uses.targets, got

    targets, got = in_session(scenario)

    structure = ByCss(css="app-favorite-button button.btn", scope=BANNER)
    assert targets == (
        Target(
            semantic=FAVORITE_BUTTON,
            locators=(
                ByRole(role="button", name="Favorite Article (0)", scope=BANNER),
                structure,
            ),
        ),
        Target(semantic=FAVORITE_BUTTON, locators=(structure,)),
    )
    assert got == [(0, [True, True]), (1, [True])]


def test_a_target_whose_locators_hold_at_every_use_stays_one() -> None:
    # The favorites count is found by structure, which favoriting doesn't
    # change, so its check before favoriting and its check after share one
    # target.
    meaning = "the favorites count in the article banner"

    async def scenario(
        session: BrowserSession,
    ) -> tuple[tuple[Target, ...], list[tuple[int, list[bool]]]]:
        uses = TargetUses(meaning, checks_text=True)
        got = []
        for page, name, _ in FAVORITING:
            await show(session, page)
            snapshot = await session.snapshot()
            ref = ref_of(snapshot, "button", name)
            button = await seen_element(session, snapshot, ref)
            count = await narrowed(button.element, "span.counter")
            index = await uses.add(session.page, count, "assertion")
            target = uses.targets[index]
            got.append(
                (
                    index,
                    await resolves_alone_to(session.page, target, "assertion", count),
                )
            )
        return uses.targets, got

    targets, got = in_session(scenario)

    assert targets == (
        Target(semantic=meaning, locators=(ByCss(css="span.counter", scope=BANNER),)),
    )
    assert got == [(0, [True]), (0, [True])]


FOLLOW = """<button class="btn btn-outline-primary">Follow anna</button>
<button class="btn btn-secondary">Share</button>"""


def test_a_class_that_flips_with_state_splits_the_target() -> None:
    # Clicking the button flips its class, as Bootstrap's btn-outline-primary
    # becomes btn-primary. The second click checks every locator, and the
    # class no longer finds the button, so that click starts a target. The
    # first target keeps the class: a locator is never dropped to keep a
    # target whole.
    async def scenario(session: BrowserSession) -> tuple[list[int], tuple[Target, ...]]:
        uses = TargetUses("the follow button", checks_text=False)
        await put(session, FOLLOW)
        indexes = []
        for _ in range(2):
            snapshot = await session.snapshot()
            ref = ref_of(snapshot, "button", "Follow anna")
            used = await seen_element(session, snapshot, ref)
            indexes.append(await uses.add(session.page, used, "action"))
            await used.element.evaluate(
                "(button) => button.classList.replace('btn-outline-primary', 'btn-primary')"
            )
        return indexes, uses.targets

    indexes, targets = in_session(scenario)

    by_name = ByRole(role="button", name="Follow anna")
    assert indexes == [0, 1]
    assert targets == (
        Target(
            semantic="the follow button",
            locators=(by_name, ByCss(css="button.btn-outline-primary")),
        ),
        Target(
            semantic="the follow button",
            locators=(by_name, ByCss(css="button.btn-primary")),
        ),
    )


SAVE = """<button class="save">Save</button><button>Cancel</button>"""
EMAIL = """<input name="email" placeholder="Email"><input name="nickname">"""


@pytest.mark.parametrize(
    ("html", "role", "name", "checks_text", "expected"),
    [
        (
            SAVE,
            "button",
            "Save",
            True,
            [
                (ByRole(role="button", name="Save"), ByCss(css="button.save")),
                (ByCss(css="button.save"),),
            ],
        ),
        (
            SAVE,
            "button",
            "Save",
            False,
            [(ByRole(role="button", name="Save"), ByCss(css="button.save"))],
        ),
        (
            EMAIL,
            "textbox",
            "Email",
            False,
            [
                (
                    ByRole(role="textbox", name="Email"),
                    ByPlaceholder(placeholder="Email"),
                    ByCss(css="input[name=email]"),
                ),
                (
                    ByRole(role="textbox", name="Email"),
                    ByCss(css="input[name=email]"),
                ),
            ],
        ),
    ],
    ids=["a text check after a click", "a check of no text", "a check after a fill"],
)
def test_a_later_use_joins_only_through_locators_its_grammar_allows(
    html: str,
    role: str,
    name: str,
    checks_text: bool,
    expected: list[tuple[Locator, ...]],
) -> None:
    # The element is acted on, then checked, on the same page. Every locator
    # still finds it, but an assertion's grammar has no label or placeholder,
    # and a text check's target has no name (ADR-0025), so the check joins
    # the action's target only when every locator is one it allows.
    async def scenario(session: BrowserSession) -> tuple[Target, ...]:
        uses = TargetUses("the form's control", checks_text=checks_text)
        await put(session, html)
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, role, name))
        await uses.add(session.page, used, "action")
        await uses.add(session.page, used, "assertion")
        return uses.targets

    assert [target.locators for target in in_session(scenario)] == expected


HEADER_LINKS = ByCss(css="ul.navbar-nav")


async def see_header_link(session: BrowserSession, uses: TargetUses, name: str) -> None:
    """Show the login page, where the header has the link `name`, and have
    `uses` see it for a later negative check."""
    await show(session, "login")
    snapshot = await session.snapshot()
    seen = await seen_element(session, snapshot, ref_of(snapshot, "link", name))
    await uses.see_for_negative_check(session.page, seen)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Sign in", (ByRole(role="link", name="Sign in", scope=HEADER_LINKS),)),
        (
            "Sign up",
            (
                ByRole(role="link", name="Sign up", scope=HEADER_LINKS),
                ByCss(css="a.nav-signup", scope=HEADER_LINKS),
            ),
        ),
    ],
)
def test_a_negative_check_target_is_always_scoped(
    name: str, expected: tuple[Locator, ...]
) -> None:
    # login checks that the header has no Sign in or Sign up link once
    # signed in. The link is seen on the login page, where it is unique
    # without a scope, and checked on the home page. Its locators are scoped
    # under the nearest ancestor that holds at both: ul.nav is unique on the
    # login page, but the home page's feed tabs are a ul.nav too.
    meaning = f"the header's {name} link"

    async def scenario(
        session: BrowserSession,
    ) -> tuple[
        Resolved | Absent | Unresolved,
        int,
        tuple[Target, ...],
        Resolved | Absent | Unresolved,
    ]:
        uses = TargetUses(meaning, checks_text=False)
        await see_header_link(session, uses, name)
        unscoped = Target(semantic=meaning, locators=(ByRole(role="link", name=name),))
        alone = await resolve(session.page, unscoped, "negative_check")
        await show(session, "home")
        index = await uses.add_negative_check(session.page)
        return (
            alone,
            index,
            uses.targets,
            await resolve(session.page, uses.targets[index], "negative_check"),
        )

    alone, index, targets, at_the_check = in_session(scenario)

    assert isinstance(alone, Resolved)
    assert index == 0
    assert targets == (Target(semantic=meaning, locators=expected),)
    assert isinstance(at_the_check, Absent)


def test_a_negative_check_target_fails_by_name_when_its_scope_is_gone() -> None:
    # An error page has no header, so no scope the link was seen under is
    # there to be empty: absence from it would be absence from anywhere.
    async def scenario(session: BrowserSession) -> int:
        uses = TargetUses("the header's Sign in link", checks_text=False)
        await see_header_link(session, uses, "Sign in")
        await put(session, "<main><h1>Bad gateway</h1></main>")
        return await uses.add_negative_check(session.page)

    with pytest.raises(LocatorError, match=r"^the header's Sign in link: .*scope"):
        in_session(scenario)


async def an_action_no_locator_finds(session: BrowserSession, uses: TargetUses) -> None:
    await put(
        session, "<ul><li><button>Edit</button></li><li><button>Edit</button></li></ul>"
    )
    snapshot = await session.snapshot()
    used = await seen_element(session, snapshot, ref_of(snapshot, "button", "Edit", 1))
    await uses.add(session.page, used, "action")


async def a_sighting_no_scope_picks_out(
    session: BrowserSession, uses: TargetUses
) -> None:
    await put(session, "<button>Edit</button>")
    snapshot = await session.snapshot()
    seen = await seen_element(session, snapshot, ref_of(snapshot, "button", "Edit"))
    await uses.see_for_negative_check(session.page, seen)


async def a_negative_check_of_nothing_seen(
    session: BrowserSession, uses: TargetUses
) -> None:
    await put(session, "<main><p>Saved</p></main>")
    await uses.add_negative_check(session.page)


@pytest.mark.parametrize(
    ("misuse", "reason"),
    [
        (an_action_no_locator_finds, "finds the button element alone for an action"),
        (
            a_sighting_no_scope_picks_out,
            "finds the button element under a scope for a negative check",
        ),
        (a_negative_check_of_nothing_seen, "wasn't seen before its negative check"),
    ],
    ids=["an action", "a sighting", "a negative check"],
)
def test_a_locator_error_from_target_uses_names_the_meaning(
    misuse: Callable[[BrowserSession, TargetUses], Awaitable[None]], reason: str
) -> None:
    # The caller holds one TargetUses per meaning, so its errors say which
    # meaning has no locator, and why (#52's reviews).
    async def scenario(session: BrowserSession) -> None:
        await misuse(session, TargetUses("the edit button", checks_text=False))

    with pytest.raises(LocatorError, match=f"^the edit button: .*{reason}"):
        in_session(scenario)


# A header whose links lost the navbar-nav class.
RESTYLED_HEADER = """<nav class="navbar"><ul class="nav">
<li class="nav-item"><a class="nav-link" href="/">Home</a></li></ul></nav>"""


def test_a_negative_check_joins_the_current_target_only_where_it_holds() -> None:
    # The Sign in link's target is scoped to the header's links, which every
    # signed-in page has without it, so the check on the editor page joins
    # the check on the home page. A header restyled without that scope starts
    # a target, from the scopes the link was seen under.
    async def scenario(
        session: BrowserSession,
    ) -> tuple[list[int], tuple[Target, ...]]:
        uses = TargetUses("the header's Sign in link", checks_text=False)
        await see_header_link(session, uses, "Sign in")
        indexes = []
        for page in ("home", "editor", None):
            if page is None:
                await put(session, RESTYLED_HEADER)
            else:
                await show(session, page)
            indexes.append(await uses.add_negative_check(session.page))
        return indexes, uses.targets

    indexes, targets = in_session(scenario)

    assert indexes == [0, 0, 1]
    assert [target.locators for target in targets] == [
        (ByRole(role="link", name="Sign in", scope=HEADER_LINKS),),
        (ByRole(role="link", name="Sign in", scope=ByCss(css="ul.nav")),),
    ]


SIGN_OUT = """<nav class="top"><a href="#" class="out">Sign out</a></nav>
<main><p>Signed in</p></main>"""


def test_a_negative_check_never_joins_a_target_with_an_unscoped_locator() -> None:
    # Clicking the link removes it. The click's target needs no scope, so
    # the check that it is gone starts a target from where it was seen, with
    # every locator scoped.
    async def scenario(session: BrowserSession) -> tuple[list[int], tuple[Target, ...]]:
        uses = TargetUses("the sign-out link", checks_text=False)
        await put(session, SIGN_OUT)
        snapshot = await session.snapshot()
        used = await seen_element(
            session, snapshot, ref_of(snapshot, "link", "Sign out")
        )
        await uses.see_for_negative_check(session.page, used)
        indexes = [await uses.add(session.page, used, "action")]
        await used.element.evaluate("(link) => link.remove()")
        indexes.append(await uses.add_negative_check(session.page))
        return indexes, uses.targets

    indexes, targets = in_session(scenario)

    top = ByCss(css="nav.top")
    assert indexes == [0, 1]
    assert [target.locators for target in targets] == [
        (ByRole(role="link", name="Sign out"), ByCss(css="a.out")),
        (
            ByRole(role="link", name="Sign out", scope=top),
            ByCss(css="a.out", scope=top),
        ),
    ]


def test_a_negative_check_of_an_element_still_shown_gets_no_target() -> None:
    # Every locator the link was seen by still finds it: the check would
    # fail, so it gets no target to fail with.
    async def scenario(session: BrowserSession) -> int:
        uses = TargetUses("the header's Sign in link", checks_text=False)
        await see_header_link(session, uses, "Sign in")
        return await uses.add_negative_check(session.page)

    with pytest.raises(LocatorError, match="nothing visible in it"):
        in_session(scenario)


@pytest.mark.parametrize(
    ("given", "used"),
    [
        ("pay", True),
        ("pay\u00a0", False),
        ("pay ", False),
        ("pay\u3000", False),
        ("pay\u00e9", False),
    ],
    ids=["plain", "a no-break space", "a space", "an ideographic space", "an accent"],
)
def test_an_id_is_used_only_when_it_is_one_plain_identifier(
    given: str, used: bool
) -> None:
    # A trailing space or non-ASCII character would need CSS escaping, and
    # Playwright trims some of them, so such an id is left out, never escaped
    # (ADR-0025's 2026-10-02 amendment, "what counts as stable").
    html = f"""<button class="go">Go</button><button>Stop</button><script>
      document.querySelector(".go").id = {json.dumps(given)};
    </script>"""

    async def scenario(
        session: BrowserSession,
    ) -> tuple[str | None, tuple[Locator, ...]]:
        await put(session, html)
        snapshot = await session.snapshot()
        seen = await seen_element(session, snapshot, ref_of(snapshot, "button", "Go"))
        return await seen.element.get_attribute("id"), await generate_for_action(
            session.page, seen
        )

    actual_id, locators = in_session(scenario)

    by_id = [
        each
        for each in locators
        if isinstance(each, ByCss) and each.css.startswith("#")
    ]
    assert actual_id == given
    assert by_id == ([ByCss(css="#pay")] if used else [])
