"""Wait for a value to stop changing, for the toggle checks (ADR-0023).

Standard library only, so its tests run without Playwright; toggle_checks.py
imports it inside the checks image, where bench/harness is mounted.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class NotSettledError(Exception):
    pass


def settled(read: Callable[[], int], quiet: float = 1.0, timeout: float = 10.0) -> int:
    """``read()``'s value once it has stayed the same for ``quiet`` seconds."""
    deadline = time.monotonic() + timeout
    value, since = read(), time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(quiet / 4)
        current = read()
        if current != value:
            value, since = current, time.monotonic()
        elif time.monotonic() - since >= quiet:
            return value
    raise NotSettledError(f"still changing after {timeout} s (last {value})")
