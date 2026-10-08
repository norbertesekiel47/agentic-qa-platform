"""The reset hook's request (ADR-0024 and its #53 amendment; DATA_MODEL §6):
a POST to the run's start origin through its egress gate, as every
runner-side request goes (`aqa_runner.runner_requests`)."""

import asyncio

from aqa_core.project import path_on_origin
from aqa_core.spec import Reset

from aqa_runner.egress import EgressGate, EgressRefusedError, EgressUpstreamError
from aqa_runner.runner_requests import runner_request


class ResetFailedError(Exception):
    """The reset hook didn't succeed (API.md §7, exit 14). Its message is
    fixed text that names no origin, host, port or path: the gate keeps
    what it refused or couldn't reach."""


async def reset(gate: EgressGate, hook: Reset, start: str, *, seconds: float) -> None:
    """POST `hook` to `start`, the run's start origin, through `gate`. Only
    a whole 2xx response within `seconds` passes: a redirect isn't followed,
    and the body is read to its end but never kept. Otherwise raises
    `ResetFailedError`."""
    try:
        async with asyncio.timeout(seconds):
            response = await runner_request(
                gate, "POST", path_on_origin(start, hook.path), keep_body=False
            )
    except TimeoutError:
        raise ResetFailedError(
            f"the reset hook didn't answer within {seconds:g} s"
        ) from None
    except EgressRefusedError, EgressUpstreamError:
        raise ResetFailedError("the reset hook couldn't be reached") from None
    if not 200 <= response.status < 300:
        raise ResetFailedError(f"the reset hook answered {response.status}")
