"""`aqa heal <spec>` (API.md §7; ADR-0024's #247 amendment): `aqa run` of that one
spec, and the words that tell it from `aqa heal apply <proposal_id>`. `test_run.py`
holds the run's own rules. Real Chromium against #240's drift pages, on a scripted
model."""

import json
import re
from pathlib import Path

import pytest
import typer.testing
from aqa_cli import run as run_module
from aqa_cli.main import app as aqa
from aqa_core.project import load_project
from aqa_runner.publication import published_text

from packages.runner.tests.drift_fixtures import (
    DETAILS,
    SHIPPING,
    order_spec,
    page,
    serving,
)
from packages.runner.tests.executor_fixtures import for_spec
from packages.runner.tests.explore_graph_fixtures import Scripted
from packages.runner.tests.heal_fixtures import rebinding


def order_at(root: Path) -> Path:
    """The order spec under `root`, with its script committed as published. The
    spec's path."""
    path = order_spec(root, SHIPPING)
    (root / "config.yaml").write_text("budgets: { resolve_seconds: 2 }\n")
    (root / ".compiled").mkdir()
    spec = load_project(root).specs["order"]
    (root / ".compiled" / "order.json").write_text(
        published_text(for_spec(DETAILS, spec))
    )
    return path


def heal(*args: str) -> typer.testing.Result:
    return typer.testing.CliRunner().invoke(aqa, ["heal", *args])


def plain(shown: str) -> str:
    """What Typer's box said, in one line. Its width wraps the words anywhere, and
    under GITHUB_ACTIONS it forces colour, which splits an option from its dashes."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", shown).replace("│", " ").split())


def test_heal_of_one_spec_is_a_run_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-aqa-heal-tests")
    monkeypatch.setattr(
        run_module, "AnthropicClient", Scripted(rebinding("button", "Show details"))
    )
    spec = order_at(tmp_path / "qa")

    with serving(page("moved")) as app:
        result = heal(str(spec), "--url", app.origin)

    assert result.exit_code == 7, result.output
    [record] = (spec.parent / ".aqa" / "runs").iterdir()
    document = json.loads((record / "replay.json").read_text())
    assert (document["mode"], document["outcome"]) == ("run", "drift_consistent")
    assert f"  heal proposal {document['heal_proposal']['id']}:\n" in result.stderr


def test_heal_without_the_providers_key_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = order_at(tmp_path / "qa")

    result = heal(str(spec), "--url", "http://127.0.0.1:9")

    assert result.exit_code == 5, result.output
    assert "ANTHROPIC_API_KEY: not set" in result.stderr


@pytest.mark.parametrize(
    ("words", "code", "said"),
    [
        pytest.param(["a.spec.md", "b.spec.md"], 2, "give one spec", id="two-specs"),
        pytest.param(["apply"], 2, "apply takes a proposal's id", id="apply-and-no-id"),
        pytest.param(
            ["apply", "ah_0123456789ab", "--url", "http://127.0.0.1:9"],
            2,
            "with no --url",
            id="apply-and-a-url",
        ),
        pytest.param(
            ["apply", "ah_0123456789ab", "--before-screenshots"],
            2,
            "with no --url or --before-screenshots",
            id="apply-and-before-screenshots",
        ),
        # A path, not the word: it reaches the project, which says it holds no such
        # spec, where the word alone is a usage error.
        pytest.param(
            ["./apply"], 5, "apply: not a spec of the project", id="a-file-named-apply"
        ),
    ],
)
def test_heal_takes_one_spec_or_apply_and_an_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    words: list[str],
    code: int,
    said: str,
) -> None:
    order_at(tmp_path / "qa")
    (tmp_path / "qa" / "apply").write_text("a file\n")
    monkeypatch.chdir(tmp_path / "qa")

    result = heal(*words)

    assert result.exit_code == code, result.output
    assert said in plain(result.stderr)


def test_heal_help_shows_both_forms() -> None:
    result = heal("--help")

    assert result.exit_code == 0, result.output
    said = plain(result.stdout)
    assert "SPEC | apply PROPOSAL_ID" in said
    assert "aqa heal <spec>" in said
    assert "aqa heal apply <proposal_id>" in said
    assert "./apply" in said
