import asyncio
import inspect
import json
import os
import pkgutil
import socket
import tempfile
import threading
import unittest
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
from aqa_core.compiled import ByRole, Locator, Press
from aqa_core.project import contracts_fingerprint, load_project
from aqa_runner import browser_session
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.egress import EgressGate, EgressPolicy
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.invariants import Observers
from aqa_runner.locator_generation import Seen, generate_for_action
from aqa_runner.model_router import ModelRouter
from aqa_runner.run_record import RunRecord
from langchain_anthropic import ChatAnthropic
from pilot_inputs import PilotInput, load_pilots
from pilot_replay import (
    AttemptHealth,
    FailedAttempt,
    ObservedAttempt,
    replay_pilot,
)
from playwright.async_api import Page, async_playwright

PAGE = "<!doctype html><h1>Title</h1><form method=post action=/submit><button>Send</button></form>"
COMPILED = """{
"schema_version":1,"spec_id":"pilot","spec_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000",
"compiled_at":"2026-10-01T00:00:00Z","compiled_by":{"mode":"explore","models":{},"price_map":"handwritten-replay-fixture","subject_contracts":"sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"},"confirmed":false,
"browser":{"timezone":"UTC","locale":"en-US","viewport":[1280,800],"device_scale_factor":1,"color_scheme":"light"},
"coverage":{"plan_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000","requires":[],
"expectations":[{"expect_index":0,"subject":"page","claim":"title","assertions":["a0"]}]},
"targets":{"send":{"semantic":"send button","locators":[{"role":"button","name":"Send"}]}},
"probe_baselines":{},"steps":[],"assertions":[{"id":"a0","expect_index":0,"check":"text_visible","text":"Title"}]}
"""
SEND = json.loads(
    '{"seq":1,"action":"click","target":"send","side_effect":true,"side_effect_basis":"posts"}'
)
PRESS = Press(seq=1, action="press", key="a+b", side_effect=False)
SECRET = {"AQA_SECRET_TEST_PASSWORD": "fake-password-value"}
ACCOUNT = {"password": {"secret": "TEST_PASSWORD"}}
FAILED = {"cleanup": "failed"}
BINDING = {"TEST_PASSWORD": {"origins": ["start"], "field": "password"}}
OUTCOME = json.loads(
    '{"outcome":"failed","failure":null,"interrupted":false,"reset_completed":false,"cleanup":"completed","resources":"unknown"}'
)
HOLD_RECEIPTS = ("cleanup-incomplete.json", "attempt.json")
CLIENTS: list[type] = [
    ModelRouter,
    AnthropicClient,
    ChatAnthropic,
    anthropic.Anthropic,
    anthropic.AsyncAnthropic,
]


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.log: list[str] = []
        self.reset = 200
        self.image = "http://blocked.test/x.png"
        self.release = threading.Event()


class Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:
        image = f"<img src={self.server.image}>" if self.path == "/image" else ""
        self.answer(200, PAGE + image)

    def do_POST(self) -> None:
        status = self.server.reset if self.route == "/reset" else 200
        if status == 0:
            self.server.log.append(f"POST {self.route}")
            self.server.release.wait(10)
            return
        self.answer(status, PAGE)

    @property
    def route(self) -> str:
        return self.path.partition("?")[0]

    def answer(self, status: int, page: str) -> None:
        if self.route != "/favicon.ico":
            self.server.log.append(f"{self.command} {self.route}")
        body = page.encode()
        self.send_response(status)
        self.send_header("Location", "/")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        """The server's own log is `Server.log`."""


def closed_origin() -> str:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{unused.getsockname()[1]}"


class PilotReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.server = Server()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.release.set)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.config: dict[str, Any] = {"base_url": self.origin}
        reset = {"http": "POST /reset"}
        conditions = {"start_url": "/", "reset": reset}
        self.spec: dict[str, Any] = {"id": "pilot", "goal": "Read"}
        self.spec["preconditions"] = conditions
        self.spec["expect"] = ["Title is visible"]
        self.steps: list[dict[str, object]] = []
        self.enterContext(patch.dict(os.environ))
        for name in ("DEBUG", "DEBUGP", *SECRET):
            os.environ.pop(name, None)

    def pilot(self) -> PilotInput:
        qa, compiled = self.root / "qa", self.root / "compiled"
        qa.mkdir(exist_ok=True)
        compiled.mkdir(exist_ok=True)
        (qa / "config.yaml").write_text(json.dumps(self.config))
        (qa / "pilot.spec.md").write_text(f"---\n{json.dumps(self.spec)}\n---\n")
        project = load_project(qa)
        fresh = {"spec_hash": project.specs["pilot"].spec_hash}
        script = json.loads(COMPILED) | fresh | {"steps": self.steps}
        script["compiled_by"]["subject_contracts"] = contracts_fingerprint(
            project.config, "pilot"
        )
        (compiled / "pilot.json").write_text(json.dumps(script))
        return load_pilots(qa, compiled, ("pilot",))[0]

    def replay(self, pilot: PilotInput) -> ObservedAttempt | FailedAttempt:
        async def bounded() -> ObservedAttempt | FailedAttempt:
            async with asyncio.timeout(60):
                return await replay_pilot(pilot, self.root)

        return asyncio.run(bounded())

    def observed(self, attempt: ObservedAttempt | FailedAttempt) -> ObservedAttempt:
        if not isinstance(attempt, ObservedAttempt):
            self.fail(f"not observed: {attempt}")
        return attempt

    def failed(self, attempt: ObservedAttempt | FailedAttempt) -> tuple[str, ...]:
        if not isinstance(attempt, FailedAttempt):
            self.fail(f"not failed: {attempt}")
        return attempt.failure, attempt.cleanup, attempt.resources

    def receipt(self, name: str) -> object:
        latest = max(self.root.glob(".aqa/runs/*"))
        return json.loads((latest / name).read_text())

    def fake_browser(self, held: bool = False) -> tuple[dict[str, int], AsyncMock]:
        release = self.release = asyncio.Event()
        if not held:
            release.set()
        calls = {"close": 0, "close_cancelled": 0}
        self.entered, self.closing = asyncio.Event(), asyncio.Event()
        self.close_error: Exception | None = None

        async def close() -> None:
            calls["close"] += 1
            self.closing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                calls["close_cancelled"] += 1
                raise
            if self.close_error is not None:
                raise self.close_error

        async def goto(*_: object) -> None:
            self.entered.set()
            await asyncio.Event().wait()

        @asynccontextmanager
        async def driver() -> AsyncIterator[MagicMock]:
            yield MagicMock(selectors=MagicMock(register=AsyncMock()))

        page = MagicMock(goto=goto)
        context = MagicMock(add_init_script=AsyncMock(), new_page=AsyncMock())
        context.new_page.return_value = page
        browser = MagicMock(new_context=AsyncMock(return_value=context), close=close)
        self.enterContext(patch("pilot_replay.async_playwright", driver))
        self.enterContext(patch.object(browser_session, "install_routes", AsyncMock()))
        watch = AsyncMock(return_value=MagicMock())
        self.enterContext(patch.object(Observers, "watch", watch))
        launch = AsyncMock(return_value=browser)
        self.enterContext(patch.object(browser_session, "launch", launch))
        return calls, launch

    def fail_proxy_exits(self) -> None:
        original = EgressProxy.__aexit__

        async def failing(proxy: EgressProxy, *args: Any) -> None:
            await original(proxy, *args)
            raise RuntimeError("fake proxy exit failure")

        self.enterContext(patch.object(EgressProxy, "__aexit__", failing))

    async def interrupt(
        self, pilot: PilotInput
    ) -> asyncio.Task[ObservedAttempt | FailedAttempt]:
        attempt = asyncio.create_task(replay_pilot(pilot, self.root))
        await self.entered.wait()
        attempt.cancel()
        return attempt

    def engine_probe(self) -> list[Locator]:
        original = Observers.watch
        generated: list[Locator] = []

        async def watch(page: Page, policy: EgressPolicy) -> Observers:
            await page.set_content(PAGE)
            button = await page.get_by_role("button", name="Send").element_handle()
            if button is None:
                self.fail("the disposable Send button is missing")
            try:
                locators = await generate_for_action(
                    page, Seen(button, role="button", name="Send")
                )
                generated.append(locators[0])
            finally:
                await button.dispose()
            return await original(page, policy)

        self.enterContext(patch.object(Observers, "watch", watch))
        return generated

    def test_pilot_adapter_registers_engines_before_its_first_context(self) -> None:
        generated = self.engine_probe()
        pilot = self.pilot()
        first, second = (self.observed(self.replay(pilot)) for _ in range(2))
        self.assertEqual(generated, [ByRole(role="button", name="Send")] * 2)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(
            (first.observation.outcome, second.observation.outcome),
            ("passed", "passed"),
        )
        self.assertEqual(self.server.log, ["POST /reset", "GET /"] * 2)

    def test_pilot_engine_probe_refuses_missing_registration(self) -> None:
        self.engine_probe()
        with (
            patch("pilot_replay.register_identity_engine", AsyncMock()),
            self.assertRaisesRegex(RuntimeError, "register_identity_engine"),
        ):
            self.replay(self.pilot())
        self.assertEqual(self.server.log, ["POST /reset"])

    def test_registration_failure_preserves_its_cause_and_closes_owned_resources(
        self,
    ) -> None:
        fault = RuntimeError("fake registration failure")
        launch = AsyncMock(side_effect=AssertionError("a browser was launched"))
        pilot = self.pilot()
        self.enterContext(patch.object(browser_session, "launch", launch))
        self.enterContext(
            patch("pilot_replay.register_identity_engine", AsyncMock(side_effect=fault))
        )
        for failed, cleanup, resources in (
            (False, "completed", "closed"),
            (True, "failed", "unknown"),
        ):
            with self.subTest(cleanup=cleanup):
                if failed:
                    self.fail_proxy_exits()
                with self.assertRaises(RuntimeError) as raised:
                    self.replay(pilot)
                self.assertIs(raised.exception, fault)
                expected = {
                    "outcome": "unexpected",
                    "reset_completed": True,
                    "cleanup": cleanup,
                    "resources": resources,
                }
                self.assertEqual(self.receipt("attempt.json"), OUTCOME | expected)
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(self.server.log, ["POST /reset"] * 2)

    def test_reset_runs_on_the_attempt_gate_before_the_first_navigation(self) -> None:
        self.config["base_url"] = f"http://pilot.test:{self.server.server_address[1]}"
        answers = iter(("127.0.0.1", "127.0.0.2"))

        async def resolve(_: str) -> list[IPv4Address | IPv6Address]:
            return [ip_address(next(answers))]

        gate = partial(EgressGate, resolve=resolve)
        self.enterContext(patch("pilot_replay.EgressGate", gate))
        attempt = self.observed(self.replay(self.pilot()))
        self.assertEqual(self.server.log, ["POST /reset", "GET /"])
        self.assertEqual(attempt.observation.outcome, "passed")
        self.assertEqual(attempt.health, AttemptHealth(True, False, False))

    def test_each_attempt_has_fresh_resources_and_its_own_reset(self) -> None:
        self.steps = [SEND]
        pilot = self.pilot()
        gates = self.enterContext(patch("pilot_replay.EgressGate", wraps=EgressGate))
        proxies = self.enterContext(
            patch("pilot_replay.EgressProxy", wraps=EgressProxy)
        )
        first, second = (self.observed(self.replay(pilot)) for _ in range(2))
        self.assertNotEqual(
            (first.run_id, first.record), (second.run_id, second.record)
        )
        self.assertEqual((gates.call_count, proxies.call_count), (2, 2))
        self.assertEqual(self.server.log, ["POST /reset", "GET /", "POST /submit"] * 2)
        for attempt in (first, second):
            start = json.loads((attempt.record / "attempt-start.json").read_text())
            self.assertEqual(start, {"spec_id": "pilot"})
            self.assertEqual(attempt.observation.outcome, "passed")
        malformed = patch("pilot_replay.observe", side_effect=ValueError("fake"))
        with malformed, self.assertRaisesRegex(ValueError, "fake"):
            self.replay(pilot)
        self.assertFalse((max(self.root.glob(".aqa/runs/*")) / "attempt.json").exists())
        observed = {
            "outcome": "observed",
            "reset_completed": True,
            "resources": "closed",
        }
        receipt = json.loads((second.record / "attempt.json").read_text())
        self.assertEqual(receipt, OUTCOME | observed)

    def test_a_failed_or_hanging_reset_dispatches_nothing(self) -> None:
        self.config["budgets"] = {"resolve_seconds": 0.3}
        self.spec["preconditions"]["reset"] = {"http": "POST /reset?fake-sensitive"}
        self.steps = [SEND]
        cases: tuple[tuple[int, str, str, list[str]], ...] = (
            (300, self.origin, "reset_rejected", ["POST /reset"]),
            (500, self.origin, "reset_rejected", ["POST /reset"]),
            (0, self.origin, "reset_timeout", ["POST /reset"]),
            (200, closed_origin(), "reset_unreachable", []),
        )
        for status, origin, failure, log in cases:
            with self.subTest(failure=failure, status=status):
                self.server.reset, self.config["base_url"] = status, origin
                self.server.log.clear()
                attempt = self.replay(self.pilot())
                self.assertEqual(self.failed(attempt), (failure, "completed", "closed"))
                self.assertEqual(self.server.log, log)
                self.assertFalse((attempt.record / "steps.jsonl").exists())
                receipts = (path.read_text() for path in attempt.record.glob("*"))
                self.assertNotIn("fake-sensitive", repr(attempt) + "".join(receipts))

    def test_a_replay_refusal_starts_no_browser(self) -> None:
        self.spec["preconditions"]["account"] = ACCOUNT
        self.config["secrets"] = BINDING
        with patch.dict(os.environ, SECRET):
            pilot = self.pilot()
        refused = AsyncMock(side_effect=AssertionError("a browser was launched"))
        self.enterContext(patch.object(browser_session, "launch", refused))
        surrogate = {"AQA_SECRET_TEST_PASSWORD": "fake-\udcff"}
        for env in ({}, SECRET | {"DEBUGP": ""}, surrogate):
            with self.subTest(env=env), patch.dict(os.environ, env):
                attempt = self.replay(pilot)
                closed = ("secret_unusable", "completed", "closed")
                self.assertEqual(self.failed(attempt), closed)
        script = pilot.script.model_copy(update={"steps": (PRESS,)})
        with patch.dict(os.environ, SECRET):
            attempt = self.replay(replace(pilot, script=script))
        self.assertEqual(
            self.failed(attempt), ("script_refused", "completed", "closed")
        )

        async def changing(*_: object) -> None:
            os.environ.update(surrogate)

        with patch.dict(os.environ, SECRET), patch("pilot_replay._reset", changing):
            attempt = self.replay(pilot)
        self.assertEqual(self.failed(attempt), closed)
        self.assertEqual(self.server.log, ["POST /reset"])
        self.assertEqual(refused.call_count, 0)

    def test_a_replay_builds_no_model_client_and_reads_no_manifest(self) -> None:
        built: list[str] = []

        def refuse(name: str) -> Callable[..., None]:
            def constructor(*_: object, **__: object) -> None:
                built.append(name)
                raise RuntimeError(name)

            return constructor

        for client in CLIENTS:
            self.enterContext(patch.object(client, "__init__", refuse(client.__name__)))
        self.enterContext(patch("manifest.load", refuse("manifest.load")))
        for call in (*CLIENTS, pkgutil.resolve_name("manifest.load")):
            with self.assertRaises(RuntimeError):
                call()
        self.assertEqual(len(built), 6)
        built.clear()
        parameters = tuple(inspect.signature(replay_pilot).parameters)
        self.assertEqual(parameters, ("pilot", "record_root"))
        attempt = self.observed(self.replay(self.pilot()))
        self.assertEqual((attempt.observation.outcome, built), ("passed", []))

    def test_health_tells_egress_blocks_from_infrastructure(self) -> None:
        del self.spec["preconditions"]["reset"]
        self.spec["preconditions"]["start_url"] = "/image"
        unreachable = closed_origin()
        cases = (
            ("http://blocked.test/x.png", AttemptHealth(False, False, True)),
            (f"{unreachable}/x.png", AttemptHealth(False, True, False)),
        )
        self.spec["allowed_origins"] = [unreachable]
        self.config["egress"] = {"private_origins": [unreachable]}
        for image, health in cases:
            with self.subTest(image=image):
                self.server.image = image
                attempt = self.observed(self.replay(self.pilot()))
                self.assertEqual(attempt.health, health)
                self.assertEqual(attempt.observation.outcome, "errored")

    def test_the_operation_limit_cancels_once_and_keeps_its_cause(self) -> None:
        del self.spec["preconditions"]["reset"]
        self.config["budgets"] = {"minutes": 0.005}
        calls, _ = self.fake_browser()
        attempt = self.replay(self.pilot())
        self.assertEqual(
            self.failed(attempt), ("operation_timeout", "completed", "unknown")
        )
        self.assertEqual(calls, {"close": 1, "close_cancelled": 0})
        receipt = self.receipt("attempt.json")
        self.assertEqual(receipt, OUTCOME | {"failure": "operation_timeout"})
        self.close_error = OSError("fake close failure")
        attempt = self.replay(self.pilot())
        self.assertEqual(
            self.failed(attempt), ("operation_timeout", "failed", "unknown")
        )
        self.close_error = None
        fault = OSError("fake evidence failure")
        self.enterContext(
            patch.object(Observers, "watch", AsyncMock(side_effect=fault))
        )
        original = EgressProxy.__aexit__

        async def stalled(proxy: EgressProxy, *args: Any) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                await original(proxy, *args)

        with (
            patch.object(EgressProxy, "__aexit__", stalled),
            self.assertRaisesRegex(OSError, "fake evidence failure") as raised,
        ):
            self.replay(self.pilot())
        self.assertIs(raised.exception, fault)
        self.assertEqual(calls, {"close": 3, "close_cancelled": 0})
        unexpected = {"outcome": "unexpected"}
        self.assertEqual(self.receipt("attempt.json"), OUTCOME | unexpected | FAILED)

    def test_hung_cleanup_writes_its_receipt_and_holds_the_attempt(self) -> None:
        del self.spec["preconditions"]["reset"]
        self.config["budgets"] = {"minutes": 0.005, "resolve_seconds": 0.1}
        calls, _ = self.fake_browser(held=True)
        pilot = self.pilot()

        async def held() -> list[object]:
            try:
                async with asyncio.timeout(20):
                    attempt = asyncio.create_task(replay_pilot(pilot, self.root))
                    await self.closing.wait()
                    # Past the 0.1 s cleanup window, which began before close did.
                    await asyncio.sleep(0.3)
                    seen: list[object] = [attempt.done(), dict(calls)]
                    attempt.cancel()
                    await asyncio.sleep(0.2)
                    seen += [attempt.done(), dict(calls)]
                    run = max(self.root.glob(".aqa/runs/*"))
                    seen += [(run / n).exists() for n in HOLD_RECEIPTS]
                    self.release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await attempt
                    return seen
            finally:
                self.release.set()

        closing = {"close": 1, "close_cancelled": 0}
        seen = [False, closing, False, closing, True, False]
        self.assertEqual(asyncio.run(held()), seen)
        self.assertEqual(
            self.receipt("cleanup-incomplete.json"),
            {
                "failure": "operation_timeout",
                "interrupted": False,
                "resources": "unknown",
                "reservation_release": "forbidden",
            },
        )
        outcome = {
            "outcome": "interrupted",
            "interrupted": True,
            "cleanup": "incomplete",
        }
        cause = {"failure": "operation_timeout"}
        self.assertEqual(self.receipt("attempt.json"), OUTCOME | outcome | cause)

    def test_a_failed_incomplete_receipt_still_holds_the_attempt(self) -> None:
        del self.spec["preconditions"]["reset"]
        self.config["budgets"] = {"resolve_seconds": 0.1}
        self.fake_browser(held=True)
        original = RunRecord.write

        def write(record: RunRecord, name: str, document: object) -> Path:
            if name == "cleanup-incomplete.json":
                raise OSError("fake full disk")
            return original(record, name, document)

        self.enterContext(patch.object(RunRecord, "write", write))
        pilot = self.pilot()

        async def held() -> bool:
            try:
                async with asyncio.timeout(20):
                    attempt = await self.interrupt(pilot)
                    await self.closing.wait()
                    await asyncio.sleep(0.3)
                    live = not attempt.done()
                    self.release.set()
                    with self.assertRaisesRegex(OSError, "fake full disk"):
                        await attempt
                    return live
            finally:
                self.release.set()

        self.assertTrue(asyncio.run(held()))

    def test_an_interrupt_outranks_a_later_cleanup_error(self) -> None:
        del self.spec["preconditions"]["reset"]
        self.fake_browser()
        self.fail_proxy_exits()
        pilot = self.pilot()

        async def interrupted() -> None:
            async with asyncio.timeout(20):
                await (await self.interrupt(pilot))

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(interrupted())
        outcome = {"outcome": "interrupted", "interrupted": True, "cleanup": "failed"}
        self.assertEqual(self.receipt("attempt.json"), OUTCOME | outcome)

    def test_a_cleanup_error_never_replaces_the_first_failure(self) -> None:
        self.fail_proxy_exits()
        pilot = self.pilot()
        script = pilot.script.model_copy(update={"steps": (PRESS,)})
        attempt = self.replay(replace(pilot, script=script))
        self.assertEqual(self.failed(attempt), ("script_refused", "failed", "unknown"))
        attempt = self.replay(pilot)
        self.assertEqual(self.failed(attempt), ("cleanup_failed", "failed", "unknown"))
        entering = AsyncMock(side_effect=OSError("fake proxy start failure"))
        with (
            patch.object(EgressProxy, "__aenter__", entering),
            self.assertRaisesRegex(OSError, "fake proxy start failure"),
        ):
            self.replay(pilot)
        started = {"outcome": "unexpected", "reset_completed": True}
        self.assertEqual(self.receipt("attempt.json"), OUTCOME | started | FAILED)
        self.assertEqual(
            self.server.log, ["POST /reset"] * 2 + ["GET /", "POST /reset"]
        )

    def test_a_cleanup_failure_outranks_a_later_operation_timeout(self) -> None:
        self.config["budgets"] = {"minutes": 0.1}
        self.fail_proxy_exits()
        real = async_playwright

        @asynccontextmanager
        async def stalling() -> AsyncIterator[Any]:
            async with real() as driver:
                yield driver
                await asyncio.Event().wait()

        self.enterContext(patch("pilot_replay.async_playwright", stalling))
        attempt = self.replay(self.pilot())
        self.assertEqual(self.failed(attempt), ("cleanup_failed", "failed", "unknown"))
