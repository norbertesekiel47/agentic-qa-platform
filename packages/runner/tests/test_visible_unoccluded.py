"""Deterministic visual checks through the origin-controlled browser session (#48)."""

import asyncio
from collections.abc import Iterator

import pytest
from aqa_core.compiled import ByCss, Target
from aqa_runner.document_origins import PolicyEvent, PolicyEventError
from aqa_runner.locators import Resolved

from packages.runner.tests.document_fixtures import Sites, browsing, serving_sites
from packages.runner.tests.test_locators import ACTIONABLE_LAYOUTS


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def judged(
    sites: Sites, html: str, min_size: tuple[int, int] = (44, 24)
) -> tuple[bool, list[int]]:
    async def scenario() -> tuple[bool, list[int]]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/kept")
            await session.page.set_content(html)
            target = Target(semantic="the link", locators=(ByCss(css='[data-is="b"]'),))
            found = await session.resolve(target, "assertion")
            assert isinstance(found, Resolved), found
            held = await session.unoccluded(found.element, min_size, True)
            scroll: list[int] = await session.page.evaluate("[scrollX, scrollY]")
            return held, scroll

    return asyncio.run(scenario())


def link(style: str = "") -> str:
    return (
        '<a href="#" data-is="b" style="position: absolute; left: 8px; top: 8px; '
        f'display: block; width: 100px; height: 30px; {style}">Sign in</a>'
    )


def test_a_link_under_a_stretched_logo_resolves_and_is_occluded(sites: Sites) -> None:
    covered = link() + (
        '<a href="#" style="position: absolute; top: 0; left: 0; '
        'width: 400px; height: 100px; z-index: 1; background: white">Logo</a>'
    )

    assert judged(sites, covered) == (False, [0, 0])


@pytest.mark.parametrize(
    "style",
    [
        "top: 110vh",
        "left: -200px",
        "left: -1px",
        "top: -1px",
        "left: calc(100vw - 99px)",
        "top: calc(100vh - 29px)",
    ],
)
def test_an_element_outside_the_viewport_fails(sites: Sites, style: str) -> None:
    assert judged(sites, link(style)) == (False, [0, 0])


@pytest.mark.parametrize("style", ["width: 43px", "height: 23px"])
def test_an_undersized_element_fails(sites: Sites, style: str) -> None:
    assert judged(sites, link(style)) == (False, [0, 0])


@pytest.mark.parametrize(
    "style",
    [
        "",
        "width: 44px; height: 24px",
        "left: calc(100vw - 100px); top: calc(100vh - 30px)",
    ],
)
def test_a_visible_uncovered_element_passes(sites: Sites, style: str) -> None:
    assert judged(sites, link(style)) == (True, [0, 0])


@pytest.mark.parametrize(
    "layout", ["wrapped onto two lines", "in an open shadow root", "hit on a child"]
)
def test_the_first_rect_hit_test_handles_wrapping_shadow_roots_and_children(
    sites: Sites,
    layout: str,
) -> None:
    assert judged(sites, ACTIONABLE_LAYOUTS[layout], (1, 1)) == (True, [0, 0])


def test_unoccluded_refuses_an_element_off_the_allowed_origins(sites: Sites) -> None:
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
                await session.unoccluded(element, (1, 1), True)
            return refused.value.event

    assert asyncio.run(scenario()) == PolicyEvent(
        "frame", f"{sites.cdn}/doc", sites.cdn
    )
