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
    capabilities: set[Capability], input_usd: float = 1, output_usd: float = 2
) -> str:
    """A `models` entry."""
    return (
        f"{{ capabilities: [{', '.join(sorted(capabilities))}], "
        f"input_usd_per_mtok: {input_usd}, output_usd_per_mtok: {output_usd} }}"
    )


def lacks(key: str, name: str, what: str, role: str) -> str:
    """The problem for a model declared under `models` that lacks `what`."""
    return (
        f"{key}: '{name}' lacks {what}, which {role} needs "
        "(its models entry wins over the pinned price map)"
    )


def not_priced(key: str, name: str) -> str:
    """The problem for a model that is in neither the map nor `models`."""
    return (
        f"{key}: '{name}' is not a priced model in the pinned price map "
        f"({vendored().version[:7]}): declare it under models with its "
        "capabilities and prices"
    )


def problems_of(tmp_path: Path, text: str) -> list[str]:
    path = write(tmp_path, text)
    with pytest.raises(SpecError) as raised:
        load_config(path)
    return [problem.removeprefix(f"{path}: ") for problem in raised.value.problems]


def lacking(role: ModelRoleName, missing: Capability) -> str:
    return (
        f"roles: {{ {role}: {{ model: acme/m }} }}\n"
        f"models: {{ acme/m: {entry(set(CAPABILITIES) - {missing})} }}\n"
    )


NEEDED = [
    (role, need) for role in ROLES for need in CAPABILITIES if need in NEEDS[role]
]
NOT_NEEDED = [
    (role, need) for role in ROLES for need in CAPABILITIES if need not in NEEDS[role]
]


@pytest.mark.parametrize(("role", "missing"), NEEDED)
def test_each_role_rejects_a_model_that_lacks_a_capability_it_needs(
    tmp_path: Path, role: ModelRoleName, missing: Capability
) -> None:
    assert problems_of(tmp_path, lacking(role, missing)) == [
        lacks(f"roles.{role}.model", "acme/m", LABELS[missing], role)
    ]


@pytest.mark.parametrize(("role", "missing"), NOT_NEEDED)
def test_a_role_accepts_a_model_that_lacks_a_capability_it_does_not_need(
    tmp_path: Path, role: ModelRoleName, missing: Capability
) -> None:
    config = load_config(write(tmp_path, lacking(role, missing)))

    assert resolve_roles(config, vendored())[role].model.name == "acme/m"


def test_every_missing_capability_is_named(tmp_path: Path) -> None:
    text = f"roles: {{ healer: {{ model: acme/m }} }}\nmodels: {{ acme/m: {entry({'tools'})} }}\n"

    assert problems_of(tmp_path, text) == [
        lacks("roles.healer.model", "acme/m", "structured output and vision", "healer")
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
        not_priced("roles.navigator.model", "acme/ghost")
    ]


def test_a_fallback_must_be_priced_and_meet_the_roles_needs(tmp_path: Path) -> None:
    text = (
        "roles:\n"
        "  verifier: { fallback: acme/text-only }\n"
        "  healer: { fallback: acme/ghost }\n"
        f"models: {{ acme/text-only: {entry({'tools', 'structured_output'})} }}\n"
    )

    assert problems_of(tmp_path, text) == [
        lacks("roles.verifier.fallback", "acme/text-only", "vision", "verifier"),
        not_priced("roles.healer.fallback", "acme/ghost"),
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
            f"models: {{ acme/m: {entry(set(CAPABILITIES), 0.5, 1.5)} }}\n",
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
            f"models: {{ claude-sonnet-5-5: {entry(set(CAPABILITIES), 1, 5)} }}\n",
        )
    )

    for resolved in resolve_roles(config, vendored()).values():
        assert resolved.model.name == "claude-sonnet-5-5"
        assert resolved.model.info.source == "config"
        assert resolved.model.info.input_usd_per_mtok == Decimal(1)
        assert resolved.model.info.output_usd_per_mtok == Decimal(5)


def test_a_models_entry_that_shadows_a_map_model_says_it_does(tmp_path: Path) -> None:
    text = f"models: {{ claude-sonnet-5-5: {entry({'tools'})} }}\n"

    assert problems_of(tmp_path, text)[0] == lacks(
        "roles.navigator.model", "claude-sonnet-5-5", "structured output", "navigator"
    )


def test_a_role_problem_does_not_hide_an_undeclared_secret(tmp_path: Path) -> None:
    root = tmp_path / "qa"
    root.mkdir()
    (root / "config.yaml").write_text("roles: { navigator: { model: acme/ghost } }\n")
    (root / "login.spec.md").write_text(
        "---\nid: login\ngoal: Sign in.\npreconditions:\n  start_url: /login\n"
        "  account: { email: a@example.test, password: { secret: TEST_PASSWORD } }\n"
        "expect:\n  - The home page is shown\n---\n"
    )

    with pytest.raises(SpecError) as raised:
        load_project(root)

    problems = raised.value.problems
    assert any("config.yaml: roles.navigator.model:" in problem for problem in problems)
    assert any("TEST_PASSWORD" in problem for problem in problems)


@pytest.mark.parametrize("zero", ["0", "0.0", "-0.0"])
def test_a_declared_rate_of_zero_is_a_plain_zero(tmp_path: Path, zero: str) -> None:
    config = load_config(
        write(
            tmp_path,
            "roles: { navigator: { model: acme/free } }\n"
            "models: { acme/free: { capabilities: [tools, structured_output], "
            f"input_usd_per_mtok: {zero}, output_usd_per_mtok: {zero} }} }}\n",
        )
    )

    info = resolve_roles(config, vendored())["navigator"].model.info

    assert str(info.input_usd_per_mtok) == str(info.cached_input_usd_per_mtok) == "0"
    assert str(info.output_usd_per_mtok) == "0"
