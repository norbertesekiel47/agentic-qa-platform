"""`BrowserSession.contrast`, `contrast_min`'s read of its target (DATA_MODEL
§7, Contrast; ADR-0025's #254 amendment): the lowest WCAG contrast ratio of
the target's text against the background its ancestors give it, computed
from styles, or the fixed reason for the first case styles can't settle.
Real Chromium: each refused page meets one case."""

import asyncio
from collections.abc import Iterator

import pytest
from aqa_core.compiled import ByCss, Target
from aqa_runner.contrast import ContrastRefusedError
from aqa_runner.document_origins import PolicyEvent, PolicyEventError
from aqa_runner.locators import Resolved

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.test_visible_unoccluded import child_frame


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def read(sites: Sites, html: str) -> float | str:
    """What `contrast` reads of the element marked `data-is="t"` on a page
    of `html`: its lowest ratio, or the reason it refused. It never
    scrolls."""

    async def scenario() -> float | str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            await session.page.set_content(html)
            target = Target(
                semantic="the notice", locators=(ByCss(css='[data-is="t"]'),)
            )
            found = await session.resolve(target, "assertion")
            assert isinstance(found, Resolved), found
            try:
                return await session.contrast(found.element)
            except ContrastRefusedError as refused:
                return str(refused)
            finally:
                assert await session.page.evaluate("[scrollX, scrollY]") == [0, 0]

    return asyncio.run(scenario())


def on_white(notice: str, style: str = "") -> str:
    """`notice` inside a white block, which gives it its background."""
    return f'<div style="background: #fff; {style}">{notice}</div>'


@pytest.mark.parametrize(
    ("html", "ratio"),
    [
        ('<p data-is="t" style="background: #fff; color: #767676">Save</p>', 4.5422),
        (on_white('<p data-is="t" style="color: #777777">Save</p>'), 4.4781),
        (
            (
                '<div style="background: #000"><p data-is="t" style="color: #000; '
                'background: rgba(255, 255, 255, 0.5)">Save</p></div>'
            ),
            5.2808,
        ),
        (on_white('<p data-is="t" style="color: rgba(0, 0, 0, 0.5)">Save</p>'), 3.9767),
        (
            on_white('<p data-is="t">Save <span style="color: #ccc">now</span></p>'),
            1.6059,
        ),
        (
            on_white(
                '<p data-is="t">Save<span style="display: none; color: #fff">'
                'now</span><span style="visibility: hidden; color: #fff">now'
                "</span></p>"
            ),
            21,
        ),
    ],
    ids=[
        "its own background",
        "an ancestor's background",
        "a translucent layer over an opaque ancestor",
        "a translucent text colour",
        "a descendant's lower contrast",
        "hidden descendant text",
    ],
)
def test_the_lowest_ratio_of_the_targets_text_is_computed_from_styles(
    sites: Sites, html: str, ratio: float
) -> None:
    assert read(sites, html) == pytest.approx(ratio, abs=1e-4)


SHADOW = "contrast_min does not support text in or slotted into a shadow tree"


def shadow(host: str, html: str) -> str:
    """A script that gives the first `host` an open shadow root of `html`."""
    return (
        f"<script>document.querySelector({host!r})"
        f".attachShadow({{mode: 'open'}}).innerHTML = {html!r}</script>"
    )


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        (
            on_white('<my-notice data-is="t">Save</my-notice>')
            + shadow("my-notice", "<slot></slot>"),
            SHADOW,
        ),
        (
            on_white("", "height: 40px") + shadow("div", '<p data-is="t">Save</p>'),
            SHADOW,
        ),
        (
            on_white('<p data-is="t"> <span style="display: none">Save</span> </p>'),
            "contrast_min found no rendered text in its target",
        ),
        (
            on_white('<p data-is="t" style="margin-top: 120vh">Save</p>'),
            "contrast_min reads only text inside the viewport",
        ),
        (
            on_white(
                '<p data-is="t">Save</p><div style="position: absolute; inset: 0; '
                'background: rgba(0, 0, 0, 0.1)"></div>',
                "position: relative",
            ),
            (
                "contrast_min's hit test at its text found another element, or not "
                "the text's own"
            ),
        ),
        (
            on_white('<p data-is="t">Save</p>', "opacity: 0.9"),
            (
                "contrast_min does not support opacity, filters, masks or blend "
                "modes on its text or behind it"
            ),
        ),
        (
            on_white(
                '<p data-is="t">Save</p>',
                "background-image: linear-gradient(#fff, #eee)",
            ),
            (
                "contrast_min does not support a background image, gradient, "
                "generated background or clipped background behind its text"
            ),
        ),
        (
            on_white('<p data-is="t" style="color: oklch(0.6 0.1 200)">Save</p>'),
            "contrast_min reads only colours that compute to rgb() or rgba()",
        ),
        (
            '<div><p data-is="t">Save</p></div>',
            "contrast_min found no opaque background behind its text",
        ),
    ],
    ids=[
        "R2 slotted",
        "R2 in a shadow tree",
        "R3",
        "R4",
        "R5",
        "R6",
        "R7",
        "R8",
        "R9",
    ],
)
def test_a_case_styles_cant_settle_is_refused_with_its_reason(
    sites: Sites, html: str, reason: str
) -> None:
    assert read(sites, html) == reason


def test_contrast_refuses_an_element_off_the_allowed_origins(sites: Sites) -> None:
    async def scenario() -> PolicyEvent:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/frames")
            frame = next(
                frame
                for frame in session.page.frames
                if frame.url == f"{sites.cdn}/doc"
            )
            element = await frame.query_selector("button")
            assert element is not None
            with pytest.raises(PolicyEventError) as refused:
                await session.contrast(element)
            return refused.value.event

    assert asyncio.run(scenario()) == PolicyEvent(
        "frame", f"{sites.cdn}/doc", sites.cdn
    )


def test_contrast_refuses_an_element_in_an_allowed_child_frame(sites: Sites) -> None:
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            frame = await child_frame(session.page.main_frame, f"{sites.app}/kept")
            await frame.set_content(on_white('<p data-is="t">Save</p>'))
            element = await frame.query_selector('[data-is="t"]')
            assert element is not None
            with pytest.raises(ContrastRefusedError) as refused:
                await session.contrast(element)
            return str(refused.value)

    assert asyncio.run(scenario()) == (
        "contrast_min does not support elements inside frames"
    )
