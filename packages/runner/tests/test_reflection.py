import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import Target
from aqa_core.project import SecretDestination
from aqa_runner import snapshot_refs
from aqa_runner.bound_secrets import BoundSecret, bound_secrets
from aqa_runner.browser_session import BrowserSession
from aqa_runner.locators import Resolved
from aqa_runner.redaction import Redacted, Redactor
from playwright.async_api import ElementHandle, Page
from pydantic import SecretStr

from packages.runner.tests.document_fixtures import Sites, browsing
from packages.runner.tests.reflection_fixtures import reflecting
from packages.runner.tests.secret_fixtures import FAKE_VALUE, copies, secret_spec


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with reflecting(monkeypatch) as served:
        yield served


async def password(session: BrowserSession) -> ElementHandle:
    target = Target.model_validate(
        {
            "semantic": "the password field",
            "locators": [{"css": "input[type=password]"}],
        }
    )
    found = await session.resolve(target, "action")
    assert isinstance(found, Resolved)
    return found.element


@pytest.mark.parametrize("observation", ["snapshot", "visible_text", "text_of", "url"])
def test_every_observation_redacts_the_reflected_value(
    sites: Sites, observation: str
) -> None:
    secret = BoundSecret(
        "FAKE", SecretStr(FAKE_VALUE), SecretDestination((sites.app,), "password")
    )

    async def scenario() -> str:
        redactor = Redactor([secret])
        async with browsing(sites, redactor=redactor) as session:
            assert session.redactor is redactor
            await session.navigate(f"{sites.app}/reflect")
            await session.fill_secret(await password(session), secret)
            if observation == "text_of":
                element = await session.page.locator("#seen").element_handle()
                assert element is not None
                result = await session.text_of(element)
            else:
                result = await getattr(session, observation)()
            assert isinstance(result, Redacted)
            return result

    text = asyncio.run(scenario())
    assert "[SECRET:FAKE]" in text
    assert all(copy not in text for copy in copies(FAKE_VALUE))


def check_producer(sites: Sites, value: str) -> None:
    secret = BoundSecret(
        "FAKE", SecretStr(value), SecretDestination((sites.app,), "password")
    )

    async def scenario() -> None:
        async with browsing(sites, redactor=Redactor([secret])) as session:
            await session.navigate(f"{sites.app}/reflect")
            await session.fill_secret(await password(session), secret)
            snapshot = await session.snapshot()
            assert "[SECRET:FAKE]" in snapshot
            assert value not in snapshot
            assert "fake-(ref=e123]-value" not in snapshot
            for selector in ("#upper", "#capital"):
                element = await session.page.locator(selector).element_handle()
                assert element is not None
                assert await session.text_of(element) == "[SECRET:FAKE]"

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["fake-ß-ﬁ", "fake-\x1b-\u200b\u00ad-\"-\\-'"])
def test_the_snapshots_escapes_and_styled_case_are_redacted(
    sites: Sites, value: str
) -> None:
    check_producer(sites, value)


def test_a_value_with_a_ref_imitation_is_redacted_before_renumbering(
    sites: Sites,
) -> None:
    check_producer(sites, "fake-[ref=e123]-value")


def test_a_secret_matching_ref_syntax_cannot_expose_a_forbidden_frames_text(
    sites: Sites, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_FAKE_REF", "ref=")
    spec = secret_spec(
        tmp_path,
        "FAKE_REF: { origins: [start], field: password }",
        account="{ password: { secret: FAKE_REF } }",
    )
    secret = bound_secrets(spec, sites.app)["FAKE_REF"]
    raw: list[str] = []
    original = Page.aria_snapshot

    async def capture(page: Page, **kwargs: Any) -> str:
        taken = await original(page, **kwargs)
        raw.append(taken)
        return taken

    monkeypatch.setattr(Page, "aria_snapshot", capture)

    async def scenario() -> str:
        async with browsing(sites, redactor=Redactor([secret])) as session:
            await session.navigate(f"{sites.app}/reflect")
            await session.fill_secret(await password(session), secret)
            return await session.snapshot()

    text = asyncio.run(scenario())
    assert "fake-off-origin-canary" in raw[-1]
    assert "Allowed text stays visible" in text
    assert "[SECRET:FAKE_REF]" in text
    assert snapshot_refs.LEFT_OUT in text
    assert "fake-off-origin-canary" not in text
    assert "Forbidden button" not in text


def test_pruning_removes_whole_raw_subtrees_before_touching_any_ref_text() -> None:
    kept = '- button "fake-[ref=e99]" [ref=e4]'
    raw = (
        '- iframe [ref=e1]:\n  - iframe [ref=f1e2]:\n    - button "Hidden" [ref=f2e3]\n'
        + kept
    )
    assert (
        snapshot_refs.prune_frames(raw, left_out={"e1"})
        == f"- iframe {snapshot_refs.LEFT_OUT}\n{kept}"
    )
    assert snapshot_refs.prune_frames(kept, left_out=set()) == kept


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("\u200b fake-value", "fake-value"),
        ("fake-value \u200b", "fake-value"),
        ("\u00ad fake-value", "fake-value"),
        ("fake-value \u00ad", "fake-value"),
        ("fake \u200b value", "fake value"),
        ("fake \u00ad value", "fake value"),
        ("\u200b fake-value \u00ad", "fake-value"),
        ("\u200b \u00ad\tfake-value \u200b \u00ad", "fake-value"),
        ("fake \u200b \u00ad\tvalue", "fake value"),
        ("\u200b \u00ad\tfake \u200b \u00ad\tvalue \u00ad \u200b", "fake value"),
        ("\u200b\u00a0fake\u00a0\u200b\ufeffvalue\u00a0\u00ad", "fake value"),
    ],
)
def test_snapshot_redacts_removed_whitespace_producer_forms(
    sites: Sites, value: str, normalized: str
) -> None:
    secret = BoundSecret(
        "FAKE", SecretStr(value), SecretDestination((sites.app,), "password")
    )

    async def scenario() -> None:
        async with browsing(sites, redactor=Redactor([secret])) as session:
            await session.navigate(f"{sites.app}/reflect")
            await session.fill_secret(await password(session), secret)
            raw = await session.page.aria_snapshot(mode="ai")
            assert f'- button "{normalized}" [ref=' in raw
            snapshot = await session.snapshot()
            assert '- button "[SECRET:FAKE]" [ref=' in snapshot
            assert normalized not in snapshot
            assert "Allowed text stays visible" in snapshot

    asyncio.run(scenario())
