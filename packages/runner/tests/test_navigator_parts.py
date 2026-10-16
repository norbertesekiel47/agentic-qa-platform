"""Parts (#53 P19a; ADR-0025's #53 P19 amendment): `Parts.describe` lists
the elements inside a ref's element that the snapshot gives no ref of their
own, each under a part's ref `p<g>.<k>` with its tag, its stable classes and
its text, every string the page chose scanned so that no bound value reaches
the model, and every text withheld while the described element's own text
holds one. A part serves only the turn after its description, and binds
only while it is still inside its element, as `Seen(handle)` through its
check's `TargetUses`. Real Chromium."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aqa_core.compiled import ByCss, Target
from aqa_core.project import SecretDestination
from aqa_core.schema import Contract
from aqa_core.text import normalize
from aqa_runner import reasons
from aqa_runner.bound_secrets import GAP, BoundSecret
from aqa_runner.browser_session import BrowserSession, RefError, open_browser_session
from aqa_runner.document_origins import DocumentChangedError
from aqa_runner.locator_generation import (
    LocatorError,
    Seen,
    TargetUses,
    register_identity_engine,
)
from aqa_runner.navigator import Prefix, Turn, navigator_request
from aqa_runner.navigator_parts import Parts
from aqa_runner.redaction import NO_SECRETS, Redactor
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import JSHandle, async_playwright
from pydantic import SecretStr

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.explore_fixtures import MODES
from packages.runner.tests.pilot_pages import ORIGIN, put, show
from packages.runner.tests.secret_fixtures import copies_found

COUNT = "the favorites count on the article's favorite button"
COUNT_ROW = Contract(region="div.banner", part="span.counter", leaf=True)
COUNTER = Target(
    semantic=COUNT, locators=(ByCss(css="span.counter"),), contract=COUNT_ROW
)
BANNER = (
    '<div class="banner"><app-favorite-button><button>Unfavorite Article '
    '<i class="ion-heart"></i><span class="counter">(1)</span></button>'
    "</app-favorite-button></div>"
)
BANNER_PARTS = (
    "e1's parts, for your next call only:\n"
    "- p1.1 i.ion-heart\n"
    '- p1.2 span.counter "(1)"'
)


def in_session[T](
    scenario: Callable[[BrowserSession], Awaitable[T]], redactor: Redactor = NO_SECRETS
) -> T:
    async def run() -> T:
        async with async_playwright() as playwright:
            await register_identity_engine(playwright)
            async with (
                egress_proxy() as egress,
                open_browser_session(
                    playwright.chromium, egress=egress, redactor=redactor
                ) as session,
                asyncio.timeout(90),
            ):
                return await scenario(session)

    return asyncio.run(run())


def bound(**values: str) -> Redactor:
    where = SecretDestination((ORIGIN,), "password")
    return Redactor(
        [BoundSecret(name, SecretStr(value), where) for name, value in values.items()]
    )


def button_refs(snapshot: str, name: str) -> list[str]:
    """The refs of the buttons whose name ends with `name`: Conduit's icon
    font puts a glyph before it."""
    return re.findall(
        rf'- button "[^"]*{re.escape(name)}"[^\n]*?\[ref=(e\d+)\]', snapshot
    )


def first(snapshot: str, role: str) -> str:
    found = re.search(rf"- {role}\b[^\n]*?\[ref=(e\d+)\]", snapshot)
    assert found is not None, snapshot
    return found[1]


async def bound_to(
    session: BrowserSession, uses: TargetUses, seen: Seen
) -> Target | str:
    """The target `uses` gives the element `seen` offers for an assertion, or
    the refusal the model would read."""
    try:
        async with session.observing_page() as page:
            index = await uses.add(page, seen, "assertion")
    except LocatorError as error:
        return reasons.reason(error, session.redactor)
    return uses.targets[index]


def count_uses() -> TargetUses:
    return TargetUses(COUNT, checks_text=True, contract=COUNT_ROW)


async def refusal(call: Awaitable[object], session: BrowserSession) -> str:
    try:
        await call
    except (RefError, DocumentChangedError) as error:
        return reasons.reason(error, session.redactor)
    raise AssertionError("no refusal")


class Handles:
    """Every handle made through `JSHandle.evaluate_handle` or
    `BrowserSession.locate`, and every one disposed, by identity."""

    def __init__(self) -> None:
        self.made: list[JSHandle] = []
        self.gone: list[JSHandle] = []

    @property
    def held(self) -> int:
        return sum(not any(made is gone for gone in self.gone) for made in self.made)


@pytest.fixture
def handles(monkeypatch: pytest.MonkeyPatch) -> Handles:
    seen = Handles()
    evaluate_handle = JSHandle.evaluate_handle
    locate = BrowserSession.locate
    dispose = JSHandle.dispose

    async def made(self: JSHandle, expression: str, arg: Any = None) -> JSHandle:
        handle = await evaluate_handle(self, expression, arg)
        seen.made.append(handle)
        return handle

    async def located(self: BrowserSession, ref: str) -> Any:
        element = await locate(self, ref)
        seen.made.append(element)
        return element

    async def disposed(self: JSHandle) -> None:
        seen.gone.append(self)
        await dispose(self)

    monkeypatch.setattr(JSHandle, "evaluate_handle", made)
    monkeypatch.setattr(BrowserSession, "locate", located)
    monkeypatch.setattr(JSHandle, "dispose", disposed)
    return seen


def test_the_banner_buttons_parts_list_the_count_and_it_binds() -> None:
    async def scenario(session: BrowserSession) -> tuple[str, str, Target | str]:
        await show(session, "article-favorited")
        snapshot = await session.snapshot()
        banner, _ = button_refs(snapshot, "Unfavorite Article (1)")
        parts = Parts()
        described = await parts.describe(session, banner)
        await parts.turned()
        target = await bound_to(
            session, count_uses(), await parts.seen(session, "p1.2")
        )
        return described.text, described.parts, target

    assert in_session(scenario) == (
        "describe_parts listed e28's parts",
        (
            "e28's parts, for your next call only:\n"
            "- p1.1 i.ion-heart\n"
            '- p1.2 span.counter "(1)"'
        ),
        COUNTER,
    )


def test_describe_parts_refuses_a_stale_ref_or_a_parts_ref() -> None:
    async def scenario(session: BrowserSession) -> tuple[str, str]:
        await put(session, BANNER)
        await session.snapshot()
        await session.snapshot()
        parts = Parts()
        return (
            await refusal(parts.describe(session, "e1"), session),
            await refusal(parts.describe(session, "p1.2"), session),
        )

    assert in_session(scenario) == (
        (
            "'e1' is from an older snapshot: a ref acts only on the snapshot that "
            "gave it, so take a new snapshot"
        ),
        "'p1.2' isn't a ref in the current snapshot",
    )


LEFT = "p1.2 has left the element describe_parts listed it under: describe that element again"


@pytest.mark.parametrize(
    "change",
    [
        (
            "document.querySelector('span.counter').replaceWith(Object.assign("
            "document.createElement('span'), {className: 'counter', textContent: '(1)'}))"
        ),
        "document.querySelector('div.banner').append(document.querySelector('span.counter'))",
        (
            "Node.prototype.contains = () => true; Object.defineProperty(Node.prototype, "
            "'isConnected', {get: () => true}); document.querySelector('span.counter').remove()"
        ),
        (
            "const button = document.querySelector('button'); "
            "button.replaceWith(button.cloneNode(true))"
        ),
    ],
    ids=["replaced", "moved-out", "lying-contains-and-isConnected", "element-replaced"],
)
def test_a_part_replaced_between_turns_is_refused(change: str) -> None:
    async def scenario(session: BrowserSession) -> str:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        await parts.describe(session, button)
        await parts.turned()
        await session.page.evaluate(change)
        return await refusal(parts.seen(session, "p1.2"), session)

    assert in_session(scenario) == LEFT


LIES = {
    "the-copys-count": "document.querySelector('div.article-actions span.counter')",
    "the-element-itself": "this",
}


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        ("the-copys-count", '- p1.1 span.counter "(1)"'),
        (
            "the-element-itself",
            (
                '- p1.1 button "Unfavorite Article (1)"\n'
                '  - p1.2 button "Unfavorite Article (1)"\n'
                '    - p1.3 button "Unfavorite Article (1)"\n'
                '      - p1.4 button "Unfavorite Article (1)"'
            ),
        ),
    ],
    ids=list(LIES),
)
def test_a_page_that_lies_about_its_children_cannot_bind_a_part_outside_its_element(
    listed: str, expected: str
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, str]:
        await put(session, BANNER + BANNER.replace("banner", "article-actions"))
        banner, _ = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        await session.page.evaluate(f"""() => {{
            const real = Object.getOwnPropertyDescriptor(Element.prototype, 'children').get;
            Object.defineProperty(Element.prototype, 'children', {{get() {{
                return this.localName === 'button' && this.closest('div.banner')
                    ? [{LIES[listed]}] : real.call(this);
            }}}});
        }}""")
        parts = Parts()
        described = await parts.describe(session, banner)
        await parts.turned()
        return described.parts, await refusal(parts.seen(session, "p1.1"), session)

    assert in_session(scenario) == (
        f"e2's parts, for your next call only:\n{expected}",
        (
            "p1.1 has left the element describe_parts listed it under: describe that "
            "element again"
        ),
    )


def cancel_after_disposing(
    monkeypatch: pytest.MonkeyPatch, picked: Callable[[JSHandle], bool]
) -> None:
    """Cancel the running task right after the real dispose of the first
    handle `picked` chooses, as a cancellation arriving while dispose awaits
    its reply."""
    dispose = JSHandle.dispose
    fired: list[JSHandle] = []

    async def disposed(self: JSHandle) -> None:
        await dispose(self)
        if not fired and picked(self):
            fired.append(self)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    monkeypatch.setattr(JSHandle, "dispose", disposed)


async def cancelled(call: Awaitable[object]) -> str:
    """What `call` raised, once the cancellation it raised is withdrawn so
    the scenario goes on."""
    try:
        await call
    except asyncio.CancelledError:
        task = asyncio.current_task()
        assert task is not None
        task.uncancel()
        return "CancelledError"
    return "returned"


def test_a_cancellation_while_parts_are_let_go_still_disposes_every_handle(
    handles: Handles, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(session: BrowserSession) -> tuple[int, str, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        await parts.describe(session, button)
        await parts.turned()
        before = handles.held
        cancel_after_disposing(monkeypatch, lambda _: True)
        return before, await cancelled(parts.void()), handles.held

    assert in_session(scenario) == (3, "CancelledError", 0)


def test_a_cancellation_while_the_reading_lets_its_temporary_go_disposes_the_parts(
    handles: Handles, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, int, int, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        # The reading's temporary is the first handle made after the element.
        cancel_after_disposing(monkeypatch, lambda handle: handle is handles.made[1])
        raised = await cancelled(parts.describe(session, button))
        gone = len(handles.gone)
        await parts.void()
        return raised, len(handles.made), handles.held, len(handles.gone) - gone

    assert in_session(scenario) == ("CancelledError", 4, 0, 0)


def test_a_dispose_failure_under_a_cancellation_leaves_the_cancellation_raised(
    handles: Handles, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispose = JSHandle.dispose
    failing: list[bool] = []

    async def disposed(self: JSHandle) -> None:
        await dispose(self)
        if failing:
            failing.clear()
            raise PlaywrightError("the dispose failed")

    monkeypatch.setattr(JSHandle, "dispose", disposed)

    async def scenario(session: BrowserSession) -> tuple[str, str, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        await parts.describe(session, button)
        failing.append(True)

        async def turns() -> None:
            try:
                await asyncio.sleep(3600)
            finally:
                await parts.void()

        task = asyncio.create_task(turns())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as error:
            return "CancelledError", type(error.__cause__).__name__, handles.held
        return "returned", "", handles.held

    assert in_session(scenario) == ("CancelledError", "Error", 0)


FAILED_READS = {
    "malformed-facts": "() => Object.defineProperty(Element.prototype, 'classList', "
    "{get() { return [{}]; }})",
    "pushstate-during-walk": """() => {
        const real = Object.getOwnPropertyDescriptor(Element.prototype, 'children').get;
        let once = false;
        Object.defineProperty(Element.prototype, 'children', {get() {
            if (this.localName === 'span' && !once) {
                once = true; history.pushState({}, '', '/moved');
            }
            return real.call(this);
        }});
    }""",
    "non-element": """() => {
        const real = Object.getOwnPropertyDescriptor(Element.prototype, 'children').get;
        Object.defineProperty(Element.prototype, 'children', {get() {
            return this.localName === 'button'
                ? [...real.call(this), {localName: 'span', classList: ['fake'], children: []}]
                : real.call(this);
        }});
    }""",
    "throwing-getter": "() => Object.defineProperty(Element.prototype, 'children', "
    "{get() { throw new Error('no'); }})",
}
LIED = "the page described the element's parts as no element is"


@pytest.mark.parametrize(
    ("failing", "expected"),
    [
        ("malformed-facts", (LIED, 2, 0)),
        (
            "pushstate-during-walk",
            (
                (
                    "the page changed while the session looked at it, so what it saw "
                    "was discarded: take a new snapshot, or look the target up again"
                ),
                4,
                0,
            ),
        ),
        ("non-element", (LIED, 5, 0)),
        (
            "throwing-getter",
            (
                (
                    "the element's parts couldn't be read: it left the page, or the "
                    "page's scripts broke the reading"
                ),
                1,
                0,
            ),
        ),
    ],
)
def test_every_handle_a_failed_read_makes_is_let_go(
    failing: str, expected: tuple[str, int, int], handles: Handles
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, int, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        await session.page.evaluate(FAILED_READS[failing])
        refused = await refusal(Parts().describe(session, button), session)
        return refused, len(handles.made), handles.held

    assert in_session(scenario) == expected


BROKEN_FACTS = {
    "depth-0": "f.depth = 0",
    "depth-5": "f.depth = 5",
    "depth-a-string": "f.depth = '1'",
    "tag-of-257": "f.tag = 'x'.repeat(257)",
    "tag-with-a-line-break": "f.tag = 'span\\n-'",
    "tag-with-a-no-break-space": "f.tag = 'span\\u00a0-\\u00a0p1.9'",
    "tag-with-a-c0-control": "f.tag = 'span\\u0001'",
    "tag-with-a-c1-control": "f.tag = 'span\\u009b'",
    "tag-a-number": "f.tag = 5",
    "257-classes": "f.classes = Array(257).fill('a')",
    "class-of-257": "f.classes = ['a'.repeat(257)]",
    "text-a-number": "f.text = 5",
}


@pytest.mark.parametrize("broken", list(BROKEN_FACTS))
def test_a_page_that_breaks_a_bound_of_its_parts_facts_is_refused(
    broken: str, handles: Handles
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        await session.page.evaluate(f"""() => {{
            const push = Array.prototype.push;
            Array.prototype.push = function (...items) {{
                const f = items[0];
                if (f !== null && typeof f === 'object' && 'depth' in f) {{ {BROKEN_FACTS[broken]}; }}
                return push.apply(this, items);
            }};
        }}""")
        return await refusal(Parts().describe(session, button), session), handles.held

    assert in_session(scenario) == (LIED, 0)


def test_a_page_that_evades_the_reading_bound_on_parts_is_refused(
    handles: Handles,
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, int, int]:
        await put(session, "<button>" + "<span>s</span>" * 12 + "</button>")
        (button,) = button_refs(await session.snapshot(), "s" * 12)
        # Thirteen at once, past the twelve the reading stops at.
        await session.page.evaluate("""() => {
            const push = Array.prototype.push;
            Array.prototype.push = function (...items) {
                const f = items[0];
                if (this.length === 11 && (f instanceof Element || (f && 'depth' in f))) {
                    push.apply(this, items);
                }
                return push.apply(this, items);
            };
        }""")
        refused = await refusal(Parts().describe(session, button), session)
        return refused, len(handles.made), handles.held

    assert in_session(scenario) == (LIED, 2, 0)


def test_a_successful_read_holds_its_element_and_parts_until_they_are_let_go(
    handles: Handles,
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, int, int, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        described = await parts.describe(session, button)
        held = handles.held
        await parts.void()
        return described.parts, len(handles.made), held, handles.held

    assert in_session(scenario) == (BANNER_PARTS, 4, 3, 0)


def test_a_page_that_pads_its_element_list_makes_no_more_handles_than_facts(
    handles: Handles,
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        await session.page.evaluate("""() => {
            const push = Array.prototype.push;
            Array.prototype.push = function (...items) {
                const times = items[0] instanceof Element ? 400 : 1;
                for (let i = 0; i < times; i++) push.apply(this, items);
                return this.length;
            };
        }""")
        described = await Parts().describe(session, button)
        return described.parts, len(handles.made)

    assert in_session(scenario) == (BANNER_PARTS, 4)


VOID = (
    "p1.2 is void: the page changed documents since describe_parts listed it, so "
    "describe the element again"
)
EARLIER = (
    "p1.2 is from an earlier describe_parts, and parts serve only the call after "
    "it: describe the element again"
)


@pytest.mark.parametrize(
    "navigation",
    [
        "() => location.reload()",
        "() => history.pushState({}, '', '/elsewhere')",
    ],
    ids=["reload", "pushState"],
)
def test_parts_are_void_after_a_navigation(navigation: str, handles: Handles) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        await parts.describe(session, button)
        await parts.turned()
        async with session.page.expect_event("framenavigated"):
            await session.page.evaluate(navigation)
        return await refusal(parts.seen(session, "p1.2"), session), handles.held

    assert in_session(scenario) == (VOID, 0)


def test_parts_serve_only_the_turn_after_their_describe(handles: Handles) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, str, str, str, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        await parts.describe(session, button)
        same_turn = await refusal(parts.seen(session, "p1.2"), session)
        await parts.turned()
        never_given = await refusal(parts.seen(session, "p9.1"), session)
        long = await refusal(parts.seen(session, "x" * 60), session)
        await parts.turned()
        later = await refusal(parts.seen(session, "p1.2"), session)
        return same_turn, never_given, long, later, handles.held

    assert in_session(scenario) == (
        EARLIER,
        "'p9.1' isn't a part describe_parts gave",
        f"'{'x' * 40}' isn't a part describe_parts gave",
        EARLIER,
        0,
    )


def test_a_second_describe_lets_the_first_go_and_serves_only_after_its_turn(
    handles: Handles,
) -> None:
    async def scenario(
        session: BrowserSession,
    ) -> tuple[int, str, str, int, Target | str]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        await parts.describe(session, button)
        await parts.turned()
        await parts.describe(session, button)
        held = handles.held
        first = await refusal(parts.seen(session, "p1.2"), session)
        second = await refusal(parts.seen(session, "p2.2"), session)
        await parts.turned()
        count = await bound_to(session, count_uses(), await parts.seen(session, "p2.2"))
        await parts.void()
        return held, first, second, handles.held, count

    assert in_session(scenario) == (
        3,
        EARLIER,
        EARLIER.replace("p1.2", "p2.2"),
        0,
        COUNTER,
    )


def test_an_element_with_no_element_inside_it_has_no_parts(handles: Handles) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, str, int, str]:
        await put(session, "<button>Save <b>now</b></button><button>Close</button>")
        snapshot = await session.snapshot()
        parts = Parts()
        described = await parts.describe(session, button_refs(snapshot, "Close")[0])
        await parts.turned()
        refused = await refusal(parts.seen(session, "p1.1"), session)
        return described.text, described.parts, handles.held, refused

    assert in_session(scenario) == (
        "e3 has no element inside it",
        "",
        0,
        (
            "p1.1 is from an earlier describe_parts, and parts serve only the call after "
            "it: describe the element again"
        ),
    )


def test_a_part_moved_with_its_element_into_another_document_is_refused() -> None:
    async def scenario(session: BrowserSession) -> str:
        await put(session, BANNER + '<iframe srcdoc="<p>elsewhere</p>"></iframe>')
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        await parts.describe(session, button)
        await parts.turned()
        await session.page.evaluate(
            "() => document.querySelector('iframe').contentDocument.body.append("
            "document.querySelector('button'))"
        )
        return await refusal(parts.seen(session, "p1.2"), session)

    assert in_session(scenario) == LEFT


def test_a_document_change_while_describe_parts_locates_its_element_discards_the_list(
    handles: Handles, monkeypatch: pytest.MonkeyPatch
) -> None:
    locate = BrowserSession.locate

    async def located_then_moved(self: BrowserSession, ref: str) -> Any:
        element = await locate(self, ref)
        async with self.page.expect_event("framenavigated"):
            await self.page.evaluate("() => history.pushState({}, '', '/moved')")
        return element

    monkeypatch.setattr(BrowserSession, "locate", located_then_moved)

    async def scenario(session: BrowserSession) -> tuple[str, int]:
        await put(session, BANNER)
        (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        return await refusal(Parts().describe(session, button), session), handles.held

    assert in_session(scenario) == (
        (
            "the page changed while the session looked at it, so what it saw was "
            "discarded: take a new snapshot, or look the target up again"
        ),
        0,
    )


UTF8 = '<meta charset="utf-8">'
VALUE = "fake-secret-49"
WITHHELD = "(text withheld: the described element's text holds a test secret)"
# A bound value holding a private-use character, which a page can write with
# others around it or leave out (#53 ZW).
HELD_PUA = "fake-se\ue000cret-58"


def visible(text: str) -> str:
    """`text` as it shows: without its gaps, normalized."""
    return normalize(GAP.sub("", text))


def as_sent(text: str, redactor: Redactor) -> str:
    """`text` as a request block carries it: scanned, escaped, scanned."""
    turn = Turn(log=(), notes="", results=(text,), snapshot=redactor.redact(""))
    sent = navigator_request(Prefix(spec="{}", plan="{}"), turn, redactor=redactor)
    return str(sent[2].content)


def referenced(text: str) -> str:
    """`text` with every character past ASCII written as a character reference."""
    return "".join(f"&#x{ord(c):X};" if ord(c) > 0x7E else c for c in text)


async def described_parts(
    session: BrowserSession, html: str, role: str = "button"
) -> str:
    await put(session, html)
    described = await Parts().describe(session, first(await session.snapshot(), role))
    return described.parts


@pytest.mark.parametrize(
    "written",
    [
        "fake-se\ue000\ue001cret-58",
        "fake-se\ue001\ue000cret-58",
        "fake-se\ue001cret-58",
        "fa\ue001ke-se\ue000cret-58",
        "fake-secret-58",
    ],
    ids=["after", "before", "in-its-place", "elsewhere", "left-out"],
)
def test_a_value_holding_a_gap_written_with_other_gaps_reaches_the_description_as_its_marker(
    written: str,
) -> None:
    redactor = bound(TEST_HELD=HELD_PUA)
    page = (
        f'{UTF8}<button aria-label="Save"><span class="note">your {referenced(written)} '
        f'code</span><span class="bare">{referenced(written)}</span>'
        f'<x-{written} class="tagged">t</x-{written}></button>'
    )

    async def scenario(session: BrowserSession) -> str:
        return await described_parts(session, page)

    parts = in_session(scenario, redactor)
    texts = [parts, as_sent(parts, redactor)]
    texts += [visible(text) for text in texts]

    assert parts == (
        "e1's parts, for your next call only:\n"
        f"- p1.1 span.note {WITHHELD}\n"
        f"- p1.2 span.bare {WITHHELD}\n"
        f"- p1.3 x-[SECRET:TEST_HELD].tagged {WITHHELD}"
    )
    assert (
        copies_found(HELD_PUA, texts=texts)
        + copies_found(visible(HELD_PUA), texts=texts)
        == []
    )
    assert [redactor.finds(text) for text in texts] == [False, False, False, False]


@pytest.mark.parametrize("gap", ["\ufeff", "\u200d"], ids=["U+FEFF", "U+200D"])
def test_a_value_holding_a_character_that_shows_nothing_withholds_every_parts_text(
    gap: str,
) -> None:
    held = f"fake-se{gap}cret-49"
    redactor = bound(TEST_PASSWORD=held)
    page = (
        f'{UTF8}<button>Save <span class="note">your {referenced(held)} code</span>'
        f'<span class="bare">{referenced(held)}</span></button>'
    )

    async def scenario(session: BrowserSession) -> str:
        return await described_parts(session, page)

    parts = in_session(scenario, redactor)

    assert parts == (
        "e1's parts, for your next call only:\n"
        f"- p1.1 span.note {WITHHELD}\n"
        f"- p1.2 span.bare {WITHHELD}"
    )
    assert copies_found(visible(held), texts=[parts, visible(parts)]) == []


def test_every_page_chosen_name_of_a_part_reaches_the_description_as_its_marker() -> (
    None
):
    redactor = bound(TEST_PASSWORD=VALUE)
    page = (
        f"{UTF8}<button>Save "
        f'<span class="note {VALUE}">your {VALUE} code and a long tail of words past '
        "forty characters</span>"
        '<span class="split fake-se\u200bcret-49">fake-se&#8203;cret-49</span>'
        '<span class="joined">fake-se&#8205;cret-49</span>'
        '<x-fake-se\u200dcret-49 class="tagged">t</x-fake-se\u200dcret-49>'
        '<span class="nobreak">fake-se&#8288;cret-49 and fake-se&#65279;cret-49</span>'
        '<span class="glyph">fake-se&#xE000;cret-49</span>'
        f'<span class="padded">{"&#8205;" * 45}(1)</span>'
        "</button>"
    )

    async def scenario(session: BrowserSession) -> str:
        return await described_parts(session, page)

    parts = in_session(scenario, redactor)
    texts = [parts, as_sent(parts, redactor)]
    texts += [visible(text) for text in texts]

    assert parts == (
        "e1's parts, for your next call only:\n"
        f"- p1.1 span.note.[SECRET:TEST_PASSWORD] {WITHHELD}\n"
        f"- p1.2 span.split {WITHHELD}\n"
        f"- p1.3 span.joined {WITHHELD}\n"
        f"- p1.4 x-[SECRET:TEST_PASSWORD].tagged {WITHHELD}\n"
        f"- p1.5 span.nobreak {WITHHELD}\n"
        f"- p1.6 span.glyph {WITHHELD}\n"
        f"- p1.7 span.padded {WITHHELD}"
    )
    assert copies_found(VALUE, texts=texts) == []
    assert [redactor.finds(text) for text in texts] == [False, False, False, False]


def test_a_parts_text_is_cut_only_after_what_shows_nothing_is_taken_out() -> None:
    page = (
        f'{UTF8}<button aria-label="Save">'
        f'<span class="zwj">{"&#8205;" * 45}(1)</span>'
        f'<span class="word-joiner">{"&#8288;" * 45}(1)</span>'
        f'<span class="selector">{"&#65039;" * 45}(1)</span>'
        f'<span class="private">{"&#xE001;" * 45}(1)</span>'
        f'<span class="spaces" style="white-space: pre">{" " * 45}(1)</span>'
        "</button>"
    )

    async def scenario(session: BrowserSession) -> str:
        return await described_parts(session, page)

    assert in_session(scenario) == (
        "e1's parts, for your next call only:\n"
        '- p1.1 span.zwj "(1)"\n'
        '- p1.2 span.word-joiner "(1)"\n'
        '- p1.3 span.selector "(1)"\n'
        '- p1.4 span.private "(1)"\n'
        '- p1.5 span.spaces "(1)"'
    )


PIECES = ("fake-", "secret-49")
PARTS_OF_E1 = "e1's parts, for your next call only:\n"


@pytest.mark.parametrize(
    ("page", "expected", "counter", "sent"),
    [
        (
            "<button>Save <span>fake-</span><span class='counter'>secret-49</span></button>",
            f"{PARTS_OF_E1}- p1.1 span {WITHHELD}\n- p1.2 span.counter {WITHHELD}",
            "p1.2",
            [],
        ),
        (
            (
                "<button><div><span>fake-</span><span class='counter'>secret-49</span>"
                "</div></button>"
            ),
            (
                f"{PARTS_OF_E1}- p1.1 div {WITHHELD}\n"
                f"  - p1.2 span {WITHHELD}\n"
                f"  - p1.3 span.counter {WITHHELD}"
            ),
            "p1.3",
            [],
        ),
        (
            "<button><span>fake-</span><br><span class='counter'>secret-49</span></button>",
            (
                f"{PARTS_OF_E1}- p1.1 span {WITHHELD}\n- p1.2 br {WITHHELD}\n"
                f"- p1.3 span.counter {WITHHELD}"
            ),
            "p1.3",
            [],
        ),
        (
            (
                "<button><span>fake-</span><span style='visibility: hidden'>x</span>"
                "<span class='counter'>secret-49</span></button>"
            ),
            (
                f"{PARTS_OF_E1}- p1.1 span {WITHHELD}\n- p1.2 span {WITHHELD}\n"
                f"- p1.3 span.counter {WITHHELD}"
            ),
            "p1.3",
            [],
        ),
        (
            (
                "<button style='display: contents'><span>fake-</span>"
                "<span class='counter'>secret-49</span></button>"
            ),
            f"{PARTS_OF_E1}- p1.1 span {WITHHELD}\n- p1.2 span.counter {WITHHELD}",
            "p1.2",
            [],
        ),
        (
            (
                "<button>Save <span>fake-</span><span class='counter'>code</span></button>"
                f"<p>Your code is {VALUE}</p>"
            ),
            (
                "e2's parts, for your next call only:\n"
                '- p1.1 span "fake-"\n- p1.2 span.counter "code"'
            ),
            "p1.2",
            ["fake-"],
        ),
    ],
    ids=[
        "siblings",
        "nested",
        "a-line-break",
        "a-hidden-piece",
        "no-box",
        "the-value-elsewhere",
    ],
)
def test_a_parts_text_is_withheld_while_the_described_elements_text_holds_a_bound_value(
    page: str, expected: str, counter: str, sent: list[str]
) -> None:
    redactor = bound(TEST_PASSWORD=VALUE)

    async def scenario(session: BrowserSession) -> tuple[str, Target | str]:
        await put(session, f'<div class="banner">{page}</div>')
        parts = Parts()
        described = await parts.describe(
            session, first(await session.snapshot(), "button")
        )
        await parts.turned()
        target = await bound_to(
            session, count_uses(), await parts.seen(session, counter)
        )
        return described.parts, target

    parts, target = in_session(scenario, redactor)
    request = as_sent(parts, redactor)

    assert (parts, [piece for piece in PIECES if piece in request], target) == (
        expected,
        sent,
        COUNTER,
    )


def test_a_part_the_page_lists_from_outside_its_element_shows_its_text_scanned() -> (
    None
):
    redactor = bound(TEST_PASSWORD=VALUE)
    page = (
        f'<button>Save</button><p class="note">your {VALUE} code and a long tail of '
        "words past forty characters</p>"
    )

    async def scenario(session: BrowserSession) -> str:
        await put(session, page)
        button = first(await session.snapshot(), "button")
        await session.page.evaluate("""() => {
            Object.defineProperty(Element.prototype, 'children', {get() {
                return this.localName === 'button' ? [document.querySelector('p.note')] : [];
            }});
        }""")
        return (await Parts().describe(session, button)).parts

    assert in_session(scenario, redactor) == (
        "e2's parts, for your next call only:\n"
        '- p1.1 p.note "your [SECRET:TEST_PASSWORD] code and a l"'
    )


def test_describe_parts_lists_four_levels_and_not_the_fifth() -> None:
    page = (
        '<div class="root"><section class="d1"><div class="d2"><p class="d3">'
        '<span class="d4">four <b class="d5">five</b></span></p></div></section>'
        "<button>go</button></div>"
    )

    async def scenario(session: BrowserSession) -> str:
        return await described_parts(session, page, "generic")

    assert in_session(scenario) == (
        "e1's parts, for your next call only:\n"
        '- p1.1 section.d1 "four five"\n'
        '  - p1.2 div.d2 "four five"\n'
        '    - p1.3 p.d3 "four five"\n'
        '      - p1.4 span.d4 "four five"\n'
        '- p1.5 button "go"'
    )


def test_describe_parts_lists_twelve_parts_at_most_in_document_order() -> None:
    items = "".join(
        f'<li class="row"><a href="#{n}"><b>Item</b> {n}</a></li>' for n in range(3000)
    )

    async def scenario(session: BrowserSession) -> str:
        return await described_parts(session, f"<ul>{items}</ul>", "list")

    assert in_session(scenario) == (
        "e1's parts, for your next call only:\n"
        '- p1.1 li.row "Item 0"\n'
        '  - p1.2 a "Item 0"\n'
        '    - p1.3 b "Item"\n'
        '- p1.4 li.row "Item 1"\n'
        '  - p1.5 a "Item 1"\n'
        '    - p1.6 b "Item"\n'
        '- p1.7 li.row "Item 2"\n'
        '  - p1.8 a "Item 2"\n'
        '    - p1.9 b "Item"\n'
        '- p1.10 li.row "Item 3"\n'
        '  - p1.11 a "Item 3"\n'
        '    - p1.12 b "Item"'
    )


def test_a_part_shows_only_its_stable_classes() -> None:
    page = (
        '<button>Save <span class="css-1x2y3z Button_root__x7Kd2 active counter '
        'sc-a1b2c3 is-open">(1)</span></button>'
    )

    async def scenario(session: BrowserSession) -> str:
        return await described_parts(session, page)

    assert in_session(scenario) == (
        'e1\'s parts, for your next call only:\n- p1.1 span.counter.is-open "(1)"'
    )


NOT_THE_PART = (
    "this check's subject is the one element matching span.counter inside "
    "div.banner, and this element is another one (describe_parts lists an "
    "element's parts)"
)


def test_a_part_of_the_article_actions_copy_is_refused_naming_the_region() -> None:
    async def scenario(session: BrowserSession) -> tuple[str, Target | str]:
        await show(session, "article-favorited")
        _, lower = button_refs(await session.snapshot(), "Unfavorite Article (1)")
        parts = Parts()
        described = await parts.describe(session, lower)
        await parts.turned()
        return described.parts, await bound_to(
            session, count_uses(), await parts.seen(session, "p1.2")
        )

    assert in_session(scenario) == (
        (
            "e46's parts, for your next call only:\n"
            "- p1.1 i.ion-heart\n"
            '- p1.2 span.counter "(1)"'
        ),
        (
            "this check's subject binds only inside div.banner, and this element is "
            "outside it"
        ),
    )


def test_a_decoy_descendant_showing_the_expected_value_is_never_chosen_for_it() -> None:
    page = (
        MODES["clean"]
        .replace(
            "Unfavorite Article <span",
            'Unfavorite Article <span class="decoy">(1)</span> <span',
            1,
        )
        .replace("(1)</span></button>", "(3)</span></button>", 1)
    )

    async def scenario(
        session: BrowserSession,
    ) -> tuple[str, Target | str, Target | str]:
        await put(session, page)
        parts = Parts()
        described = await parts.describe(
            session, first(await session.snapshot(), "button")
        )
        await parts.turned()
        decoy = await bound_to(session, count_uses(), await parts.seen(session, "p1.1"))
        counter = await bound_to(
            session, count_uses(), await parts.seen(session, "p1.2")
        )
        return described.parts, decoy, counter

    assert in_session(scenario) == (
        (
            "e2's parts, for your next call only:\n"
            '- p1.1 span.decoy "(1)"\n'
            '- p1.2 span.counter "(3)"'
        ),
        NOT_THE_PART,
        COUNTER,
    )


SAME_COUNTER = (
    "(e) => e === [...document.querySelectorAll('span.counter')]"
    ".find((s) => s.textContent === '(1)')"
)


async def after_change(
    session: BrowserSession, change: str
) -> tuple[bool, Target | str]:
    """Whether p1.2 still names the banner's count once the page has made
    `change`, and what it binds."""
    await put(session, BANNER)
    (button,) = button_refs(await session.snapshot(), "Unfavorite Article (1)")
    parts = Parts()
    await parts.describe(session, button)
    await parts.turned()
    await session.page.evaluate(change)
    seen = await parts.seen(session, "p1.2")
    same: bool = await session.page.evaluate(SAME_COUNTER, seen.element)
    return same, await bound_to(session, count_uses(), seen)


@pytest.mark.parametrize(
    ("inserted", "binds"),
    [
        ("{className: 'badge', textContent: 'new'}", COUNTER),
        (
            "{className: 'counter', textContent: '(9)'}",
            (
                "this check's subject is the one element matching span.counter inside "
                "div.banner, and that region has several"
            ),
        ),
    ],
    ids=["a-badge", "another-counter"],
)
def test_a_descendant_inserted_before_the_part_leaves_its_binding_on_the_same_node(
    inserted: str, binds: Target | str
) -> None:
    change = (
        "document.querySelector('button').prepend("
        f"Object.assign(document.createElement('span'), {inserted}))"
    )

    assert in_session(lambda session: after_change(session, change)) == (True, binds)


def test_reordered_descendants_leave_the_binding_on_the_same_node() -> None:
    change = (
        "document.querySelector('button').prepend("
        "document.querySelector('i'), document.querySelector('span.counter'))"
    )

    assert in_session(lambda session: after_change(session, change)) == (True, COUNTER)


def test_a_noted_part_binds_its_negative_check_once_it_is_gone() -> None:
    async def scenario(session: BrowserSession) -> tuple[str, Target]:
        await put(
            session,
            '<nav class="bar"><button>Save <span class="badge">unsaved</span></button></nav>',
        )
        (button,) = button_refs(await session.snapshot(), "Save unsaved")
        parts = Parts()
        described = await parts.describe(session, button)
        await parts.turned()
        uses = TargetUses("the unsaved badge on the save button", checks_text=False)
        seen = await parts.seen(session, "p1.1")
        async with session.observing_page() as page:
            await uses.see_for_negative_check(page, seen)
        await session.page.evaluate("document.querySelector('span.badge').remove()")
        async with session.observing_page() as page:
            index = await uses.add_negative_check(page)
        return described.parts, uses.targets[index]

    assert in_session(scenario) == (
        'e2\'s parts, for your next call only:\n- p1.1 span.badge "unsaved"',
        Target(
            semantic="the unsaved badge on the save button",
            locators=(ByCss(scope=ByCss(css="nav.bar"), css="span.badge"),),
        ),
    )


@pytest.mark.parametrize(
    ("other", "binds"),
    [
        ("", Target(semantic="the save status", locators=(ByCss(css="span.state"),))),
        (
            '<p>Other: <span class="state">idle</span></p>',
            (
                "the save status: no locator of the grammar finds the span element alone "
                "for an assertion"
            ),
        ),
    ],
    ids=["alone", "with-another"],
)
def test_a_part_with_no_distinguishing_locator_is_refused_never_positional(
    other: str, binds: Target | str
) -> None:
    async def scenario(session: BrowserSession) -> Target | str:
        await put(session, f'<p>Status: <span class="state">saved</span></p>{other}')
        parts = Parts()
        await parts.describe(session, first(await session.snapshot(), "paragraph"))
        await parts.turned()
        uses = TargetUses("the save status", checks_text=True)
        return await bound_to(session, uses, await parts.seen(session, "p1.1"))

    assert in_session(scenario) == binds
