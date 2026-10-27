"""A css scope by the run's own typed value (ADR-0025's #58 amendment): a
scope holding the value an earlier fill step typed finds only the elements
whose text holds it as whole words, ignoring case, so of two comment cards
by one author, the one the run posted."""

import html
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from aqa_core.compiled import ByCss, HeldValue, Locator, Target
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.text import has_text
from aqa_runner.browser_session import BrowserSession
from aqa_runner.locator_generation import LocatorError, Seen, TargetUses
from aqa_runner.locators import (
    Absent,
    Resolved,
    Shown,
    Unresolved,
    Use,
    css_query,
    resolve,
    resolve_every,
)
from aqa_runner.navigator import AssertCheck, Click, Fill, Finish
from aqa_runner.navigator_tools import Attempt, Finished

from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.navigator_fixtures import on_app, ref
from packages.runner.tests.pilot_pages import in_session, put
from packages.runner.tests.test_link_paths import CARD, comments
from packages.runner.tests.test_locator_generation import is_same
from packages.runner.tests.test_navigator_checks import BY, COMPILED

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


# Page texts and values a scope may hold, where the browser's match and
# `has_text`, which text checks compare by, must agree (ADR-0025's #58
# amendment): word boundaries, case and what normalizing strips.
AGREED = [
    ("éfoo bar", "foo bar"),
    ("Éfoo bar", "éfoo bar"),
    ("Thanks for the warm welcome!", "anks for the warm welcome!"),
    ("Thanks for the warm welcome!", "for the WARM"),
    ("Thanks for\n      the warm   welcome!", POSTED),
    ("Thanks\u00ad for the warm welcome!", POSTED),
    ("Tha\ue000nks for the warm welcome!", POSTED),
    ("x\u00adThanks for the warm welcome!", POSTED),
    ("foo_bar", "bar"),
    ("foo-bar", "bar"),
    ("STRASSE ist lang", "Straße"),
]


def holds(text: str, value: str) -> int:
    """How many paragraphs holding `text` a css scope holding `value` finds."""

    async def scenario(session: BrowserSession) -> int:
        # As character references: the page is served with no charset.
        written = html.escape(text).encode("ascii", "xmlcharrefreplace").decode()
        await put(session, f"<p>{written}</p>")
        scope = ByCss(css="p", holds_value=HeldValue(step=1, value=value))
        return await css_query(session.page, scope).count()

    return in_session(scenario)


@pytest.mark.parametrize(
    ("text", "value"),
    AGREED,
    ids=[
        "a letter beyond ASCII touches it",
        "case beyond ASCII",
        "a suffix of a longer word",
        "whole words of it in another case",
        "newlines and runs of spaces",
        "a soft hyphen normalizing strips",
        "a glyph inside a word",
        "a stripped character makes no boundary",
        "an underscore is a word character",
        "a hyphen is not",
        "a value whose case folding expands",
    ],
)
def test_a_held_value_holds_where_has_text_says_it_does(text: str, value: str) -> None:
    assert holds(text, value) == int(has_text(text, value))


def test_a_page_text_whose_case_folding_expands_holds_no_value_it_folds_to() -> None:
    # JavaScript folds case one character to one: a page's ß never finds
    # "ss", so the scope finds fewer cards than has_text would, never more.
    assert has_text("Straße", "STRASSE")
    assert holds("Straße", "STRASSE") == 0


AUTHOR = "the new comment's author name"
# Two values typed earlier, nearest first: the first holds in no card.
TYPED = (HeldValue(step=4, value="Nobody wrote this"), HeldValue(step=3, value=POSTED))


def is_held(locator: Locator) -> bool:
    return isinstance(locator, ByCss) and locator.holds_value is not None


@pytest.mark.parametrize(
    ("typed", "drop", "expected"),
    [
        (
            TYPED,
            None,
            (
                ByCss(
                    css='a[href="/profile/reader"]:not(:has(img))',
                    scope=ByCss(css="div.card", holds_value=TYPED[1]),
                ),
            ),
        ),
        ((), None, None),
        (TYPED, is_held, None),
    ],
    ids=["a value typed earlier", "no value typed", "every held scope dropped"],
)
def test_a_target_two_cards_share_is_scoped_by_a_value_typed_earlier(
    typed: tuple[HeldValue, ...],
    drop: Callable[[Locator], bool] | None,
    expected: tuple[Locator, ...] | None,
) -> None:
    # Only once no locator finds the link, alone or under a scope, does
    # generating try each scope again holding each value, nearest first.
    async def scenario(session: BrowserSession) -> tuple[Locator, ...]:
        await put(session, TWO)
        links = await session.page.query_selector_all(NAME_LINK)
        uses = TargetUses(
            AUTHOR,
            checks_text=True,
            drop=drop or (lambda _: False),
            typed=lambda: typed,
        )
        used = Seen(links[1], "link", "reader")
        index = await uses.add(session.page, used, "assertion")
        return uses.targets[index].locators

    if expected is None:
        with pytest.raises(LocatorError, match="finds the a element alone for an"):
            in_session(scenario)
    else:
        assert in_session(scenario) == expected


# reader's earlier comment, a comment box that posts the next one above it,
# and a search box.
COMMENTING = f"""<input placeholder="Search">
    <form class="card comment-form"><textarea placeholder="Write a comment...">
    </textarea><button type="button" id="post">Post Comment</button></form>
    <div class="comment-list">{CARD.format(text=EARLIER, who="reader")}</div>
    <template id="card">{CARD.format(text="", who="reader")}</template>
    <script>
        document.querySelector("#post").addEventListener("click", () => {{
            const text = document.querySelector("textarea").value;
            fetch("/write/comment", {{method: "POST"}}).then(() => {{
                const card = document.querySelector("#card").content.cloneNode(true);
                card.querySelector("p.card-text").textContent = text;
                document.querySelector(".comment-list").prepend(card);
            }});
        }});
    </script>"""
BOX, POST, SEARCH = "the comment box", "the post button", "the search box"
TEXT = "the new comment's text"


def checking(meaning: str, **check: str) -> CoveragePlan:
    """A plan whose one check reads `meaning`'s text for `check`'s literal
    or pattern."""
    return CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": meaning,
                    "claim": "reads as the check says",
                    "checks": [
                        {
                            "check": "text_in_target",
                            "target_meaning": meaning,
                            **check,
                        }
                    ],
                }
            ],
            "requires": [],
        }
    )


async def posted(attempt: Attempt) -> None:
    """Post the comment, then search for words of it."""
    box = ref(attempt, "textbox", "Write a comment...")
    await attempt.act(Fill(ref=box, meaning=BOX, text=POSTED))
    await attempt.observe()
    await attempt.act(Click(ref=ref(attempt, "button", "Post Comment"), meaning=POST))
    await attempt.observe()
    search = ref(attempt, "textbox", "Search")
    await attempt.act(Fill(ref=search, meaning=SEARCH, text="the warm welcome!"))
    await attempt.observe()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setitem(PAGES, "commenting", COMMENTING)
    with serving_app() as served:
        yield served


def test_explore_scopes_the_new_comments_author_by_the_nearest_value_it_typed(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> tuple[str, Finished]:
        await posted(attempt)
        # The posted card comes first.
        told = await attempt.check(
            AssertCheck(check_id="a1", ref=ref(attempt, "link", "reader"))
        )
        return told, await attempt.finish(Finish(steps=(1, 2, 3)), BY)

    told, finished = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/commenting",
        plan=checking(AUTHOR, text="reader"),
    )

    assert told.startswith("a1 is bound, and on this page it is ")
    assert finished.text == COMPILED
    assert finished.script is not None
    [target] = [
        each for each in finished.script.targets.values() if each.semantic == AUTHOR
    ]
    assert target.locators == (
        ByCss(
            css='a[href="/profile/reader"]:not(:has(img))',
            scope=ByCss(
                css="div.card",
                holds_value=HeldValue(step=3, value="the warm welcome!"),
            ),
        ),
    )


def test_explore_never_scopes_a_target_by_a_value_its_text_check_reads(
    app: App, tmp_path: Path
) -> None:
    # Found by the card holding the very text its check reads, a comment
    # that never posted would be drift, not the failure it is.
    async def scenario(attempt: Attempt) -> tuple[str, bool]:
        await posted(attempt)
        [card_text] = re.findall(
            r"- paragraph \[ref=(e\d+)\]: " + re.escape(POSTED),
            attempt.observation or "",
        )
        told = await attempt.check(AssertCheck(check_id="a1", ref=card_text))
        return told, attempt.checks["a1"] is None

    told, unbound = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/commenting",
        plan=checking(TEXT, text=POSTED),
    )

    assert told == (
        f"assert_check did not run: {TEXT}: no locator of the grammar finds the p "
        "element alone for an assertion"
    )
    assert unbound


def test_explore_never_runs_a_text_checks_pattern_against_a_typed_value(
    app: App, tmp_path: Path
) -> None:
    # A pattern runs only inside a run's bounded text search: a target its
    # pattern checks is held by no value, whichever (ADR-0025's #58
    # amendment), so a catastrophic one costs nothing here.
    pattern, value = "^(a+)+$", "a" * 28 + "!"

    async def scenario(attempt: Attempt) -> tuple[str, float]:
        box = ref(attempt, "textbox", "Write a comment...")
        await attempt.act(Fill(ref=box, meaning=BOX, text=value))
        await attempt.observe()
        await attempt.act(
            Click(ref=ref(attempt, "button", "Post Comment"), meaning=POST)
        )
        await attempt.observe()
        [card_text] = re.findall(
            r"- paragraph \[ref=(e\d+)\]: " + re.escape(value),
            attempt.observation or "",
        )
        started = time.monotonic()
        told = await attempt.check(AssertCheck(check_id="a1", ref=card_text))
        return told, time.monotonic() - started

    told, took = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/commenting",
        plan=checking(TEXT, pattern=pattern),
    )

    assert took < 5
    assert told == (
        f"assert_check did not run: {TEXT}: no locator of the grammar finds the p "
        "element alone for an assertion"
    )
