"""Each model role gets a model with the capabilities it needs, checked when the
project config loads (TECH_STACK §3; ADR-0007 and its amendments; #40)."""

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
from aqa_core.config import ModelRoleName, ProjectConfig
from aqa_core.model_roles import RoleError, resolve_roles
from aqa_core.price_map import Capability, ModelInfo, PriceMap, vendored
from aqa_core.project import SpecError, load_config, load_project
from aqa_core.strict_yaml import parse

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


def foreign(key: str, name: str, actual: str, wanted: str = "anthropic") -> str:
    """The problem for a map model that belongs to another provider."""
    return (
        f"{key}: '{name}' belongs to provider '{actual}' in the pinned price map, "
        f"not '{wanted}'"
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
    assert all(problem.startswith(f"{path}: ") for problem in raised.value.problems)
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


def small_map(*, extra: dict[str, ModelInfo]) -> PriceMap:
    """The default model, fully capable, and `extra`, as a pinned map would list
    them (every Anthropic model in the vendored map has vision)."""
    return PriceMap(
        version="v" * 40,
        models={"claude-sonnet-5-5": vendored().models["claude-sonnet-5-5"], **extra},
    )


def map_info(capabilities: set[Capability]) -> ModelInfo:
    return ModelInfo(
        capabilities=frozenset(capabilities),
        input_usd_per_mtok=Decimal(1),
        output_usd_per_mtok=Decimal(1),
        cached_input_usd_per_mtok=Decimal(1),
        source="map",
        provider="anthropic",
    )


def role_problems(config_text: str, price_map: PriceMap) -> dict[str, str]:
    config = ProjectConfig.model_validate(parse(config_text))
    with pytest.raises(RoleError) as raised:
        resolve_roles(config, price_map)
    return dict(raised.value.problems)


def test_a_map_model_without_vision_is_rejected_for_the_verifier() -> None:
    price_map = small_map(extra={"text-only": map_info({"tools", "structured_output"})})

    problems = role_problems("roles: { verifier: { model: text-only } }", price_map)

    # No "models entry wins" note: the map is where it came from.
    assert problems == {
        "roles.verifier.model": "'text-only' lacks vision, which verifier needs"
    }


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
    (problem,) = problems_of(tmp_path, "roles: { navigator: { effort: turbo } }\n")

    assert problem.startswith("roles.navigator.effort: ")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_a_role_resolves_with_its_effort_and_a_fallback(
    tmp_path: Path, effort: str
) -> None:
    config = load_config(
        write(
            tmp_path,
            f"roles: {{ healer: {{ effort: {effort}, fallback: claude-opus-5-5 }} }}\n",
        )
    )

    resolved = resolve_roles(config, vendored())

    assert resolved["healer"].effort == effort
    fallback = resolved["healer"].fallback
    assert fallback is not None
    assert (fallback.provider, fallback.name) == ("anthropic", "claude-opus-5-5")
    assert fallback.info == vendored().models["claude-opus-5-5"]
    # The model stays the default, and the other roles are untouched.
    assert resolved["healer"].model.name == "claude-sonnet-5-5"
    assert resolved["navigator"].effort is None
    assert resolved["navigator"].fallback is None


def test_a_fallback_declared_under_models_is_priced_from_the_config(
    tmp_path: Path,
) -> None:
    config = load_config(
        write(
            tmp_path,
            "roles: { navigator: { fallback: acme/m } }\n"
            f"models: {{ acme/m: {entry(set(CAPABILITIES), 3, 4)} }}\n",
        )
    )

    fallback = resolve_roles(config, vendored())["navigator"].fallback

    assert fallback is not None
    assert fallback.info.source == "config"
    assert (fallback.info.input_usd_per_mtok, fallback.info.output_usd_per_mtok) == (
        Decimal(3),
        Decimal(4),
    )


def test_a_provider_without_an_adapter_is_rejected(tmp_path: Path) -> None:
    assert problems_of(tmp_path, "roles: { navigator: { provider: openai } }\n") == [
        "roles.navigator.provider: 'openai' has no adapter: only anthropic is supported"
    ]


def test_every_role_problem_is_reported_at_once(tmp_path: Path) -> None:
    text = (
        "roles:\n"
        "  navigator: { model: acme/ghost }\n"
        "  verifier: { model: acme/text-only, fallback: acme/ghost }\n"
        "  vision_fallback: { provider: openai }\n"
        f"models: {{ acme/text-only: {entry({'tools', 'structured_output'})} }}\n"
    )

    keys = sorted(problem.split(":")[0] for problem in problems_of(tmp_path, text))
    assert keys == [
        "roles.navigator.model",
        "roles.verifier.fallback",
        "roles.verifier.model",
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

    assert lacks(
        "roles.navigator.model", "claude-sonnet-5-5", "structured output", "navigator"
    ) in problems_of(tmp_path, text)


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


def map_model(want: Callable[[ModelInfo], bool]) -> str:
    """A model of the pinned map with all three capabilities that `want`s, chosen
    at test time so a refresh can't break the test."""
    name = next(
        (
            name
            for name, info in sorted(vendored().models.items())
            if info.capabilities == set(CAPABILITIES) and want(info)
        ),
        None,
    )
    assert name is not None, "the pinned map has no such model"
    return name


def test_a_map_model_of_another_provider_is_rejected_as_model_and_as_fallback(
    tmp_path: Path,
) -> None:
    openai = map_model(lambda info: info.provider == "openai" and not info.tiered)
    bedrock = map_model(
        lambda info: info.provider == "bedrock_converse" and not info.tiered
    )
    text = f"roles:\n  navigator: {{ model: {openai} }}\n  healer: {{ fallback: {bedrock} }}\n"

    assert problems_of(tmp_path, text) == [
        foreign("roles.navigator.model", openai, "openai"),
        foreign("roles.healer.fallback", bedrock, "bedrock_converse"),
    ]


def test_a_models_entry_is_trusted_to_belong_to_the_provider(tmp_path: Path) -> None:
    # The map says nothing about a model it lacks, so a project's word stands.
    config = load_config(
        write(
            tmp_path,
            "roles: { navigator: { model: acme/m } }\n"
            f"models: {{ acme/m: {entry(set(CAPABILITIES))} }}\n",
        )
    )

    assert resolve_roles(config, vendored())["navigator"].model.provider == "anthropic"


def test_a_map_model_with_token_threshold_prices_is_rejected_for_a_role(
    tmp_path: Path,
) -> None:
    name = map_model(lambda info: info.provider == "anthropic" and info.tiered)

    assert problems_of(tmp_path, f"roles: {{ healer: {{ model: {name} }} }}\n") == [
        (
            f"roles.healer.model: '{name}' has token-threshold prices in the pinned "
            "price map, which cost records don't apply: declare it under models "
            "with the flat rates to record"
        )
    ]


def test_a_tiered_map_model_declared_under_models_is_priced_flat(
    tmp_path: Path,
) -> None:
    name = map_model(lambda info: info.provider == "anthropic" and info.tiered)
    config = load_config(
        write(
            tmp_path,
            f"roles: {{ healer: {{ model: {name} }} }}\n"
            f"models: {{ {name}: {entry(set(CAPABILITIES), 3, 15)} }}\n",
        )
    )

    info = resolve_roles(config, vendored())["healer"].model.info

    assert (info.source, info.input_usd_per_mtok, info.output_usd_per_mtok) == (
        "config",
        Decimal(3),
        Decimal(15),
    )


def test_an_unsupported_provider_is_the_only_problem_of_its_role(
    tmp_path: Path,
) -> None:
    # An adapter that doesn't exist can't say whether a model suits it.
    text = (
        "roles: { navigator: { provider: openai, model: claude-sonnet-5-5, "
        "fallback: acme/ghost } }\n"
    )

    assert problems_of(tmp_path, text) == [
        "roles.navigator.provider: 'openai' has no adapter: only anthropic is supported"
    ]


def test_every_missing_capability_of_three_is_named() -> None:
    price_map = small_map(extra={"bare": map_info(set())})

    problems = role_problems("roles: { healer: { model: bare } }", price_map)

    assert problems == {
        "roles.healer.model": (
            "'bare' lacks tools, structured output and vision, which healer needs"
        )
    }


def test_role_problems_are_a_role_error_that_prints_them() -> None:
    config = ProjectConfig.model_validate(
        {"roles": {"navigator": {"model": "acme/ghost"}}}
    )

    with pytest.raises(RoleError) as raised:
        resolve_roles(config, vendored())

    assert raised.value.problems == (
        (
            "roles.navigator.model",
            not_priced("roles.navigator.model", "acme/ghost").split(": ", 1)[1],
        ),
    )
    assert "roles.navigator.model: 'acme/ghost'" in str(raised.value)


def assign(target: object, field: str, value: object) -> None:
    setattr(target, field, value)


def test_a_resolved_role_cannot_be_changed() -> None:
    resolved = resolve_roles(ProjectConfig(), vendored())["navigator"]

    with pytest.raises(FrozenInstanceError):
        assign(resolved, "effort", "high")
    with pytest.raises(FrozenInstanceError):
        assign(resolved.model, "name", "other")


@pytest.mark.parametrize("capability", CAPABILITIES)
def test_a_models_entry_accepts_each_capability(
    tmp_path: Path, capability: Capability
) -> None:
    load_config(write(tmp_path, f"models: {{ acme/m: {entry({capability})} }}\n"))


@pytest.mark.parametrize("capability", ["audio", "Tools", "function_calling"])
def test_a_models_entry_rejects_a_capability_it_does_not_know(
    tmp_path: Path, capability: str
) -> None:
    text = (
        f"models: {{ acme/m: {{ capabilities: [{capability}], "
        "input_usd_per_mtok: 1, output_usd_per_mtok: 2 } }\n"
    )

    assert problems_of(tmp_path, text)[0].startswith("models.acme/m.capabilities[0]")


@pytest.mark.parametrize("rate", ["0.1", "0.3", "1.1", "2.675", "0.000001234"])
def test_a_declared_rate_is_the_decimal_that_was_written(
    tmp_path: Path, rate: str
) -> None:
    config = load_config(
        write(
            tmp_path,
            "roles: { navigator: { model: acme/m } }\n"
            "models: { acme/m: { capabilities: [tools, structured_output], "
            f"input_usd_per_mtok: {rate}, output_usd_per_mtok: {rate} }} }}\n",
        )
    )

    info = resolve_roles(config, vendored())["navigator"].model.info

    assert info.input_usd_per_mtok == info.output_usd_per_mtok == Decimal(rate)


def test_a_declared_model_carries_the_pinned_maps_version(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            "roles: { navigator: { model: acme/m } }\n"
            f"models: {{ acme/m: {entry(set(CAPABILITIES))} }}\n",
        )
    )

    resolved = resolve_roles(config, vendored())["navigator"]

    assert resolved.model.price_map_version == vendored().version


def test_a_map_model_that_names_no_provider_is_trusted_to_suit_the_role() -> None:
    nameless = ModelInfo(
        capabilities=frozenset(CAPABILITIES),
        input_usd_per_mtok=Decimal(1),
        output_usd_per_mtok=Decimal(1),
        cached_input_usd_per_mtok=Decimal(1),
        source="map",
    )
    price_map = small_map(extra={"nameless": nameless})
    config = ProjectConfig.model_validate({"roles": {"healer": {"model": "nameless"}}})

    assert resolve_roles(config, price_map)["healer"].model.name == "nameless"
