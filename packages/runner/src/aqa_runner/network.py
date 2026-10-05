"""Network checks over the browser's own response metadata (#48, DATA_MODEL §7)."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from aqa_core.compiled import NetworkNone, NetworkSeen

from aqa_runner.text_search import any_url_matches

if TYPE_CHECKING:
    # Traffic calls `held`, so a runtime import would be a cycle.
    from aqa_runner.document_origins import Records
    from aqa_runner.settling import Exchange


class WindowsOverflowError(Exception):
    """Kept responses establish no match, and discarded responses may hide one."""


async def held(
    check: NetworkNone | NetworkSeen, windows: Sequence[Records[Exchange]]
) -> bool:
    """Whether browser responses, each settle window's in order, establish
    `check`, in one bounded URL search. A kept match decides the outcome.
    Without one, any window that discarded responses raises
    `WindowsOverflowError` rather than claiming absence."""
    urls = tuple(
        response.url
        for responses in windows
        for response in responses.kept
        if response.method == check.method
        and f"{response.status // 100}xx" == check.status_class
    )
    overflow = next(
        (
            (index, len(responses.kept), responses.total)
            for index, responses in enumerate(windows)
            if responses.total > len(responses.kept)
        ),
        None,
    )
    if await any_url_matches(check.url_pattern, urls):
        return check.check == "network_seen"
    if overflow is not None:
        index, kept, total = overflow
        raise WindowsOverflowError(
            f"settle window {index} kept {kept} of {total} responses"
        )
    return check.check == "network_none"
