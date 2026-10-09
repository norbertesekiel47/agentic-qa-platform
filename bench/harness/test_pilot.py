import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping
from contextlib import (
    AbstractContextManager,
    nullcontext,
    redirect_stderr,
    redirect_stdout,
)
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pilot
import pilot_report
from aqa_core.project import SpecError, load_project
from aqa_core.spec import canonical_hash
from pilot_inputs import PilotInput, load_pilots
from pilot_replay import FailedAttempt, ObservedAttempt, replay_pilot
from test_flags import FakeDocker
from test_pilot_replay import ACCOUNT, BINDING, CLIENTS, COMPILED, SEND, Handler, Server
from test_pilot_report import A0, DRIFTED, HEALTH, PASSED, PRIVATE

SPEC = '{"id":"pilot","goal":"Send","preconditions":{"start_url":"/","reset":{"http":"POST /reset?fake-sensitive"}},"expect":["Title is visible"]}'
SOURCE = pilot.Source("c" * 40, "t" * 40, False)
OK = ObservedAttempt("r1", PRIVATE, replace(PASSED, assertions=(A0,)), HEALTH)
LOST = replace(OK, observation=replace(DRIFTED, assertions=DRIFTED.assertions[:1]))
ZERO, MISSED = frozenset({0}), (A0._replace(outcome="failed"),)
FAILING = replace(PASSED, outcome="failed", assertions=MISSED, failed_expectations=ZERO)
FAILED_0 = replace(OK, observation=FAILING)
PRESS_AB = {"seq": 1, "action": "press", "key": "a+b", "side_effect": False}
INPUT = "pilot.json: invalid pilot input"
HELD = FailedAttempt("r9", PRIVATE, "operation_timeout", "incomplete", "unknown")
REJECTED = FailedAttempt("r9", PRIVATE, "reset_rejected", "completed", "closed")
ROW = {"spec": "pilot", "expect": 0, "region": "main", "part": "button"}


def case(kind: str, flag: str, split: str = "dev", **row: object) -> dict[str, Any]:
    verdict = "expectation_violated" if kind == "bug" else "drift_consistent"
    entry = {"spec": "pilot", "verdict": verdict} | row
    category = {"category": "functional"} if kind == "bug" else {}
    family = f"conduit-{flag}"
    data = {"app": "conduit", "kind": kind, "split": split, "family": family}
    return data | category | {"flag": flag, "summary": "s", "expected": [entry]}


class FakeReplay:
    """Answers each attempt by the flags that are on, in turn."""

    def __init__(self, docker: FakeDocker, answers: Mapping[str, list[Any]]) -> None:
        self.docker, self.answers = docker, {k: list(v) for k, v in answers.items()}
        self.seen: list[tuple[str, PilotInput]] = []

    async def __call__(self, pilot_input: PilotInput, _root: Path) -> Any:
        self.seen.append((self.docker.bench_flags, pilot_input))
        answer = self.answers[self.docker.bench_flags].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FlagServer(Server):
    """B2's page: the button reads Post under ben1; the reset answers 500 under bug1."""

    def __init__(self) -> None:
        super().__init__()
        self.RequestHandlerClass = FlagHandler
        self.label = "Send"


class FlagHandler(Handler):
    server: FlagServer

    def answer(self, status: int, page: str) -> None:
        super().answer(status, page.replace("Send", self.server.label))


class FlagDocker(FakeDocker):
    def __init__(self, server: FlagServer) -> None:
        super().__init__()
        self.server = server

    def compose(self, *args: Any, **kwargs: Any) -> str:
        answer = super().compose(*args, **kwargs)
        on = self.bench_flags.split(",")
        self.server.label = "Post" if "ben1" in on else "Send"
        self.server.reset = 500 if "bug1" in on else 200
        return answer


class BrokenDocker(FakeDocker):
    """At its `fail_at`th switch, the app serves a page that won't decode."""

    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self.fail_at = fail_at

    def fetch(self, url: str) -> str:
        self.fail_at -= 1
        if self.fail_at == 0:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "fake page")
        return super().fetch(url)


class PilotCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.qa = self.root / "bench" / "apps" / "conduit" / "qa"
        self.compiled, self.out = self.root / "compiled", self.root / "out"
        self.qa.mkdir(parents=True)
        self.compiled.mkdir()
        self.config: dict[str, Any] = {"base_url": "http://127.0.0.1:4100"}
        self.spec: dict[str, Any] = json.loads(SPEC)
        self.steps: list[object] = [SEND]
        self.cases = {
            "conduit-benign-001": case("benign", "ben1"),
            "conduit-bug-001": case("bug", "bug1", spec="other", expect=[0]),
            "conduit-bug-002": case("bug", "tst1", "test", expect=[0]),
        }
        self.patches: dict[str, object] = {}
        self.docker = FakeDocker()
        self.enterContext(patch.dict(os.environ))
        for name in ("DEBUG", "DEBUGP", "AQA_SECRET_TEST_PASSWORD"):
            os.environ.pop(name, None)

    def save(self, *, stale: bool = False) -> None:
        (self.qa / "config.yaml").write_text(json.dumps(self.config))
        self.write_specs()
        spec_hash = load_project(self.qa).specs["pilot"].spec_hash
        script = json.loads(COMPILED) | {"steps": self.steps, "spec_hash": spec_hash}
        (self.compiled / "pilot.json").write_text(json.dumps(script))
        if stale:
            self.spec["expect"] = ["The title is visible"]
            self.write_specs()
        manifest = {"schema_version": 1, "cases": self.cases}
        (self.root / "bench" / "manifest.v1.json").write_text(json.dumps(manifest))
        patches = self.root / "patches"
        patches.mkdir(exist_ok=True)
        for name, document in self.patches.items():
            text = document if isinstance(document, str) else json.dumps(document)
            (patches / name).write_text(text)

    def write_specs(self) -> None:
        for spec_id in ("pilot", "other"):
            spec = json.dumps(self.spec | {"id": spec_id})
            (self.qa / f"{spec_id}.spec.md").write_text(f"---\n{spec}\n---\n")

    def patch_to(self, label: str, path: str = "/targets/send/locators") -> None:
        self.save()
        script = load_pilots(self.qa, self.compiled, ("pilot",))[0].script
        value = [{"role": "button", "name": label}]
        operation = {"op": "replace", "path": path, "value": value}
        base = canonical_hash(script.model_dump(mode="json"))
        name = "conduit-benign-001.pilot.json"
        self.patches[name] = {"base_hash": base, "operations": [operation]}

    def command(
        self,
        replay: Callable[[PilotInput, Path], Any],
        *extra: str,
        source: Callable[[Path, Path], Any] = lambda *_: SOURCE,
        stale: bool = False,
        specs: tuple[str, ...] = ("pilot",),
    ) -> tuple[int, str]:
        self.save(stale=stale)
        argv = ["--root", str(self.root), "conduit", "--out", str(self.out)]
        argv += [arg for spec in specs for arg in ("--spec", spec)]
        argv += ["--compiled-dir", str(self.compiled)]
        argv += ["--repeat", "1", "--patches", str(self.root / "patches"), *extra]
        shown = StringIO()
        with redirect_stdout(shown), redirect_stderr(shown):
            code = pilot.main(argv, self.docker, replay, source)
        return code, shown.getvalue()

    def report(self) -> Any:
        return json.loads((self.out / "report.json").read_text())

    def ups(self) -> list[str]:
        return [e["BENCH_FLAGS"] for _, a, e in self.docker.calls if a[0] == "up"]

    def test_runs_clean_repeats_then_each_dev_flag_serially(self) -> None:
        self.patch_to("Post")
        answers = {"": [OK, OK], "bug1": [LOST], "ben1": [LOST, OK]}
        replay = FakeReplay(self.docker, answers)
        code, shown = self.command(replay, "--repeat", "2")
        self.assertEqual(code, 0, shown)
        self.assertEqual(self.docker.calls[0][1], ("build", "--quiet"))
        self.assertEqual(self.ups(), ["", "ben1", "bug1", ""])
        flags_seen = [flags for flags, _ in replay.seen]
        self.assertEqual(flags_seen, ["", "", "ben1", "ben1", "bug1"])
        locators = [p.script.targets["send"].locators[0] for _, p in replay.seen[2:4]]
        names = [loc.model_dump()["name"] for loc in locators]
        statuses = [p["status"] for p in self.report()["pairs"]]
        want = ["passed", "accepted_pending_C", "unscored_binding_blocked"]
        self.assertEqual((names, statuses), (["Send", "Post"], want))
        kept = sorted((self.out / "attempts").iterdir())
        roles = [json.loads(path.read_text())["role"] for path in kept]
        legs = ["clean", "clean", "diagnostic", "acceptance", "unscored"]
        self.assertEqual(roles, legs)
        self.assertEqual(self.report()["switched_back_clean"], True)
        hashes, used = self.report()["hashes"], replay.seen[0][1]
        kinds = [name.partition(":")[0] for name in sorted(hashes)]
        self.assertEqual(kinds, ["manifest", "patch", "script", "spec"])
        script = canonical_hash(used.script.model_dump(mode="json"))
        cited = (hashes["spec:pilot"], hashes["script:pilot"])
        self.assertEqual(cited, (used.spec.spec_hash, script))

    def test_reports_the_selection_and_every_omitted_spec(self) -> None:
        self.cases, other = {}, self.compiled / "other.json"
        self.save()
        spec_hash = load_project(self.qa).specs["other"].spec_hash
        script = json.loads((self.compiled / "pilot.json").read_text())
        script |= {"spec_id": "other", "spec_hash": spec_hash}
        other.write_text(json.dumps(script))
        found: list[tuple[object, ...]] = []
        for specs in ((), ("other", "pilot"), ("pilot", "other"), ("pilot",)):
            if specs == ("pilot",):
                other.unlink()
            self.docker, self.out = FakeDocker(), self.root / f"out-{len(found)}"
            replay = FakeReplay(self.docker, {"": [OK, OK]})
            code, _ = self.command(replay, specs=specs)
            doc, seen = self.report(), [p.spec.frontmatter.id for _, p in replay.seen]
            found.append((code, doc["selected"], seen, doc["omitted"]))
        both, back = ["other", "pilot"], ["pilot", "other"]
        want: list[tuple[object, ...]] = [(0, both, both, []), (0, both, both, [])]
        want += [(0, back, back, []), (0, ["pilot"], ["pilot"], ["other"])]
        self.assertEqual(found, want)

    def test_an_unreadable_project_stops_before_build(self) -> None:
        for error in (SpecError(["fake-sensitive spec"]), OSError("fake-sensitive")):
            with self.subTest(type(error).__name__):
                replay = FakeReplay(self.docker, {})
                with patch("pilot.load_project", side_effect=error):
                    code, shown = self.command(replay)
                self.assertEqual((code, self.docker.calls, replay.seen), (2, [], []))
                self.assertEqual(shown, f"error: {self.qa}: invalid pilot input\n")
                self.assertFalse(self.out.exists())

    def test_manifest_answers_never_cross_replay_boundary(self) -> None:
        found = []
        for row in ({"expect": [0]}, {"invariants": ["js_exceptions"]}):
            with self.subTest(row=row):
                self.cases = {"conduit-bug-001": case("bug", "bug1", **row)}
                self.docker = FakeDocker()
                replay = FakeReplay(self.docker, {"": [OK], "bug1": [FAILED_0]})
                code, _ = self.command(replay)
                found.append((code, [pilot_input for _, pilot_input in replay.seen]))
                self.out.rename(self.root / f"out-{len(found)}")
        self.assertEqual([code for code, _ in found], [0, 1])
        self.assertEqual(found[0][1], found[1][1])

    def test_invalid_input_stops_before_build(self) -> None:
        def secret() -> None:
            self.spec["preconditions"]["account"] = ACCOUNT
            self.config["secrets"] = BINDING

        def deep() -> None:
            self.patches["conduit-benign-001.pilot.json"] = "[" * 1_000_000

        def stray() -> None:
            self.cases["conduit-bug-001"] = case("bug", "bug1", expect=[0])
            self.patches["conduit-bug-001.pilot.json"] = {}

        def unparsed() -> None:
            self.cases["conduit-bug-001"]["flag"] = "fake-sensitive"

        cases: list[tuple[str, Callable[[], object], int, str]] = [
            ("stale", lambda: None, 2, INPUT),
            ("press", lambda: self.steps.__setitem__(0, PRESS_AB), 2, INPUT),
            ("no reset", lambda: self.spec["preconditions"].pop("reset"), 2, INPUT),
            ("secret", secret, 12, "pilot.json: unusable test secret"),
            ("patched check", lambda: self.patch_to("x", "/assertions/0"), 2, "patch"),
            ("non-drift patch", stray, 2, "not a patch for a selected"),
            ("existing out", lambda: self.out.mkdir(exist_ok=False), 2, "out: exists"),
            ("bad manifest", unparsed, 2, "error: ManifestError"),
            ("subject row", lambda: self.config.update(subjects=[ROW]), 2, INPUT),
            ("deep patch", deep, 2, "invalid patch"),
        ]
        for name, change, want, message in cases:
            with self.subTest(name), patch.dict(os.environ):
                self.setUp()
                change()
                replay = FakeReplay(self.docker, {})
                code, shown = self.command(replay, stale=name == "stale")
                self.assertEqual((code, self.docker.calls, replay.seen), (want, [], []))
                self.assertIn(message, shown)
                self.assertNotIn("fake-sensitive", shown)
                self.assertFalse((self.out / "report.json").exists())
        refusals: list[tuple[tuple[str, ...], Any, str]] = [
            ((), lambda *_: SOURCE._replace(dirty=True), "uncommitted changes"),
            (("--patches", str(self.root / "none")), lambda *_: SOURCE, "FileNotFound"),
            (("--repeat", "0"), lambda *_: SOURCE, "--repeat below 1"),
            ((), pilot.git_source, "CalledProcessError"),
        ]
        self.setUp()
        replay = FakeReplay(self.docker, {})
        for extra, source, message in refusals:
            with self.subTest(message):
                self.out = self.root / f"out-{len(message)}"
                code, shown = self.command(replay, *extra, source=source)
                self.assertEqual((code, self.docker.calls), (2, []))
                self.assertIn(message, shown)

    def test_unwritable_evidence_is_an_operational_failure(self) -> None:
        replay = FakeReplay(self.docker, {"": [OK]})
        with patch("pilot_report.write_once", side_effect=OSError("fake disk full")):
            code, shown = self.command(replay)
        self.assertEqual((code, self.ups()), (3, [""]))
        self.assertIn("couldn't be completed", shown)

    def test_an_error_after_docker_holds_the_app_and_still_reports(self) -> None:
        at: list[int] = []

        def fails(error: Exception, real: Callable[..., Any], call: int) -> Any:
            """`real`, but call number `call` notes Docker's call count, then raises."""
            calls = iter(range(1, 100))

            def answer(*args: Any, **kwargs: Any) -> Any:
                if next(calls) != call:
                    return real(*args, **kwargs)
                at.append(len(self.docker.calls))
                raise error

            return answer

        def source(error: Exception) -> Any:
            return fails(error, lambda *_: SOURCE, 2)

        def broken(name: str, error: Exception, call: int) -> Any:
            real = getattr(pilot_report, name)
            return patch(f"pilot_report.{name}", fails(error, real, call))

        git = subprocess.CalledProcessError(1, "fake-")
        held, ended = ["", "ben1", "bug1"], ["", "ben1", "bug1", ""]
        cases: dict[str, tuple[AbstractContextManager[Any], Any, list[str], int]] = {
            "receipt": (broken("write_once", OSError("fake-"), 2), None, held[:2], 1),
            "judge": (broken("judge_case", ValueError("fake-"), 1), None, held[:2], 2),
            "source OSError": (nullcontext(), source(OSError("fake-")), held, 3),
            "source git": (nullcontext(), source(git), held, 3),
            "source ValueError": (nullcontext(), source(ValueError("fake-")), held, 3),
            "document": (broken("document", ValueError("fake-"), 1), None, ended, 3),
        }
        answers: dict[str, list[Any]] = {"": [OK], "ben1": [LOST], "bug1": [OK]}
        for name, (trap, read, ups, kept) in cases.items():
            with self.subTest(name), trap:
                at.clear()
                self.docker, self.out = FakeDocker(), self.root / name.replace(" ", "-")
                replay = FakeReplay(self.docker, answers)
                code, shown = self.command(replay, source=read or (lambda *_: SOURCE))
                doc, written = self.report(), (self.out / "report.json").read_text()
                receipts = len(list((self.out / "attempts").iterdir()))
                found = (code, doc["halt"], doc["reservation_release"], self.ups())
                want = (3, "unexpected", "forbidden", ups, kept)
                self.assertEqual((*found, receipts), want)
                fresh = [len(self.docker.calls)], not name.startswith("source")
                self.assertEqual((at, doc["source_unchanged"]), fresh)
                self.assertNotIn("fake-", shown + written)
        self.docker, self.out = FakeDocker(), self.root / "interrupted"
        answers["ben1"] = [asyncio.CancelledError()]
        stopped = FakeReplay(self.docker, answers)
        code, _ = self.command(stopped, source=source(OSError("fake-")))
        self.assertEqual((code, self.report()["halt"]), (130, "interrupted"))

    def test_a_failed_switch_halts_with_the_app_held(self) -> None:
        for fail_at, judged in ((1, 0), (4, 3)):
            with self.subTest(fail_at=fail_at):
                self.docker, self.out = BrokenDocker(fail_at), self.root / f"o{fail_at}"
                replay = FakeReplay(self.docker, {"": [OK], "ben1": [OK], "bug1": [OK]})
                self.assertEqual(self.command(replay)[0], 3)
                doc = self.report()
                ended = (doc["halt"], doc["switched_back_clean"], len(doc["pairs"]))
                self.assertEqual(ended, ("switch_failed", False, judged))

    def serve(self) -> FlagServer:
        server = FlagServer()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config["base_url"] = f"http://127.0.0.1:{server.server_address[1]}"
        self.config["budgets"] = {"resolve_seconds": 1}
        self.docker = FlagDocker(server)
        return server

    def test_benign_patch_replays_fresh_and_preserves_original(self) -> None:
        server = self.serve()
        self.cases = {"conduit-benign-001": case("benign", "ben1")}
        self.patch_to("Post")
        built: list[str] = []

        def construct(*_: object, **__: object) -> None:
            built.append("model client")

        for client in CLIENTS:
            self.enterContext(patch.object(client, "__init__", construct))
        code, shown = self.command(replay_pilot)
        self.assertEqual((code, built), (0, []), shown)
        submit = ["POST /reset", "GET /", "POST /submit"]
        self.assertEqual(server.log, submit + submit[:2] + submit)
        clean, benign = self.report()["pairs"]
        statuses = (clean["status"], benign["status"])
        self.assertEqual(statuses, ("passed", "accepted_pending_C"))
        legs = [(a["role"], a["kind"], a["outcome"]) for a in benign["attempts"]]
        expected = [
            ("diagnostic", "binding_only", "failed"),
            ("acceptance", "eligible", "passed"),
        ]
        self.assertEqual(legs, expected)
        self.assertNotEqual(*(a["run_id"] for a in benign["attempts"]))
        files = (self.out / "report.json", *(self.out / "attempts").iterdir())
        written = "".join(path.read_text() for path in files) + shown
        self.assertNotIn("fake-sensitive", written)
        self.assertEqual(self.ups(), ["", "ben1", ""])

    def test_fatal_original_never_replays_its_patch(self) -> None:
        self.serve()
        self.patch_to("Post")
        self.cases = {"conduit-benign-001": case("benign", "bug1")}
        code, _ = self.command(replay_pilot)
        self.assertEqual(code, 3)
        benign = self.report()["pairs"][1]
        self.assertEqual((benign["status"], len(benign["attempts"])), ("fatal", 1))
        self.assertEqual(benign["attempts"][0]["failure"], "reset_rejected")
        self.assertEqual(self.ups(), ["", "bug1", ""])
        infrastructure = replace(LOST, health=replace(HEALTH, infrastructure=True))
        stopped = (A0._replace(outcome="check_timed_out"),)
        timed_out = replace(
            OK, observation=replace(LOST.observation, assertions=stopped)
        )
        broke = (
            PASSED.steps[0],
            PASSED.steps[1]._replace(outcome="failed", error=True),
        )
        bad = replace(OK.observation, outcome="errored", eligible=False, steps=broke)
        failed_step = replace(OK, observation=bad)
        flagged = (PASSED.steps[0], PASSED.steps[1]._replace(error=True))
        fatal = {
            "infrastructure": infrastructure,
            "check timeout": timed_out,
            "failed step": failed_step,
            "held": HELD,
            "eligible, error flag": replace(
                OK, observation=replace(OK.observation, steps=flagged)
            ),
        }
        self.cases["conduit-benign-001"] = case("benign", "ben1")
        self.cases["conduit-bug-001"] = case("bug", "bug1", spec="other", expect=[0])
        for name, attempt in fatal.items():
            with self.subTest(name):
                self.docker = FakeDocker()
                self.out = self.root / f"out-{name.replace(' ', '-')}"
                replay = FakeReplay(self.docker, {"": [OK], "ben1": [attempt]})
                code, _ = self.command(replay)
                self.assertEqual((code, len(replay.seen)), (3, 2))
                allowed = self.report()["reservation_release"] == "allowed"
                after = ["", "ben1"] + ([] if name == "held" else [""])
                self.assertEqual((self.ups(), allowed), (after, name != "held"))
        self.docker, self.out = FakeDocker(), self.root / "out-clean"
        replay = FakeReplay(self.docker, {"": [REJECTED]})
        code, _ = self.command(replay, "--repeat", "3")
        self.assertEqual((code, len(replay.seen), self.ups()), (3, 1, ["", ""]))

    def test_a_short_clean_run_never_passes(self) -> None:
        self.cases, found = {}, {}
        runs: dict[str, list[Any]] = {"full": [OK, OK, OK], "held": [OK, HELD]}
        for name, answers in runs.items():
            self.docker, self.out = FakeDocker(), self.root / name
            replay = FakeReplay(self.docker, {"": answers})
            code, _ = self.command(replay, "--repeat", "3")
            doc = self.report()
            (pair,) = doc["pairs"]
            seen = (pair["status"], len(pair["attempts"]), doc["reservation_release"])
            found[name] = (code, *seen, self.ups())
        want = {"full": (0, "passed", 3, "allowed", ["", ""])}
        self.assertEqual(found, want | {"held": (3, "fatal", 2, "forbidden", [""])})

    def test_an_enter_failing_after_the_cancel_prints_nothing(self) -> None:
        self.config["budgets"] = {"minutes": 0.005, "resolve_seconds": 0.1}
        self.steps, self.cases = [], {}
        self.spec["preconditions"].pop("reset")

        async def late() -> None:
            await asyncio.sleep(1.5)
            raise ValueError("fake-enter-text")

        driver = MagicMock(__aenter__=AsyncMock(side_effect=late))
        self.enterContext(patch("pilot_replay.async_playwright", lambda: driver))
        with self.assertNoLogs("asyncio"):
            code, shown = self.command(replay_pilot)
        attempt = self.report()["pairs"][0]["attempts"][0]
        ended = (code, attempt["cleanup"], attempt["resources"])
        self.assertEqual(ended, (3, "incomplete", "unknown"))
        self.assertNotIn("fake-enter-text", shown)

    def test_a_stop_short_keeps_earlier_receipts_and_holds_the_app(self) -> None:
        stops: list[tuple[BaseException, str, int]] = [
            (asyncio.CancelledError(), "interrupted", 130),
            (KeyboardInterrupt(), "interrupted", 130),
            (RuntimeError("fake-error-text"), "unexpected", 3),
            (SystemExit("fake-exit-payload"), "system_exit", 3),
        ]
        self.patch_to("Post")
        for error, halt, want in stops:
            with self.subTest(halt=halt, error=type(error).__name__):
                self.docker = FakeDocker()
                self.out = self.root / f"out-{type(error).__name__}"
                replay = FakeReplay(self.docker, {"": [OK], "ben1": [LOST, error]})
                code, shown = self.command(replay)
                doc = self.report()
                self.assertEqual((code, doc["halt"], doc["exit"]), (want, halt, want))
                held = (doc["reservation_release"], self.ups())
                self.assertEqual(held, ("forbidden", ["", "ben1"]))
                self.assertEqual([p["status"] for p in doc["pairs"]], ["passed"])
                kept = sorted((self.out / "attempts").iterdir())
                receipts = [json.loads(path.read_text()) for path in kept]
                found = [(r["case"], r["role"], r["kind"]) for r in receipts]
                benign = ("conduit-benign-001", "diagnostic", "binding_only")
                self.assertEqual(found, [(None, "clean", "eligible"), benign])
                self.assertNotIn(
                    "fake-", shown + (self.out / "report.json").read_text()
                )

    def test_a_source_change_during_the_run_is_not_citable(self) -> None:
        changed = SOURCE._replace(commit="d" * 40)
        for reads in ([SOURCE, changed], [SOURCE, SOURCE, changed]):
            self.docker, self.out = FakeDocker(), self.root / f"out-{len(reads)}"
            replay = FakeReplay(self.docker, {"": [OK], "ben1": [OK], "bug1": [OK]})
            code, _ = self.command(replay, source=MagicMock(side_effect=reads))
            self.assertEqual((code, self.report()["source_unchanged"]), (3, False))
            want = {"commit": SOURCE.commit, "tree": SOURCE.tree}
            self.assertEqual(self.report()["source"], want)

    def git(self, *args: str) -> str:
        """Git without inherited GIT_* (setUp restores them): one from `git rebase
        --exec` aimed `git init` at the real repository (LAB_NOTES, 2026-10-09)."""
        for name in [name for name in os.environ if name.startswith("GIT_")]:
            del os.environ[name]
        git = ["git", "-c", "user.name=fake", "-c", "user.email=fake@example.invalid"]
        return subprocess.check_output([*git, *args], text=True)

    def git_repo(self) -> tuple[Path, Path]:
        """A disposable repo with one commit, and its `out` directory."""
        repo, out = self.root / "repo", self.root / "repo" / "out"
        out.mkdir(parents=True)
        self.git("init", "-q", str(repo))
        commit = ["commit", "-q", "--allow-empty", "--no-verify", "--no-gpg-sign"]
        self.git("-C", str(repo), *commit, "-m", "x")
        return repo, out

    def test_git_runs_in_the_temp_repo_despite_an_inherited_git_dir(self) -> None:
        decoy = self.root / "decoy"
        self.git("init", "-q", str(decoy))
        os.environ["GIT_DIR"] = str(decoy / ".git")
        repo, out = self.git_repo()
        os.environ["GIT_DIR"] = str(decoy / ".git")
        commit_id = pilot.git_source(repo, out).commit
        found = (
            self.git("-C", str(decoy), "config", "core.bare"),
            self.git("-C", str(decoy), "rev-list", "--all"),
            self.git("-C", str(repo), "rev-parse", "HEAD"),
        )
        self.assertEqual(found, ("false\n", "", f"{commit_id}\n"))

    def test_git_source_reads_head_and_skips_the_output(self) -> None:
        repo, out = self.git_repo()
        (out / "report.json").write_text("{}")
        commit_id, tree, dirty = pilot.git_source(repo, out)
        self.assertEqual((len(commit_id), len(tree), dirty), (40, 40, False))
        self.assertTrue(pilot.git_source(repo, self.root / "elsewhere")[2])
        self.assertTrue(pilot.git_source(repo, repo / "*")[2])
        (repo / "new").write_text("x")
        self.assertTrue(pilot.git_source(repo, out)[2])
