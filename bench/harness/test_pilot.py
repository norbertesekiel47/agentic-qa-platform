import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pilot
from aqa_core.project import load_project
from aqa_core.spec import canonical_hash
from pilot_inputs import PilotInput, load_pilots
from pilot_replay import FailedAttempt, ObservedAttempt, replay_pilot
from test_flags import FakeDocker
from test_pilot_replay import CLIENTS, COMPILED, SEND, Handler, Server
from test_pilot_report import A0, DRIFTED, HEALTH, PASSED, PRIVATE

SPEC = '{"id":"pilot","goal":"Send","preconditions":{"start_url":"/","reset":{"http":"POST /reset?fake-sensitive"}},"expect":["Title is visible"]}'
SOURCE = ("c" * 40, "t" * 40, False)
OK = ObservedAttempt("r1", PRIVATE, replace(PASSED, assertions=(A0,)), HEALTH)
LOST = replace(OK, observation=replace(DRIFTED, assertions=DRIFTED.assertions[:1]))
ZERO, MISSED = frozenset({0}), (A0._replace(outcome="failed"),)
FAILING = replace(PASSED, outcome="failed", assertions=MISSED, failed_expectations=ZERO)
FAILED_0 = replace(OK, observation=FAILING)
PRESS_AB = {"seq": 1, "action": "press", "key": "a+b", "side_effect": False}
HELD = FailedAttempt("r9", PRIVATE, "operation_timeout", "incomplete", "unknown")


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
    """B2's disposable page, whose button reads Post under ben1 and whose
    reset answers 500 under bug1."""

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

    def compose(
        self, app_dir: Path, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> str:
        answer = super().compose(app_dir, args, env)
        on = self.bench_flags.split(",")
        self.server.label = "Post" if "ben1" in on else "Send"
        self.server.reset = 500 if "bug1" in on else 200
        return answer


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
            (patches / name).write_text(json.dumps(document))

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
    ) -> tuple[int, str]:
        self.save(stale=stale)
        argv = ["--root", str(self.root), "conduit", "--out", str(self.out)]
        argv += ["--spec", "pilot", "--compiled-dir", str(self.compiled)]
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
        self.assertEqual(
            [loc.model_dump()["name"] for loc in locators], ["Send", "Post"]
        )
        pairs = [(p["case"], p["status"]) for p in self.report()["pairs"]]
        self.assertEqual(
            pairs,
            [
                (None, "passed"),
                ("conduit-benign-001", "accepted_pending_C"),
                ("conduit-bug-001", "unscored_binding_blocked"),
            ],
        )
        self.assertEqual(len(list((self.out / "attempts").iterdir())), 5)
        self.assertEqual(self.report()["switched_back_clean"], True)

    def test_manifest_answers_never_cross_replay_boundary(self) -> None:
        self.cases = {"conduit-bug-001": case("bug", "bug1", expect=[0])}
        found = []
        for row in ({"expect": [0]}, {"invariants": ["js_exceptions"]}):
            with self.subTest(row=row):
                self.cases["conduit-bug-001"]["expected"] = [
                    {"spec": "pilot", "verdict": "expectation_violated"} | row
                ]
                self.docker = FakeDocker()
                replay = FakeReplay(self.docker, {"": [OK], "bug1": [FAILED_0]})
                code, _ = self.command(replay)
                found.append((code, [pilot_input for _, pilot_input in replay.seen]))
                self.out.rename(self.root / f"out-{len(found)}")
        self.assertEqual([code for code, _ in found], [0, 1])
        self.assertEqual(found[0][1], found[1][1])

    def test_invalid_input_stops_before_build(self) -> None:
        def existing() -> None:
            self.out.mkdir()

        def secret() -> None:
            self.spec["preconditions"]["account"] = {
                "password": {"secret": "TEST_PASSWORD"}
            }
            self.config["secrets"] = {
                "TEST_PASSWORD": {"origins": ["start"], "field": "password"}
            }

        cases: list[tuple[str, Callable[[], object], int, str]] = [
            ("stale", lambda: None, 2, "pilot.json: invalid pilot input"),
            (
                "press",
                lambda: self.steps.__setitem__(0, PRESS_AB),
                2,
                "pilot.json: invalid pilot input",
            ),
            (
                "no reset",
                lambda: self.spec["preconditions"].pop("reset"),
                2,
                "pilot.json: invalid pilot input",
            ),
            ("secret", secret, 12, "pilot.json: unusable test secret"),
            (
                "patched check",
                lambda: self.patch_to("Post", "/assertions/0/text"),
                2,
                "pilot.json: invalid patch",
            ),
            (
                "unknown patch",
                lambda: self.patches.update({"conduit-bug-001.pilot.json": {}}),
                2,
                "not a patch for a selected",
            ),
            ("existing out", existing, 2, "out: already exists"),
        ]
        for name, change, expected, message in cases:
            with self.subTest(name), patch.dict(os.environ):
                self.setUp()
                change()
                replay = FakeReplay(self.docker, {})
                code, shown = self.command(replay, stale=name == "stale")
                self.assertEqual(
                    (code, self.docker.calls, replay.seen), (expected, [], [])
                )
                self.assertIn(message, shown)
                self.assertFalse((self.out / "report.json").exists())
        replay = FakeReplay(self.docker, {})
        code, shown = self.command(replay, source=lambda *_: ("c" * 40, "t" * 40, True))
        self.assertEqual((code, self.docker.calls), (2, []))
        self.assertIn("uncommitted changes", shown)
        missing = ["--patches", str(self.root / "none"), "--out", str(self.root / "o")]
        code, shown = self.command(FakeReplay(self.docker, {}), *missing)
        self.assertEqual((code, self.docker.calls), (2, []))
        self.assertIn("No such file or directory", shown)

    def test_unwritable_evidence_is_an_operational_failure(self) -> None:
        replay = FakeReplay(self.docker, {"": [OK]})
        with patch("pilot_report.write_once", side_effect=OSError("fake disk full")):
            code, shown = self.command(replay)
        self.assertEqual((code, self.ups()), (3, [""]))
        self.assertIn("couldn't be written", shown)

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
        submit, drifted = (
            ["POST /reset", "GET /", "POST /submit"],
            ["POST /reset", "GET /"],
        )
        self.assertEqual(server.log, submit + drifted + submit)
        clean, benign = self.report()["pairs"]
        self.assertEqual(
            (clean["status"], benign["status"]), ("passed", "accepted_pending_C")
        )
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
        timed_out = replace(
            OK,
            observation=replace(
                LOST.observation, assertions=(A0._replace(outcome="check_timed_out"),)
            ),
        )
        failed_step = replace(
            OK,
            observation=replace(
                OK.observation,
                outcome="errored",
                eligible=False,
                steps=(
                    PASSED.steps[0],
                    PASSED.steps[1]._replace(outcome="failed", error=True),
                ),
            ),
        )
        fatal = {
            "infrastructure": infrastructure,
            "check timeout": timed_out,
            "failed step": failed_step,
            "held": HELD,
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

    def test_a_stop_short_keeps_earlier_receipts_and_holds_the_app(self) -> None:
        stops: list[tuple[BaseException, str, int]] = [
            (asyncio.CancelledError(), "interrupted", 130),
            (KeyboardInterrupt(), "interrupted", 130),
            (RuntimeError("fake-error-text"), "unexpected", 3),
            (SystemExit("fake-exit-payload"), "system_exit", 3),
        ]
        for error, halt, expected in stops:
            with self.subTest(halt=halt, error=type(error).__name__):
                self.docker = FakeDocker()
                self.out = self.root / f"out-{type(error).__name__}"
                replay = FakeReplay(self.docker, {"": [OK], "ben1": [error]})
                code, shown = self.command(replay)
                report = self.report()
                self.assertEqual(
                    (code, report["halt"], report["exit"]), (expected, halt, expected)
                )
                self.assertEqual(
                    (report["reservation_release"], self.ups()),
                    ("forbidden", ["", "ben1"]),
                )
                self.assertEqual([p["status"] for p in report["pairs"]], ["passed"])
                receipt = json.loads((self.out / "attempts" / "001.json").read_text())
                self.assertEqual((receipt["case"], receipt["kind"]), (None, "eligible"))
                self.assertNotIn(
                    "fake-", shown + (self.out / "report.json").read_text()
                )

    def test_a_source_change_during_the_run_is_not_citable(self) -> None:
        identities = iter([SOURCE, ("d" * 40, "t" * 40, False)])
        replay = FakeReplay(self.docker, {"": [OK], "ben1": [OK], "bug1": [OK]})
        code, _ = self.command(replay, source=lambda *_: next(identities))
        self.assertEqual((code, self.report()["source_unchanged"]), (3, False))
        self.assertEqual(
            self.report()["source"], {"commit": "c" * 40, "tree": "t" * 40}
        )

    def test_git_source_reads_head_and_skips_the_output(self) -> None:
        repo, out = self.root / "repo", self.root / "repo" / "out"
        out.mkdir(parents=True)
        git = ["git", "-c", "user.name=fake", "-c", "user.email=fake@example.invalid"]
        subprocess.run([*git, "init", "-q", str(repo)], check=True)
        commit = [
            "commit",
            "-q",
            "--allow-empty",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            "x",
        ]
        subprocess.run([*git, "-C", str(repo), *commit], check=True)
        (out / "report.json").write_text("{}")
        commit_id, tree, dirty = pilot.git_source(repo, out)
        self.assertEqual((len(commit_id), len(tree), dirty), (40, 40, False))
        self.assertTrue(pilot.git_source(repo, self.root / "elsewhere")[2])
        (repo / "new").write_text("x")
        self.assertTrue(pilot.git_source(repo, out)[2])
