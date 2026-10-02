"""The model each role uses, with the capabilities the role needs checked
against the pinned price map (TECH_STACK §3; ADR-0007 and its amendments)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, get_args

from aqa_core.config import (
    Effort,
    ModelOverride,
    ModelRole,
    ModelRoleName,
    ProjectConfig,
)
from aqa_core.price_map import Capability, ModelInfo, PriceMap, plain

DEFAULT_PROVIDER: Final = "anthropic"
# TECH_STACK §3: every role starts on Claude Sonnet 5.5.
DEFAULT_MODEL: Final = "claude-sonnet-5-5"

# What a role needs of its model (TECH_STACK §3), in the order a missing one is
# named.
NEEDS: Final[Mapping[ModelRoleName, tuple[Capability, ...]]] = {
    "navigator": ("tools", "structured_output"),
    "verifier": ("structured_output", "vision"),
    "healer": ("tools", "structured_output", "vision"),
    "vision_fallback": ("tools", "vision"),
}


@dataclass(frozen=True, kw_only=True)
class RoutedModel:
    """A model to call: whose it is, its name, what it can do and costs, and the
    pinned price map's version in force."""

    provider: str
    name: str
    info: ModelInfo
    price_map_version: str


@dataclass(frozen=True)
class ResolvedRole:
    """What a role calls: its model, the effort to ask for, and the model to
    fall back to when the first refuses."""

    model: RoutedModel
    effort: Effort | None
    fallback: RoutedModel | None


class RoleError(Exception):
    """The roles in a project config can't be routed. Each problem is the config
    key it concerns and what is wrong."""

    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        super().__init__("\n".join(f"{key}: {problem}" for key, problem in problems))
        self.problems = tuple(problems)


def _exact(price: float) -> Decimal:
    """`price`, a float read from YAML, as the shortest decimal that reads back as
    it: 0.3, not 0.299999999999999988897769753748."""
    return plain(Decimal(str(price)))


def _declared(entry: ModelOverride) -> ModelInfo:
    input_rate = _exact(entry.input_usd_per_mtok)
    # A declared model has no cache-read price: cached input costs the input rate.
    return ModelInfo(
        capabilities=frozenset(entry.capabilities),
        input_usd_per_mtok=input_rate,
        output_usd_per_mtok=_exact(entry.output_usd_per_mtok),
        cached_input_usd_per_mtok=input_rate,
        source="config",
    )


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _routed_or_problem(
    role: ModelRoleName,
    name: str,
    provider: str,
    config: ProjectConfig,
    price_map: PriceMap,
) -> RoutedModel | str:
    """The model `name` for `role`, or what is wrong with it. A `models` entry
    wins over the map (ADR-0007 amendment)."""
    info: ModelInfo | None
    if (entry := config.models.get(name)) is not None:
        info = _declared(entry)
    elif (info := price_map.models.get(name)) is None:
        return (
            f"'{name}' is not a priced model in the pinned price map "
            f"({price_map.version[:7]}): declare it under models with its "
            "capabilities and prices"
        )
    elif info.provider != provider:
        owner = f"provider '{info.provider}'" if info.provider else "no provider"
        return (
            f"'{name}' is listed under {owner} in the pinned price map, not "
            f"'{provider}'"
        )
    elif info.tiered:
        return (
            f"'{name}' has token-threshold prices in the pinned price map, which "
            "cost records don't apply: declare it under models with the flat "
            "rates to record"
        )
    missing = [
        need.replace("_", " ") for need in NEEDS[role] if need not in info.capabilities
    ]
    if missing:
        wins = (
            " (its models entry wins over the pinned price map)"
            if entry is not None
            else ""
        )
        return f"'{name}' lacks {_and(missing)}, which {role} needs{wins}"
    return RoutedModel(
        provider=provider, name=name, info=info, price_map_version=price_map.version
    )


def _resolve_role(
    role: ModelRoleName,
    override: ModelRole,
    config: ProjectConfig,
    price_map: PriceMap,
    problems: list[tuple[str, str]],
) -> ResolvedRole | None:
    provider = override.provider or DEFAULT_PROVIDER
    if provider != DEFAULT_PROVIDER:
        problems.append(
            (
                f"roles.{role}.provider",
                f"'{provider}' has no adapter: only {DEFAULT_PROVIDER} is supported",
            )
        )
        # An adapter that doesn't exist can't say whether a model suits it.
        return None
    model = _routed_or_problem(
        role, override.model or DEFAULT_MODEL, provider, config, price_map
    )
    fallback = (
        None
        if override.fallback is None
        else _routed_or_problem(role, override.fallback, provider, config, price_map)
    )
    for field, result in (("model", model), ("fallback", fallback)):
        if isinstance(result, str):
            problems.append((f"roles.{role}.{field}", result))
    if isinstance(model, str) or isinstance(fallback, str):
        return None
    return ResolvedRole(model, override.effort, fallback)


def resolve_roles(
    config: ProjectConfig, price_map: PriceMap
) -> dict[ModelRoleName, ResolvedRole]:
    """The model each role calls, from the config's overrides and the defaults.
    Raises `RoleError` with every problem at once."""
    resolved: dict[ModelRoleName, ResolvedRole] = {}
    problems: list[tuple[str, str]] = []
    for role in get_args(ModelRoleName):
        override = config.roles.get(role, ModelRole())
        if (
            found := _resolve_role(role, override, config, price_map, problems)
        ) is not None:
            resolved[role] = found
    if problems:
        raise RoleError(problems)
    return resolved
