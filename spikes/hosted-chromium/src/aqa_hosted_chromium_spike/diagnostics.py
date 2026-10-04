"""Bounded phase records for the spike, never request or exception content."""

import json
import sys
import time
from enum import StrEnum


class Stage(StrEnum):
    """The complete vocabulary accepted by the diagnostic sink."""

    HANDLER_RECEIVED = "handler_received"
    BODY_READ = "body_read"
    TRIAL_START = "trial_start"
    REPORT_READY = "report_ready"
    RESPONSE_SENT = "response_sent"
    PLAYWRIGHT_START = "playwright_start"
    PLAYWRIGHT_READY = "playwright_ready"
    LAUNCH_START = "launch_start"
    LAUNCH_READY = "launch_ready"
    CLOSE_START = "close_start"
    CLOSE_DONE = "close_done"
    LAUNCH_REFUSED = "launch_refused"
    MEMORY_START = "memory_start"
    MEMORY_DONE = "memory_done"


def emit_stage(stage: Stage) -> None:
    """Write one fixed-schema record to stderr and flush it before proceeding."""
    if not isinstance(stage, Stage):
        raise TypeError("diagnostic stage must be a Stage")
    record = {"stage": stage.value, "monotonic_seconds": time.monotonic()}
    sys.stderr.write(json.dumps(record) + "\n")
    sys.stderr.flush()
