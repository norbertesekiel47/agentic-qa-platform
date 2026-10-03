"""`fill_secret` on the browser session: a test secret goes only into the
field its binding names, on the top-level page of one of its destinations,
and never into a frame from another origin (#49; ADR-0026, Test secrets;
SECURITY §5). The browser tests launch real Chromium on the OS that runs
them: Linux in CI, macOS locally."""

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from aqa_core.config import RoleField
from aqa_core.project import SecretDestination
from aqa_runner.bound_secrets import BoundSecret, bound_secrets
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import PolicyEvent, PolicyEventError
from aqa_runner.redaction import Redactor
from aqa_runner.secret_fields import SecretNotFilledError, SecretRefusedError
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


# What fill_secret raises when TEST_PASSWORD didn't go in.
NOT_FILLED = (
    "fill_secret: TEST_PASSWORD wasn't filled: the field takes no text, its page "
    "changed the value, or the page broke or closed during the fill or its checks"
)


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


@pytest.fixture
def redactor(sites: Sites) -> Redactor:
    return Redactor([password(sites.app), token(sites.app)])


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
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> tuple[str, str]:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/signin")
            secret = await field(session, "textbox", "Password")
            await session.fill_secret(secret, password(sites.app))
            email = await field(session, "textbox", "Email")
            return await held_value(secret), await held_value(email)

    assert asyncio.run(scenario()) == (FAKE_VALUE, "")


def test_a_password_type_written_in_capitals_is_a_password_field(
    redactor: Redactor,
    sites: Sites,
) -> None:
    # HTML reads the type attribute case-insensitively.
    async def scenario() -> str:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/signin")
            upper = await field(session, "textbox", "Upper")
            await session.fill_secret(upper, password(sites.app))
            return await held_value(upper)

    assert asyncio.run(scenario()) == FAKE_VALUE


def test_fill_secret_on_a_page_off_the_allowed_origins_is_a_policy_event(
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> PolicyEventError:
        async with browsing(sites, redactor=redactor) as session:
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
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites, redactor=redactor) as session:
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
    redactor: Redactor, sites: Sites, name: str
) -> None:
    # A text input, a textarea and a contenteditable textbox, each holding
    # text already.
    async def scenario() -> SecretRefusedError:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/fields")
            other = await field(session, "textbox", name)
            return await refused(
                session, other, password(sites.app), SecretRefusedError
            )

    assert str(asyncio.run(scenario())) == (
        'fill_secret refused TEST_PASSWORD: the field isn\'t an <input type="password">'
    )


def test_fill_secret_fills_the_field_whose_role_and_name_match(
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> tuple[str, str, str]:
        async with browsing(sites, redactor=redactor) as session:
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
    ("role", "name"),
    [
        ("textbox", "API key"),
        ("searchbox", "API token"),
        # A name the binding's is only part of: names match whole.
        ("textbox", "API tokens"),
    ],
)
def test_fill_secret_refuses_a_field_whose_role_or_name_doesnt_match(
    redactor: Redactor, sites: Sites, role: str, name: str
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/tokens")
            other = await field(session, role, name)
            return await refused(session, other, token(sites.app), SecretRefusedError)

    assert str(asyncio.run(scenario())) == (
        'fill_secret refused API_TOKEN: the field isn\'t the textbox named "API token"'
    )


def test_fill_secret_refuses_a_field_in_a_frame_from_another_allowed_origin(
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites, redactor=redactor) as session:
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
        # Sandboxed by its response's CSP, which no attribute shows.
        ("/framed-ugc", "a frame the page can't reach"),
    ],
)
def test_fill_secret_refuses_a_field_whose_frames_are_not_all_the_page_s_origin(
    redactor: Redactor, sites: Sites, path: str, why: str
) -> None:
    async def scenario() -> SecretRefusedError:
        async with browsing(sites, redactor=redactor) as session:
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
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> PolicyEventError:
        async with browsing(sites, redactor=redactor) as session:
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
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> str:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/framed")
            inner = await field(session, "textbox", "Inner")
            await session.fill_secret(inner, password(sites.app))
            return await held_value(inner)

    assert asyncio.run(scenario()) == FAKE_VALUE


def test_a_spec_edit_cannot_move_a_secret_to_an_origin_it_adds(
    redactor: Redactor, sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        async with browsing(sites, redactor=redactor) as session:
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

    assert str(moved) == (
        f"fill_secret refused TEST_PASSWORD: the page is on {sites.other}, which "
        f"isn't one of its destinations: {sites.app}"
    )
    assert filled == FAKE_VALUE


def test_a_page_that_throws_the_value_back_gets_it_into_no_error(
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> Error:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/throws")
            thrown = await field(session, "textbox", "Password")
            with pytest.raises(Error) as raised:
                await session.fill_secret(thrown, password(sites.app))
            return raised.value

    error = asyncio.run(scenario())

    assert isinstance(error, SecretNotFilledError)
    assert error.message == NOT_FILLED
    # Nothing of the page's own error is kept, not even as the context.
    assert error.__cause__ is None
    assert error.__context__ is None
    assert FAKE_VALUE not in repr(error)


def test_fill_secret_refuses_a_page_sandboxed_onto_an_opaque_origin(
    redactor: Redactor,
    sites: Sites,
) -> None:
    # The page's URL is on a destination, but its response sandboxes it, as
    # an app does with content it doesn't trust.
    async def scenario() -> SecretRefusedError:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/ugc")
            sandboxed = await field(session, "textbox", "Password")
            return await refused(
                session, sandboxed, password(sites.app), SecretRefusedError
            )

    assert str(asyncio.run(scenario())) == (
        "fill_secret refused TEST_PASSWORD: the page's document is on an opaque "
        f"origin (sandboxed), though its URL is on {sites.app}"
    )


def test_a_value_the_page_throws_back_from_a_check_reaches_no_error(
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> Error:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/rethrows")
            secret = await field(session, "textbox", "Password")
            await session.fill_secret(secret, password(sites.app))
            # The field now holds the value, and its page throws it back from
            # the check every action makes.
            with pytest.raises(Error) as raised:
                await session.fill_secret(secret, password(sites.app))
            return raised.value

    error = asyncio.run(scenario())

    assert isinstance(error, SecretNotFilledError)
    assert error.message == NOT_FILLED
    assert error.__cause__ is None
    assert error.__context__ is None
    assert FAKE_VALUE not in repr(error)


def test_fill_secret_returns_the_window_of_the_requests_the_fill_started(
    redactor: Redactor,
    sites: Sites,
) -> None:
    async def scenario() -> list[tuple[str, str]]:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/reports")
            secret = await field(session, "textbox", "Password")
            window = await session.fill_secret(secret, password(sites.app))
            await session.settle(window)
            return [
                (request.method, urlsplit(request.url).path)
                for request in window.requests.kept
            ]

    assert asyncio.run(scenario()) == [("GET", "/typed")]


def test_a_binding_s_name_is_compared_normalized(
    sites: Sites, redactor: Redactor
) -> None:
    # Written with two spaces, as a config may; the page's name has one.
    spaced = BoundSecret(
        "API_TOKEN",
        SecretStr(FAKE_VALUE),
        SecretDestination((sites.app,), RoleField(role="textbox", name="API  token")),
    )

    async def scenario() -> str:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/tokens")
            textbox = await field(session, "textbox", "API token")
            await session.fill_secret(textbox, spaced)
            return await held_value(textbox)

    assert asyncio.run(scenario()) == FAKE_VALUE


def test_a_binding_s_name_that_compares_as_empty_names_no_field(
    sites: Sites, redactor: Redactor
) -> None:
    # A soft hyphen alone, which comparing strips, against a field with no
    # name at all.
    empty = BoundSecret(
        "API_TOKEN",
        SecretStr(FAKE_VALUE),
        SecretDestination((sites.app,), RoleField(role="textbox", name="\u00ad")),
    )

    async def scenario() -> SecretRefusedError:
        async with browsing(sites, redactor=redactor) as session:
            await session.navigate(f"{sites.app}/unnamed")
            found = re.search(r"- textbox \[ref=(e\d+)\]", await session.snapshot())
            assert found is not None
            unnamed = await session.locate(found[1])
            return await refused(session, unnamed, empty, SecretRefusedError)

    assert "the field isn't the textbox named" in str(asyncio.run(scenario()))


@pytest.mark.parametrize("coverage", ["absent", "wrong_name", "wrong_value"])
def test_a_session_fills_no_secret_its_redactor_does_not_cover(
    sites: Sites, coverage: str
) -> None:
    bound = password(sites.app)
    covered = (
        []
        if coverage == "absent"
        else [
            BoundSecret(
                "OTHER_FAKE" if coverage == "wrong_name" else bound.name,
                SecretStr("another-fake-value")
                if coverage == "wrong_value"
                else bound.value,
                bound.destination,
            )
        ]
    )

    async def scenario() -> None:
        manager = (
            browsing(sites)
            if coverage == "absent"
            else browsing(sites, redactor=Redactor(covered))
        )
        async with manager as session:
            await session.navigate(f"{sites.app}/signin")
            element = await field(session, "textbox", "Password")
            error = await refused(session, element, bound, ValueError)
            assert "session redactor does not cover" in str(error)
            assert await held_value(element) == ""

    asyncio.run(scenario())
