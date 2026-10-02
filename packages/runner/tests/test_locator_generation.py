"""Building a target's locators from the element a use put it to (ADR-0025,
"Locator grammar", and its 2026-10-02 amendment, "generating locators";
DATA_MODEL §7, Locators). The rules are tested on the pilot's pages
(`pilot_pages.py`) where the pilot has an example, and on fixture pages where
it has none, in real Chromium through the browser session."""

import asyncio
import re
from typing import Any

import pytest
from aqa_core.compiled import (
    ByCss,
    ByLabel,
    ByPlaceholder,
    ByRole,
    ByTestId,
    Locator,
)
from aqa_core.text import normalize
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.locator_generation import (
    LocatorError,
    Seen,
    generate_for_action,
    seen_element,
    snapshot_elements,
)
from playwright.async_api import ElementHandle, Error, Page, async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.pilot_pages import RENDERINGS, in_session, put, show


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
    # so only controls get a name (ADR-0025: never text-only locators for
    # containers).
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
