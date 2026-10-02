"""Searching page text and URLs for a compiled check, bounded in time and off
the event loop: the page controls the text, and Python's `re` can't be
interrupted (DATA_MODEL §7, Text parameters; ADR-0024's 2026-10-02
amendment; #46)."""

import asyncio
import os
import subprocess
import time
from typing import Any

import pytest
from aqa_core.compiled import TextVisible
from aqa_runner import text_search
from aqa_runner.text_search import SearchTimeoutError, text_matches, url_matches

from packages.runner.tests.test_browser_session import (
    RUNNER_SECRETS,
    environment_of,
    fake_value,
)

# Exponential for re: each extra "a" doubles the work before the "b" fails it.
CATASTROPHIC = "(a+)+$"
HOSTILE_TEXT = "a" * 40 + "b"


def check(**fields: Any) -> TextVisible:
    return TextVisible.model_validate(
        {"id": "a1", "expect_index": 0, "check": "text_visible", **fields}
    )


@pytest.mark.parametrize(
    ("fields", "rendered", "found"),
    [
        # Whole words, any case, against normalized text (ADR-0025).
        ({"text": "post comment"}, "POST\xa0 COMMENT", True),
        ({"text": "(3)"}, "Cart(3)", True),
        ({"text": "1"}, "10 items", False),
        ({"text": "Payment"}, "Pay\xadment", True),  # a soft hyphen
        # A pattern searches the same normalized text, with its own flags.
        ({"pattern": "Classic Hoodie.*\\bM\\b"}, "Classic Hoodie\n  Size: M", True),
        ({"pattern": "post comment"}, "Post Comment", False),
        ({"pattern": "(?i)^post comment$"}, "  Post\n Comment ", True),
        # Text that isn't ASCII, and a lone surrogate a page's string can hold,
        # reach the search as they are.
        ({"text": "café ✓"}, "Café ✓", True),
        ({"pattern": "\\ud800"}, "x\ud800y", True),
    ],
)
def test_searches_agree_with_has_text_and_has_pattern(
    fields: dict[str, Any], rendered: str, found: bool
) -> None:
    assert asyncio.run(text_matches(check(**fields), rendered)) is found


@pytest.mark.parametrize(
    ("pattern", "url", "found"),
    [
        ("/checkout/payment", "https://shop.test/checkout/payment?step=2", True),
        # The URL as it is: never decoded, normalized or casefolded.
        ("a%20b", "https://shop.test/a%20b", True),
        ("a b", "https://shop.test/a%20b", False),
        ("/Checkout", "https://shop.test/checkout", False),
    ],
)
def test_a_url_pattern_searches_the_url_as_it_is(
    pattern: str, url: str, found: bool
) -> None:
    assert asyncio.run(url_matches(pattern, url)) is found


def searches_running() -> list[int]:
    """The PIDs of this process's search processes still running."""
    listed = subprocess.run(
        ["pgrep", "-P", str(os.getpid()), "-f", "aqa_core.text"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(pid) for pid in listed.stdout.split()]


async def started_search() -> list[int]:
    """The PIDs of this process's search processes, once one is running."""
    for _ in range(500):
        if running := searches_running():
            return running
        await asyncio.sleep(0.01)
    pytest.fail("no search process started within 5 s")


def test_a_catastrophic_pattern_is_stopped_at_the_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(SearchTimeoutError):
        asyncio.run(text_matches(check(pattern=CATASTROPHIC), HOSTILE_TEXT))
    elapsed = time.monotonic() - started

    assert text_search.SEARCH_SECONDS <= elapsed < text_search.SEARCH_SECONDS + 1.5
    assert searches_running() == []


def test_the_event_loop_keeps_running_during_a_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(text_search, "SEARCH_SECONDS", 0.5)

    async def scenario() -> int:
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(tick())
        try:
            with pytest.raises(SearchTimeoutError):
                await url_matches(CATASTROPHIC, HOSTILE_TEXT)
        finally:
            ticker.cancel()
        return ticks

    # A search on the loop's own thread would leave it no tick until the end.
    assert asyncio.run(scenario()) >= 20


def test_a_cancelled_search_leaves_no_process_behind() -> None:
    async def scenario() -> None:
        search = asyncio.create_task(
            text_matches(check(pattern=CATASTROPHIC), HOSTILE_TEXT)
        )
        await started_search()
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search

    asyncio.run(scenario())

    assert searches_running() == []


def test_the_search_process_gets_none_of_the_runners_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in RUNNER_SECRETS:
        monkeypatch.setenv(name, fake_value(name))

    async def scenario() -> str:
        search = asyncio.create_task(url_matches(CATASTROPHIC, HOSTILE_TEXT))
        try:
            [pid] = await started_search()
            return environment_of(pid)
        finally:
            search.cancel()

    environment = asyncio.run(scenario())

    # The control that the reader sees an environment is in
    # test_browser_session.py.
    assert [name for name in RUNNER_SECRETS if fake_value(name) in environment] == []
