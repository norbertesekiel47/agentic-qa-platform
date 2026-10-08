import asyncio
from unittest.mock import patch

import pytest
from aqa_core.schema import Contract
from aqa_runner import binding
from aqa_runner.binding import (
    Bind,
    HeldRegion,
    Refused,
    _passes,
    _query,
    binding_verdict,
    held_region,
)
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.locator_generation import _stable
from playwright.async_api import ElementHandle, Error, async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.explore_fixtures import App, in_app
from packages.runner.tests.pilot_pages import PAGES, in_session, put, show

COUNT = Contract(region="div.banner", part="span.counter", leaf=True)


def test_a_listed_offered_lower_copy_is_refused_without_being_replaced() -> None:
    async def scenario(session: BrowserSession) -> None:
        await show(session, "article-favorited")
        offered = await session.page.query_selector("div.article-actions span.counter")
        assert offered is not None
        try:
            assert await binding_verdict(session.page, offered, COUNT) == Refused(
                "outside_region"
            )
            assert await offered.inner_text() == "(1)"
        finally:
            await offered.dispose()

    in_session(scenario)


async def verdict(
    session: BrowserSession, selector: str, contract: Contract | None
) -> Bind | Refused:
    element = await session.page.query_selector(selector)
    assert element is not None
    try:
        return await binding_verdict(session.page, element, contract)
    finally:
        await element.dispose()


@pytest.mark.parametrize(
    ("html", "selector", "reason"),
    [
        ('<div><span class="counter">1</span></div>', "span.counter", "region_absent"),
        (
            '<div class="banner"><span class="counter">1</span></div><div class="banner"></div>',
            "span.counter",
            "region_ambiguous",
        ),
        ('<div class="banner"><button>1</button></div>', "button", "part_absent"),
        (
            '<div class="banner"><span class="counter">1</span><span class="counter">2</span></div>',
            "span.counter",
            "part_ambiguous",
        ),
        (
            '<div class="banner"><span class="counter">3</span><span class="decoy">1</span></div>',
            "span.decoy",
            "not_the_part",
        ),
        (
            '<div class="banner"><button><span class="counter">1</span></button></div>',
            "button",
            "not_the_part",
        ),
        (
            '<div class="banner"><span class="counter"><b>1</b></span></div>',
            "span.counter",
            "not_leaf",
        ),
    ],
)
def test_listed_subject_refusals_name_the_boundary(
    html: str, selector: str, reason: str
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, html)
        found = await verdict(session, selector, COUNT)
        assert isinstance(found, Refused)
        assert found.reason == reason

    in_session(scenario)


@pytest.mark.parametrize(
    ("page", "part", "leaf"),
    [
        ("article-signed-out", "a.author", True),
        ("article-signed-out", "span.date", True),
        ("article", "a.author", True),
        ("article-favorited", "app-favorite-button > button", False),
        ("article-favorited", "span.counter", True),
    ],
)
def test_each_reviewed_capture_subject_binds_only_its_banner_part(
    page: str, part: str, leaf: bool
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await show(session, page)
        contract = Contract(region="div.banner", part=part, leaf=leaf)
        assert await verdict(session, f"div.banner {part}", contract) == Bind()
        assert await verdict(
            session, f"div.article-actions {part}", contract
        ) == Refused("outside_region")

    in_session(scenario)


@pytest.mark.parametrize(
    ("style", "inside"),
    [
        ("display:none", "value"),
        ("visibility:hidden", "value"),
        ("display:block;width:0;height:0;overflow:hidden", ""),
        ("display:contents", "value"),
        ("display:contents", "<b>value</b>"),
        ("opacity:0", "value"),
        ("position:absolute;top:200vh", "value"),
        ("", "value"),
    ],
)
def test_atomic_visibility_matches_pinned_playwright(style: str, inside: str) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            f'<div class="banner"><span class="counter" style="{style}">{inside}</span></div>',
        )
        element = await session.page.query_selector("span.counter")
        assert element is not None
        try:
            visible = await element.is_visible()
            async with held_region(session.page, COUNT) as held:
                assert held is not None
                assert await held.absent() is not visible
        finally:
            await element.dispose()

    in_session(scenario)


@pytest.mark.parametrize(
    "change",
    [
        "r.replaceWith(r.cloneNode(true))",
        'r.className = "moved"',
        "r.after(r.cloneNode(true))",
        "r.remove()",
    ],
)
def test_region_change_after_zero_never_proves_absence(change: str) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session, '<div class="banner"><span class="counter" hidden>1</span></div>'
        )
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            assert await held.absent()
            assert (
                await held.root.locator("span.counter").filter(visible=True).count()
                == 0
            )
            await session.page.evaluate(
                '(code) => { const r = document.querySelector("div.banner"); eval(code); }',
                change,
            )
            assert not await held.absent()

    in_session(scenario)


def test_a_zero_locator_cannot_hide_a_visible_part_and_tokens_are_retired() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, '<div class="banner"><span class="counter">1</span></div>')
        offered: ElementHandle | None = None
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            offered = await session.page.query_selector("span.counter")
            assert offered is not None
            assert await held.root.locator("span.missing").count() == 0
            assert not await held.absent()
            assert await held.bound(offered)
        try:
            assert not await held.bound(offered)
        finally:
            await offered.dispose()

    in_session(scenario)


@pytest.mark.parametrize(
    ("left", "right", "custom", "expected"),
    [
        ("banner", "footer", True, Refused("unlisted_copies")),
        ("row", "row", True, Bind()),
        ("banner", "footer", False, Bind()),
        ("css-fake", "ng-star-inserted", True, Bind()),
    ],
)
def test_unlisted_copy_rule_preserves_repeats_and_documented_residuals(
    left: str, right: str, *, custom: bool, expected: Bind | Refused
) -> None:
    async def scenario(session: BrowserSession) -> None:
        tag = "x-item" if custom else "section"
        await put(
            session,
            f'<main><div class="{left}"><{tag}><span>first</span></{tag}></div><div class="{right}"><{tag}><span>second</span></{tag}></div></main>',
        )
        assert await verdict(session, "main > div:last-child span", None) == expected
        assert (
            await session.page.locator("main > div:last-child span").inner_text()
            == "second"
        )

    in_session(scenario)


@pytest.mark.parametrize(
    "token",
    [
        "active",
        "checked",
        "collapsed",
        "disabled",
        "expanded",
        "focus",
        "hidden",
        "hover",
        "open",
        "selected",
        "show",
        "css-fake",
        "sc-fake",
        "jsx-fake",
        "emotion-fake",
        "svelte-fake",
        "ng-fake",
        "mixed-x7Kd2",
        "stable",
        "row2",
        "_private",
        "1digit",
        "a:b",
    ],
)
def test_copy_class_filter_agrees_with_generator_grammar(token: str) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            f'<main><div class="row"><x-item><span>1</span></x-item></div><div class="row {token}"><x-item><span>2</span></x-item></div></main>',
        )
        expected = Refused("unlisted_copies") if _stable(token) else Bind()
        assert await verdict(session, "main > div:first-child span", None) == expected

    in_session(scenario)


@pytest.mark.parametrize(
    ("copies", "expected"),
    [
        (8, Refused("unlisted_copies")),
        (9, Refused("too_many")),
        (65, Refused("too_many")),
    ],
)
def test_copy_and_child_bounds_refuse_rather_than_pick(
    copies: int, expected: Refused
) -> None:
    async def scenario(session: BrowserSession) -> None:
        html = "".join(
            f'<div class="region-{chr(65 + i)}"><x-item><span>{i}</span></x-item></div>'
            for i in range(copies)
        )
        await put(session, "<main>" + html + "</main>")
        assert await verdict(session, "main > div:first-child span", None) == expected

    in_session(scenario)


def test_a_listed_subject_does_not_consult_the_copy_detector() -> None:
    async def scenario(session: BrowserSession) -> None:
        html = (
            '<div class="banner"><x-item><span class="counter">1</span></x-item></div>'
        )
        html += "".join(
            f'<div class="region-{chr(65 + i)}"><x-item><span>{i}</span></x-item></div>'
            for i in range(64)
        )
        await put(session, "<main>" + html + "</main>")
        assert await verdict(session, "div.banner span.counter", None) == Refused(
            "too_many"
        )
        assert await verdict(session, "div.banner span.counter", COUNT) == Bind()

    in_session(scenario)


@pytest.mark.parametrize("contract", [None, COUNT])
def test_missing_engine_registration_is_a_programming_error(
    contract: Contract | None,
) -> None:

    async def scenario() -> None:
        async with (
            async_playwright() as playwright,
            egress_proxy() as egress,
            open_browser_session(playwright.chromium, egress=egress) as session,
        ):
            await put(
                session, '<div class="banner"><span class="counter">1</span></div>'
            )
            with pytest.raises(RuntimeError, match=r"register_identity_engine.*before"):
                await verdict(session, "span.counter", contract)

    asyncio.run(scenario())


def test_page_world_overrides_cannot_change_trusted_binding_or_copy_answers() -> None:
    async def scenario(session: BrowserSession) -> None:
        await show(session, "article-favorited")
        inside = await session.page.query_selector("div.banner span.counter")
        outside = await session.page.query_selector("div.article-actions span.counter")
        assert inside is not None
        assert outside is not None
        try:
            assert await binding_verdict(session.page, inside, COUNT) == Bind()
            assert await binding_verdict(session.page, outside, None) == Refused(
                "unlisted_copies"
            )
            await session.page.evaluate("""() => {
                Node.prototype.contains = () => true;
                Element.prototype.querySelector = () => null;
                Element.prototype.querySelectorAll = () => [];
                Document.prototype.querySelector = () => null;
                Document.prototype.querySelectorAll = () => [];
                Element.prototype.closest = () => document.body;
                Element.prototype.matches = () => true;
                Element.prototype.checkVisibility = () => false;
                window.getComputedStyle = () => ({display: "none", visibility: "hidden"});
                for (const [proto, key, value] of [
                    [Element.prototype, 'childElementCount', 0],
                    [Element.prototype, 'children', []],
                    [Element.prototype, 'firstElementChild', null],
                    [Element.prototype, 'className', 'fake'],
                    [Node.prototype, 'isConnected', true],
                    [Node.prototype, 'ownerDocument', document],
                    [Node.prototype, 'parentNode', document.body],
                    [Node.prototype, 'parentElement', document.body],
                ]) Object.defineProperty(proto, key, {get: () => value});
            }""")
            assert (
                await session.page.evaluate(
                    'document.querySelectorAll("span.counter").length'
                )
                == 0
            )
            assert await binding_verdict(session.page, inside, COUNT) == Bind()
            assert await binding_verdict(session.page, outside, COUNT) == Refused(
                "outside_region"
            )
            assert await binding_verdict(session.page, outside, None) == Refused(
                "unlisted_copies"
            )
            async with held_region(session.page, COUNT) as held:
                assert held is not None
                assert not await held.absent()
        finally:
            await inside.dispose()
            await outside.dispose()

    in_session(scenario)


def test_grown_leaf_and_moved_attributes_keep_the_offered_handle_boundary() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            '<div class="banner"><span class="counter" id="unique" data-testid="only"><b>3</b></span></div><div class="article-actions"><span>1</span></div>',
        )
        offered = await session.page.query_selector("div.banner span.counter")
        assert offered is not None
        try:
            await session.page.evaluate("""() => {
                const a = document.querySelector('div.banner span');
                const b = document.querySelector('div.article-actions span');
                for (const key of ['id', 'class', 'data-testid']) {
                    b.setAttribute(key, a.getAttribute(key)); a.removeAttribute(key);
                }
                Object.defineProperty(Element.prototype, 'childElementCount', {get: () => 0});
            }""")
            assert await verdict(session, "#unique", COUNT) == Refused("outside_region")
            assert await binding_verdict(
                session.page,
                offered,
                Contract(region="div.banner", part="span", leaf=True),
            ) == Refused("not_leaf")
        finally:
            await offered.dispose()

    in_session(scenario)


@pytest.mark.parametrize("page", PAGES)
def test_capture_copy_census_is_exactly_the_article_meta_pairs(page: str) -> None:
    async def scenario(session: BrowserSession) -> None:
        await show(session, page)
        elements = await session.page.query_selector_all("body *")
        groups: set[tuple[int, ...]] = set()
        try:
            for element in elements:
                copies = await element.query_selector_all("aqa-binding=copies")
                try:
                    if copies:
                        indexes = [
                            await copy.evaluate(
                                '(e) => [...document.querySelectorAll("body *")].indexOf(e)'
                            )
                            for copy in copies
                        ]
                        groups.add(tuple(sorted(indexes)))
                finally:
                    for copy in copies:
                        await copy.dispose()
            assert len(groups) == (15 if page.startswith("article") else 0)
            assert all(len(group) == 2 for group in groups)
        finally:
            for element in elements:
                await element.dispose()

    in_session(scenario)


def test_detector_ancestor_limit_is_a_refusal() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            "<main>"
            + "<section>" * 16
            + "<x-item><span>1</span></x-item>"
            + "</section>" * 16
            + "</main>",
        )
        assert await verdict(session, "span", None) == Refused("too_many")

    in_session(scenario)


def test_fixture_reset_slow_write_websocket_and_cleanup_are_real() -> None:

    async def scenario(app: App, session: BrowserSession) -> str:
        await session.page.goto(app.origin)
        assert (
            await session.page.locator("div.banner span.counter").inner_text() == "(1)"
        )
        assert (
            await session.page.evaluate(
                "async () => (await fetch('/reset?mode=unknown', {method:'POST'})).status"
            )
            == 400
        )
        assert app.mode == "clean"
        assert (
            await session.page.evaluate(
                "async () => (await fetch('/reset?mode=banner-bug', {method:'POST'})).status"
            )
            == 204
        )
        await session.page.reload()
        assert (
            await session.page.locator("div.banner span.counter").inner_text() == "(3)"
        )
        assert (
            await session.page.locator("div.article-actions span.counter").inner_text()
            == "(1)"
        )
        slow = asyncio.create_task(
            session.page.evaluate(
                "async () => (await fetch('/slow-write', {method:'POST'})).status"
            )
        )
        async with asyncio.timeout(5):
            await app.slow_started.wait()
        assert app.writes == 0
        app.release_slow.set()
        assert await slow == 204
        assert app.writes == 1
        assert (
            await session.page.evaluate("""() => new Promise((resolve, reject) => {
            const socket = new WebSocket(location.origin.replace('http:', 'ws:'));
            socket.onopen = () => socket.send('fixture write');
            socket.onerror = () => reject(new Error('fixture websocket failed'));
            socket.onmessage = (event) => { socket.close(); resolve(event.data); };
        })""")
            == "fixture write"
        )
        assert app.messages == [b"fixture write"]
        assert app.writes == 2
        return app.origin

    origin = in_app(scenario)

    async def refused() -> None:

        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", int(origin.rsplit(":", 1)[1]))

    asyncio.run(refused())


@pytest.mark.parametrize(
    "name",
    [
        "wrapped-lower",
        "flattened",
        "decoy",
        "published-template-inferred",
        "lower-bug",
        "article-favorited",
    ],
)
def test_app_fixture_modes_and_capture_are_explicit(name: str) -> None:

    async def scenario(app: App, session: BrowserSession) -> None:
        await session.page.goto(app.origin + "/page/" + name)
        if name == "published-template-inferred":
            assert (
                await verdict(
                    session,
                    "a.author",
                    Contract(region="div.banner", part="a.author", leaf=True),
                )
                == Bind()
            )
            assert await session.page.locator("a.author").inner_text() == "jake"
        elif name == "flattened":
            assert await verdict(session, "div.banner button", COUNT) == Refused(
                "part_absent"
            )
        elif name == "decoy":
            assert await verdict(session, "span.decoy", COUNT) == Refused(
                "not_the_part"
            )
        else:
            assert await verdict(
                session, "div.article-actions span.counter", COUNT
            ) == Refused("outside_region")
            if name == "wrapped-lower":
                assert (
                    await verdict(session, "div.article-actions span.counter", None)
                    == Bind()
                )
            if name == "lower-bug":
                assert (
                    await session.page.locator("div.banner span.counter").inner_text()
                    == "(1)"
                )
                assert (
                    await session.page.locator(
                        "div.article-actions span.counter"
                    ).inner_text()
                    == "(3)"
                )

    in_app(scenario)


@pytest.mark.parametrize(
    "selector",
    [":scope + div.article-actions span.counter", ":scope ~ div span.counter"],
)
def test_native_sibling_escape_still_fails_the_held_postcondition(
    selector: str,
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            '<div class="banner"><span class="counter">3</span></div><div class="article-actions"><span class="counter">1</span></div>',
        )
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            matches = await held.root.locator(selector).element_handles()
            assert len(matches) == 1
            try:
                assert await matches[0].inner_text() == "1"
                assert not await held.contains(matches[0])
                assert not await held.bound(matches[0])
            finally:
                await matches[0].dispose()

    in_session(scenario)


def test_tokens_drop_on_exception_and_other_hold_stays_valid() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, '<div class="banner"><span class="counter">1</span></div>')
        offered = await session.page.query_selector("span.counter")
        assert offered is not None
        try:
            async with held_region(session.page, COUNT) as first:
                assert first is not None
                retired: list[HeldRegion] = []

                async def fail() -> None:
                    async with held_region(session.page, COUNT) as second:
                        assert second is not None
                        assert await second.bound(offered)
                        retired.append(second)
                        raise ValueError("fixture stop")

                with pytest.raises(ValueError, match="fixture stop"):
                    await fail()
                assert not await retired[0].bound(offered)
                assert await first.bound(offered)
        finally:
            await offered.dispose()

    in_session(scenario)


@pytest.mark.parametrize(
    ("parts", "absent"),
    [
        ("", True),
        (
            '<span class="counter" hidden>1</span><span class="counter" hidden>2</span>',
            True,
        ),
        ('<span class="counter" hidden>1</span><span class="counter">2</span>', False),
    ],
)
def test_all_parts_visibility_decides_direct_absence(
    parts: str, *, absent: bool
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, f'<div class="banner">{parts}</div>')
        async with held_region(session.page, COUNT) as held:
            assert held is not None
            assert await held.root.locator("span.missing").count() == 0
            assert await held.absent() is absent

    in_session(scenario)


@pytest.mark.parametrize(
    ("children", "expected"), [(64, Bind()), (65, Refused("too_many"))]
)
def test_child_limit_counts_same_styled_repeats(
    children: int, expected: Bind | Refused
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            "<main>"
            + '<div class="row"><x-item><span>1</span></x-item></div>' * children
            + "</main>",
        )
        assert await verdict(session, "main > div:first-child span", None) == expected

    in_session(scenario)


def test_sixteen_ancestors_without_copies_still_bind_as_offered() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(
            session,
            "<main>"
            + "<section>" * 14
            + "<x-item><span>1</span></x-item>"
            + "</section>" * 14
            + "</main>",
        )
        assert await verdict(session, "span", None) == Bind()

    in_session(scenario)


def test_reset_rearms_a_completed_slow_write_cycle() -> None:
    async def scenario(app: App, session: BrowserSession) -> None:
        await session.page.goto(app.origin)
        request = "async () => (await fetch('/slow-write', {method:'POST'})).status"
        first = asyncio.create_task(session.page.evaluate(request))
        try:
            await asyncio.wait_for(app.slow_started.wait(), 5)
            assert app.writes == 0
            assert not first.done()
        finally:
            app.release_slow.set()
            assert await first == 204
        assert app.writes == 1
        assert (
            await session.page.evaluate(
                "async () => (await fetch('/reset', {method:'POST'})).status"
            )
            == 204
        )
        assert app.writes == 0
        assert app.messages == []
        assert not app.slow_started.is_set()
        assert not app.release_slow.is_set()
        second = asyncio.create_task(session.page.evaluate(request))
        try:
            await asyncio.wait_for(app.slow_started.wait(), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second), 0.2)
            assert app.writes == 0
            assert not second.done()
        finally:
            app.release_slow.set()
            assert await second == 204
        assert app.writes == 1

    in_app(scenario)


def test_attempted_hold_is_retired_when_acknowledgement_is_cancelled() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, '<div class="banner"><span class="counter">1</span></div>')
        offered = await session.page.query_selector("span.counter")
        assert offered is not None
        acknowledged = asyncio.Event()
        block = asyncio.Event()
        hold_body: str | None = None

        async def delayed_query(
            element: ElementHandle, body: str
        ) -> list[ElementHandle]:
            nonlocal hold_body
            found = await _query(element, body)
            if body.startswith("hold:"):
                hold_body = body
                acknowledged.set()
                await block.wait()
            return found

        async def enter() -> None:
            async with held_region(session.page, COUNT):
                raise AssertionError("cancelled entry must not enter its body")

        try:
            async with held_region(session.page, COUNT) as normal:
                assert normal is not None
                assert await normal.bound(offered)
            assert not await normal.bound(offered)
            with patch.object(binding, "_query", delayed_query):
                task = asyncio.create_task(enter())
                try:
                    await asyncio.wait_for(acknowledged.wait(), 5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    if not task.done():
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
            assert hold_body is not None
            token = hold_body.removeprefix("hold:")
            assert not await _passes(
                offered, f"bound:{token}|div.banner|span.counter|leaf"
            )
        finally:
            if hold_body is not None:
                await _query(offered, "drop:" + hold_body.removeprefix("hold:"))
            await offered.dispose()

    in_session(scenario)


def cause_chain(
    error: BaseException | None, failures: dict[str, BaseException]
) -> tuple[str, ...]:
    names = {id(failure): name for name, failure in failures.items()}
    found: list[str] = []
    while error is not None:
        found.append(names.get(id(error), repr(error)))
        error = error.__cause__
    return tuple(found)


@pytest.mark.parametrize(
    ("body", "disposal_fails", "expected"),
    [
        (None, False, ("drop",)),
        (None, True, ("disposal", "drop")),
        (ValueError, False, ("drop", "body")),
        (ValueError, True, ("disposal", "drop", "body")),
        (asyncio.CancelledError, False, ("body", "drop")),
        (asyncio.CancelledError, True, ("body", "disposal", "drop")),
    ],
)
def test_an_open_document_drop_error_propagates_and_does_not_claim_retirement(
    body: type[BaseException] | None, *, disposal_fails: bool, expected: tuple[str, ...]
) -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, '<div class="banner"><span class="counter">1</span></div>')
        offered = await session.page.query_selector("span.counter")
        assert offered is not None
        original_dispose = ElementHandle.dispose
        failures: dict[str, BaseException] = {
            "drop": Error("fake drop failure"),
            "disposal": Error("fake disposal failure"),
        }
        if body is not None:
            failures["body"] = body("fixture stop")
        disposed: list[ElementHandle] = []
        held: HeldRegion | None = None

        async def fail_drop(element: ElementHandle, text: str) -> list[ElementHandle]:
            if text.startswith("drop:"):
                raise failures["drop"]
            return await _query(element, text)

        async def dispose(element: ElementHandle) -> None:
            disposed.append(element)
            await original_dispose(element)
            if disposal_fails and held is not None and element is held.element:
                raise failures["disposal"]

        async def exit_held() -> None:
            nonlocal held
            async with held_region(session.page, COUNT) as held:
                assert held is not None
                assert await held.bound(offered)
                if "body" in failures:
                    raise failures["body"]

        try:
            with (
                patch.object(binding, "_query", fail_drop),
                patch.object(ElementHandle, "dispose", dispose),
                pytest.raises((Error, asyncio.CancelledError)) as caught,
            ):
                await exit_held()
            assert cause_chain(caught.value, failures) == expected
            assert held is not None
            assert held.element in disposed
            assert not session.page.is_closed()
            assert await held.bound(offered)
            await _query(offered, f"drop:{held.token}")
            assert not await held.bound(offered)
        finally:
            if held is not None:
                await _query(offered, f"drop:{held.token}")
            await offered.dispose()

    in_session(scenario)


def test_successful_cleanup_preserves_body_cancellation() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, '<div class="banner"><span class="counter">1</span></div>')
        offered = await session.page.query_selector("span.counter")
        assert offered is not None
        cancellation = asyncio.CancelledError()
        retired: list[HeldRegion] = []

        async def cancel() -> None:
            async with held_region(session.page, COUNT) as held:
                assert held is not None
                retired.append(held)
                raise cancellation

        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                await cancel()
            assert caught.value is cancellation
            assert caught.value.__cause__ is None
            assert not await retired[0].bound(offered)
        finally:
            await offered.dispose()

    in_session(scenario)


def test_a_cancelled_drop_still_disposes_and_is_not_retried() -> None:
    async def scenario(session: BrowserSession) -> None:
        await put(session, '<div class="banner"><span class="counter">1</span></div>')
        offered = await session.page.query_selector("span.counter")
        assert offered is not None
        original_dispose = ElementHandle.dispose
        dropping = asyncio.Event()
        stop = ValueError("fixture stop")
        disposed: list[ElementHandle] = []
        held: HeldRegion | None = None

        async def block_drop(element: ElementHandle, text: str) -> list[ElementHandle]:
            if text.startswith("drop:"):
                dropping.set()
                await asyncio.Event().wait()
            return await _query(element, text)

        async def dispose(element: ElementHandle) -> None:
            disposed.append(element)
            await original_dispose(element)

        async def fail() -> None:
            nonlocal held
            async with held_region(session.page, COUNT) as held:
                raise stop

        try:
            with (
                patch.object(binding, "_query", block_drop),
                patch.object(ElementHandle, "dispose", dispose),
            ):
                task = asyncio.create_task(fail())
                try:
                    await asyncio.wait_for(dropping.wait(), 5)
                finally:
                    task.cancel()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
            assert caught.value.__cause__ is stop
            assert held is not None
            assert held.element in disposed
            assert await held.bound(offered)
        finally:
            if held is not None:
                await _query(offered, f"drop:{held.token}")
            await offered.dispose()

    in_session(scenario)
