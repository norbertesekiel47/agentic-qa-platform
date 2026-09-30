"""Tests for toggle.py: proving each case's flag switches its planted change (ADR-0023).

Docker and the checks container are faked here. The real run's output is the
evidence in the pull request that adds or changes cases.

Run: python3 -m unittest discover -s bench/harness
"""

from __future__ import annotations

import io
import json
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import manifest
import toggle
from test_flags import FakeDocker
from test_manifest import Workspace, valid_data


class FakeChecks:
    """Reports a case as planted exactly when its flag is on in the fake stack."""

    def __init__(self, root: Path, docker: FakeDocker) -> None:
        self.root = root
        self.docker = docker
        self.cases = sorted(manifest.load(root).cases)
        self.runs: list[list[str]] = []
        self.always_planted: set[str] = set()
        self.failing: set[str] = set()
        # A case whose flag also plants another case's change: {case: other case}.
        self.cross_wired: dict[str, str] = {}

    def registered(self) -> list[str]:
        return list(self.cases)

    def run(self, case_ids: Sequence[str]) -> list[toggle.Result]:
        self.runs.append(list(case_ids))
        active = {f for f in self.docker.bench_flags.split(",") if f}
        flag_of = {c.id: c.flag for c in manifest.load(self.root).cases.values()}
        results = []
        for case_id in case_ids:
            if case_id in self.failing:
                results.append(toggle.Result(case_id, None, "", "TimeoutError: boom"))
                continue
            wired = [src for src, dst in self.cross_wired.items() if dst == case_id]
            on = (
                flag_of[case_id] in active
                or case_id in self.always_planted
                or any(flag_of[src] in active for src in wired)
            )
            state = toggle.PLANTED if on else toggle.CLEAN
            results.append(toggle.Result(case_id, state, "'observed'", None))
        return results


class ToggleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.ws.write_manifest(valid_data())
        self.docker = FakeDocker()
        self.checks = FakeChecks(self.ws.root, self.docker)

    def run_toggle(self, cycles: int = 1, only: Sequence[str] = ()) -> list[toggle.Row]:
        return toggle.toggle(
            self.ws.root, "conduit", cycles, self.docker, self.checks, only=only
        )


class ToggleRunTest(ToggleTestCase):
    def test_every_case_toggles_in_every_cycle(self) -> None:
        rows = self.run_toggle(cycles=2)
        # 2 cycles x (2 checks on the clean app + 2 flags x 2 checks)
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(row.ok for row in rows))
        on = [(r.cycle, r.case) for r in rows if r.flag_on]
        self.assertEqual(
            on,
            [
                (1, "conduit-benign-001"),
                (1, "conduit-bug-001"),
                (2, "conduit-benign-001"),
                (2, "conduit-bug-001"),
            ],
        )

    def test_every_check_runs_on_the_clean_app_and_under_each_flag(self) -> None:
        self.run_toggle(cycles=1)
        both = ["conduit-benign-001", "conduit-bug-001"]
        self.assertEqual(self.checks.runs, [both, both, both])
        self.assertEqual(
            [env for _, env in self.docker.up_calls()],
            [
                {"BENCH_FLAGS": ""},
                {"BENCH_FLAGS": "h3k8"},
                {"BENCH_FLAGS": "k3q9"},
                {"BENCH_FLAGS": ""},
            ],
        )

    def test_builds_the_app_images_before_switching(self) -> None:
        self.run_toggle(cycles=1)
        self.assertEqual(self.docker.calls[0], ("conduit", ("build", "--quiet"), {}))
        builds = [args for _, args, _ in self.docker.calls if args[0] == "build"]
        self.assertEqual(len(builds), 1)

    def test_ends_on_the_clean_app(self) -> None:
        self.run_toggle(cycles=1)
        self.assertEqual(self.docker.bench_flags, "")
        self.assertEqual(self.docker.up_calls()[-1][1], {"BENCH_FLAGS": ""})

    def test_change_present_on_the_clean_app_fails(self) -> None:
        self.checks.always_planted.add("conduit-bug-001")
        rows = self.run_toggle()
        bad = [(r.case, r.active) for r in rows if not r.ok]
        self.assertEqual(
            bad, [("conduit-bug-001", None), ("conduit-bug-001", "conduit-benign-001")]
        )

    def test_flag_that_also_switches_another_case_fails(self) -> None:
        self.checks.cross_wired["conduit-benign-001"] = "conduit-bug-001"
        rows = self.run_toggle()
        bad = [(r.case, r.active) for r in rows if not r.ok]
        self.assertEqual(bad, [("conduit-bug-001", "conduit-benign-001")])

    def test_check_error_fails(self) -> None:
        self.checks.failing.add("conduit-benign-001")
        rows = self.run_toggle()
        self.assertEqual({r.case for r in rows if not r.ok}, {"conduit-benign-001"})

    def test_case_without_a_check_stops_before_switching(self) -> None:
        self.checks.cases = ["conduit-bug-001"]
        with self.assertRaisesRegex(
            toggle.ToggleError, "no toggle check for conduit-benign-001"
        ):
            self.run_toggle()
        self.assertEqual(self.docker.calls, [])

    def test_only_switches_the_selected_flags_but_runs_every_check(self) -> None:
        rows = self.run_toggle(only=["conduit-bug-001"])
        self.assertEqual({r.active for r in rows}, {None, "conduit-bug-001"})
        self.assertEqual(
            {r.case for r in rows}, {"conduit-benign-001", "conduit-bug-001"}
        )
        self.assertTrue(all(r.ok for r in rows))

    def test_unknown_selected_case(self) -> None:
        with self.assertRaisesRegex(
            toggle.ToggleError, "no case conduit-bug-009 for app conduit"
        ):
            self.run_toggle(only=["conduit-bug-009"])

    def test_app_without_cases(self) -> None:
        self.ws.write_manifest({"schema_version": 1, "cases": {}})
        with self.assertRaisesRegex(toggle.ToggleError, "no cases for app conduit"):
            self.run_toggle()


class ParseResultsTest(unittest.TestCase):
    def test_parses_one_line_per_case(self) -> None:
        output = (
            json.dumps({"case": "a", "state": "clean", "detail": "'x'"})
            + "\n"
            + json.dumps({"case": "b", "error": "TimeoutError: slow"})
            + "\n"
        )
        results = toggle.parse_results(output, ["a", "b"])
        self.assertEqual(results[0], toggle.Result("a", "clean", "'x'", None))
        self.assertEqual(results[1], toggle.Result("b", None, "", "TimeoutError: slow"))

    def test_rejects_other_output(self) -> None:
        with self.assertRaisesRegex(
            toggle.ToggleError, "unexpected output from the checks"
        ):
            toggle.parse_results("Traceback (most recent call last):\n", ["a"])

    def test_rejects_missing_or_reordered_cases(self) -> None:
        output = json.dumps({"case": "b", "state": "clean", "detail": ""}) + "\n"
        with self.assertRaisesRegex(
            toggle.ToggleError, r"reported \['b'\], want \['a'\]"
        ):
            toggle.parse_results(output, ["a"])


class FixturePasswordTest(unittest.TestCase):
    def test_reads_the_seed_build_argument(self) -> None:
        ws = Workspace()
        self.addCleanup(ws.close)
        compose = ws.root / "bench" / "apps" / "conduit" / "compose.yaml"
        compose.write_text("args:\n  CONDUIT_SEED_PASSWORD: fake-fixture-pw\n")
        self.assertEqual(toggle.fixture_password(ws.root, "conduit"), "fake-fixture-pw")

    def test_missing_build_argument(self) -> None:
        ws = Workspace()
        self.addCleanup(ws.close)
        (ws.root / "bench" / "apps" / "conduit" / "compose.yaml").write_text(
            "services: {}\n"
        )
        with self.assertRaisesRegex(toggle.ToggleError, "no CONDUIT_SEED_PASSWORD"):
            toggle.fixture_password(ws.root, "conduit")


class RecordingDockerChecks(toggle.DockerChecks):
    """DockerChecks with the docker CLI replaced by a recorder."""

    def __init__(self, root: Path, app: str) -> None:
        super().__init__(root, app)
        self.commands: list[tuple[list[str], dict[str, str]]] = []

    def _docker(self, args: Sequence[str], env: dict[str, str] | None = None) -> str:
        self.commands.append((list(args), dict(env or {})))
        if args[0] == "build":
            return ""
        case_ids = list(args[args.index("toggle_checks.py") + 1 :])
        if case_ids == ["--list"]:
            return json.dumps(["conduit-bug-001"])
        return "".join(
            json.dumps({"case": c, "state": "clean", "detail": "''"}) + "\n"
            for c in case_ids
        )


class DockerChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        compose = self.ws.root / "bench" / "apps" / "conduit" / "compose.yaml"
        compose.write_text("args:\n  CONDUIT_SEED_PASSWORD: fake-fixture-pw\n")
        self.checks = RecordingDockerChecks(self.ws.root, "conduit")

    def networks(self) -> list[str]:
        runs = [args for args, _ in self.checks.commands if args[0] == "run"]
        return [args[args.index("--network") + 1] for args in runs]

    def test_listing_the_checks_needs_no_app_network(self) -> None:
        # toggle() lists the checks before its first switch creates the network.
        self.assertEqual(self.checks.registered(), ["conduit-bug-001"])
        self.assertEqual(self.networks(), ["none"])

    def test_checks_run_on_the_app_network(self) -> None:
        self.checks.run(["conduit-bug-001"])
        self.assertEqual(self.networks(), ["conduit-bench_default"])

    def test_builds_the_checks_image_once(self) -> None:
        self.checks.registered()
        self.checks.run(["conduit-bug-001"])
        builds = [args for args, _ in self.checks.commands if args[0] == "build"]
        self.assertEqual(len(builds), 1)

    def test_password_travels_in_the_environment_only(self) -> None:
        self.checks.run(["conduit-bug-001"])
        for args, _ in self.checks.commands:
            self.assertNotIn("fake-fixture-pw", " ".join(args))
        run_envs = [env for args, env in self.checks.commands if args[0] == "run"]
        self.assertEqual(run_envs, [{"BENCH_FIXTURE_PASSWORD": "fake-fixture-pw"}])


class CliTest(ToggleTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = toggle.main(
                ["--root", str(self.ws.root), "conduit", *args],
                self.docker,
                self.checks,
            )
        return code, out.getvalue(), err.getvalue()

    def test_all_as_expected(self) -> None:
        code, out, _ = self.run_cli("--cycles", "2")
        self.assertEqual(code, 0)
        self.assertIn("12 checks over 2 cycles, all as expected", out)

    def test_failure_exits_1(self) -> None:
        self.checks.failing.add("conduit-bug-001")
        code, out, _ = self.run_cli("--cycles", "1")
        self.assertEqual(code, 1)
        # bug-001's check fails in all 3 states: the clean app and both flags.
        self.assertIn("3 of 6 checks not as expected", out)

    def test_setup_error_exits_1(self) -> None:
        self.checks.cases = []
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("no toggle check for", err)


if __name__ == "__main__":
    unittest.main()
