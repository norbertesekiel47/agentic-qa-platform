import json
import tempfile
import unittest
from pathlib import Path

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_compiled, parse_compiled
from aqa_core.spec import canonical_hash
from pilot_rebinding import apply_rebinding

CONFIG = ProjectConfig.model_validate(
    {"secrets": {"TEST_PASSWORD": {"origins": ["start"], "field": "password"}}}
)


def example() -> CompiledScript:
    section = (
        (Path(__file__).resolve().parents[2] / "DATA_MODEL.md")
        .read_text()
        .split("\n## 7. ")[1]
    )
    data = json.loads(section.split("```json\n")[1].split("\n```")[0])
    data["targets"]["payment_error"] = {
        "semantic": "the payment error",
        "locators": [{"testid": "payment-error", "scope": {"css": "app-payment-step"}}],
    }
    data["assertions"].append(
        {
            "id": "a7",
            "expect_index": 0,
            "check": "not_visible",
            "target": "payment_error",
        }
    )
    data["assertions"][3]["target"] = "payment_error"
    return parse_compiled(json.dumps(data), CONFIG, source=Path("fixture.json"))


def patch(script: CompiledScript, path: str, value: object) -> str:
    return json.dumps(
        {
            "base_hash": canonical_hash(script.model_dump(mode="json")),
            "operations": [{"op": "replace", "path": path, "value": value}],
        }
    )


class RebindingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "compiled.json"
        self.source.write_text(example().model_dump_json())
        self.original = load_compiled(self.source, CONFIG)

    def apply(self, text: str) -> CompiledScript:
        return apply_rebinding(self.original, text, CONFIG, source=self.source)

    def test_rebinding_cannot_remove_primary_or_fallback_scope(self) -> None:
        before = self.source.read_bytes()
        for unscoped in (0, 1):
            with self.subTest(unscoped=unscoped):
                locators = [
                    {"testid": "new-error", "scope": {"css": "app-payment-step"}},
                    {"css": ".new-error", "scope": {"css": "app-payment-step"}},
                ]
                del locators[unscoped]["scope"]
                with self.assertRaisesRegex(
                    SpecError, rf"payment_error.locators\[{unscoped}\]: has no scope"
                ):
                    self.apply(
                        patch(
                            self.original, "/targets/payment_error/locators", locators
                        )
                    )
                self.assertEqual(self.source.read_bytes(), before)
                self.assertEqual(self.original.model_dump_json().encode(), before)

    def test_rebinding_refuses_every_non_locator_path(self) -> None:
        paths = (
            "",
            "/",
            "/assertions",
            "/targets/payment_error/semantic",
            "/steps/0/side_effect",
            "/steps",
            "/browser",
            "/coverage",
            "/spec_hash",
            "/invariants",
            "/probe_baselines",
            "/compiled_at",
            "/targets/unknown/locators",
            "/targets/payment_error~2/locators",
            "/targets/payment_error/locators/0",
        )
        for path in paths:
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, "known target"),
            ):
                self.apply(patch(self.original, path, [{"css": ".x"}]))

    def test_rebinding_refuses_bad_documents(self) -> None:
        values = [{"css": ".x", "scope": {"css": "app-payment-step"}}]
        valid = patch(self.original, "/targets/payment_error/locators", values)
        self.assertEqual(
            self.apply(valid)
            .targets["payment_error"]
            .locators[0]
            .model_dump(exclude_none=True),
            values[0],
        )
        problems = {
            "stale": "base hash is stale",
            "duplicate": "exactly once",
            "key": "op",
            "extra": "Extra inputs are not permitted",
            "add": "Input should be 'replace'",
            "remove": "Input should be 'replace'",
            "move": "Input should be 'replace'",
            "copy": "Input should be 'replace'",
            "empty": "must list at least 1 item",
            "invalid": "string_too_short",
        }
        for change, problem in problems.items():
            data = json.loads(valid)
            operation = data["operations"][0]
            if change == "stale":
                data["base_hash"] = "sha256:" + "0" * 64
            elif change == "duplicate":
                data["operations"].append(operation)
            elif change == "extra":
                operation["from"] = "/assertions"
            elif change in ("empty", "invalid"):
                operation["value"] = (
                    [] if change == "empty" else [{"css": "", "unknown": 1}]
                )
            elif change != "key":
                operation["op"] = change
            text = (
                json.dumps(data)
                if change != "key"
                else valid.replace(
                    '"op": "replace"', '"op": "replace", "op": "replace"'
                )
            )
            with (
                self.subTest(change=change),
                self.assertRaisesRegex((ValueError, SpecError), problem),
            ):
                self.apply(text)

    def test_valid_rebinding_and_explicit_noop_preserve_the_original(self) -> None:
        before = self.original.model_dump_json()
        positive_target = "pay_button"
        self.assertEqual(
            [
                (assertion.id, assertion.check)
                for assertion in self.original.assertions
                if getattr(assertion, "target", None) == positive_target
            ],
            [("a6", "visible_unoccluded")],
        )
        replacements: dict[str, list[dict[str, object]]] = {
            "payment_error": [
                {"testid": "new-error", "scope": {"css": "app-payment-step"}}
            ],
            positive_target: [{"css": ".pay"}],
        }
        for target, values in replacements.items():
            with self.subTest(target=target):
                changed = self.apply(
                    patch(self.original, f"/targets/{target}/locators", values)
                )
                self.assertEqual(
                    changed.targets[target].locators[0].model_dump(exclude_none=True),
                    values[0],
                )
                expected = self.original.model_dump(mode="json")
                expected["targets"][target]["locators"] = changed.targets[
                    target
                ].model_dump(mode="json")["locators"]
                self.assertEqual(changed.model_dump(mode="json"), expected)
                noop = json.dumps(
                    {
                        "base_hash": canonical_hash(
                            self.original.model_dump(mode="json")
                        ),
                        "operations": [],
                    }
                )
                self.assertEqual(self.apply(noop), self.original)
                self.assertEqual(self.original.model_dump_json(), before)
                self.assertEqual(self.source.read_text(), before)

    def test_pointer_escapes_name_one_existing_target(self) -> None:
        for target, encoded in (
            ("cart/~items", "cart~1~0items"),
            ("cart~1items", "cart~01items"),
        ):
            with self.subTest(target=target):
                data = example().model_dump(mode="json")
                data["targets"][target] = data["targets"].pop("cart_items")
                self.original = parse_compiled(
                    json.dumps(data), CONFIG, source=self.source
                )
                changed = self.apply(
                    patch(
                        self.original,
                        f"/targets/{encoded}/locators",
                        [{"css": ".cart"}],
                    )
                )
                self.assertEqual(
                    changed.targets[target].locators[0].model_dump(exclude_none=True),
                    {"css": ".cart"},
                )
                self.assertEqual(tuple(changed.targets), tuple(self.original.targets))
