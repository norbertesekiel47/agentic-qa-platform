"""Every locator at every use, on request (ADR-0025's #53 P11 amendment;
DATA_MODEL §7): `resolve_every` and the session's `every=` in real Chromium,
negatives decided by one utility-world read, the identity read, and cleanup
on every exit."""

import asyncio
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from aqa_core.compiled import ByCss, Target
from aqa_runner.binding import HeldRegion, held_region, same_element
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.locators import (
    Absent,
    Miss,
    Resolved,
    Shown,
    Unresolved,
    Use,
    resolve,
    resolve_every,
)
from playwright.async_api import ElementHandle, Error, JSHandle, Page, async_playwright
from playwright.async_api import Locator as PlaywrightLocator

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.pilot_pages import in_session, put
from packages.runner.tests.test_subject_enforcement import (
    COUNT,
    HIDDEN,
    REGIONS,
    contracted,
)


def target(*locators: dict[str, Any]) -> Target:
    return Target.model_validate(
        {"semantic": "an element on the fixture page", "locators": locators}
    )


def inside(css: str, scope: str) -> dict[str, Any]:
    """A css locator scoped to a css scope."""
    return {"css": css, "scope": {"css": scope}}


def on[T](html: str, scenario: Callable[[Page], Awaitable[T]]) -> T:
    """`scenario`'s result on `html`, in a session with the engines registered."""

    async def run(session: BrowserSession) -> T:
        await put(session, html)
        return await scenario(session.page)

    return in_session(run)


async def plain(found: Resolved | Shown | Absent | Unresolved) -> object:
    """A resolution as a literal: a resolved element becomes its locator
    index and data-is marker, its handle let go."""
    if not isinstance(found, Resolved):
        return found
    try:
        marker = await found.element.get_attribute("data-is")
        return ("resolved", found.locator_index, marker)
    finally:
        await found.element.dispose()


def every(html: str, looked_at: Target, use: Use = "negative_check") -> object:
    """`resolve_every`'s answer for `looked_at` on `html`, as a literal."""

    async def scenario(page: Page) -> object:
        return await plain(await resolve_every(page, looked_at, use))

    return on(html, scenario)


def looks(
    html: str, looked_at: Target, use: Use = "negative_check"
) -> tuple[object, object]:
    """`resolve_every`'s answer, then normal `resolve`'s, as literals."""

    async def scenario(page: Page) -> tuple[object, object]:
        return (
            await plain(await resolve_every(page, looked_at, use)),
            await plain(await resolve(page, looked_at, use)),
        )

    return on(html, scenario)


EMPTY = '<div id="area"></div>'
ERROR = '<div id="area"><p class="error">Oops</p></div>'
# The banner's favorites count, by two scoped locators (ADR-0025's #53 P7b-II
# amendment).
BOTH = contracted(
    inside("span.counter", "app-favorite-button"),
    {"testid": "count", "scope": {"css": "div.article-meta"}},
)
TOKEN = re.compile(r"[0-9a-f]{32}")


def binding_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The binding engine's element reads from now on, each token named by
    its order of first appearance: t0, t1 and so on."""
    original = ElementHandle.query_selector_all
    reads: list[str] = []
    tokens: dict[str, str] = {}

    def named(found: re.Match[str]) -> str:
        return tokens.setdefault(found[0], f"t{len(tokens)}")

    async def query_selector_all(self: ElementHandle, selector: str) -> Any:
        if selector.startswith("aqa-binding="):
            reads.append(TOKEN.sub(named, selector.removeprefix("aqa-binding=")))
        return await original(self, selector)

    monkeypatch.setattr(ElementHandle, "query_selector_all", query_selector_all)
    return reads


def test_every_locator_absent_passes_a_negative_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both = target(inside(".error", "#area"), inside("[data-testid=error]", "#side"))
    assert every(EMPTY + '<div id="side"></div>', both) == Absent(0)
    reads = binding_reads(monkeypatch)
    assert every(HIDDEN, BOTH) == Absent(0)
    # One region held, and one read proving it shows no part.
    assert reads == ["hold:t0", "absent:t0|div.banner|span.counter", "drop:t0"]


def test_one_locator_without_its_scope_fails_a_negative_use() -> None:
    gone = target(inside(".error", "#area"), inside(".error", "#gone"))
    # Normal replay's first-match absence is unchanged.
    assert looks(EMPTY, gone) == (Unresolved(("no match", "no scope")), Absent(0))
    hero = HIDDEN.replace('class="banner"', 'class="hero"')
    assert every(hero, BOTH) == Unresolved(("no region", "no region"))


def test_one_fallback_finding_a_visible_element_fails_a_negative_use() -> None:
    fallback = target(inside(".old", "#area"), inside(".error", "#area"))
    assert every(ERROR, fallback) == Shown(1)
    # A visible match decides before a later locator's missing scope.
    assert every(ERROR, target(inside(".error", "#area"), inside(".x", "#gone"))) == (
        Shown(0)
    )
    # Several visible matches fail at once, where first-match calls them drift.
    two = '<div id="area"><p class="error">A</p><p class="error">B</p></div>'
    assert looks(two, target(inside(".error", "#area"))) == (
        Shown(0),
        Unresolved(("ambiguous",)),
    )
    assert every(REGIONS, BOTH) == Shown(0)
    # The lower copy's visible count, found by a locator that escapes the
    # banner, is a binding miss in both modes: never the banner's failure.
    escaping = contracted({"css": ":scope + div.container.page span.counter"})
    outside = Unresolved(("outside region",))
    assert looks(HIDDEN, escaping) == (outside, outside)


def test_a_visible_match_hidden_after_its_count_still_fails_under_every_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = PlaywrightLocator.count
    fired: list[int] = []

    async def count(self: PlaywrightLocator) -> int:
        found = await original(self)
        if found == 1 and not fired:
            fired.append(found)
            await self.page.evaluate(
                '() => { document.querySelector("p.error").hidden = true }'
            )
        return found

    monkeypatch.setattr(PlaywrightLocator, "count", count)
    assert every('<p class="error">Oops</p>', target({"css": "p.error"})) == Shown(0)
    assert fired == [1]


# A node that leaves just before a read and comes back, the same node in
# the same place, right after it; or a copy of it that stands for that read.
REMOVE = (
    "() => { const a = document.querySelector(%r); window.kept = a;"
    " window.at = [a.parentNode, a.nextSibling]; a.remove() }"
)
DUPLICATE = (
    "() => { const a = document.querySelector(%r); window.copy = a.cloneNode(true);"
    " a.after(window.copy) }"
)
RESTORE = "() => window.at[0].insertBefore(window.kept, window.at[1])"
UNDUPLICATE = "() => window.copy.remove()"
# The banner's button, moved into the lower copy, and back by RESTORE.
MOVE_BUTTON = (
    '() => { const b = document.querySelector("div.banner app-favorite-button");'
    " window.kept = b; window.at = [b.parentNode, b.nextSibling];"
    ' document.querySelector("div.container div.article-meta").append(b) }'
)
NESTED = '<div id="area"><p class="box"><span class="error">x</span></p></div>'
NO_SCOPE = Unresolved(("no scope",))
CONTROL = (1, "", "")
IN_AREA = target(inside(".error", "#area"))
IN_BOX = target({"css": ".error", "scope": inside("p.box", "#area")})
IN_BUTTON = contracted(inside("span.counter", "app-favorite-button"))
AREA, BOX, BANNER = (REMOVE % "#area", REMOVE % "p.box", REMOVE % "div.banner")
AREAS, BOXES = (DUPLICATE % "#area", DUPLICATE % "p.box")


def around(
    monkeypatch: pytest.MonkeyPatch, needle: str, change: tuple[int, str, str]
) -> list[int]:
    """Make `change`'s first script run just before its numbered count among
    those whose selector names `needle`, and its second once that count
    returns. The list holds the read the change ran around."""
    nth, before, after = change
    original = PlaywrightLocator.count
    reads: list[int] = []
    fired: list[int] = []

    async def count(self: PlaywrightLocator) -> int:
        if needle not in repr(self):
            return await original(self)
        reads.append(len(reads) + 1)
        if len(reads) != nth or not before:
            return await original(self)
        fired.append(nth)
        await self.page.evaluate(before)
        try:
            return await original(self)
        finally:
            await self.page.evaluate(after)

    monkeypatch.setattr(PlaywrightLocator, "count", count)
    return fired


@pytest.mark.parametrize(
    ("html", "looked_at", "change", "expected"),
    [
        (ERROR, IN_AREA, (1, AREA, RESTORE), NO_SCOPE),
        (ERROR, IN_AREA, (1, AREAS, UNDUPLICATE), NO_SCOPE),
        (ERROR, IN_AREA, CONTROL, Shown(0)),
        # The second read sees the scope standing again, which proves the zero.
        (EMPTY, IN_AREA, (1, AREA, RESTORE), Absent(0)),
        (EMPTY, IN_AREA, (2, AREA, RESTORE), NO_SCOPE),
        (EMPTY, IN_AREA, (2, AREAS, UNDUPLICATE), NO_SCOPE),
        (EMPTY, IN_AREA, CONTROL, Absent(0)),
        (NESTED, IN_BOX, (1, BOX, RESTORE), NO_SCOPE),
        (NESTED, IN_BOX, (1, BOXES, UNDUPLICATE), NO_SCOPE),
        (NESTED, IN_BOX, CONTROL, Shown(0)),
        (HIDDEN, IN_BUTTON, (2, MOVE_BUTTON, RESTORE), NO_SCOPE),
        (HIDDEN, IN_BUTTON, (2, BANNER, RESTORE), NO_SCOPE),
        (REGIONS, IN_BUTTON, (1, MOVE_BUTTON, RESTORE), NO_SCOPE),
        (REGIONS, IN_BUTTON, (1, BANNER, RESTORE), NO_SCOPE),
        (HIDDEN, IN_BUTTON, CONTROL, Absent(0)),
        (REGIONS, IN_BUTTON, CONTROL, Shown(0)),
    ],
    ids=[
        "shown, scope removed around read 1",
        "shown, scope duplicated around read 1",
        "shown, control",
        "empty, scope removed around read 1",
        "empty, scope removed around read 2",
        "empty, scope duplicated around read 2",
        "empty, control",
        "nested, inner scope removed around read 1",
        "nested, inner scope duplicated around read 1",
        "nested, control",
        "contracted, hidden, scope moved out of the region around read 2",
        "contracted, hidden, region removed around read 2",
        "contracted, shown, scope moved out of the region around read 1",
        "contracted, shown, region removed around read 1",
        "contracted, hidden, control",
        "contracted, shown, control",
    ],
)
def test_a_scope_that_leaves_and_returns_around_a_read_is_drift_never_absence(
    monkeypatch: pytest.MonkeyPatch,
    html: str,
    looked_at: Target,
    change: tuple[int, str, str],
    expected: object,
) -> None:
    [locator] = looked_at.locators
    assert isinstance(locator, ByCss)
    fired = around(monkeypatch, locator.css, change)

    async def scenario(page: Page) -> tuple[object, bool]:
        before = await page.content()
        found = await resolve_every(page, looked_at, "negative_check")
        # The page is as it was before the look.
        return found, await page.content() == before

    assert on(html, scenario) == (expected, True)
    assert fired == ([change[0]] if change[1] else [])


KINDS = (
    '<section aria-label="Panel"><button>Go</button><label>Email<input></label>'
    '<input placeholder="Search"><span data-testid="err">bad</span>'
    '<p class="error">x</p></section>'
)
QUIET = (
    '<section aria-label="Panel"><button hidden>Go</button>'
    "<label hidden>Email<input></label>"
    '<input hidden placeholder="Search"><span hidden data-testid="err">bad</span>'
    '<p hidden class="error">x</p></section>'
)
SCOPES: dict[str, dict[str, Any]] = {
    "role": {"role": "region", "name": "Panel"},
    "css": {"css": "section"},
    "css in body": {"css": "section", "scope": {"css": "body"}},
}


@pytest.mark.parametrize(
    "child",
    [
        {"role": "button", "name": "Go"},
        {"label": "Email"},
        {"placeholder": "Search"},
        {"testid": "err"},
        {"css": "p.error"},
    ],
    ids=["role", "label", "placeholder", "testid", "css"],
)
def test_one_read_negatives_agree_with_resolve_for_each_locator_kind(
    child: dict[str, Any],
) -> None:
    async def scenario(page: Page) -> dict[str, tuple[object, object]]:
        found: dict[str, tuple[object, object]] = {}
        for name, scope in SCOPES.items():
            each = target({**child, "scope": scope})
            found[name] = (
                await resolve_every(page, each, "negative_check"),
                await plain(await resolve(page, each, "negative_check")),
            )
        return found

    shown = ("resolved", 0, None)
    assert on(KINDS, scenario) == dict.fromkeys(SCOPES, (Shown(0), shown))
    assert on(QUIET, scenario) == dict.fromkeys(SCOPES, (Absent(0), Absent(0)))


BUTTONS = (
    '<button id="keep" class="keep" data-is="keep">Keep</button>'
    '<button id="save" data-is="save">Save</button>'
)
KEEP = target({"css": "#keep"}, {"role": "button", "name": "Keep"})
SAVE = target({"css": "#keep"}, {"role": "button", "name": "Save"})


def test_every_locator_without_the_engines_registered_is_a_programming_error() -> None:
    async def scenario() -> None:
        async with (
            async_playwright() as playwright,
            egress_proxy() as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            await put(session, BUTTONS)
            with pytest.raises(RuntimeError, match=r"register_identity_engine.*before"):
                await resolve_every(session.page, KEEP, "assertion")

    asyncio.run(scenario())


@dataclass
class _Spied:
    """The handles a look found, the indexes of those it released, and the
    binding engine verbs it read."""

    found: list[ElementHandle] = field(default_factory=list)
    released: list[int] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    blocked: asyncio.Event = field(default_factory=asyncio.Event)


def spied(monkeypatch: pytest.MonkeyPatch, case: str) -> _Spied:
    """Spy on handles with `case`'s failure planted: the second found
    handle's release fails or blocks, or the first identity read blocks."""
    spy = _Spied()
    element_handles = PlaywrightLocator.element_handles
    dispose = JSHandle.dispose
    query_selector_all = ElementHandle.query_selector_all

    async def finding(self: PlaywrightLocator) -> list[ElementHandle]:
        handles = await element_handles(self)
        spy.found.extend(handles)
        return handles

    async def releasing(self: JSHandle) -> None:
        index = next((n for n, each in enumerate(spy.found) if each is self), None)
        if index is not None:
            spy.released.append(index)
        if index == 1 and case == "fails":
            raise Error("fake dispose failure")
        if index == 1 and case == "cancelled release":
            spy.blocked.set()
            await asyncio.sleep(3600)
        await dispose(self)

    async def querying(self: ElementHandle, selector: str) -> list[ElementHandle]:
        verb = selector.removeprefix("aqa-binding=").partition(":")[0]
        spy.reads.append(verb)
        if verb == "is" and case == "cancelled identity read":
            spy.blocked.set()
            await asyncio.sleep(3600)
        return await query_selector_all(self, selector)

    monkeypatch.setattr(PlaywrightLocator, "element_handles", finding)
    monkeypatch.setattr(JSHandle, "dispose", releasing)
    monkeypatch.setattr(ElementHandle, "query_selector_all", querying)
    return spy


@pytest.mark.parametrize(
    "case", ["clean", "fails", "cancelled release", "cancelled identity read"]
)
def test_every_locator_releases_each_handle_it_found_on_every_exit(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    three = target(
        {"css": "#keep"}, {"role": "button", "name": "Keep"}, {"css": "button.keep"}
    )
    spy = spied(monkeypatch, case)

    async def scenario(page: Page) -> tuple[object, bool, int, list[int]]:
        look = asyncio.create_task(resolve_every(page, three, "assertion"))
        if case.startswith("cancelled"):
            await spy.blocked.wait()
            look.cancel()
        [outcome] = await asyncio.gather(look, return_exceptions=True)
        # What the look released, before the caller lets go of what it kept.
        released = sorted(spy.released)
        if isinstance(outcome, BaseException):
            failure = (type(outcome), str(outcome))
            return failure, look.cancelled(), len(spy.found), released
        assert outcome == Resolved(0, spy.found[0])
        return await plain(outcome), look.cancelled(), len(spy.found), released

    outcome = on(BUTTONS, scenario)
    if case == "clean":
        # The two compared handles are released; the first is the caller's.
        assert outcome == (("resolved", 0, "keep"), False, 3, [1, 2])
        assert spy.reads == ["hold", "is", "drop", "hold", "is", "drop"]
        return
    # Nothing after the failure is looked for, and both handles found are
    # released once each.
    if case == "fails":
        assert outcome == ((Error, "fake dispose failure"), False, 2, [0, 1])
    else:
        assert outcome == ((asyncio.CancelledError, ""), True, 2, [0, 1])
    assert spy.reads == ["hold", "is", "drop"]


class _World:
    """A fake page's state: what its locators found and released."""

    def __init__(self, case: str) -> None:
        self.case = case
        self.held: set[str] = set()
        self.found: list[int] = []
        self.released: list[int] = []
        self.blocked = asyncio.Event()


class _Answer:
    async def dispose(self) -> None:
        return None


class _Handle:
    """A found element: all are one node, so every locator agrees."""

    def __init__(self, world: _World, index: int) -> None:
        self.world = world
        self.index = index

    async def query_selector_all(self, selector: str) -> list[_Answer]:
        verb, _, token = selector.removeprefix("aqa-binding=").partition(":")
        if verb == "hold":
            self.world.held.add(token)
        if verb == "drop":
            self.world.held.discard(token)
        return [_Answer()] if verb == "is" and token in self.world.held else []

    async def dispose(self) -> None:
        self.world.released.append(self.index)
        if self.index == 1200 and self.world.case == "fails":
            raise Error("fake dispose failure")
        if self.index == 1200 and self.world.case == "cancelled":
            self.world.blocked.set()
            await asyncio.sleep(3600)
        if self.index == 0 and self.world.case == "cancelled":
            raise Error("fake first dispose failure")


class _Query:
    def __init__(self, world: _World, index: int) -> None:
        self.world = world
        self.index = index

    async def count(self) -> int:
        return 1

    async def element_handles(self) -> list[_Handle]:
        self.world.found.append(self.index)
        return [_Handle(self.world, self.index)]


class _Page:
    def __init__(self, world: _World) -> None:
        self.world = world

    def locator(self, selector: str) -> _Query:
        return _Query(self.world, int(selector.removeprefix("css=#e")))


@pytest.mark.parametrize("case", ["clean", "fails", "cancelled"])
def test_every_locator_handles_more_locators_than_the_recursion_limit(
    case: str,
) -> None:
    assert sys.getrecursionlimit() < 1500
    many = target(*({"css": f"#e{n}"} for n in range(1500)))
    world = _World(case)

    async def scenario() -> tuple[object, bool]:
        look = asyncio.create_task(
            resolve_every(cast(Page, _Page(world)), many, "assertion")
        )
        if case == "cancelled":
            await world.blocked.wait()
            look.cancel()
        try:
            found = await look
        except (Error, asyncio.CancelledError) as error:
            cause = error.__cause__
            return (type(error), str(error), repr(cause)), look.cancelled()
        assert isinstance(found, Resolved)
        return (found.locator_index, cast(_Handle, found.element).index), False

    outcome = asyncio.run(scenario())
    assert not world.held
    if case == "clean":
        assert outcome == ((0, 0), False)
        assert len(world.found) == 1500
        # Every handle but the one returned is released once.
        assert sorted(world.released) == list(range(1, 1500))
        return
    if case == "fails":
        assert outcome == ((Error, "fake dispose failure", "None"), False)
    else:
        cause = repr(Error("fake first dispose failure"))
        assert outcome == ((asyncio.CancelledError, "", cause), True)
    # The look stops at handle 1,200's release; every handle found,
    # the first included, is released once.
    assert len(world.found) == 1201
    assert sorted(world.released) == list(range(1201))


# Page-world globals a page could poison; the title shows the poison took.
POISON = """() => {
    Map.prototype.get = function () { return document.body; };
    Map.prototype.has = () => true;
    Object.is = () => true;
    Node.prototype.isSameNode = () => true;
    Node.prototype.contains = () => true;
    Element.prototype.querySelectorAll = () => [];
    Document.prototype.querySelectorAll = () => [];
    Element.prototype.checkVisibility = () => false;
    window.getComputedStyle = () => ({display: "none", visibility: "hidden"});
    Object.defineProperty(Node.prototype, "isConnected", {get: () => false});
    document.title = [
        new Map([[1, 2]]).get(1) === document.body,
        Object.is(1, 2),
        document.querySelectorAll("*").length,
    ].join(" ");
}"""


def test_identity_and_part_reads_ignore_a_poisoned_page_world() -> None:
    async def poisoned(session: BrowserSession, html: str) -> str:
        await put(session, html)
        await session.page.evaluate(POISON)
        return await session.page.title()

    async def scenario(session: BrowserSession) -> list[object]:
        page = session.page
        await put(session, BUTTONS)
        keep = await page.query_selector("#keep")
        keep_again = await page.query_selector("button.keep")
        save = await page.query_selector("#save")
        assert keep is not None
        assert keep_again is not None
        assert save is not None
        await page.evaluate(POISON)
        seen: list[object] = [
            await page.title(),
            await same_element(keep, keep_again),
            await same_element(keep, save),
            await plain(await resolve_every(page, KEEP, "assertion")),
            await resolve_every(page, SAVE, "assertion"),
        ]
        for handle in (keep, keep_again, save):
            await handle.dispose()
        seen.append(await poisoned(session, REGIONS))
        seen.append(await resolve_every(page, BOTH, "negative_check"))
        async with held_region(page, COUNT) as held:
            assert held is not None
            counter = held.root.locator("css=span.counter").filter(visible=True)
            seen.append(await held.bound(counter))
        seen.append(await poisoned(session, HIDDEN))
        seen.append(await resolve_every(page, BOTH, "negative_check"))
        seen.append(await poisoned(session, ERROR))
        seen.append(await resolve_every(page, IN_AREA, "negative_check"))
        seen.append(await poisoned(session, EMPTY))
        seen.append(await resolve_every(page, IN_AREA, "negative_check"))
        return seen

    took = "true true 0"
    assert in_session(scenario) == [
        took,
        True,
        False,
        ("resolved", 0, "keep"),
        Unresolved(("not unanimous", "not unanimous")),
        took,
        Shown(0),
        True,
        took,
        Absent(0),
        took,
        Shown(0),
        took,
        Absent(0),
    ]


def test_every_locator_resolves_inside_its_targets_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    three = contracted(
        {"css": "span.counter"},
        {"testid": "count"},
        inside("span.counter", "div.article-meta"),
    )
    reads = binding_reads(monkeypatch)
    assert every(REGIONS, three, "assertion") == ("resolved", 0, "banner-count")
    # One region held for the call, t0: each locator's element is its part,
    # and each other locator's is compared with the first under a token of
    # its own.
    part = "bound:t0|div.banner|span.counter|leaf"
    assert reads == [
        *("hold:t0", part),
        *(part, "hold:t1", "is:t1", "drop:t1"),
        *("in:t0|div.banner", part, "hold:t2", "is:t2", "drop:t2"),
        "drop:t0",
    ]


@pytest.mark.parametrize(
    ("other", "miss"),
    [
        ({"css": ":scope + div.container.page span.counter"}, "outside region"),
        ({"css": ":scope ~ div span.counter"}, "outside region"),
        ({"css": "app-favorite-button > button"}, "not the part"),
    ],
    ids=["adjacent sibling", "general sibling", "the part's parent"],
)
def test_every_locator_resolution_refuses_a_sibling_selector_outside_the_region(
    other: dict[str, Any], miss: Miss
) -> None:
    pair = contracted({"css": "span.counter"}, other)
    assert looks(REGIONS, pair, "assertion") == (
        Unresolved(("not unanimous", miss)),
        ("resolved", 0, "banner-count"),
    )


@pytest.mark.parametrize("replaced", [True, False], ids=["replaced", "control"])
def test_a_part_replaced_before_the_second_locators_retrieval_is_not_unanimous(
    monkeypatch: pytest.MonkeyPatch, replaced: bool
) -> None:
    element_handles = PlaywrightLocator.element_handles
    bound = HeldRegion.bound
    markers: list[str | None] = []
    verdicts: list[bool] = []

    async def retrieving(self: PlaywrightLocator) -> list[ElementHandle]:
        # After the first locator's element is bound, before the second
        # locator retrieves its own.
        if replaced and markers.count("banner-count") == 1:
            markers.append("replaced")
            await self.page.evaluate(
                '() => { const s = document.querySelector("div.banner span.counter");'
                " s.replaceWith(s.cloneNode(true)) }"
            )
        found = await element_handles(self)
        markers.extend([await each.get_attribute("data-is") for each in found])
        return found

    async def judged(
        self: HeldRegion,
        element: ElementHandle | PlaywrightLocator,
        *,
        leaf: bool | None = None,
    ) -> bool:
        verdict = await bound(self, element, leaf=leaf)
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(PlaywrightLocator, "element_handles", retrieving)
    monkeypatch.setattr(HeldRegion, "bound", judged)
    two = contracted({"css": "span.counter"}, {"testid": "count"})
    found = every(REGIONS, two, "assertion")
    # Each locator's own element was the region's part when it was read.
    assert verdicts == [True, True]
    if replaced:
        assert found == Unresolved(("not unanimous", "not unanimous"))
        assert markers == [None, "banner-count", "replaced", "banner-count"]
    else:
        assert found == ("resolved", 0, "banner-count")
        assert markers == [None, "banner-count", "banner-count"]


REGION_CHANGES = {
    "replaced": "r.replaceWith(r.cloneNode(true))",
    "relabelled": 'r.className = "moved"',
    "second": "r.after(r.cloneNode(true))",
    "removed": "r.remove()",
    "adopted": 'document.implementation.createHTMLDocument("").body.appendChild(r)',
    "adopted into a frame": (
        'const f = document.createElement("iframe"); document.body.append(f);'
        "f.contentDocument.body.appendChild(r)"
    ),
    "control": "",
}


@pytest.mark.parametrize("change", REGION_CHANGES.values(), ids=REGION_CHANGES.keys())
def test_a_region_change_after_every_locators_zero_result_is_drift_not_absence(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    count = PlaywrightLocator.count
    changed: list[str] = []

    async def changing(self: PlaywrightLocator) -> int:
        found = await count(self)
        if found == 0 and not changed:
            changed.append(change)
            await self.page.evaluate(
                f'() => {{ const r = document.querySelector("div.banner"); {change} }}'
            )
        return found

    monkeypatch.setattr(PlaywrightLocator, "count", changing)
    reads = binding_reads(monkeypatch)
    two = contracted({"css": "span.counter"}, {"testid": "count"})

    async def scenario(session: BrowserSession) -> list[tuple[object, int, bool]]:
        found: list[tuple[object, int, bool]] = []
        for look in (resolve_every, resolve):
            await put(session, HIDDEN)
            changed.clear()
            reads.clear()
            each = await look(session.page, two, "negative_check")
            holds = [read for read in reads if read.startswith("hold:")]
            found.append((each, len(holds), changed == [change]))
        return found

    # Every-locator mode first, then normal mode: one region held each.
    expected = Absent(0) if not change else Unresolved(("no region", "no region"))
    assert in_session(scenario) == [(expected, 1, True), (expected, 1, True)]
