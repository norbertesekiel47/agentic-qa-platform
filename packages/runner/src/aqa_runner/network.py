"""Network checks over the browser's own response metadata (#48, DATA_MODEL §7)."""

from collections.abc import Sequence

from aqa_core.compiled import NetworkNone, NetworkSeen

from aqa_runner.settling import Window
from aqa_runner.text_search import any_url_matches


class WindowsOverflowError(Exception):
    """Kept responses establish no match, and discarded responses may hide one."""


async def held(check: NetworkNone | NetworkSeen, windows: Sequence[Window]) -> bool:
    """Whether browser responses establish `check`, in one bounded URL search.
    A kept match decides the outcome. Without one, any window that discarded
    responses raises `WindowsOverflowError` rather than claiming absence."""
    urls = tuple(
        response.url
        for window in windows
        for response in window.responses.kept
        if response.method == check.method
        and f"{response.status // 100}xx" == check.status_class
    )
    overflow = next(
        (
            (index, len(window.responses.kept), window.responses.total)
            for index, window in enumerate(windows)
            if window.responses.total > len(window.responses.kept)
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
