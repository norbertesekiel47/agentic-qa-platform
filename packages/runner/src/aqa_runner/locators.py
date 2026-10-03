"""Finding a target's element on the page for the use it is put to
(DATA_MODEL §7, Resolution per use; ADR-0025 and its amendments).

Each call is one look at the page. Waiting for a target to resolve, within
the run's `resolve_seconds`, is the executor's loop around it (#46)."""

import re
from dataclasses import dataclass
from typing import Literal, overload

from aqa_core.compiled import (
    ByCss,
    ByLabel,
    ByPlaceholder,
    ByRole,
    ByTestId,
    Locator,
    Target,
)
from playwright.async_api import ElementHandle, Error, Page
from playwright.async_api import Locator as PlaywrightLocator

# What the target is for: an element a step acts on, an element an
# assertion checks, or an element a negative check (not_visible) expects
# not to see.
type Use = Literal["action", "assertion", "negative_check"]

# Why one locator didn't give the match its use needs.
type Miss = Literal["no scope", "no match", "ambiguous", "not actionable"]


@dataclass(frozen=True)
class Resolved:
    """The element found, and the index of the locator that found it."""

    locator_index: int
    element: ElementHandle


@dataclass(frozen=True)
class Absent:
    """A negative check's result: the locator at this index found its scope
    and nothing inside it, and no locator found the element."""

    locator_index: int


@dataclass(frozen=True)
class Unresolved:
    """Drift: no locator gave the match the use needs. One miss per locator,
    in order."""

    misses: tuple[Miss, ...]


# A private-use glyph that Chromium puts into an accessible name, from an
# icon font's CSS ::before (LAB_NOTES, 2026-09-29): the Basic Multilingual
# Plane's range, or one of planes 15 and 16 as a surrogate pair. Playwright
# hands the pattern to JavaScript without the u flag, where a range above
# U+FFFF can't be written, so the pair matches code units (LAB_NOTES,
# 2026-10-02).
_GLYPH = "(?:[\\ue000-\\uf8ff]|[\\udb80-\\udbff][\\udc00-\\udfff])"
_GLYPHS = f"{_GLYPH}*"
_SPACE = f"(?:\\s|{_GLYPH})*\\s(?:\\s|{_GLYPH})*"
_ENDS = f"(?:\\s|{_GLYPH})*"


def _escaped(char: str) -> str:
    # \uXXXX means the same in Python's and JavaScript's regexes, so no
    # character of a name can be read as syntax: not a quote, a slash or the
    # >> Playwright splits selectors at. An astral character stays literal,
    # which both read as itself.
    if char.isascii() and char.isalnum():
        return char
    return f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else char


def name_pattern(name: str) -> str:
    """An anchored regex that matches an accessible name, as Playwright
    reports it, exactly when the name normalizes to `name` (written
    normalized, DATA_MODEL §7): private-use glyphs may sit anywhere, and a
    space in `name` may be any run of whitespace and glyphs."""
    parts: list[str] = []
    for index, char in enumerate(name):
        if char == " ":
            parts.append(_SPACE)
            continue
        if index > 0 and name[index - 1] != " ":
            parts.append(_GLYPHS)
        parts.append(_escaped(char))
    return f"^{_ENDS}{''.join(parts)}{_ENDS}$"


def _query(
    root: Page | PlaywrightLocator, locator: Locator, *, hidden: bool
) -> PlaywrightLocator:
    """Playwright's locator for one of ours, under `root`.
    https://playwright.dev/python/docs/locators (Playwright 1.63). `hidden`
    lets a role locator see elements the accessibility tree leaves out, as the
    other kinds always do."""
    match locator:
        case ByRole(role=role, name=name):
            # A compiled pattern, which Playwright matches against the name
            # with its whitespace already collapsed; None means any name.
            pattern = None if name is None else re.compile(name_pattern(name))
            return root.get_by_role(role, name=pattern, include_hidden=hidden)
        case ByLabel(label=label):
            return root.get_by_label(label, exact=True)
        case ByPlaceholder(placeholder=placeholder):
            return root.get_by_placeholder(placeholder, exact=True)
        case ByTestId(testid=testid):
            return root.get_by_test_id(testid)
        case ByCss(css=css):
            # Always the css engine: another engine's syntax is a parse
            # error, never a match, and the format refuses >> and any quote
            # left open (ADR-0025, "reading a compiled script").
            return root.locator(f"css={css}")


async def _scoped(
    page: Page, locator: Locator, *, hidden: bool = False
) -> PlaywrightLocator | None:
    """`locator`'s query inside its scope, or None when the scope doesn't
    resolve to exactly one element on the page. A scope is judged as an
    assertion's target is, so it never sees hidden elements by role."""
    if locator.scope is None:
        return _query(page, locator, hidden=hidden)
    scope = await _scoped(page, locator.scope)
    if scope is None or await scope.count() != 1:
        return None
    return _query(scope, locator, hidden=hidden)


# Whether the element receives pointer events, as a click would: a hit test
# at the center of its first piece (a wrapped link has several), through its
# own root, so an open shadow root's element is seen. If that misses, the
# element is scrolled into view at once, ignoring smooth scrolling, and tested
# again. The test runs in the page's own world, so it is the page's word, not
# a control: a page can make it pass or fail, but not choose the element.
_RECEIVES_POINTER = """(element) => {
    const hits = () => {
        const piece = [...element.getClientRects()].find(
            (rect) => rect.width > 0 && rect.height > 0
        );
        if (piece === undefined) return false;
        const hit = element.getRootNode().elementFromPoint(
            piece.left + piece.width / 2, piece.top + piece.height / 2
        );
        return hit !== null && element.contains(hit);
    };
    if (hits()) return true;
    element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    return hits();
}"""


async def _actionable(page: Page, element: ElementHandle) -> bool:
    """Visible, enabled and receiving pointer events, judged now (ADR-0025,
    "resolving a target per use"): the same for every step that targets an
    element."""
    try:
        return (
            await element.is_visible()
            and await element.is_enabled()
            and bool(await element.evaluate(_RECEIVES_POINTER))
        )
    except Error:
        # Playwright's general error type: here the element left the page
        # between two checks, a navigation replaced the document, or the
        # page's scripts broke the hit test. Each is the page's doing, so it
        # is drift. A closed page is not.
        if page.is_closed():
            raise
        return False


def _miss(count: int) -> Miss:
    return "no match" if count == 0 else "ambiguous"


async def _match(page: Page, locator: Locator, use: Use) -> ElementHandle | Miss:
    """The one element `locator` finds for `use`, or why it doesn't."""
    negative = use == "negative_check"
    query = await _scoped(page, locator, hidden=negative)
    if query is None:
        return "no scope"
    if negative:
        # A negative check counts what is on screen: a role locator sees past
        # the accessibility tree (aria-hidden), and only visible matches
        # count, so a hidden element with the target's name can't stand in
        # for the visible one a fallback finds.
        query = query.filter(visible=True)
    # Counted first, so a broad locator costs one call, not one per match.
    count = await query.count()
    if count != 1:
        return _miss(count)
    found = await query.element_handles()
    if len(found) != 1:
        # The page changed between the two calls.
        for element in found:
            await element.dispose()
        return _miss(len(found))
    [element] = found
    if use == "action" and not await _actionable(page, element):
        await element.dispose()
        return "not actionable"
    return element


@overload
async def resolve(
    page: Page, target: Target, use: Literal["action", "assertion"]
) -> Resolved | Unresolved: ...


@overload
async def resolve(
    page: Page, target: Target, use: Literal["negative_check"]
) -> Resolved | Absent | Unresolved: ...


async def resolve(
    page: Page, target: Target, use: Use
) -> Resolved | Absent | Unresolved:
    """`target`'s element for `use`, from its locators in order. The caller
    owns a resolved element's handle and disposes of it.

    - An action needs the first unique match that is actionable.
    - An assertion needs the first unique match; being attached is enough.
    - A negative check counts visible matches only, so it resolves to the
      first unique visible match. Only when no locator finds one, none finds
      several, and at least one finds no visible match in its scope is the
      element absent; otherwise it is drift. An unscoped locator's scope is
      the page.

    A css value that isn't valid CSS raises Playwright's Error: the script is
    broken, which is not drift.
    """
    misses: list[Miss] = []
    for index, locator in enumerate(target.locators):
        found = await _match(page, locator, use)
        if isinstance(found, ElementHandle):
            return Resolved(index, found)
        misses.append(found)
    if use == "negative_check" and "ambiguous" not in misses and "no match" in misses:
        return Absent(misses.index("no match"))
    return Unresolved(tuple(misses))


async def rendered_text(element: ElementHandle) -> str:
    """The element's rendered text, which text checks compare: its
    innerText, where CSS text-transform shows (LAB_NOTES, 2026-09-29)."""
    return await element.inner_text()
