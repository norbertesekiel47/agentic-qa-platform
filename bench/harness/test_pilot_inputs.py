import inspect
import json
import os
import pkgutil
import tempfile
import traceback
import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pilot_inputs
from aqa_core.project import contracts_fingerprint, load_project, parse_compiled
from aqa_core.spec import canonical_hash
from pilot_inputs import PilotInput, ResetRequest, load_pilots, validate_pilot
from pilot_rebinding import apply_rebinding

RESET = ResetRequest("http://127.0.0.1:4100/test-api/reset?fixture=seed")
CONFIG = '{"base_url":"http://127.0.0.1:4100","secrets":{"TEST_PASSWORD":{"origins":["start"],"field":"password"}}}'
SPEC = '{"id":"pilot","goal":"Read an article","preconditions":{"start_url":"/","reset":{"http":"POST /test-api/reset?fixture=seed"},"probes":{"count":"GET /test-api/count"}},"expect":["Title is visible","Body is visible"],"tags":["articles"]}'
COMPILED = """{
"schema_version":1,"spec_id":"pilot","spec_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000",
"compiled_at":"2026-10-01T00:00:00Z","compiled_by":{"mode":"explore","models":{},"price_map":"handwritten-admission-fixture","subject_contracts":"sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"},"confirmed":false,
"browser":{"timezone":"UTC","locale":"en-US","viewport":[1280,800],"device_scale_factor":1,"color_scheme":"light"},
"coverage":{"plan_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000","requires":[],"expectations":[
{"expect_index":0,"subject":"article","claim":"title","assertions":["a0"]},
{"expect_index":1,"subject":"article","claim":"body","assertions":["a1"]}]},
"targets":{"article":{"semantic":"article","locators":[{"testid":"article","scope":{"css":"main"}}]},"password":{"semantic":"password field","locators":[{"label":"Password"}]}},
"probe_baselines":{"count":{"capture_before_seq":1,"json_path":"$.count"}},
"steps":[{"seq":1,"action":"navigate","url":"/","side_effect":false}],
"assertions":[{"id":"a0","expect_index":0,"check":"text_visible","text":"Title"},{"id":"a1","expect_index":1,"check":"text_visible","text":"Body"}]}
"""
CHECKS = json.loads("""[
{"check":"text_visible","text":"Title"},
{"check":"text_in_target","target":"article","text":"Title"},
{"check":"not_visible","target":"article"},
{"check":"url_matches","pattern":"/article$"},
{"check":"network_none","method":"POST","url_pattern":"/orders","status_class":"2xx"},
{"check":"network_seen","method":"GET","url_pattern":"/article","status_class":"2xx"},
{"check":"probe_equals","probe":"count","json_path":"$.count","value":0},
{"check":"probe_equals_baseline","probe":"count"},
{"check":"visible_unoccluded","target":"article","min_size_px":[1,1],"in_viewport":true}]""")
ACCOUNT = {"password": {"secret": "TEST_PASSWORD"}}
SECRET = {"AQA_SECRET_TEST_PASSWORD": "fake-password-value"}
RELOAD = {"seq": 2, "action": "reload", "side_effect": False}


class PilotInputTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.qa = self.root / "qa"
        self.compiled = self.root / "compiled"
        self.qa.mkdir()
        self.compiled.mkdir()
        self.source = self.compiled / "pilot.json"
        self.config = json.loads(CONFIG)
        self.spec = json.loads(SPEC)
        self.data = json.loads(COMPILED)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.save()

    def save(self, *, fresh: bool = True) -> None:
        (self.qa / "config.yaml").write_text(json.dumps(self.config))
        (self.qa / "pilot.spec.md").write_text(f"---\n{json.dumps(self.spec)}\n---\n")
        if fresh:
            project = load_project(self.qa)
            self.data["spec_hash"] = project.specs["pilot"].spec_hash
            self.data["compiled_by"]["subject_contracts"] = contracts_fingerprint(
                project.config, "pilot"
            )
        self.source.write_text(json.dumps(self.data))

    def load(self, selected: tuple[str, ...] = ()) -> tuple[PilotInput, ...]:
        return load_pilots(self.qa, self.compiled, selected)

    def ids(self, selected: tuple[str, ...] = ()) -> tuple[str, ...]:
        return tuple(pilot.spec.frontmatter.id for pilot in self.load(selected))

    def memory(self) -> PilotInput:
        project = load_project(self.qa)
        text = self.source.read_text()
        script = parse_compiled(text, project.config, source=self.source)
        spec = project.specs["pilot"]
        return validate_pilot(spec, project.config, script, source=self.source)

    def refuses(
        self, call: Callable[[], object], category: str, source: Path | None = None
    ) -> None:
        with self.assertRaises(ValueError) as caught:
            call()
        error = caught.exception
        self.assertEqual(str(error), f"{source or self.source}: {category}")
        self.assertIsNone(error.__context__)

    def test_selection_is_complete_and_explicit(self) -> None:
        spec = (self.qa / "pilot.spec.md").read_text()
        (self.qa / "zeta.spec.md").write_text(spec.replace('"pilot"', '"zeta"'))
        project = load_project(self.qa)
        zeta_hash = project.specs["zeta"].spec_hash
        zeta = self.data | {
            "spec_id": "zeta",
            "spec_hash": zeta_hash,
            "compiled_by": {
                **self.data["compiled_by"],
                "subject_contracts": contracts_fingerprint(project.config, "zeta"),
            },
        }
        (self.compiled / "zeta.json").write_text(json.dumps(zeta))
        self.assertEqual(self.ids(), ("pilot", "zeta"))
        self.assertEqual(self.ids(("zeta", "pilot")), ("zeta", "pilot"))
        self.assertEqual(self.ids(("pilot",)), ("pilot",))
        for ids in (("pilot", "pilot"), ("unknown",), ("../outside",)):
            with self.subTest(ids=ids):
                self.refuses(partial(self.load, ids), "invalid selection", self.qa)
        (self.qa / "zeta.spec.md").write_text("---\nid: [\n---\n")
        self.refuses(self.load, "invalid pilot input", self.qa)
        unreadable = partial(load_pilots, self.source, self.compiled, ())
        self.refuses(unreadable, "invalid pilot input", self.source)
        empty = self.root / "empty"
        empty.mkdir()
        (empty / "config.yaml").write_text(CONFIG)
        call = partial(load_pilots, empty, self.compiled, ())
        self.refuses(call, "invalid selection", empty)

    def test_selected_compiled_inputs_must_exist_under_root(self) -> None:
        spec = (self.qa / "pilot.spec.md").read_text()
        (self.qa / "missing.spec.md").write_text(spec.replace('"pilot"', '"missing"'))
        alias = self.root / "alias"
        alias.symlink_to(self.compiled)
        pilots = load_pilots(self.qa, alias, ("pilot",))
        self.assertEqual([pilot.spec.frontmatter.id for pilot in pilots], ["pilot"])
        missing = self.compiled / "missing.json"
        self.refuses(self.load, "invalid compiled input", missing)
        outside = self.root / "outside.json"
        outside.write_bytes(self.source.read_bytes())
        self.source.unlink()
        self.source.symlink_to(outside)
        self.refuses(partial(self.load, ("pilot",)), "invalid compiled input")
        self.assertEqual(outside.read_text(), json.dumps(self.data))

    def test_strict_compiled_refusals_are_preserved(self) -> None:
        original = self.source.read_text()
        cases = (
            ('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
            ('"navigate"', '"unknown-action"'),
            ('"text_visible"', '"unknown-check"'),
            ('"navigate", "url": "/"', '"click", "target": "missing"'),
            ('"id": "a1"', '"id": "a0"'),
            ('"assertions": ["a0"]', '"assertions": ["missing"]'),
            ('"capture_before_seq": 1', '"capture_before_seq": 99'),
        )
        for old, new in cases:
            with self.subTest(change=new):
                self.assertIn(old, original)
                self.source.write_text(original.replace(old, new))
                self.refuses(self.load, "invalid compiled input")
        self.source.write_text(original)
        self.assertEqual(self.ids(), ("pilot",))

    def test_stale_or_wrong_spec_identity_is_refused(self) -> None:
        for key, value in (("spec_id", "other"), ("spec_hash", "sha256:" + "f" * 64)):
            with self.subTest(key=key):
                self.source.write_text(json.dumps(self.data | {key: value}))
                self.refuses(self.load, "invalid pilot input")
        changes = (
            ('"Title is visible"', '"Different claim"'),
            ("fixture=seed", "fixture=other"),
            ('"tags"', '"browser": {"locale": "de-DE"}, "tags"'),
        )
        for old, new in changes:
            with self.subTest(change=new):
                self.spec = json.loads(SPEC)
                self.save()
                self.spec = json.loads(json.dumps(self.spec).replace(old, new))
                self.save(fresh=False)
                self.refuses(self.load, "invalid pilot input")

    def test_tags_and_body_do_not_change_admission(self) -> None:
        self.spec["tags"] = ["changed"]
        self.config["browser"] = {"locale": "de-DE"}
        self.save(fresh=False)
        spec = self.qa / "pilot.spec.md"
        spec.write_text(spec.read_text() + "Human notes changed.\n")
        self.assertEqual(self.load()[0].script.browser.locale, "en-US")

    def test_current_expectations_are_covered_bidirectionally(self) -> None:
        changes: dict[str, Callable[[Any, Any], object]] = {
            "drop": lambda rows, checks: (rows.pop(), checks.pop()),
            "extra": lambda rows, _: rows.append(rows[1] | {"expect_index": 2}),
            "reorder": lambda rows, _: rows.reverse(),
            "label": lambda rows, _: [
                r.update(expect_index=1 - i) for i, r in enumerate(rows)
            ],
            "swap": lambda _, checks: checks[0].update(expect_index=1),
            "orphan": lambda _, checks: checks.append(checks[0] | {"id": "a2"}),
            "twice": lambda rows, _: rows[1]["assertions"].append("a0"),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.data = json.loads(COMPILED)
                change(self.data["coverage"]["expectations"], self.data["assertions"])
                self.save()
                self.refuses(self.memory, "invalid pilot input")
        self.data = json.loads(COMPILED)
        self.data["assertions"].append(self.data["assertions"][0] | {"id": "a2"})
        self.data["coverage"]["expectations"][0]["assertions"] = ["a2", "a0"]
        self.save()
        admitted = self.memory().script.assertions
        self.assertEqual(tuple(a.id for a in admitted), ("a0", "a1", "a2"))

    def test_required_condition_needs_a_satisfying_step(self) -> None:
        required = [{"id": "loaded", "condition": "article loaded"}]
        self.data["coverage"]["requires"] = required
        self.save()
        self.refuses(self.memory, "invalid pilot input")
        self.data["steps"].append(RELOAD | {"satisfies": ["loaded"]})
        self.save()
        self.assertEqual(self.memory().script.steps[1].satisfies, ("loaded",))

    def test_press_requires_one_key(self) -> None:
        press = {"seq": 1, "action": "press", "side_effect": False}
        self.data["steps"] = [press | {"key": "a+b"}]
        self.save()
        self.refuses(self.memory, "invalid pilot input")
        for key in ("Shift+Enter", "Shift++"):
            with self.subTest(key=key):
                self.data["steps"] = [press | {"key": key}]
                self.save()
                step = self.memory().script.steps[0]
                self.assertEqual(step.model_dump()["key"], key)

    def test_other_schema_actions_remain_admitted(self) -> None:
        actions = (
            {"action": "navigate", "url": "/"},
            {"action": "reload"},
            {"action": "click", "target": "article"},
            {"action": "fill", "target": "article", "value": "text"},
            {"action": "select", "target": "article", "option": "one"},
        )
        observed = []
        for action in actions:
            self.data["steps"] = [{"seq": 1, "side_effect": False, **action}]
            self.save()
            observed.append(self.memory().script.steps[0].action)
        self.assertEqual(observed, ["navigate", "reload", "click", "fill", "select"])

    def test_fill_secret_must_be_referenced_by_selected_spec(self) -> None:
        step = {"seq": 1, "action": "fill_secret", "target": "password"}
        self.data["steps"] = [step | {"secret": "TEST_PASSWORD", "side_effect": False}]
        self.save()
        with patch.dict(os.environ, SECRET):
            self.refuses(self.memory, "invalid pilot input")
            self.spec["preconditions"]["account"] = ACCOUNT
            self.save()
            self.assertEqual(self.memory().script.steps[0].action, "fill_secret")

    def test_all_nine_executor_checks_are_admitted(self) -> None:
        observed = []
        for check in CHECKS:
            self.data["assertions"][0] = {"id": "a0", "expect_index": 0, **check}
            self.save()
            observed.append(self.load()[0].script.assertions[0].check)
        expected = """text_visible text_in_target not_visible url_matches network_none
            network_seen probe_equals probe_equals_baseline visible_unoccluded"""
        self.assertEqual(observed, expected.split())

    def test_probe_assertions_require_declared_names(self) -> None:
        baseline = {"count": {"capture_before_seq": 1, "json_path": "$.count"}}
        for check, baselines in ((CHECKS[6], {}), (CHECKS[7], baseline)):
            with self.subTest(check=check["check"]):
                self.data["assertions"][0] = {"id": "a0", "expect_index": 0, **check}
                self.data["probe_baselines"] = baselines
                self.spec["preconditions"]["probes"] = {}
                self.save()
                self.refuses(self.memory, "invalid pilot input")
                self.spec["preconditions"]["probes"] = {"count": "GET /test-api/count"}
                self.save()
                assertion = self.memory().script.assertions[0]
                self.assertEqual(assertion.model_dump()["probe"], "count")

    def test_unused_baseline_must_name_a_declared_probe(self) -> None:
        ghost = {"capture_before_seq": 1, "json_path": "$.count"}
        self.data["probe_baselines"]["ghost"] = ghost
        self.save()
        self.refuses(self.memory, "invalid pilot input")
        self.spec["preconditions"]["probes"]["ghost"] = "GET /test-api/ghost"
        self.save()
        baselines = self.memory().script.probe_baselines
        self.assertEqual(tuple(baselines), ("count", "ghost"))

    def test_visual_requires_in_viewport_true(self) -> None:
        visual = {"id": "a0", "expect_index": 0, **CHECKS[8]}
        self.data["assertions"][0] = visual | {"in_viewport": False}
        self.save()
        self.refuses(self.memory, "invalid pilot input")
        self.data["assertions"][0] = visual
        self.save()
        assertion = self.memory().script.assertions[0].model_dump()
        self.assertEqual(
            (assertion["min_size_px"], assertion["in_viewport"]), ((1, 1), True)
        )

    def test_reset_is_a_post_path_on_the_start_origin(self) -> None:
        invalid = (
            "GET /reset|POST https://evil.test/reset|POST //evil.test/reset|POST /a//b"
            "|POST /a\\b|POST /a b|POST /a\tb|POST /a\nb|POST /a/../b|POST /%2e%2e/b"
            "|POST /a#fragment|POST /café|POST /a\x00b|POST /a/./b|POST /%2E./b|POST"
        )
        for reset in invalid.split("|"):
            with self.subTest(reset=reset):
                self.spec["preconditions"]["reset"] = {"http": reset}
                self.save(fresh=False)
                self.refuses(self.load, "invalid pilot input", self.qa)
        self.spec = json.loads(SPEC)
        self.save()
        self.assertEqual(self.load()[0].reset, RESET)

    def test_side_effect_requires_reset_even_for_one_future_repeat(self) -> None:
        del self.spec["preconditions"]["reset"]
        self.save()
        self.assertIsNone(self.memory().reset)
        posting = RELOAD | {"side_effect": True, "side_effect_basis": "posts"}
        self.data["steps"].append(posting)
        self.save()
        self.refuses(self.memory, "invalid pilot input")
        self.spec = json.loads(SPEC)
        self.save()
        self.assertEqual(self.memory().reset, RESET)

    def test_secret_admission_precedes_resource_use(self) -> None:
        self.assertEqual(self.ids(), ("pilot",))
        self.spec["preconditions"]["account"] = ACCOUNT
        self.save()
        values = ("", " \t", "fak", "fake-\udcff")
        unusable = [{}, *({"AQA_SECRET_TEST_PASSWORD": v} for v in values)]
        logged = ({"DEBUGP": ""}, {"DEBUG": "pw:protocol"}, {"DEBUG": "pw:browser"})
        for env in unusable + [SECRET | each for each in logged]:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.refuses(self.load, "unusable test secret")
                self.assertRaises(pilot_inputs.UnusableSecretError, self.load)
        self.spec["expect"], expect = ["Changed"], self.spec["expect"]
        self.save(fresh=False)
        self.refuses(self.load, "invalid pilot input")
        self.spec["expect"] = expect
        self.save()
        with patch.dict(os.environ, SECRET):
            self.assertEqual(self.ids(), ("pilot",))
            self.config["secrets"]["TEST_PASSWORD"]["origins"] = ["https://evil.test"]
            self.save()
            self.refuses(self.load, "invalid pilot input")

    def test_in_memory_rebound_candidate_uses_same_admission(self) -> None:
        before = self.source.read_bytes()
        project = load_project(self.qa)
        spec = project.specs["pilot"]
        original = parse_compiled(before.decode(), project.config, source=self.source)
        locators = [{"testid": "new-article", "scope": {"css": "main"}}]
        base = canonical_hash(original.model_dump(mode="json"))
        path = "/targets/article/locators"
        operation = {"op": "replace", "path": path, "value": locators}
        text = json.dumps({"base_hash": base, "operations": [operation]})
        candidate = apply_rebinding(original, text, project.config, source=self.source)
        admitted = validate_pilot(spec, project.config, candidate, source=self.source)
        rebound = admitted.script.targets["article"].locators
        self.assertEqual(
            [each.model_dump(exclude_none=True) for each in rebound], locators
        )
        self.assertEqual(self.source.read_bytes(), before)
        malformed = candidate.model_copy(update={"spec_id": "other"})
        call = partial(
            validate_pilot, spec, project.config, malformed, source=self.source
        )
        self.refuses(call, "invalid pilot input")

    def test_inputs_and_errors_exclude_answers_and_secret_values(self) -> None:
        self.spec["preconditions"]["account"] = ACCOUNT
        self.save()
        with patch.dict(os.environ, SECRET):
            admitted = self.load()[0]
        self.assertEqual(admitted.start, "http://127.0.0.1:4100")
        self.assertNotIn("fake-password-value", repr(admitted))
        names = tuple(field.name for field in fields(admitted))
        self.assertEqual(names, ("spec", "config", "script", "start", "reset"))
        for name in names:
            with self.subTest(field=name), self.assertRaises(FrozenInstanceError):
                setattr(admitted, name, None)
        parameters = [
            tuple(inspect.signature(seam).parameters)
            for seam in (load_pilots, validate_pilot)
        ]
        self.assertEqual(parameters[0], ("qa_root", "compiled_dir", "selected_ids"))
        self.assertEqual(parameters[1], ("spec", "config", "script", "source"))
        self.source.write_text('{"fake-sensitive-sentinel": "fake-password-value"}')
        self.refuses(self.load, "invalid compiled input")
        with self.assertRaises(ValueError) as caught:
            self.load()
        shown = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("fake-sensitive-sentinel", shown)
        self.assertNotIn("fake-password-value", shown)

    def test_preflight_never_enters_mutation_seams(self) -> None:
        seams = (
            "flags.build",
            "flags.switch",
            "aqa_runner.runner_requests.runner_request",
            "aqa_runner.browser_session.open_browser_session",
            "aqa_runner.executor.replay",
            "subprocess.Popen",
            "socket.socket",
        )
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def trap(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            raise AssertionError("resource mutation trap")

        for seam in seams:
            self.enterContext(patch(seam, new=trap))
            with self.assertRaisesRegex(AssertionError, "resource mutation trap"):
                pkgutil.resolve_name(seam)()
        self.assertEqual(len(calls), 7)
        calls.clear()
        self.assertEqual(self.load()[0].start, "http://127.0.0.1:4100")
        self.assertEqual(self.memory().script.spec_id, "pilot")
        missing = partial(self.load, ("pilot", "missing"))
        self.refuses(missing, "invalid selection", self.qa)
        self.data["spec_id"] = "other"
        self.save()
        self.refuses(self.memory, "invalid pilot input")
        self.assertEqual(calls, [])
