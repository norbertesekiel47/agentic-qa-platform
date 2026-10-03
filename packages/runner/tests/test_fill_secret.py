"""`fill_secret` on the browser session: a test secret goes only into the
field its binding names, on the top-level page of one of its destinations,
and never into a frame from another origin (#49; ADR-0026, Test secrets;
SECURITY §5). The browser tests launch real Chromium on the OS that runs
them: Linux in CI, macOS locally."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from aqa_core.config import RoleField
from aqa_core.project import SecretDestination
from aqa_runner.bound_secrets import BoundSecret, bound_secrets
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import PolicyEvent, PolicyEventError
from aqa_runner.secret_fields import SecretRefusedError
from playwright.async_api import ElementHandle, Error
from pydantic import SecretStr

from packages.runner.tests.document_fixtures import (
    Sites,
    browsing,
    ref_for,
    serving_sites,
    to,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE, secret_spec


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def password(*origins: str) -> BoundSecret:
    """TEST_PASSWORD, bound to password fields on `origins`."""
    return BoundSecret(
        "TEST_PASSWORD", SecretStr(FAKE_VALUE), SecretDestination(origins, "password")
    )


def token(*origins: str) -> BoundSecret:
    """API_TOKEN, bound to the textbox named "API token" on `origins`."""
    return BoundSecret(
        "API_TOKEN",
        SecretStr(FAKE_VALUE),
        SecretDestination(origins, RoleField(role="textbox", name="API token")),
    )


async def held_value(element: ElementHandle) -> str:
    """What `element`, a field, holds."""
    return str(
        await element.evaluate("(e) => e.isContentEditable ? e.textContent : e.value")
    )


async def field(session: BrowserSession, role: str, name: str) -> ElementHandle:
    """The element with `role` and `name` in a new snapshot, as the
    navigator's tool would find it."""
    return await session.locate(ref_for(await session.snapshot(), role, name))


async def refused[E: Exception](
    session: BrowserSession, element: ElementHandle, secret: BoundSecret, error: type[E]
) -> E:
    """The `error` fill_secret raises for `element`, once the element is shown
    to hold what it held before, none of the secret's value."""
    before = await held_value(element)
    with pytest.raises(error) as raised:
        await session.fill_secret(element, secret)
    assert await held_value(element) == before
    assert FAKE_VALUE not in str(raised.value)
    return raised.value


def test_fill_secret_fills_a_bound_password_field_on_the_start_origin(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[str, str]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/signin")
            secret = await field(session, "textbox", "Password")
            await session.fill_secret(secret, password(sites.app))
            email = await field(session, "textbox", "Email")
            return await held_value(secret), await held_value(email)

    assert asyncio.run(scenario()) == (FAKE_VALUE, "")


def test_a_password_type_written_in_capitals_is_a_password_field(
    sites: Sites,
) -> None:
    # HTML reads the type attribute case-insensitively.
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/signin")
            upper = await field(session, "textbox", "Upper")
            await session.fill_secret(upper, password(sites.app))
            return await held_value(upper)

    assert asyncio.run(scenario()) == FAKE_VALUE


def test_fill_secret_on_a_page_off_the_allowed_origins_is_a_policy_event(
    sites: Sites,
) -> None:
    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            # A redirect lands the page on the subresource host's sign-in
            # page, which navigate reports; the field is taken unchecked, as
            # a stale handle could be.
            with pytest.raises(PolicyEventError):
                await session.navigate(
                    f"{sites.app}/redirect?to={to(f'{sites.cdn}/signin')}"
                )
            elsewhere = await session.page.get_by_label("Password").element_handle()
            return await refused(
                session, elsewhere, password(sites.app, sites.cdn), PolicyEventError
            )

    assert asyncio.run(scenario()).event == PolicyEvent(
        "document", f"{sites.cdn}/signin", sites.cdn
    )


def test_fill_secret_refuses_an_allowed_origin_that_isnt_in_the_binding(
    sites: Sites,
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites) as session:
            # The run allows the second origin; the secret is bound to the
            # start origin only.
            await session.navigate(f"{sites.other}/signin")
            other = await field(session, "textbox", "Password")
            return await refused(
                session, other, password(sites.app), SecretRefusedError
            )

    assert str(asyncio.run(scenario())) == (
        f"fill_secret refused TEST_PASSWORD: the page is on {sites.other}, which "
        f"isn't one of its destinations: {sites.app}"
    )


@pytest.mark.parametrize("name", ["Name", "Notes", "Story"])
def test_fill_secret_refuses_a_field_that_isnt_a_password_input(
    sites: Sites, name: str
) -> None:
    # A text input, a textarea and a contenteditable textbox, each holding
    # text already.
    async def scenario() -> SecretRefusedError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/fields")
            other = await field(session, "textbox", name)
            return await refused(
                session, other, password(sites.app), SecretRefusedError
            )

    assert str(asyncio.run(scenario())) == (
        'fill_secret refused TEST_PASSWORD: the field isn\'t an <input type="password">'
    )


def test_fill_secret_fills_the_field_whose_role_and_name_match(
    sites: Sites,
) -> None:
    async def scenario() -> tuple[str, str, str]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/tokens")
            textbox = await field(session, "textbox", "API token")
            await session.fill_secret(textbox, token(sites.app))
            return (
                await held_value(textbox),
                await held_value(await field(session, "textbox", "API key")),
                await held_value(await field(session, "searchbox", "API token")),
            )

    assert asyncio.run(scenario()) == (FAKE_VALUE, "", "")


@pytest.mark.parametrize(
    ("role", "name"), [("textbox", "API key"), ("searchbox", "API token")]
)
def test_fill_secret_refuses_a_field_whose_role_or_name_doesnt_match(
    sites: Sites, role: str, name: str
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/tokens")
            other = await field(session, role, name)
            return await refused(session, other, token(sites.app), SecretRefusedError)

    assert str(asyncio.run(scenario())) == (
        'fill_secret refused API_TOKEN: the field isn\'t the textbox named "API token"'
    )


def test_fill_secret_refuses_a_field_in_a_frame_from_another_allowed_origin(
    sites: Sites,
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/framed")
            # The second allowed origin's sign-in page, framed by the start
            # origin's, with the secret bound to both: only the page's own
            # origin's fields may take it.
            framed = await field(session, "textbox", "Password")
            return await refused(
                session, framed, password(sites.app, sites.other), SecretRefusedError
            )

    assert str(asyncio.run(scenario())) == (
        f"fill_secret refused TEST_PASSWORD: the field is in a frame on "
        f"{sites.other}, not on the page's origin, {sites.app}"
    )


@pytest.mark.parametrize(
    ("path", "why"),
    [
        # Sandboxed: on an opaque origin, though its URL is on the page's.
        ("/sandboxed", "a frame the page can't reach"),
        # On the page's origin, inside a frame of another.
        ("/sandwich", "a frame on {other}"),
    ],
)
def test_fill_secret_refuses_a_field_whose_frames_are_not_all_the_page_s_origin(
    sites: Sites, path: str, why: str
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}{path}")
            framed = await field(session, "textbox", "Password")
            return await refused(
                session, framed, password(sites.app, sites.other), SecretRefusedError
            )

    assert str(asyncio.run(scenario())) == (
        f"fill_secret refused TEST_PASSWORD: the field is in "
        f"{why.format(other=sites.other)}, not on the page's origin, {sites.app}"
    )


def test_fill_secret_into_a_frame_off_the_allowed_origins_is_a_policy_event(
    sites: Sites,
) -> None:
    async def scenario() -> PolicyEventError:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/framed")
            # The subresource host's frame never shows in a snapshot, so its
            # field is taken unchecked.
            [frame] = [f for f in session.page.frames if f.url == f"{sites.cdn}/signin"]
            framed = await frame.get_by_label("Password").element_handle()
            return await refused(
                session, framed, password(sites.app, sites.cdn), PolicyEventError
            )

    assert asyncio.run(scenario()).event == PolicyEvent(
        "frame", f"{sites.cdn}/signin", sites.cdn
    )


def test_fill_secret_fills_a_field_in_a_frame_of_the_page_s_own_origin(
    sites: Sites,
) -> None:
    async def scenario() -> str:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/framed")
            inner = await field(session, "textbox", "Inner")
            await session.fill_secret(inner, password(sites.app))
            return await held_value(inner)

    assert asyncio.run(scenario()) == FAKE_VALUE


def test_a_spec_edit_cannot_move_a_secret_to_an_origin_it_adds(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    # The spec now starts at a path that decoded would name another host,
    # and allows the second origin, as the run does.
    spec = secret_spec(
        tmp_path,
        start_url="/%2f%2fother.example.test/signin",
        allowed_origins=(sites.other,),
    )
    secret = bound_secrets(spec, sites.app)["TEST_PASSWORD"]

    async def scenario() -> tuple[SecretRefusedError, str]:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.other}/signin")
            moved = await refused(
                session,
                await field(session, "textbox", "Password"),
                secret,
                SecretRefusedError,
            )
            await session.navigate(f"{sites.app}/signin")
            start = await field(session, "textbox", "Password")
            await session.fill_secret(start, secret)
            return moved, await held_value(start)

    moved, filled = asyncio.run(scenario())

    assert f"the page is on {sites.other}" in str(moved)
    assert filled == FAKE_VALUE


def test_a_page_that_throws_the_value_back_gets_it_into_no_error(
    sites: Sites,
) -> None:
    async def scenario() -> Error:
        async with browsing(sites) as session:
            await session.navigate(f"{sites.app}/throws")
            thrown = await field(session, "textbox", "Password")
            with pytest.raises(Error) as raised:
                await session.fill_secret(thrown, password(sites.app))
            return raised.value

    error = asyncio.run(scenario())

    assert error.message == (
        "fill_secret: the field didn't take TEST_PASSWORD: it takes no text, its "
        "page changed the value, or its page broke the fill"
    )
    # Nothing of the page's own error is kept, not even as the context.
    assert error.__cause__ is None
    assert error.__context__ is None
    assert FAKE_VALUE not in repr(error)
