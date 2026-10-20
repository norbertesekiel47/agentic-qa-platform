"""Faults for `aqa` run as a child process (TESTING.md §1, Console-script
children). Python runs this module at startup when its directory is on the
child's PYTHONPATH, so the installed console script runs as users run it and
production code has no fault flag. AQA_CONSOLE_FAULT holds the test's
request as JSON: `events`, the file each step is noted in as it happens;
`plan`, the coverage plan the navigator's model answers the plan call with;
`fault`, a name in FAULTS; and `wait`, "plan" or "turn" for the call that
never answers, so the test can signal the child there. Python goes on
without a sitecustomize that raises, so the tests read the noted events,
never the exit status alone, to know a fault fired."""

import asyncio
import atexit
import errno
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from aqa_cli import explore
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.model_costs import Usage
from aqa_runner import sandbox
from aqa_runner.chat_client import Reply
from aqa_runner.loopback_server import LoopbackServer
from langchain_core.messages import AIMessage, BaseMessage
from playwright._impl._browser_type import BrowserType
from playwright.async_api import Error

REQUEST = json.loads(os.environ["AQA_CONSOLE_FAULT"])
# A fault's own words, which the command must never print: the advice of
# Playwright's launch error, and a sentinel for any other cause.
CAUSE = "fake-cause-4u49: relaunch with chromiumSandbox: false"  # ADR-0026
FAKE_KEY = "fake-key-for-tests"
USAGE = Usage(input_tokens=1000, cached_input_tokens=0, output_tokens=100)


def note(event: str) -> None:
    with Path(REQUEST["events"]).open("a") as events:
        events.write(f"{event}\n")


class Model:
    """Every model the router builds: the plan for the plan call, unless the
    test waits there, then a turn that waits only when asked to."""

    def __init__(self, *_: object) -> None:
        pass

    async def call(
        self, messages: Sequence[BaseMessage], tools: Sequence[Any], schema: Any
    ) -> Reply:
        del messages, tools
        call = "plan" if schema is not None else "turn"
        if call == "plan" and REQUEST["wait"] != "plan":
            note("planned")
            parsed = CoveragePlan.model_validate(REQUEST["plan"])
            message = AIMessage(content="")
            return Reply(message, USAGE, refused=False, complete=True, parsed=parsed)
        if REQUEST["wait"] != call:
            raise AssertionError(f"the test scripted no {call} call")
        note("waiting")
        await asyncio.Event().wait()
        raise AssertionError("an event nobody sets was set")


def raising(name: str, error: BaseException) -> Callable[..., Any]:
    def fault(*_: object, **__: object) -> Any:
        note(f"fired {name}")
        raise error

    return fault


def awaited(fault: Callable[..., Any]) -> Callable[..., Any]:
    async def run(*args: object, **kwargs: object) -> Any:
        return fault(*args, **kwargs)

    return run


def at_launch(name: str, error: BaseException) -> None:
    """Playwright's launch raises `error`: no browser starts."""
    mock.patch.object(BrowserType, "launch", awaited(raising(name, error))).start()


def at_check(name: str, error: BaseException) -> None:
    """The sandbox check raises `error`, once Chromium has launched."""
    mock.patch.object(sandbox, "check_processes", raising(name, error)).start()


def at_listener() -> None:
    """The egress proxy's listener can't open (EMFILE)."""
    error = OSError(errno.EMFILE, CAUSE)
    fault = awaited(raising("listener", error))
    mock.patch.object(LoopbackServer, "__aenter__", fault).start()


def environment() -> None:
    """What the child holds: its variables' names, and whether its provider
    key is the fake one, never a value."""
    note(f"environment {' '.join(sorted(os.environ))}")
    note(f"key fake: {os.environ.get('ANTHROPIC_API_KEY') == FAKE_KEY}")


SPAWN = asyncio.create_subprocess_exec


async def spawned(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    """Playwright's driver, as it is spawned, noted at exit with its exit
    status: None while the command never stopped it."""
    process = await SPAWN(*args, **kwargs)
    if "run-driver" in args:
        atexit.register(lambda: note(f"driver at exit: {process.returncode}"))
    return process


FAULTS: dict[str, Callable[[], None]] = {
    "none": lambda: None,
    "environment": environment,
    "SandboxUnavailableError": lambda: at_launch(
        "SandboxUnavailableError", Error(f"[pid=1][err] No usable sandbox! {CAUSE}")
    ),
    "Error": lambda: at_launch("Error", Error(CAUSE)),
    "KeyError": lambda: at_check("KeyError", KeyError(CAUSE)),
    "ValueError": lambda: at_check("ValueError", ValueError(CAUSE)),
    "OSError": lambda: at_check("OSError", OSError(errno.EIO, CAUSE)),
    "ProcessLookupError": lambda: at_check(
        "ProcessLookupError", ProcessLookupError(CAUSE)
    ),
    "listener": at_listener,
}

mock.patch.object(explore, "AnthropicClient", Model).start()
mock.patch.object(asyncio, "create_subprocess_exec", spawned).start()
FAULTS[REQUEST["fault"]]()
note(f"armed {REQUEST['fault']} by {Path(sys.argv[0]).name}")
