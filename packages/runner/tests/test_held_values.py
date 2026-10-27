"""A css scope by the run's own typed value (ADR-0025's #58 amendment): a
scope holding the value an earlier fill step typed finds only the elements
whose text holds it as whole words, ignoring case, so of two comment cards
by one author, the one the run posted."""

import pytest
from aqa_core.compiled import ByCss, HeldValue, Target
from aqa_runner.browser_session import BrowserSession
from aqa_runner.locators import (
    Absent,
    Resolved,
    Shown,
    Unresolved,
    Use,
    resolve,
    resolve_every,
)

from packages.runner.tests.pilot_pages import in_session, put
from packages.runner.tests.test_link_paths import comments
from packages.runner.tests.test_locator_generation import is_same

POSTED = "Thanks for the warm welcome!"
EARLIER = "Glad to be here, thanks for the warm welcome back."
# Two comments by reader: the name links tell the cards apart by nothing
# but the comment each card holds.
TWO = comments((EARLIER, "reader"), (POSTED, "reader"))
NAME_LINK = "a.comment-author:not(:has(img))"


def card(value: str) -> ByCss:
    """A comment card holding `value`, which step 3 typed."""
    return ByCss(css="app-article-comment", holds_value=HeldValue(step=3, value=value))


def author(value: str) -> Target:
    return Target(
        semantic="the new comment's author name",
        locators=(ByCss(css=NAME_LINK, scope=card(value)),),
    )


async def found_in(
    session: BrowserSession, target: Target, use: Use, *, every: bool = False
) -> int | Resolved | Shown | Absent | Unresolved:
    """Which name link, by its place on the page, `target` resolves to for
    `use`, or what it gave instead."""
    look = resolve_every if every else resolve
    found = await look(session.page, target, use)
    if not isinstance(found, Resolved):
        return found
    links = await session.page.query_selector_all(NAME_LINK)
    for index, link in enumerate(links):
        if await is_same(session.page, found.element, link):
            return index
    return found


@pytest.mark.parametrize(
    "value",
    [POSTED, "thanks FOR the  warm\twelcome!", "the warm welcome!"],
    ids=["as typed", "case and spacing", "whole words of it"],
)
@pytest.mark.parametrize("every", [False, True], ids=["resolve", "every locator"])
def test_a_scope_holding_the_typed_value_finds_the_card_that_holds_it(
    value: str, every: bool
) -> None:
    async def scenario(session: BrowserSession) -> object:
        await put(session, TWO)
        return await found_in(session, author(value), "assertion", every=every)

    assert in_session(scenario) == 1


@pytest.mark.parametrize(
    ("html", "value"),
    [
        (comments((EARLIER, "reader")), POSTED),  # the comment never posted
        (TWO, "Thanks for the warm"),  # held by both cards
        (TWO, "Thanks for the warm welcome!!"),
        (comments(("Thanksgiving for the warm welcome!", "reader")), "Thanks"),
    ],
    ids=[
        "no card holds it",
        "both cards hold it",
        "more than it holds",
        "part of a word",
    ],
)
def test_a_scope_holding_the_typed_value_needs_one_card_that_holds_it_as_words(
    html: str, value: str
) -> None:
    async def scenario(session: BrowserSession) -> object:
        await put(session, html)
        return await found_in(session, author(value), "assertion")

    assert in_session(scenario) == Unresolved(("no scope",))


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (TWO, Absent(0)),
        (comments((EARLIER, "reader")), Unresolved(("no scope",))),
    ],
    ids=["the card is there", "the card never posted"],
)
@pytest.mark.parametrize("every", [False, True], ids=["resolve", "every locator"])
def test_a_negative_check_under_a_held_scope_needs_the_card_to_stand(
    html: str, expected: object, every: bool
) -> None:
    # Nothing in the posted card is a delete control: absent only where the
    # card stands, and never because the comment never posted.
    gone = Target(
        semantic="the new comment's delete control",
        locators=(ByCss(css="span.mod-options", scope=card(POSTED)),),
    )

    async def scenario(session: BrowserSession) -> object:
        await put(session, html)
        return await found_in(session, gone, "negative_check", every=every)

    assert in_session(scenario) == expected
