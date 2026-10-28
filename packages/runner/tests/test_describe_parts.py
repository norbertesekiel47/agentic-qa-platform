"""The navigator's `describe_parts` (#53 P19b; ADR-0025's and ADR-0024's #53
P19 amendments): the attempt lists an element's parts for the next turn's
call, its check tools take a part's ref in their one ref slot, and every
action and every exit of the turns lets the parts go with their handles.
Real Chromium against the executor's fixture app, with a scripted model for
the turns."""

import asyncio
import json
import re
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import ByCss, Target
from aqa_core.config import ProjectConfig, SubjectContract
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.schema import Contract
from aqa_core.text import normalize
from aqa_runner.browser_session import BrowserSession
from aqa_runner.chat_client import Reply
from aqa_runner.evaluation import AssertionResult
from aqa_runner.explore_turns import Turning, take_turns
from aqa_runner.navigator import (
    AssertCheck,
    Click,
    DescribeParts,
    Fill,
    NoteForAbsence,
    Press,
)
from aqa_runner.navigator_parts import Parts
from aqa_runner.navigator_tools import Action, Attempt
from aqa_runner.redaction import GAP
from langchain_core.messages import AIMessage, BaseMessage
from playwright.async_api import JSHandle

from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    Scripted,
    answer,
    calling,
    record_of,
    tool,
    turned,
    turning,
)
from packages.runner.tests.explore_graph_fixtures import ref as graph_ref
from packages.runner.tests.navigator_fixtures import bound, on_app, ref
from packages.runner.tests.pilot_pages import _STYLESHEETS, RENDERINGS
from packages.runner.tests.secret_fixtures import copies_found

COUNT = "the favorites count on the article's favorite button"
COUNT_ROW = Contract(region="div.banner", part="span.counter", leaf=True)
COUNTER = Target(
    semantic=COUNT, locators=(ByCss(css="span.counter"),), contract=COUNT_ROW
)
FAVORITE = "the banner's favorite button"
BADGE = "the unsaved badge on the save button"

# Bound values a part's line could carry, as each request and turn record
# must not.
VALUE, HELD = "fake-secret-49", "fake-se\ufeffcret-58"
PUA, PUA1 = "\ue000", "\ue001"
HELD_PUA = f"fake-se{PUA}cret-58"
WITHHELD = "(text withheld: the described element's text holds a test secret)"

# A favorite button, in the banner and again in the article's actions.
COPY = (
    "<app-favorite-button><button>Unfavorite Article "
    '<i class="ion-heart"></i><span class="counter">(1)</span></button>'
    "</app-favorite-button>"
)
PARTS_PAGES = {
    "parts-article": (
        f'<div class="banner">{COPY}</div><div class="article-actions">{COPY}</div>'
    ),
    # The banner's button with its count flattened into its text.
    "parts-flattened": (
        '<div class="banner"><app-favorite-button><button>Unfavorite Article bonus '
        '1 (3)</button></app-favorite-button></div><div class="article-actions">'
        f"{COPY}</div>"
    ),
    # A save button's badge, which another button clears.
    "parts-badge": (
        '<nav class="bar"><button>Save <span class="badge">unsaved</span></button>'
        "</nav><button onclick=\"document.querySelector('span.badge').remove()\">"
        "Clear the badge</button>"
    ),
    # Conduit's favorited article, styled as the app is.
    "parts-conduit": (RENDERINGS / "article-favorited.html").read_text()
    + "".join(f"<style>{sheet.read_text()}</style>" for sheet in _STYLESHEETS),
    # A class spelling a value, one split by U+200D, a tag split by U+200D,
    # and a value holding U+FEFF, under a button the snapshot names Save.
    "parts-secrets": (
        '<meta charset="utf-8"><div class="banner"><button aria-label="Save">'
        '<span class="note fake-secret-49">your code</span>'
        '<span class="split fake-se&#x200D;cret-49">t</span>'
        '<x-fake-se\u200dcret-49 class="tagged">t</x-fake-se\u200dcret-49>'
        '<span class="held">fake-se&#65279;cret-58</span>'
        "</button></div>"
    ),
    # A value holding U+E000, with U+E001 written after it, elsewhere in a
    # text and before it in a tag, and with U+E000 left out.
    "parts-held": (
        '<meta charset="utf-8"><div class="banner"><button aria-label="Save">'
        '<span class="after">fake-se&#xE000;&#xE001;cret-58</span>'
        '<span class="elsewhere">your fa&#xE001;ke-se&#xE000;cret-58 code</span>'
        f'<x-fake-se{PUA1}{PUA}cret-58 class="tagged">t</x-fake-se{PUA1}{PUA}cret-58>'
        '<span class="left-out">fake-secret-58</span>'
        "</button></div>"
    ),
}


# A plan whose one check reads the favorites count.
COUNTED = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": COUNT,
                "claim": "shows one favorite",
                "checks": [
                    {"check": "text_in_target", "target_meaning": COUNT, "text": "(1)"}
                ],
            }
        ],
        "requires": [],
    }
)


EARLIER = (
    "assert_check did not run: p1.2 is from an earlier describe_parts, and parts "
    "serve only the call after it: describe the element again"
)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    for name, page in PARTS_PAGES.items():
        monkeypatch.setitem(PAGES, name, page)
    with serving_app() as served:
        yield served


class Held:
    """Every handle `Parts.describe` made, through `JSHandle.evaluate_handle`
    or `BrowserSession.locate`, and every handle disposed, by identity."""

    def __init__(self) -> None:
        self.made: list[JSHandle] = []
        self.gone: list[JSHandle] = []
        self.describing = False

    @property
    def count(self) -> int:
        """How many of the handles a description made are not yet disposed."""
        return sum(not any(made is gone for gone in self.gone) for made in self.made)


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> Held:
    seen = Held()
    describe = Parts.describe
    evaluate_handle = JSHandle.evaluate_handle
    locate = BrowserSession.locate
    dispose = JSHandle.dispose

    async def describing(
        self: Parts, session: BrowserSession, snapshot: str, ref: str
    ) -> Any:
        seen.describing = True
        try:
            return await describe(self, session, snapshot, ref)
        finally:
            seen.describing = False

    async def made(self: JSHandle, expression: str, arg: Any = None) -> JSHandle:
        handle = await evaluate_handle(self, expression, arg)
        if seen.describing:
            seen.made.append(handle)
        return handle

    async def located(self: BrowserSession, ref: str) -> Any:
        element = await locate(self, ref)
        if seen.describing:
            seen.made.append(element)
        return element

    async def disposed(self: JSHandle) -> None:
        seen.gone.append(self)
        await dispose(self)

    monkeypatch.setattr(Parts, "describe", describing)
    monkeypatch.setattr(JSHandle, "evaluate_handle", made)
    monkeypatch.setattr(BrowserSession, "locate", located)
    monkeypatch.setattr(JSHandle, "dispose", disposed)
    return seen


def button(attempt: Attempt, name: str) -> str:
    """The ref of the first button whose name ends with `name`: Conduit's
    icon font puts a glyph before it."""
    found = re.search(
        rf'- button "[^"]*{re.escape(name)}"[^\n]*?\[ref=(e\d+)\]',
        attempt.observation or "",
    )
    assert found is not None, attempt.observation
    return found[1]


def favorite(attempt: Attempt) -> str:
    """The ref of the banner's favorite button, the first of the page's."""
    return button(attempt, "Unfavorite Article (1)")


# The tools.


@pytest.mark.parametrize(
    ("action", "said"),
    [
        (
            lambda attempt: Click(ref=favorite(attempt), meaning=FAVORITE),
            "step 1 ran, and the page settled",
        ),
        (
            lambda _: Press(key="Tab+a"),
            (
                "press did not run: it takes one key, with only modifiers held "
                "before it, such as Enter or Shift+a"
            ),
        ),
    ],
    ids=["ran", "refused"],
)
def test_parts_are_void_after_an_action(
    app: App,
    tmp_path: Path,
    held: Held,
    action: Callable[[Attempt], Action],
    said: str,
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        await attempt.describe(DescribeParts(ref=favorite(attempt)))
        await attempt.parts.turned()
        serving = held.count
        acted = await attempt.act(action(attempt))
        after = held.count
        refused = await attempt.check(AssertCheck(check_id="a1", ref="p1.2"))
        return [serving, acted.text, after, refused]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/parts-article",
        plan=COUNTED,
        contracts={COUNT: COUNT_ROW},
    ) == [3, said, 0, EARLIER]


def test_the_favorites_count_binds_through_a_part_of_the_banner_button(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        banner = favorite(attempt)
        described = await attempt.describe(DescribeParts(ref=banner))
        await attempt.parts.turned()
        bound = await attempt.check(AssertCheck(check_id="a1", ref="p1.2"))
        return [
            banner,
            described.text,
            described.parts,
            bound,
            attempt.uses(COUNT).targets,
        ]

    banner, *rest = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/parts-conduit",
        plan=COUNTED,
        contracts={COUNT: COUNT_ROW},
    )
    assert rest == [
        f"describe_parts listed {banner}'s parts",
        (
            f"{banner}'s parts, for your next call only:\n"
            "- p1.1 i.ion-heart\n"
            '- p1.2 span.counter "(1)"'
        ),
        "a1 is bound, and on this page it is pass",
        (COUNTER,),
    ]


def test_a_flattened_banner_button_offers_no_part_for_the_count_and_the_check_stays_unbound(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        banner = ref(attempt, "button", "Unfavorite Article bonus 1 (3)")
        described = await attempt.describe(DescribeParts(ref=banner))
        refused = await attempt.check(AssertCheck(check_id="a1", ref=banner))
        return [banner, described.text, described.parts, refused, attempt.checks]

    banner, *rest = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/parts-flattened",
        plan=COUNTED,
        contracts={COUNT: COUNT_ROW},
    )
    assert rest == [
        f"{banner} has no element inside it",
        "",
        (
            "assert_check did not run: this check's subject is the element matching "
            "span.counter inside div.banner, and that region has none"
        ),
        {"a1": None},
    ]


def test_a_wrong_kind_of_ref_is_refused_by_name(app: App, tmp_path: Path) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        described = await attempt.describe(DescribeParts(ref="p1.2"))
        checked = await attempt.check(AssertCheck(check_id="a1", ref="o1"))
        return [described.text, described.parts, checked]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/parts-article",
        plan=COUNTED,
        contracts={COUNT: COUNT_ROW},
    ) == [
        "describe_parts did not run: 'p1.2' isn't a ref in the current snapshot",
        "",
        "assert_check did not run: 'o1' isn't a ref in the current snapshot",
    ]


def test_describe_parts_does_nothing_once_the_attempt_has_ended(
    app: App, tmp_path: Path, held: Held
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        await attempt.act(Fill(ref=favorite(attempt), meaning=FAVORITE, text="x"))
        described = await attempt.describe(DescribeParts(ref=favorite(attempt)))
        return [described.text, described.parts, attempt.parts.described, held.made]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/parts-article",
        plan=COUNTED,
        contracts={COUNT: COUNT_ROW},
    ) == [
        (
            "describe_parts did not run: this attempt has ended, since step 1 was "
            "dispatched and its completion isn't on disk"
        ),
        "",
        0,
        [],
    ]


def test_note_for_absence_takes_a_part_and_its_check_binds_once_it_is_gone(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        save = ref(attempt, "button", "Save unsaved")
        described = await attempt.describe(DescribeParts(ref=save))
        await attempt.parts.turned()
        noted = await attempt.note(NoteForAbsence(check_id="a1", ref="p1.1"))
        clear = ref(attempt, "button", "Clear the badge")
        await attempt.act(Click(ref=clear, meaning="the page's clear button"))
        await attempt.observe()
        bound = await attempt.check(AssertCheck(check_id="a1", ref=None))
        return [save, described.parts, noted, bound, attempt.uses(BADGE).targets]

    save, *rest = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/parts-badge",
        plan=CoveragePlan.model_validate(
            {
                "expectations": [
                    {
                        "expect_index": 0,
                        "subject": BADGE,
                        "claim": "goes once the badge is cleared",
                        "checks": [{"check": "not_visible", "target_meaning": BADGE}],
                    }
                ],
                "requires": [],
            }
        ),
    )
    assert rest == [
        f'{save}\'s parts, for your next call only:\n- p1.1 span.badge "unsaved"',
        (
            "a1's element is noted where it shows: call assert_check for a1 with no "
            "ref once it is gone"
        ),
        "a1 is bound, and on this page it is pass",
        (
            Target(
                semantic=BADGE,
                locators=(ByCss(scope=ByCss(css="nav.bar"), css="span.badge"),),
            ),
        ),
    ]


# The turns.

# The project's row for the count's contract, which compiling checks.
CONFIG = ProjectConfig(
    subjects=(
        SubjectContract(
            spec="replay", expect=0, region="div.banner", part="span.counter", leaf=True
        ),
    )
)


def favorite_in(request: Sequence[BaseMessage]) -> str:
    """The ref the request's snapshot gives the banner's favorite button."""
    found = re.search(
        r'- button "[^"]*Unfavorite Article \(1\)"[^\n]*?\[ref=(e\d+)\]',
        request[-1].text,
    )
    assert found is not None, request[-1].text
    return found[1]


def save_in(request: Sequence[BaseMessage]) -> str:
    """The ref the request's snapshot gives the Save button."""
    return graph_ref(request, "button", "Save")


# A scripted move that answers at once: a reply, or a function of the request.
type Plain = Reply | Callable[[Sequence[BaseMessage]], Reply]

DESCRIBE = tool("describe_parts", lambda m: {"ref": favorite_in(m)})
WHOLE = tool("assert_check", lambda m: {"check_id": "a1", "ref": favorite_in(m)})
ASSERT_PART = calling("assert_check", {"check_id": "a1", "ref": "p1.2"})
NOTES = answer(AIMessage(content="notes only"))
FINISH = calling("finish", {"steps": []})


def blocks(request: Sequence[BaseMessage]) -> list[str]:
    """The parts blocks of a navigator request's turn."""
    found: list[str] = re.findall(r"<parts>\n.*?\n</parts>", request[2].text, re.DOTALL)
    return found


def results(request: Sequence[BaseMessage]) -> str:
    """The results block of a navigator request's turn."""
    found = re.search(r"<results>\n(.*?)\n</results>", request[2].text, re.DOTALL)
    assert found is not None
    return found[1]


def stops(context: Turning) -> list[tuple[str, str]]:
    return [(stop.outcome, stop.reason) for stop in context.stops]


def banner_parts(ref: str) -> str:
    return (
        f"<parts>\n{ref}'s parts, for your next call only:\n"
        '- p1.1 i.ion-heart\n- p1.2 span.counter "(1)"\n</parts>'
    )


def on_the_article(
    app: App,
    tmp_path: Path,
    client: Scripted,
    *,
    turns: int = 40,
    page: str = "parts-article",
) -> Turning:
    return turned(
        app,
        tmp_path,
        client,
        turns=turns,
        page=f"/page/{page}",
        plan=COUNTED,
        contracts={COUNT: COUNT_ROW},
        config=CONFIG,
    )


def test_a_part_described_in_one_turn_binds_in_the_next(
    app: App, tmp_path: Path, held: Held
) -> None:
    client = Scripted(DESCRIBE, ASSERT_PART, FINISH)

    context = on_the_article(app, tmp_path, client)

    banner = favorite_in(client.requests[0])
    second = client.requests[1][2].text
    assert [blocks(request) for request in client.requests] == [
        [],
        [banner_parts(banner)],
        [],
    ]
    assert second.index("</results>") < second.index("<parts>")
    assert second.index("</parts>") < second.index("<snapshot>")
    assert [results(request) for request in client.requests[1:]] == [
        f"describe_parts listed {banner}'s parts",
        "a1 is bound, and on this page it is pass",
    ]
    assert stops(context) == []
    assert context.script is not None
    assert (len(held.made), held.count) == (4, 0)


def test_parts_are_void_two_turns_later(app: App, tmp_path: Path, held: Held) -> None:
    during: list[int] = []

    def counting(move: Plain) -> Callable[[Sequence[BaseMessage]], Reply]:
        """`move`, noting how many of the parts' handles are held as the
        model is called."""

        def counted(messages: Sequence[BaseMessage]) -> Reply:
            during.append(held.count)
            return move if isinstance(move, Reply) else move(messages)

        return counted

    moves: list[Plain] = [DESCRIBE, NOTES, ASSERT_PART, NOTES]
    client = Scripted(*map(counting, moves))

    context = on_the_article(app, tmp_path, client, turns=4)

    assert [bool(blocks(request)) for request in client.requests] == [
        False,
        True,
        False,
        False,
    ]
    assert results(client.requests[3]) == EARLIER
    # Held through the serving turn's call only, and let go at its end.
    assert during == [0, 3, 0, 0]
    assert stops(context) == [("gave_up", "the attempt took all 4 of its turns")]
    assert (len(held.made), held.count) == (4, 0)


def test_the_turns_budget_ending_after_a_describe_lets_its_parts_go(
    app: App, tmp_path: Path, held: Held
) -> None:
    client = Scripted(DESCRIBE)

    context = on_the_article(app, tmp_path, client, turns=1)

    assert stops(context) == [("gave_up", "the attempt took all 1 of its turns")]
    assert (len(held.made), held.count) == (4, 0)


def test_a_cancelled_serving_turn_lets_its_parts_go(
    app: App, tmp_path: Path, held: Held
) -> None:
    limits: list[asyncio.Timeout] = []
    serving: list[int] = []

    async def cancelled(_: Sequence[BaseMessage]) -> Reply:
        serving.append(held.count)
        limits[0].reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(3600)
        raise AssertionError("the turn was never cancelled")

    client = Scripted(DESCRIBE, cancelled)

    async def scenario(attempt: Attempt) -> tuple[str, int]:
        context = turning(attempt, client)
        try:
            async with asyncio.timeout(None) as limit:
                limits.append(limit)
                await take_turns(context)
        except TimeoutError:
            return "cancelled", held.count
        return "finished", held.count

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/parts-article",
        plan=COUNTED,
        contracts={COUNT: COUNT_ROW},
    ) == ("cancelled", 0)
    assert serving == [3]


def test_a_scripted_model_offering_the_whole_button_for_the_count_is_refused_and_binds_its_part(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(WHOLE, DESCRIBE, ASSERT_PART, NOTES)

    context = on_the_article(app, tmp_path, client, turns=4)

    # Each snapshot gives its elements refs of its own.
    banner = favorite_in(client.requests[1])
    assert [results(request) for request in client.requests[1:]] == [
        (
            "assert_check did not run: this check's subject is the one element "
            "matching span.counter inside div.banner, and this element is another "
            "one (describe_parts lists an element's parts)"
        ),
        f"describe_parts listed {banner}'s parts",
        "a1 is bound, and on this page it is pass",
    ]
    assert context.attempt.checks == {
        "a1": AssertionResult("a1", "pass", locator_index=0)
    }


def test_no_compiled_locator_from_a_part_is_positional(
    app: App, tmp_path: Path
) -> None:
    client = Scripted(DESCRIBE, ASSERT_PART, FINISH)

    context = on_the_article(app, tmp_path, client, page="parts-conduit")

    script = context.script
    assert script is not None
    assert script.targets == {"t1": COUNTER}
    assert "nth" not in json.dumps(
        [target.model_dump(mode="json") for target in script.targets.values()]
    )


def visible(text: str) -> str:
    """`text` as it shows: without its gaps, normalized."""
    return normalize(GAP.sub("", text))


def everything_sent(client: Scripted, context: Turning) -> list[str]:
    """Every request message sent and every turn record written, each also
    as it shows."""
    sent = [message.text for request in client.requests for message in request]
    records = [
        path.read_text() for path in sorted((record_of(context) / "turns").iterdir())
    ]
    return [*sent, *records, *map(visible, [*sent, *records])]


def described_with_secrets(
    app: App, tmp_path: Path, page: str, secrets: dict[str, str]
) -> tuple[Scripted, Turning]:
    """The turns of an attempt on `page` that bind `secrets`, by name: one
    describes the Save button's parts, and the next reads them."""
    client = Scripted(tool("describe_parts", lambda m: {"ref": save_in(m)}), NOTES)
    context = turned(
        app,
        tmp_path,
        client,
        turns=2,
        page=f"/page/{page}",
        plan=COUNTED,
        secrets=[bound(app.origin, value, name) for name, value in secrets.items()],
    )
    return client, context


def test_a_part_description_holding_secrets_reaches_the_model_as_markers(
    app: App, tmp_path: Path, held: Held
) -> None:
    client, context = described_with_secrets(
        app,
        tmp_path,
        "parts-secrets",
        {"TEST_PASSWORD": VALUE, "TEST_HELD": HELD},
    )

    save = save_in(client.requests[0])
    assert blocks(client.requests[1]) == [
        (
            f"<parts>\n{save}'s parts, for your next call only:\n"
            f"- p1.1 span.note.[SECRET:TEST_PASSWORD] {WITHHELD}\n"
            f"- p1.2 span.split {WITHHELD}\n"
            f"- p1.3 x-[SECRET:TEST_PASSWORD].tagged {WITHHELD}\n"
            f"- p1.4 span.held {WITHHELD}\n</parts>"
        )
    ]
    texts = everything_sent(client, context)
    assert [
        copies_found(value, texts=texts) for value in (VALUE, HELD, visible(HELD))
    ] == [[], [], []]
    assert (len(held.made), held.count) == (6, 0)


def test_a_value_holding_a_gap_written_with_other_gaps_reaches_the_model_as_its_marker(
    app: App, tmp_path: Path, held: Held
) -> None:
    client, context = described_with_secrets(
        app, tmp_path, "parts-held", {"TEST_HELD": HELD_PUA}
    )

    save = save_in(client.requests[0])
    assert blocks(client.requests[1]) == [
        (
            f"<parts>\n{save}'s parts, for your next call only:\n"
            f"- p1.1 span.after {WITHHELD}\n"
            f"- p1.2 span.elsewhere {WITHHELD}\n"
            f"- p1.3 x-[SECRET:TEST_HELD].tagged {WITHHELD}\n"
            f"- p1.4 span.left-out {WITHHELD}\n</parts>"
        )
    ]
    snapshot = re.search(r'- button "Save"[^\n]*', client.requests[0][2].text)
    assert snapshot is not None
    assert snapshot[0] == (
        f'- button "Save" [ref={save}]: '
        "[SECRET:TEST_HELD]your [SECRET:TEST_HELD] codet[SECRET:TEST_HELD]"
    )
    texts = everything_sent(client, context)
    assert [
        copies_found(value, texts=texts) for value in (HELD_PUA, visible(HELD_PUA))
    ] == [
        [],
        [],
    ]
    assert (len(held.made), held.count) == (6, 0)
