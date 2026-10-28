"""Searching page text and URLs for a compiled check, bounded in time and off
the event loop (DATA_MODEL §7, Text parameters; ADR-0024's 2026-10-02
amendment).

The text comes from the page under test and the pattern from a compiled
script, and Python's `re` can backtrack for exponential time on such a pair.
A search in progress can't be interrupted, not even from another thread, so
each one runs in a child Python process that is killed at the deadline. The
child runs fixed code in isolated mode with an empty environment, so it never
holds the runner's keys or test secrets, and reads the pattern and the text
from stdin."""

import asyncio
import contextlib
import json
import sys
from typing import Literal, cast

from aqa_core.compiled import TextInTarget, TextVisible
from aqa_core.text import (
    SEARCH_CHILD,
    SEARCH_SECONDS,
    SearchTimeoutError,
    search_found,
)

type _Kind = Literal["text", "pattern", "url", "urls"]


async def _search(kind: _Kind, needle: str, haystack: str | tuple[str, ...]) -> bool:
    """Whether `needle` is found in `haystack`, searched as `kind` says, in a
    child process killed at the deadline."""
    # ASCII only, with every other character escaped, so the bytes say the
    # same to the child whatever its locale, a lone surrogate included.
    payload = json.dumps([kind, needle, haystack]).encode("ascii")
    # https://docs.python.org/3.14/library/asyncio-subprocess.html
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        SEARCH_CHILD,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={},
    )
    try:
        async with asyncio.timeout(SEARCH_SECONDS):
            stdout, stderr = await child.communicate(payload)
    except TimeoutError:
        raise SearchTimeoutError(
            f"the search didn't finish within {SEARCH_SECONDS} s"
        ) from None
    finally:
        # Stopped on a timeout or a cancellation, never left running.
        if child.returncode is None:
            # The child may exit before it is killed; then there is no
            # process left to kill.
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            await child.wait()
    return search_found(child.returncode, stdout, stderr)


async def text_matches(check: TextVisible | TextInTarget, rendered: str) -> bool:
    """Whether an element's or the page's rendered text meets `check`: its
    literal as whole words, ignoring case, or its pattern found anywhere, both
    against the normalized text (DATA_MODEL §7). Raises `SearchTimeoutError`
    when the search runs out of time."""
    if check.text is not None:
        return await _search("text", check.text, rendered)
    # The format guarantees a pattern when there is no text.
    return await _search("pattern", cast(str, check.pattern), rendered)


async def pattern_matches(pattern: str, text: str) -> bool:
    """Whether the Python regex `pattern` is found in `text`, normalized as an
    element's rendered text is (DATA_MODEL §7). Raises `SearchTimeoutError`
    when the search runs out of time."""
    return await _search("pattern", pattern, text)


async def url_matches(pattern: str, url: str) -> bool:
    """Whether the Python regex `pattern` is found in `url`, the URL as it is
    (DATA_MODEL §7). Raises `SearchTimeoutError` when the search runs out of
    time."""
    return await _search("url", pattern, url)


async def any_url_matches(pattern: str, urls: tuple[str, ...]) -> bool:
    """Whether `pattern` matches any complete URL, without normalization.
    One child searches every candidate under one `SEARCH_SECONDS` bound.
    An empty candidate list starts no child. A timeout raises `SearchTimeoutError`."""
    if not urls:
        return False
    return await _search("urls", pattern, urls)
