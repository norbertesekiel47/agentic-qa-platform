import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_compiled, parse_compiled

from packages.core.tests.test_compiled import data_models_example
from packages.core.tests.test_load_compiled import DECLARES_TEST_PASSWORD


def test_parsing_json_reads_no_file_and_keeps_the_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "missing.json"
    script = parse_compiled(
        data_models_example(), DECLARES_TEST_PASSWORD, source=source
    )
    assert script.spec_id == "checkout-expired-card"
    assert script.compiled_at == datetime(2026, 10, 12, 14, 3, 22, tzinfo=UTC)
    assert not source.exists()


@pytest.mark.parametrize(
    ("old", "new", "config", "problem"),
    [
        (
            '"confirmed": true',
            '"confirmed": true, "confirmed": false',
            DECLARES_TEST_PASSWORD,
            "confirmed: the key appears more than once, and a reader keeps only its last value, so write it once",
        ),
        (
            '"target": "sign_in"',
            '"target": "missing"',
            DECLARES_TEST_PASSWORD,
            "steps[3].target: 'missing' names no target in targets",
        ),
        (
            '"confirmed": true',
            '"confirmed": true',
            ProjectConfig(),
            "steps[2].secret: secret TEST_PASSWORD is not declared in the project config's secrets",
        ),
    ],
)
def test_both_entry_points_report_literal_problems(
    tmp_path: Path, old: str, new: str, config: ProjectConfig, problem: str
) -> None:
    source = tmp_path / "script.json"
    text = data_models_example().replace(old, new)
    source.write_text(text)
    with pytest.raises(SpecError) as parsed:
        parse_compiled(text, config, source=source)
    with pytest.raises(SpecError) as loaded:
        load_compiled(source, config)
    assert parsed.value.problems == (f"{source}: {problem}",)
    assert loaded.value.problems == (f"{source}: {problem}",)


@pytest.mark.parametrize(("target", "index"), [("cart_items", 0), ("pay_button", 1)])
def test_parser_and_loader_refuse_each_unscoped_negative_locator(
    tmp_path: Path, target: str, index: int
) -> None:
    data = json.loads(data_models_example())
    data["assertions"].append(
        {"id": "a7", "expect_index": 2, "check": "not_visible", "target": target}
    )
    text, source = json.dumps(data), tmp_path / "script.json"
    source.write_text(text)
    readers: tuple[Callable[[], CompiledScript], ...] = (
        lambda: parse_compiled(text, DECLARES_TEST_PASSWORD, source=source),
        lambda: load_compiled(source, DECLARES_TEST_PASSWORD),
    )
    for read in readers:
        with pytest.raises(SpecError) as refused:
            read()
        assert refused.value.problems == (
            (
                f"{source}: targets.{target}.locators[{index}]: has no scope, but "
                "assertions[6] (a7) checks that this target is not visible: without "
                "a scope, its absence passes on any page that lacks a match, such "
                "as a wrong page or a 404"
            ),
        )
