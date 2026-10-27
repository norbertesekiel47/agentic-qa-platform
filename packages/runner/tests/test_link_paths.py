"""A link's stable path as a structural locator (ADR-0025's #58 amendment):
the feed's article previews and the header's links, which the snapshot leaves
unnamed or a text check may not locate by name, are told apart by their
`href`; a path with a generated part, a query or a fragment never is."""

import pytest
from aqa_core.compiled import ByCss, Locator, Target
from aqa_runner.browser_session import BrowserSession
from aqa_runner.locator_generation import (
    LocatorError,
    Seen,
    generate_for_action,
    generate_for_assertion,
    seen_element,
)
from aqa_runner.locators import Resolved, resolve

from packages.runner.tests.pilot_pages import in_session, put, show
from packages.runner.tests.test_locator_generation import is_same, ref_of

PREVIEWS = (
    "notes-on-reading-lists",
    "flaky-tests-are-bugs",
    "dragons-of-the-north",
    "css-grid-for-layouts",
    "measuring-page-performance",
    "a-readers-first-post",
    "writing-accessible-forms",
    "angular-signals-in-practice",
    "testing-without-flakes",
    "welcome-to-conduit",
)


def test_each_feed_preview_link_is_found_by_its_path_alone() -> None:
    # The snapshot names none of the ten preview links, and nothing else
    # tells one card from the next.
    async def scenario(session: BrowserSession) -> list[tuple[Locator, ...] | bool]:
        await show(session, "home")
        found: list[tuple[Locator, ...] | bool] = []
        for slug in PREVIEWS:
            link = await session.page.query_selector(
                f'a.preview-link[href="/article/{slug}"]'
            )
            assert link is not None
            locators = await generate_for_action(session.page, Seen(link, "link"))
            target = Target(semantic="a feed preview link", locators=locators)
            resolved = await resolve(session.page, target, "action")
            found.append(locators)
            found.append(
                isinstance(resolved, Resolved)
                and await is_same(session.page, resolved.element, link)
            )
        return found

    found = in_session(scenario)

    assert found == [
        each
        for slug in PREVIEWS
        for each in ((ByCss(css=f'a[href="/article/{slug}"]'),), True)
    ]


NAV = ByCss(css="ul.navbar-nav")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("New Article", ByCss(css='a[href="/editor"]')),
        ("Settings", ByCss(css='a[href="/settings"]')),
        # The feed's author links share this path, so the header scopes it.
        ("reader", ByCss(css='a[href="/profile/reader"]', scope=NAV)),
    ],
)
def test_a_text_checks_header_link_is_found_by_its_path(
    name: str, expected: Locator
) -> None:
    # A text check may not locate a link by its name, which is the text.
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await show(session, "home")
        snapshot = await session.snapshot()
        used = await seen_element(session, snapshot, ref_of(snapshot, "link", name))
        return await generate_for_assertion(session.page, used, checks_text=True)

    assert in_session(scenario) == (expected,)


@pytest.mark.parametrize(
    ("href", "found"),
    [
        ("/article/testing-without-flakes", True),
        ("/article/Benchmarks-we-trust-5d7dc78d", False),  # a generated suffix
        # Conduit's suffix is crypto.randomUUID().slice(0, 8): all digits, or
        # all hex letters, are a generated part too.
        ("/article/Benchmarks-we-trust-12345678", False),
        ("/article/Benchmarks-we-trust-deadbeef", False),
        ("/user/u-1234567", False),
        ("/article/css-grid-for-layouts", True),
        ("/article/tests?page=2", False),
        ("/article/tests#comments", False),
        ("//elsewhere.test/article/tests", False),
        ("//elsewhere/article/tests", False),
        ("/article/", False),  # an empty segment
        ("/article/2026", False),
        ("article/tests", False),
    ],
)
def test_only_a_stable_path_on_the_origin_locates_a_link(
    href: str, found: bool
) -> None:
    card = '<div class="card"><a class="go" href="{}">Read</a></div>'
    html = card.format(href) + card.format("/elsewhere")

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        link = await session.page.query_selector("a.go")
        assert link is not None
        return await generate_for_action(session.page, Seen(link, "link", "Read"))

    if found:
        assert in_session(scenario) == (ByCss(css=f'a[href="{href}"]'),)
    else:
        with pytest.raises(LocatorError, match="finds the a element alone"):
            in_session(scenario)


LONG = "x" * 1100


@pytest.mark.parametrize(
    ("html", "css", "role", "name", "expected"),
    [
        (
            f'<a href="https://track.example/?u={LONG}"><button id="go">Go</button></a>',
            "button",
            "button",
            "Go",
            ByCss(css="#go"),
        ),
        (
            f'<a id="settings" href="/login?state={LONG}">Settings</a>',
            "a",
            "link",
            "Settings",
            ByCss(css="#settings"),
        ),
    ],
    ids=["a button inside a long tracking link", "a link with an id and a long href"],
)
def test_a_long_href_never_costs_the_element_its_other_locators(
    html: str, css: str, role: str, name: str, expected: Locator
) -> None:
    # Past the facts' size bound, an href is left out rather than cut into a
    # candidate, so the rest of the element's facts still stand.
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        element = await session.page.query_selector(css)
        assert element is not None
        return await generate_for_action(session.page, Seen(element, role, name))

    assert expected in in_session(scenario)


# A Conduit comment card, as `article-comment.component.ts` renders it: the
# author's image link and name link share a class and a path, and only what
# they hold tells them apart. The header's profile link holds an image too.
CARD = """<app-article-comment><div class="card"><div class="card-block">
    <p class="card-text"> {text} </p></div><div class="card-footer">
    <a class="comment-author" href="/profile/{who}"><img src="/a.svg"
        class="comment-author-img"></a>
    &nbsp;<a class="comment-author" href="/profile/{who}"> {who} </a>
    <span class="date-posted"> October 26, 2026 </span></div></div>
    </app-article-comment>"""
HEADER = """<nav class="navbar"><ul class="nav navbar-nav"><li class="nav-item">
    <a class="nav-link" href="/profile/reader"><img class="user-pic" src="/a.svg">
    reader </a></li></ul></nav>"""


def comments(*cards: tuple[str, str]) -> str:
    """The header and a comment list of `cards`, each its text and author."""
    listed = "".join(CARD.format(text=text, who=who) for text, who in cards)
    return f'{HEADER}<div class="comment-list">{listed}</div>'


def test_a_comment_authors_name_link_is_found_as_the_link_that_holds_no_image() -> None:
    # post-comment's a2 reads the new comment's author name, which a text
    # check may not locate by name (#58).
    html = comments(
        ("Glad to be here.", "jake"), ("Thanks for the warm welcome!", "reader")
    )

    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, html)
        links = await session.page.query_selector_all("a.comment-author")
        return await generate_for_assertion(
            session.page, Seen(links[3], "link", "reader"), checks_text=True
        )

    assert in_session(scenario) == (
        ByCss(css='a[href="/profile/reader"]:not(:has(img))'),
    )
