"""The browser driver and the egress listener behind `aqa explore <spec>`
(API.md §7; ADR-0026's #53 P17 amendment): a driver that can't start, or a
listener that can't open, exits 13 with fixed words and no traceback; any
other failure goes on as it came; and a driver that started is stopped once,
however the command ends, a cancellation included. Playwright's real driver
where a test says so, with no browser; otherwise stand-ins for the driver
and the runner's explore. No provider."""

import asyncio
import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import typer.testing
from aqa_cli import explore as explore_module
from aqa_cli.main import app as aqa
from aqa_runner.egress_proxy import ListenerUnavailableError
from aqa_runner.explore import Explored
from aqa_runner.loopback_server import LoopbackServer
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from packages.cli.tests.test_explore_output import (
    SENTINEL,
    START,
    WITHHELD,
    Driver,
    Runner,
    ended,
    invoke,
    signed_in,
)
from packages.cli.tests.test_explore_runs import KEY, explore, record_in, shop
from packages.runner.tests.explore_graph_fixtures import Scripted
from packages.runner.tests.test_explore_preflight import planned

NO_DRIVER = (
    "unreachable shop: the browser driver couldn't start, so nothing was explored\n"
)
NO_LISTENER = (
    "unreachable shop: the egress listener couldn't start, so nothing was explored\n"
)
# Playwright 1.63's words for a driver that exits before its handshake,
# written out, so this file fails first when Playwright changes them.
CLOSED_EARLY = ("Connection closed while reading from the driver",)
REASON = "the model refused to write the plan"


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)


def asyncio_log(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What asyncio logged: an unread task's error, with its traceback."""
    return [
        record.getMessage() for record in caplog.records if record.name == "asyncio"
    ]


def exits_at_once(tmp_path: Path) -> str:
    """A stand-in for node that exits before the handshake, found with no
    PATH lookup."""
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 1\n")
    node.chmod(0o755)
    return str(node)


def test_a_driver_that_cannot_spawn_exits_13_with_no_os_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", f"/nonexistent/{SENTINEL}/node")
    spec = shop(tmp_path / "qa")

    result, runner = invoke(monkeypatch, spec, ended("gave_up", 3))

    assert (result.exit_code, result.stdout, result.stderr) == (13, "", NO_DRIVER)
    assert SENTINEL not in result.output
    assert "Traceback" not in result.output
    assert asyncio_log(caplog) == []
    assert runner.calls == []
    assert not (tmp_path / "qa" / ".aqa").exists()


# The real command in a child process, from START.
CHILD = """
import sys
from aqa_cli.main import app
app(prog_name="aqa", args=["explore", sys.argv[1], "--url", sys.argv[2]])
"""


@pytest.mark.parametrize("node", ["missing", "exits-at-once"])
def test_a_driver_that_cannot_start_prints_one_line_in_a_real_process(
    tmp_path: Path, node: str
) -> None:
    spec = shop(tmp_path / "qa")
    path = (
        f"/nonexistent/{SENTINEL}/node"
        if node == "missing"
        else exits_at_once(tmp_path)
    )
    environment = {
        "PATH": os.environ["PATH"],
        "ANTHROPIC_API_KEY": KEY,
        "PLAYWRIGHT_NODEJS_PATH": path,
    }

    child = subprocess.run(
        [sys.executable, "-I", "-B", "-c", CHILD, str(spec), START],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert (child.returncode, child.stdout, child.stderr) == (13, "", NO_DRIVER)


def test_a_driver_whose_handshake_fails_exits_13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = Driver(starts=PlaywrightError(SENTINEL))

    result, runner = invoke(
        monkeypatch, shop(tmp_path / "qa"), ended("gave_up", 3), failing
    )

    assert (result.exit_code, result.stdout, result.stderr) == (13, "", NO_DRIVER)
    assert runner.calls == []


def test_an_egress_listener_that_cannot_open_exits_13_with_no_os_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(_: LoopbackServer) -> LoopbackServer:
        raise OSError(errno.EMFILE, SENTINEL)

    monkeypatch.setattr(LoopbackServer, "__aenter__", refuse)
    root = tmp_path / "qa"

    result = explore(
        monkeypatch,
        [str(shop(root)), "--url", START],
        Scripted(planned()),
        browser=False,
    )

    assert (result.exit_code, result.stdout, result.stderr) == (13, "", NO_LISTENER)
    assert SENTINEL not in result.output
    record = record_in(root)
    assert '"outcome": "planned"' in (record / "plan.json").read_text()
    assert len((record / "costs.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize(
    "failure",
    [
        OSError(errno.EIO, SENTINEL),
        TimeoutError(),
        FileNotFoundError(errno.ENOENT, SENTINEL),
    ],
    ids=["publication-random-source", "timeout", "file-not-found"],
)
def test_an_os_error_from_another_seam_is_not_called_the_listeners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    driver = Driver()

    result, _ = invoke(monkeypatch, shop(tmp_path / "qa"), failure, driver)

    assert result.exit_code == 1
    assert result.exception is failure
    assert "egress listener" not in result.output
    assert driver.stopped == 1


def test_the_listeners_own_error_is_13_with_no_os_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = Driver()
    failed = ListenerUnavailableError()
    failed.__cause__ = OSError(errno.EMFILE, SENTINEL)

    result, _ = invoke(monkeypatch, shop(tmp_path / "qa"), failed, driver)

    assert (result.exit_code, result.stdout, result.stderr) == (13, "", NO_LISTENER)
    assert SENTINEL not in result.output
    assert driver.stopped == 1


def test_a_driver_that_fails_to_stop_leaves_the_run_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = Driver(stops=PlaywrightError(SENTINEL))
    done = ended("compiled", 0, cost="0.015", published="confirmed")

    result, _ = invoke(monkeypatch, shop(tmp_path / "qa"), done, driver)

    assert result.exit_code == 0
    assert result.stdout.startswith("compiled shop: ")
    assert SENTINEL not in result.output
    assert driver.stopped == 1


def test_a_driver_start_refusal_with_a_marker_unsafe_binding_prints_only_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # TEST_DRIVER's markers spell its value, `driver`, as the diagnostic does.
    monkeypatch.setenv("AQA_SECRET_TEST_DRIVER", "driver")
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", f"/nonexistent/{SENTINEL}/node")
    spec = signed_in(tmp_path / "qa", name="TEST_DRIVER")

    result, runner = invoke(monkeypatch, spec, ended("gave_up", 3))

    assert (result.exit_code, result.stdout, result.stderr) == (
        13,
        "",
        WITHHELD.format("unreachable", "shop"),
    )
    assert "driver" not in result.output.lower()
    assert SENTINEL not in result.output
    assert asyncio_log(caplog) == []
    assert runner.calls == []


def test_a_fallback_that_spells_a_bound_value_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_SHOP", "shop")
    spec = signed_in(tmp_path / "qa", name="TEST_SHOP")
    failing = Driver(starts=PlaywrightError(SENTINEL))

    result, _ = invoke(monkeypatch, spec, ended("gave_up", 3), failing)

    assert (result.exit_code, result.stdout, result.stderr) == (13, "", "")


@pytest.mark.parametrize("node", ["false", "script"])
def test_a_driver_that_exits_before_its_handshake_exits_13(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    node: str,
) -> None:
    path = shutil.which("false") if node == "false" else exits_at_once(tmp_path)
    assert path is not None
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", path)

    result, runner = invoke(monkeypatch, shop(tmp_path / "qa"), ended("gave_up", 3))

    assert (result.exit_code, result.stdout, result.stderr) == (13, "", NO_DRIVER)
    assert "Traceback" not in result.output
    assert asyncio_log(caplog) == []
    assert runner.calls == []


def test_playwrights_early_exit_words_are_the_ones_recognized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", exits_at_once(tmp_path))

    async def started() -> BaseException:
        with pytest.raises(Exception, match=r"^Connection closed") as raised:
            await async_playwright().start()
        return raised.value

    error = asyncio.run(started())

    assert type(error) is Exception
    assert error.args == CLOSED_EARLY


class EarlyLookalikeError(Exception):
    pass


@pytest.mark.parametrize(
    "failure",
    [
        Exception("something else broke"),
        Exception(*CLOSED_EARLY, "and more"),
        EarlyLookalikeError(*CLOSED_EARLY),
        RuntimeError(*CLOSED_EARLY),
    ],
    ids=["other-words", "more-args", "subclass", "runtime-error"],
)
def test_a_driver_start_failure_not_recognized_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    result, runner = invoke(
        monkeypatch, shop(tmp_path / "qa"), ended("gave_up", 3), Driver(starts=failure)
    )

    assert result.exit_code == 1
    assert result.exception is failure
    assert runner.calls == []


class Reporting(Driver):
    """A start that reports to the loop's handler while it runs: an
    unrelated error and, when it fails, its own."""

    def __init__(self, starts: BaseException | None = None) -> None:
        super().__init__(starts=starts)
        self.other = ValueError("an unrelated report")

    async def start(self) -> Driver:
        loop = asyncio.get_running_loop()
        loop.call_exception_handler(
            {"message": "unrelated report", "exception": self.other}
        )
        if self.starts is not None:
            loop.call_exception_handler(
                {"message": "its own report", "exception": self.starts}
            )
        return await super().start()


class Seeing(Runner):
    """The runner's explore, seeing the loop's exception handler."""

    def __init__(self, explored: Explored) -> None:
        super().__init__(explored)
        self.handlers: list[object] = []

    async def __call__(
        self, _spec: Any, config: Any, *args: Any, **kwargs: Any
    ) -> Explored:
        self.handlers.append(asyncio.get_running_loop().get_exception_handler())
        return await super().__call__(_spec, config, *args, **kwargs)


@pytest.mark.parametrize(
    "starts", [None, PlaywrightError(SENTINEL)], ids=["started", "failed"]
)
def test_reports_made_while_the_driver_starts_are_passed_on_and_the_handler_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    starts: BaseException | None,
) -> None:
    seeing = Seeing(ended("gave_up", 3))
    monkeypatch.setattr(explore_module, "explore_spec", seeing)
    monkeypatch.setattr(explore_module, "async_playwright", Reporting(starts))

    result = typer.testing.CliRunner().invoke(
        aqa, ["explore", str(shop(tmp_path / "qa")), "--url", START]
    )

    assert asyncio_log(caplog) == ["unrelated report"]
    if starts is None:
        assert result.exit_code == 3
        assert seeing.handlers == [None]
    else:
        assert (result.exit_code, result.stderr) == (13, NO_DRIVER)
        assert seeing.handlers == []


class Cancelling(Driver):
    """A start that cancels the task running the command: while still
    pending (`pending`; `returned-anyway` swallows the cancellation and
    returns a driver), or after it returned one (`finished-first`)."""

    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.seen: dict[str, bool] = {}
        self.task: asyncio.Task[Any] | None = None

    def __call__(self) -> Cancelling:
        self.owner = asyncio.current_task()
        return self

    async def start(self) -> Driver:
        self.task = asyncio.current_task()
        self.loop = asyncio.get_running_loop()
        assert self.owner is not None
        if self.mode == "finished-first":
            await asyncio.sleep(0)
            self.loop.call_soon(self.owner.cancel)
            return self
        self.loop.call_soon(self.owner.cancel)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.seen["owner_done_when_cancelled"] = self.owner.done()
            if self.mode == "pending":
                raise
        return self


@pytest.mark.parametrize("mode", ["pending", "finished-first", "returned-anyway"])
def test_a_cancellation_while_the_driver_starts_leaves_no_driver_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    driver = Cancelling(mode)
    runner = Runner(ended("gave_up", 3))
    monkeypatch.setattr(explore_module, "explore_spec", runner)
    monkeypatch.setattr(explore_module, "async_playwright", driver)

    with pytest.raises(asyncio.CancelledError):
        typer.testing.CliRunner().invoke(
            aqa, ["explore", str(shop(tmp_path / "qa")), "--url", START]
        )

    assert runner.calls == []
    assert driver.stopped == (0 if mode == "pending" else 1)
    assert driver.task is not None
    assert driver.task.done()
    assert driver.task.cancelled() is (mode == "pending")
    assert driver.loop.get_exception_handler() is None
    assert asyncio_log(caplog) == []
    if mode != "finished-first":
        # The start saw the cancellation while the command's task still
        # ran: gather passed it on, not asyncio.run's shutdown.
        assert driver.seen == {"owner_done_when_cancelled": False}


class Owning(Driver):
    """A start whose driver is a resource once returned: `immediate` returns
    it at its first step; `swallows` waits, swallows a cancellation and
    returns it anyway; `fails` waits and fails when cancelled."""

    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.ran = False
        self.opened = 0

    async def start(self) -> Driver:
        self.ran = True
        if self.mode != "immediate":
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                if self.mode == "fails":
                    raise PlaywrightError(SENTINEL) from None
        self.opened += 1
        return self


@pytest.mark.parametrize("when", ["before-it-ran", "after-a-step"])
@pytest.mark.parametrize("mode", ["immediate", "swallows", "fails"])
def test_a_start_the_command_never_read_leaves_no_driver_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    when: str,
) -> None:
    # The driver's reader fails before it reads the start: at once, before
    # the start's task ran, or after one step, once it ran.
    failed = RuntimeError(SENTINEL)
    tasks: list[asyncio.Task[Any]] = []

    async def failing(starting: asyncio.Task[Any]) -> Any:
        tasks.append(starting)
        if when == "after-a-step":
            await asyncio.sleep(0)
        raise failed

    monkeypatch.setattr(explore_module, "_driver", failing)
    driver = Owning(mode)

    result, runner = invoke(
        monkeypatch, shop(tmp_path / "qa"), ended("gave_up", 3), driver
    )

    ran = when == "after-a-step"
    produced = int(ran and mode != "fails")
    assert result.exception is failed
    assert runner.calls == []
    assert (driver.ran, driver.opened, driver.stopped) == (ran, produced, produced)
    [task] = tasks
    assert task.done()
    assert task.cancelled() is (not ran)
    assert asyncio_log(caplog) == []


class CancelledAsItEnds(Runner):
    """The runner's explore queueing the first cancellation of the task
    running the command (`loop.call_soon(owner.cancel)`) as it returns its
    result or raises its error, keeping what that `cancel()` returned."""

    def __init__(self, explored: Explored | BaseException) -> None:
        super().__init__(explored)
        self.cancel_took: list[bool] = []

    async def __call__(
        self, _spec: Any, config: Any, *args: Any, **kwargs: Any
    ) -> Explored:
        owner = asyncio.current_task()
        assert owner is not None
        asyncio.get_running_loop().call_soon(
            lambda: self.cancel_took.append(owner.cancel())
        )
        return await super().__call__(_spec, config, *args, **kwargs)


class Started(Driver):
    """A start that returns its driver at its first step, keeping its task;
    each stop completes at once."""

    def __init__(self) -> None:
        super().__init__()
        self.opened = 0
        self.task: asyncio.Task[Any] | None = None

    async def start(self) -> Driver:
        self.task = asyncio.current_task()
        self.opened += 1
        return self


@pytest.mark.parametrize("ending", ["returns", "raises"])
def test_a_cancellation_queued_as_the_runner_ends_keeps_its_ending_and_stops_the_driver_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ending: str,
) -> None:
    failed = RuntimeError(SENTINEL)
    gave_up = ended("gave_up", 3, reasons=[REASON])
    runner = CancelledAsItEnds(failed if ending == "raises" else gave_up)
    driver = Started()
    monkeypatch.setattr(explore_module, "explore_spec", runner)
    monkeypatch.setattr(explore_module, "async_playwright", driver)

    result = typer.testing.CliRunner().invoke(
        aqa, ["explore", str(shop(tmp_path / "qa")), "--url", START]
    )

    if ending == "returns":
        assert (result.exit_code, result.stdout) == (3, "")
        assert result.stderr.endswith(f", $0.003, run record: /runs/r1\n  {REASON}\n")
        assert result.stderr.startswith("gave_up shop: ")
    else:
        assert result.exit_code == 1
        assert result.exception is failed
    assert len(runner.calls) == 1
    # The queued cancellation was delivered, to a task already done.
    assert runner.cancel_took == [False]
    assert (driver.opened, driver.stopped) == (1, 1)
    assert driver.task is not None
    assert driver.task.done()
    assert not driver.task.cancelled()
    assert asyncio_log(caplog) == []
