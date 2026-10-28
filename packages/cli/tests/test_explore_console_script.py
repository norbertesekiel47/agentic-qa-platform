"""`aqa explore` as users run it: the installed console script in a child
process, with its faults and its model's plan installed by the test-only
`console_faults/sitecustomize.py` (TESTING.md §1, Console-script children).
Each test reads the child's exit status, its real stdout and stderr, and the
events that module noted, so a fault that never fired fails the test (#53
AC10; ADR-0024, ADR-0026)."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from aqa_runner.sandbox import HOST_FIXES, LINUX_FIX, OTHER_FIX

from packages.runner.tests.egress_fixtures import unused_port
from packages.runner.tests.executor_fixtures import serving_app
from packages.runner.tests.explore_graph_fixtures import PLAN

FAULTS = Path(__file__).parent / "console_faults"
KEY = "fake-key-for-tests"
# A start origin no case reaches: each stops before a page loads.
START = "http://127.0.0.1:9"
# Words of the faults' causes, never printed: Playwright's advice (ADR-0026).
CAUSE = ("chromiumSandbox: false", "fake-cause-4u49")  # ADR-0026
# The driver the command started, stopped and reaped before it exited.
STOPPED = "driver at exit: 0"
# A driver that can't be spawned: Playwright's node, at a path that isn't.
NO_NODE = "/nonexistent/fake-cause-4u49/node"
NO_DRIVER = "the browser driver couldn't start, so nothing was explored"
ACCOUNT = (
    "  account: { email: reader@example.test, password: { secret: TEST_DRIVER } }\n"
)
SECRETS = '{"secrets": {"TEST_DRIVER": {"origins": ["start"], "field": "password"}}}\n'


@dataclass(frozen=True)
class Child:
    """How a child `aqa` ended: its exit status, what it wrote to its real
    stdout and stderr, and what its faults module noted, in order."""

    returncode: int
    stdout: str
    stderr: str
    events: list[str]


def shop(root: Path, *, account: str = "", config: str = "{}\n") -> Path:
    """The shop spec (`explore_graph_fixtures.PLAN` covers it) under the
    spec root `root`, made."""
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(config)
    path = root / "shop.spec.md"
    path.write_text(
        "---\nid: shop\ngoal: A shopper saves their name.\n"
        f"preconditions:\n  start_url: /page/shop\n{account}"
        "expect:\n  - The shop says the name was saved\n---\n"
    )
    return path


def environment(
    tmp_path: Path, fault: str, *, wait: str | None = None, **more: str
) -> dict[str, str]:
    """The child's whole environment, built rather than inherited, so no
    provider key, cloud credential or test secret in this process's
    environment reaches it, and a model call goes to a loopback port nothing
    listens on. HOME stays, for Playwright's browser cache, and where
    Playwright's browsers are is passed on when this process says, so a
    real launch finds Chromium."""
    request = {
        "events": str(tmp_path / "events"),
        "plan": PLAN.model_dump(mode="json"),
        "fault": fault,
        "wait": wait,
    }
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    provider = f"http://127.0.0.1:{unused_port()}"
    return {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(FAULTS),
        "AQA_CONSOLE_FAULT": json.dumps(request),
        "ANTHROPIC_API_KEY": KEY,
        "ANTHROPIC_API_URL": provider,
        "ANTHROPIC_BASE_URL": provider,
        **({"PLAYWRIGHT_BROWSERS_PATH": browsers} if browsers else {}),
        **more,
    }


def events(tmp_path: Path) -> list[str]:
    noted = tmp_path / "events"
    return noted.read_text().splitlines() if noted.exists() else []


def explored(aqa: str, tmp_path: Path, spec: Path, environ: dict[str, str]) -> Child:
    done = subprocess.run(
        [aqa, "explore", str(spec), "--url", START],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environ,
    )
    return Child(done.returncode, done.stdout, done.stderr, events(tmp_path))


def faulted(aqa: str, tmp_path: Path, fault: str, **more: str) -> Child:
    """The shop spec explored by a child with `fault`, which must fire."""
    spec = shop(tmp_path / "qa")
    environ = environment(tmp_path, fault, **more)
    child = explored(aqa, tmp_path, spec, environ)
    if f"fired {fault}" not in child.events:
        raise AssertionError(
            f"the {fault} fault never fired: events {child.events}, "
            f"exit {child.returncode}"
        )
    return child


def record(tmp_path: Path) -> Path:
    [run] = (tmp_path / "qa" / ".aqa" / "runs").iterdir()
    return run


def no_sandbox(tmp_path: Path, why: str) -> str:
    """The stderr of a run its browser's launch stopped: its line, our
    message alone, and the plan's check it never bound."""
    run = re.escape(str(record(tmp_path)))
    return (
        rf"no_sandbox shop: \d+\.\ds, \$0\.003, run record: {run}\n"
        rf"  {re.escape(why)}\n  a1: never bound\n"
    )


FIX = HOST_FIXES.get(sys.platform, OTHER_FIX)
LAUNCHES = [
    (
        "SandboxUnavailableError",
        f"Chromium's sandbox can't start on this host. {LINUX_FIX}",
    ),
    *(
        (kind, f"Chromium could not be launched or its sandbox checked ({kind}). {FIX}")
        for kind in ("Error", "KeyError", "ValueError", "OSError", "ProcessLookupError")
    ),
]


@pytest.mark.parametrize(("fault", "why"), LAUNCHES, ids=[f for f, _ in LAUNCHES])
def test_each_launch_failure_through_the_console_script_exits_10_naming_the_host_fix(
    aqa: str, tmp_path: Path, fault: str, why: str
) -> None:
    child = faulted(aqa, tmp_path, fault)

    assert child.events == [
        f"armed {fault} by aqa",
        "planned",
        f"fired {fault}",
        STOPPED,
    ]
    assert child.returncode == 10, child.stderr
    assert child.stdout == ""
    assert re.fullmatch(no_sandbox(tmp_path, why), child.stderr), child.stderr
    for text in (child.stdout, child.stderr):
        assert "Traceback" not in text
        assert "direct cause" not in text
        assert not any(cause in text for cause in CAUSE)


def test_a_real_missing_browser_exits_10(aqa: str, tmp_path: Path) -> None:
    browsers = tmp_path / "browsers"
    browsers.mkdir()  # empty: no Chromium to launch
    spec = shop(tmp_path / "qa")
    environ = environment(tmp_path, "none", PLAYWRIGHT_BROWSERS_PATH=str(browsers))

    child = explored(aqa, tmp_path, spec, environ)

    assert child.events == ["armed none by aqa", "planned", STOPPED]
    assert child.returncode == 10, child.stderr
    assert child.stdout == ""
    why = f"Chromium could not be launched or its sandbox checked (Error). {FIX}"
    assert re.fullmatch(no_sandbox(tmp_path, why), child.stderr), child.stderr


class Unanswered(BaseHTTPRequestHandler):
    """Keeps each POST's request line and closes with no response."""

    seen: list[str]

    def do_POST(self) -> None:
        self.seen.append(self.requestline)
        self.close_connection = True


@contextmanager
def unanswering() -> Iterator[tuple[str, list[str]]]:
    """A loopback provider that never answers a POST: its URL, and each
    POST's request line."""
    seen: list[str] = []
    handler = type("Handler", (Unanswered,), {"seen": seen})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        serving = threading.Thread(target=server.serve_forever)
        serving.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", seen
        finally:
            server.shutdown()
            serving.join()


def test_the_harness_fails_closed_without_its_fault(aqa: str, tmp_path: Path) -> None:
    with unanswering() as (url, seen), pytest.raises(AssertionError) as raised:
        faulted(aqa, tmp_path, "KeyError", PYTHONPATH="", ANTHROPIC_API_URL=url)

    assert str(raised.value) == "the KeyError fault never fired: events [], exit 11"
    # The unscripted plan call reached the URL the test built: one try and
    # the SDK's two retries (anthropic_client.py).
    assert seen == ["POST /v1/messages HTTP/1.1"] * 3


def test_the_child_environment_holds_no_provider_key(
    aqa: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "fake-parent-value-4u49")
    spec = shop(tmp_path / "qa")
    environ = environment(tmp_path, "environment", PLAYWRIGHT_NODEJS_PATH=NO_NODE)

    child = explored(aqa, tmp_path, spec, environ)

    noted, key, armed = child.events
    names = set(noted.removeprefix("environment ").split())
    assert names.isdisjoint({"AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY"}), names
    # Python's C-locale coercion (PEP 538) and macOS add these two themselves.
    assert names - environ.keys() <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}, names
    assert (key, armed) == ("key fake: True", "armed environment by aqa")
    assert child.returncode == 13, child.stderr
    assert (child.stdout, child.stderr) == ("", f"unreachable shop: {NO_DRIVER}\n")


@pytest.mark.parametrize("case", ["driver", "listener", "withheld"])
def test_a_start_failure_through_the_console_script_exits_13_with_its_one_line(
    aqa: str, tmp_path: Path, case: str
) -> None:
    if case == "listener":
        child = faulted(aqa, tmp_path, "listener")
        assert child.events == [
            "armed listener by aqa",
            "planned",
            "fired listener",
            STOPPED,
        ]
        said = "the egress listener couldn't start, so nothing was explored"
    else:
        # TEST_DRIVER's value, `driver`, is in the driver's line, so the
        # fixed fallback is written instead.
        bound = case == "withheld"
        spec = shop(
            tmp_path / "qa",
            account=ACCOUNT if bound else "",
            config=SECRETS if bound else "{}\n",
        )
        more = {"AQA_SECRET_TEST_DRIVER": "driver"} if bound else {}
        environ = environment(tmp_path, "none", PLAYWRIGHT_NODEJS_PATH=NO_NODE, **more)
        child = explored(aqa, tmp_path, spec, environ)
        assert child.events == ["armed none by aqa"]
        said = (
            "the rest is withheld, since it would spell a bound value"
            if bound
            else NO_DRIVER
        )

    assert child.returncode == 13, child.stderr
    assert child.stdout == ""
    assert child.stderr == f"unreachable shop: {said}\n"


def test_a_node_options_path_holding_a_bound_value_exits_12_before_the_driver(
    aqa: str, tmp_path: Path
) -> None:
    # Node would print the preload it can't find, path and all, to the
    # stderr it shares with the CLI, where no scan reaches (#215).
    value = "fake-preload-4u215"
    spec = shop(tmp_path / "qa", account=ACCOUNT, config=SECRETS)
    environ = environment(
        tmp_path,
        "none",
        AQA_SECRET_TEST_DRIVER=value,
        NODE_OPTIONS=f"--require /nonexistent/{value}/x.js",
    )

    child = explored(aqa, tmp_path, spec, environ)

    # Refused before the driver started: it never ran, so never stopped.
    assert child.events == ["armed none by aqa"]
    assert child.returncode == 12, child.stderr
    assert child.stdout == ""
    assert child.stderr == (
        "secret_unusable shop: nothing was explored\n"
        "  a test secret's value is in the environment outside AQA_SECRET_* "
        "variables, where Playwright's driver could print it; remove it from "
        "these variables to run with test secrets: NODE_OPTIONS\n"
    )


# A terminal's Ctrl-C reaches the whole foreground process group, Playwright's
# driver included (`group`); `cli` signals the CLI's process alone. The child
# leads a session of its own, so the group is only the processes it started.
@pytest.mark.parametrize("to", ["cli", "group"])
@pytest.mark.parametrize("wait", ["plan", "turn"])
def test_a_sigint_while_exploring_exits_130_with_its_driver_stopped(
    aqa: str, tmp_path: Path, wait: str, to: str
) -> None:
    spec = shop(tmp_path / "qa")
    environ = environment(tmp_path, "none", wait=wait)
    with (
        serving_app() as app,
        subprocess.Popen(
            [aqa, "explore", str(spec), "--url", app.origin],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environ,
            start_new_session=True,
        ) as child,
    ):
        try:
            deadline = time.monotonic() + 60
            while events(tmp_path)[-1:] != ["waiting"]:
                assert child.poll() is None, child.communicate()
                assert time.monotonic() < deadline, "the call never started"
                time.sleep(0.05)
            if to == "group":
                os.killpg(child.pid, signal.SIGINT)
            else:
                child.send_signal(signal.SIGINT)
            stdout, stderr = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()  # only this recorded child, never by pattern

    assert child.returncode == 130, stderr
    assert (stdout, stderr) == ("", "")
    run = record(tmp_path)
    assert not (run / "explore.json").exists()
    # A driver that launched a browser closes it and exits 130 on a SIGINT
    # of its own (Playwright's handleSIGINT); with none yet, it ignores one.
    driver = "driver at exit: 130" if (to, wait) == ("group", "turn") else STOPPED
    if wait == "turn":
        assert events(tmp_path) == ["armed none by aqa", "planned", "waiting", driver]
        assert len((run / "costs.jsonl").read_text().splitlines()) == 1
    else:
        assert events(tmp_path) == ["armed none by aqa", "waiting", driver]
        assert not (run / "costs.jsonl").exists()
