"""A locator candidate or scope that spells a bound value is dropped before
generation tries it (ADR-0025's #53 P13 amendment; relay: drop, never
redact): Playwright is never handed it, and the next candidate of its kind,
or the next scope, is tried instead. In real Chromium, through the session's
scanned snapshot, as the navigator's tools generate."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aqa_core.compiled import (
    ByCss,
    ByLabel,
    ByPlaceholder,
    ByRole,
    ByTestId,
    Locator,
    Target,
)
from aqa_core.project import SecretDestination
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.locator_generation import (
    LocatorError,
    Seen,
    TargetUses,
    register_identity_engine,
    seen_element,
)
from aqa_runner.redaction import Redactor
from playwright.async_api import Locator as PlaywrightLocator
from playwright.async_api import Page, async_playwright
from pydantic import SecretStr

from packages.runner.tests.document_fixtures import ref_for
from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.pilot_pages import ORIGIN, put
from packages.runner.tests.secret_fixtures import OTHER_FAKE_VALUE

VALUE = OTHER_FAKE_VALUE
REDACTOR = Redactor(
    [
        BoundSecret(
            "API_TOKEN", SecretStr(VALUE), SecretDestination((ORIGIN,), "password")
        )
    ]
)
MARKER = "[SECRET:API_TOKEN]"
MEANING = "the element the test acts on"


def spelled(locator: Locator) -> bool:
    return REDACTOR.spells(locator.model_dump(mode="json"))


@pytest.fixture
def handed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every selector, text and name pattern Playwright's page and locators
    are handed from now on."""
    texts: list[str] = []

    def recording(method: Callable[..., Any]) -> Callable[..., Any]:
        def recorded(self: object, *args: object, **options: object) -> Any:
            name = options.get("name")
            texts.extend(arg for arg in args if isinstance(arg, str))
            texts.extend([name.pattern] if isinstance(name, re.Pattern) else [])
            return method(self, *args, **options)

        return recorded

    for kind in (Page, PlaywrightLocator):
        for method in (
            "get_by_role",
            "get_by_label",
            "get_by_placeholder",
            "get_by_test_id",
            "locator",
        ):
            monkeypatch.setattr(kind, method, recording(getattr(kind, method)))
    return texts


def generated[T](scenario: Callable[[BrowserSession], Awaitable[T]]) -> T:
    """`scenario`'s result in a session that scans for the value."""

    async def run() -> T:
        async with async_playwright() as playwright:
            await register_identity_engine(playwright)
            async with (
                egress_proxy() as egress,
                open_browser_session(
                    playwright.chromium, egress=egress, redactor=REDACTOR
                ) as session,
                asyncio.timeout(60),
            ):
                return await scenario(session)

    return asyncio.run(run())


async def offered(
    session: BrowserSession, html: str, role: str, name: str, *, named: str | None
) -> Seen:
    """The element with `role` and `name` in the page's scanned snapshot, as
    a tool sees it; `named` stands in for its snapshot name."""
    await put(session, html)
    snapshot = await session.snapshot()
    seen = await seen_element(session, snapshot, ref_for(snapshot, role, name))
    return seen if named is None else Seen(seen.element, seen.role, named)


SAVE = ByRole(role="button", name="Save")
BUTTON = ByCss(css="button")
PANEL = ByCss(css="div.panel")


@pytest.mark.parametrize(
    ("html", "role", "name", "named", "kept"),
    [
        (
            f'<label>{VALUE} <input placeholder="Name"></label>',
            "textbox",
            MARKER,
            None,
            (ByPlaceholder(placeholder="Name"), ByCss(css="input")),
        ),
        (
            f'<label>Name <input placeholder="{VALUE}"></label>',
            "textbox",
            "Name",
            None,
            (
                ByRole(role="textbox", name="Name"),
                ByLabel(label="Name"),
                ByCss(css="input"),
            ),
        ),
        (
            f'<button data-testid="{VALUE}">Save</button>',
            "button",
            "Save",
            None,
            (SAVE, BUTTON),
        ),
        (f'<button id="{VALUE}">Save</button>', "button", "Save", None, (SAVE, BUTTON)),
        (
            f'<button class="{VALUE}">Save</button>',
            "button",
            "Save",
            None,
            (SAVE, BUTTON),
        ),
        (
            f'<input name="{VALUE}" aria-label="Query">',
            "textbox",
            "Query",
            None,
            (ByRole(role="textbox", name="Query"), ByCss(css="input")),
        ),
        (f'<{VALUE} role="button">Save</{VALUE}>', "button", "Save", None, (SAVE,)),
        (
            (
                f'<div id="{VALUE}" class="panel"><button>Save</button></div>'
                "<div><button>Save</button></div>"
            ),
            "button",
            "Save",
            None,
            (
                ByRole(role="button", name="Save", scope=PANEL),
                ByCss(css="button", scope=PANEL),
            ),
        ),
        (
            f'<button aria-label="{VALUE}">Save</button>',
            "button",
            MARKER,
            VALUE,
            (BUTTON,),
        ),
    ],
    ids=[
        "label",
        "placeholder",
        "test-id",
        "stable-id",
        "class",
        "name-attribute",
        "custom-element-tag",
        "ancestor-scope",
        "role-name",
    ],
)
def test_a_dropped_candidate_or_scope_never_reaches_playwright(
    handed: list[str],
    *,
    html: str,
    role: str,
    name: str,
    named: str | None,
    kept: tuple[Locator, ...],
) -> None:
    async def scenario(session: BrowserSession) -> tuple[Target, ...]:
        seen = await offered(session, html, role, name, named=named)
        uses = TargetUses(MEANING, checks_text=False, drop=spelled)
        handed.clear()
        async with session.observing_page() as page:
            await uses.add(page, seen, "action")
        return uses.targets

    assert generated(scenario) == (Target(semantic=MEANING, locators=kept),)
    assert handed
    assert [text for text in handed if VALUE in text] == []
    assert [text for text in handed if re.search(text, VALUE)] == []


def test_an_element_only_a_dropped_kind_finds_is_a_locator_error(
    handed: list[str],
) -> None:
    async def scenario(session: BrowserSession) -> tuple[str, tuple[Target, ...]]:
        twins = '<button data-testid="{}">Save</button><button>Save</button>'
        uses = TargetUses(MEANING, checks_text=False, drop=spelled)
        seen = await offered(session, twins.format(VALUE), "button", "Save", named=None)
        with pytest.raises(LocatorError) as refused:
            async with session.observing_page() as page:
                await uses.add(page, seen, "action")
        seen = await offered(
            session, twins.format("save-button"), "button", "Save", named=None
        )
        async with session.observing_page() as page:
            await uses.add(page, seen, "action")
        return str(refused.value), uses.targets

    refused, targets = generated(scenario)

    assert refused == (
        f"{MEANING}: no locator of the grammar finds the button element alone for "
        "an action"
    )
    assert targets == (
        Target(semantic=MEANING, locators=(ByTestId(testid="save-button"),)),
    )
    assert [text for text in handed if VALUE in text] == []
