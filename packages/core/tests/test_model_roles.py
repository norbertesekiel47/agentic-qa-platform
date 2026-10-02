"""Each model role gets a model with the capabilities it needs, checked when the
project config loads (TECH_STACK §3; ADR-0007 and its amendments; #40)."""

from decimal import Decimal
from pathlib import Path

import pytest
from aqa_core.config import ModelRoleName
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import Capability, vendored
from aqa_core.project import SpecError, load_config, load_project

ROLES: list[ModelRoleName] = ["navigator", "verifier", "healer", "vision_fallback"]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


@pytest.mark.parametrize("role", ROLES)
def test_an_empty_config_routes_every_role_to_claude_sonnet_5_5(
    tmp_path: Path, role: ModelRoleName
) -> None:
    config = load_config(write(tmp_path, ""))

    resolved = resolve_roles(config, vendored())[role]

    assert resolved.model.provider == "anthropic"
    assert resolved.model.name == "claude-sonnet-5-5"
    assert resolved.model.info.source == "map"
    # TECH_STACK §3's table: $2 in, $10 out. A refresh that changes these must
    # change that table too. Cached input is $0.20.
    assert resolved.model.info.input_usd_per_mtok == Decimal(2)
    assert resolved.model.info.output_usd_per_mtok == Decimal(10)
    assert resolved.model.info.cached_input_usd_per_mtok == Decimal("0.2")
    assert resolved.effort is None
    assert resolved.fallback is None


# What TECH_STACK §3 says each role needs, and how a missing one reads.
NEEDS: dict[ModelRoleName, set[Capability]] = {
    "navigator": {"tools", "structured_output"},
    "verifier": {"vision", "structured_output"},
    "healer": {"tools", "vision", "structured_output"},
    "vision_fallback": {"vision", "tools"},
}
CAPABILITIES: list[Capability] = ["tools", "structured_output", "vision"]
LABELS: dict[Capability, str] = {
    "tools": "tools",
    "structured_output": "structured output",
    "vision": "vision",
}


def entry(
    capabilities: set[Capability], prices: str = "1, output_usd_per_mtok: 2"
) -> str:
    """A `models` entry."""
    return (
        f"{{ capabilities: [{', '.join(sorted(capabilities))}], "
        f"input_usd_per_mtok: {prices} }}"
    )


def problems_of(tmp_path: Path, text: str) -> list[str]:
    path = write(tmp_path, text)
    with pytest.raises(SpecError) as raised:
        load_config(path)
    return [problem.removeprefix(f"{path}: ") for problem in raised.value.problems]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("missing", CAPABILITIES)
def test_each_role_rejects_a_model_that_lacks_a_capability_it_needs(
    tmp_path: Path, role: ModelRoleName, missing: Capability
) -> None:
    text = (
        f"roles: {{ {role}: {{ model: acme/m }} }}\n"
        f"models: {{ acme/m: {entry(set(CAPABILITIES) - {missing})} }}\n"
    )

    if missing not in NEEDS[role]:
        load_config(write(tmp_path, text))
        return
    assert problems_of(tmp_path, text) == [
        f"roles.{role}.model: 'acme/m' lacks {LABELS[missing]}, which {role} needs"
    ]


def test_every_missing_capability_is_named(tmp_path: Path) -> None:
    text = f"roles: {{ healer: {{ model: acme/m }} }}\nmodels: {{ acme/m: {entry({'tools'})} }}\n"

    assert problems_of(tmp_path, text) == [
        "roles.healer.model: 'acme/m' lacks structured output and vision, which healer needs"
    ]


def test_a_map_model_without_vision_is_rejected_for_the_verifier(
    tmp_path: Path,
) -> None:
    # Chosen from the pinned map at test time, so a refresh can't break it.
    name = next(
        name
        for name, info in sorted(vendored().models.items())
        if info.capabilities == {"tools", "structured_output"}
    )

    assert problems_of(tmp_path, f"roles: {{ verifier: {{ model: {name} }} }}\n") == [
        f"roles.verifier.model: '{name}' lacks vision, which verifier needs"
    ]


def test_a_model_missing_from_the_map_with_no_models_entry_is_rejected(
    tmp_path: Path,
) -> None:
    assert problems_of(tmp_path, "roles: { navigator: { model: acme/ghost } }\n") == [
        (
            f"roles.navigator.model: 'acme/ghost' is not in the pinned price map "
            f"({vendored().version[:7]}): declare it under models with its "
            "capabilities and prices"
        )
    ]


def test_a_fallback_must_be_priced_and_meet_the_roles_needs(tmp_path: Path) -> None:
    text = (
        "roles:\n"
        "  verifier: { fallback: acme/text-only }\n"
        "  healer: { fallback: acme/ghost }\n"
        f"models: {{ acme/text-only: {entry({'tools', 'structured_output'})} }}\n"
    )

    assert problems_of(tmp_path, text) == [
        "roles.verifier.fallback: 'acme/text-only' lacks vision, which verifier needs",
        (
            f"roles.healer.fallback: 'acme/ghost' is not in the pinned price map "
            f"({vendored().version[:7]}): declare it under models with its "
            "capabilities and prices"
        ),
    ]


def test_an_unknown_effort_is_rejected(tmp_path: Path) -> None:
    assert problems_of(tmp_path, "roles: { navigator: { effort: turbo } }\n") == [
        "roles.navigator.effort: Input should be 'low', 'medium', 'high', 'xhigh' or 'max'"
    ]


def test_a_provider_without_an_adapter_is_rejected(tmp_path: Path) -> None:
    assert problems_of(tmp_path, "roles: { navigator: { provider: openai } }\n") == [
        "roles.navigator.provider: 'openai' has no adapter yet: M1 supports anthropic"
    ]


def test_every_role_problem_is_reported_at_once(tmp_path: Path) -> None:
    text = (
        "roles:\n"
        "  navigator: { model: acme/ghost }\n"
        "  verifier: { model: acme/text-only, fallback: acme/ghost }\n"
        "  vision_fallback: { provider: openai }\n"
        f"models: {{ acme/text-only: {entry({'tools', 'structured_output'})} }}\n"
    )

    assert [problem.split(":")[0] for problem in problems_of(tmp_path, text)] == [
        "roles.navigator.model",
        "roles.verifier.model",
        "roles.verifier.fallback",
        "roles.vision_fallback.provider",
    ]


def test_a_role_problem_is_reported_with_the_specs_problems(tmp_path: Path) -> None:
    root = tmp_path / "qa"
    root.mkdir()
    (root / "config.yaml").write_text("roles: { navigator: { model: acme/ghost } }\n")
    (root / "login.spec.md").write_text("---\nid: login\n---\n")

    with pytest.raises(SpecError) as raised:
        load_project(root)

    problems = raised.value.problems
    assert any("config.yaml: roles.navigator.model:" in problem for problem in problems)
    assert any("login.spec.md: goal: missing key" in problem for problem in problems)


def test_a_models_entry_serves_a_model_the_map_lacks(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            "roles: { navigator: { model: acme/m } }\n"
            f"models: {{ acme/m: {entry(set(CAPABILITIES), '0.5, output_usd_per_mtok: 1.5')} }}\n",
        )
    )

    model = resolve_roles(config, vendored())["navigator"].model

    assert model.name == "acme/m"
    assert model.info.source == "config"
    assert model.info.input_usd_per_mtok == Decimal("0.5")
    assert model.info.output_usd_per_mtok == Decimal("1.5")
    # A config entry has no cache-read price: cached input costs the input rate.
    assert model.info.cached_input_usd_per_mtok == Decimal("0.5")


def test_a_models_entry_wins_over_the_map(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            f"models: {{ claude-sonnet-5-5: {entry(set(CAPABILITIES), '1, output_usd_per_mtok: 5')} }}\n",
        )
    )

    for resolved in resolve_roles(config, vendored()).values():
        assert resolved.model.name == "claude-sonnet-5-5"
        assert resolved.model.info.source == "config"
        assert resolved.model.info.input_usd_per_mtok == Decimal(1)
        assert resolved.model.info.output_usd_per_mtok == Decimal(5)
