"""Tests for flags.py: switching a benchmark app between cases (ADR-0022).

Docker is faked here. The real stack is exercised by `flags.py selftest`,
whose output is the evidence in the pull request that added it.

Run: uv run python -m unittest discover -s bench/harness
"""

from __future__ import annotations

import io
import json
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import flags
import manifest
from test_manifest import Workspace, valid_data


class FakeDocker:
    """A Compose stack in memory: `up` recreates containers and sets BENCH_FLAGS."""

    def __init__(self) -> None:
        self.bench_flags = ""
        self.generation = 0
        self.calls: list[tuple[str, tuple[str, ...], dict[str, str]]] = []
        # Knobs that break one part of the stack, to prove verification catches it.
        self.recreate = True
        self.backend_env: str | None = None
        self.html: str | None = None

    def compose(
        self, app_dir: Path, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> str:
        self.calls.append((app_dir.name, tuple(args), dict(env or {})))
        if args[0] == "up":
            self.bench_flags = (env or {})["BENCH_FLAGS"]
            self.generation += 1 if self.recreate else 0
            return ""
        if tuple(args[:2]) == ("ps", "-q"):
            return f"{args[2]}-{self.generation}\n"
        if args[0] == "exec":
            env_value = (
                self.bench_flags if self.backend_env is None else self.backend_env
            )
            return env_value + "\n"
        if args[0] == "port":
            return "127.0.0.1:4100\n"
        if args[0] == "build":
            return ""
        raise AssertionError(f"unexpected compose call {args}")

    def fetch(self, url: str) -> str:
        if self.html is not None:
            return self.html
        ids = [f for f in self.bench_flags.split(",") if f]
        return page(json.dumps(ids, separators=(",", ":")))

    def up_calls(self) -> list[tuple[tuple[str, ...], dict[str, str]]]:
        return [(args, env) for _, args, env in self.calls if args[0] == "up"]


def page(flag_json: str) -> str:
    element = f'<script id="app-flags" type="application/json">{flag_json}</script>'
    return f"<!doctype html><html><head><title>Conduit</title>{element}</head></html>"


class ServedFlagsTest(unittest.TestCase):
    def test_reads_the_flag_list(self) -> None:
        self.assertEqual(flags.served_flags(page('["k3q9","h3k8"]')), ("k3q9", "h3k8"))
        self.assertEqual(flags.served_flags(page("[]")), ())

    def test_missing_element(self) -> None:
        with self.assertRaisesRegex(flags.FlagError, "found 0 flag elements"):
            flags.served_flags("<html><head></head></html>")

    def test_two_elements(self) -> None:
        html = page("[]").replace("</head>", page("[]"))
        with self.assertRaisesRegex(flags.FlagError, "found 2 flag elements"):
            flags.served_flags(html)

    def test_not_a_list_of_strings(self) -> None:
        for bad in ('{"k3q9": true}', "[1]", "not json"):
            with self.subTest(bad=bad), self.assertRaises(flags.FlagError):
                flags.served_flags(page(bad))


class SwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.docker = FakeDocker()
        self.root = Path("/repo")

    def test_recreates_with_the_flags_and_verifies(self) -> None:
        result = flags.switch(self.root, "conduit", ("k3q9",), self.docker)
        self.assertEqual(result.flags, ("k3q9",))
        ups = self.docker.up_calls()
        self.assertEqual(len(ups), 1)
        args, env = ups[0]
        self.assertIn("--force-recreate", args)
        self.assertIn("--wait", args)
        self.assertEqual(env, {"BENCH_FLAGS": "k3q9"})
        self.assertEqual(self.docker.calls[-1][0], "conduit")

    def test_clean_app_is_an_empty_flag_set(self) -> None:
        flags.switch(self.root, "conduit", (), self.docker)
        self.assertEqual(self.docker.up_calls()[0][1], {"BENCH_FLAGS": ""})

    def test_rejects_invalid_flag_ids_before_touching_docker(self) -> None:
        with self.assertRaisesRegex(flags.FlagError, "invalid flag id 'K3Q9'"):
            flags.switch(self.root, "conduit", ("K3Q9",), self.docker)
        self.assertEqual(self.docker.calls, [])

    def test_unknown_app(self) -> None:
        with self.assertRaisesRegex(flags.FlagError, "no stack for app 'shop'"):
            flags.switch(self.root, "shop", (), self.docker)

    def test_backend_environment_mismatch(self) -> None:
        self.docker.backend_env = ""
        with self.assertRaisesRegex(flags.FlagError, "backend has BENCH_FLAGS=''"):
            flags.switch(self.root, "conduit", ("k3q9",), self.docker)

    def test_served_flag_list_mismatch(self) -> None:
        self.docker.html = page("[]")
        with self.assertRaisesRegex(flags.FlagError, r"frontend serves \(\)"):
            flags.switch(self.root, "conduit", ("k3q9",), self.docker)

    def test_containers_must_be_recreated(self) -> None:
        self.docker.recreate = False
        with self.assertRaisesRegex(
            flags.FlagError, "backend container was not recreated"
        ):
            flags.switch(self.root, "conduit", ("k3q9",), self.docker)

    def test_build_builds_the_app_images(self) -> None:
        flags.build(self.root, "conduit", self.docker)
        self.assertEqual(self.docker.calls, [("conduit", ("build", "--quiet"), {})])

    def test_build_unknown_app(self) -> None:
        with self.assertRaisesRegex(flags.FlagError, "no stack for app 'shop'"):
            flags.build(self.root, "shop", self.docker)
        self.assertEqual(self.docker.calls, [])


class ManifestBackedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.ws.write_manifest(valid_data())
        self.docker = FakeDocker()

    def test_set_case_switches_to_its_flag(self) -> None:
        result = flags.set_case(self.ws.root, "conduit-benign-001", self.docker)
        self.assertEqual(result.flags, ("h3k8",))

    def test_set_case_refuses_unknown_case(self) -> None:
        with self.assertRaisesRegex(flags.FlagError, "no case 'conduit-bug-999'"):
            flags.set_case(self.ws.root, "conduit-bug-999", self.docker)
        self.assertEqual(self.docker.calls, [])

    def test_set_case_refuses_an_invalid_manifest(self) -> None:
        data = valid_data()
        data["cases"]["conduit-bug-001"]["split"] = "train"
        self.ws.write_manifest(data)
        with self.assertRaises(manifest.ManifestError):
            flags.set_case(self.ws.root, "conduit-bug-001", self.docker)

    def test_selftest_alternates_and_ends_clean(self) -> None:
        results = flags.selftest(self.ws.root, "conduit", 3, self.docker)
        self.assertEqual(
            [r.flags for r in results],
            [(flags.SELFTEST,), (), (flags.SELFTEST,), (), (flags.SELFTEST,), ()],
        )
        self.assertEqual(self.docker.bench_flags, "")

    def test_show_names_the_active_case(self) -> None:
        flags.set_case(self.ws.root, "conduit-bug-001", self.docker)
        self.assertEqual(
            flags.show(self.ws.root, "conduit", self.docker),
            "conduit-bug-001 (flag k3q9)",
        )
        flags.switch(self.ws.root, "conduit", (), self.docker)
        self.assertEqual(
            flags.show(self.ws.root, "conduit", self.docker), "clean (no flags)"
        )

    def test_show_reports_tiers_that_disagree(self) -> None:
        flags.set_case(self.ws.root, "conduit-bug-001", self.docker)
        self.docker.html = page("[]")
        with self.assertRaisesRegex(flags.FlagError, "tiers disagree"):
            flags.show(self.ws.root, "conduit", self.docker)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.ws.write_manifest(valid_data())
        self.docker = FakeDocker()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = flags.main(["--root", str(self.ws.root), *args], self.docker)
        return code, out.getvalue(), err.getvalue()

    def test_set(self) -> None:
        code, out, _ = self.run_cli("set", "conduit-bug-001")
        self.assertEqual(code, 0)
        self.assertIn("conduit-bug-001", out)
        self.assertIn("k3q9", out)

    def test_set_unknown_case_fails(self) -> None:
        code, _, err = self.run_cli("set", "conduit-bug-999")
        self.assertEqual(code, 1)
        self.assertIn("no case 'conduit-bug-999'", err)

    def test_clean(self) -> None:
        code, out, _ = self.run_cli("clean", "conduit")
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_selftest_summary(self) -> None:
        code, out, _ = self.run_cli("selftest", "conduit", "--cycles", "2")
        self.assertEqual(code, 0)
        self.assertIn("4 switches, all verified", out)


if __name__ == "__main__":
    unittest.main()
