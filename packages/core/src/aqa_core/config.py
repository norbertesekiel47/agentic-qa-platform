"""The project config, `qa/config.yaml` (DATA_MODEL §9). Every key is
optional, and unknown keys are errors."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, PlainValidator, StrictInt, StrictStr

from aqa_core.browser import BrowserOverrides
from aqa_core.schema import (
    Host,
    Items,
    NonEmpty,
    NotEmpty,
    Origin,
    PositiveNumber,
    SecretName,
    StrictModel,
    parse_origin,
)

_PositiveCount = Annotated[StrictInt, Field(gt=0)]
_Price = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ModelRole(StrictModel):
    """Overrides for one model role's defaults (TECH_STACK §3). Only their
    shape is checked here."""

    provider: NonEmpty | None = None
    model: NonEmpty | None = None
    effort: NonEmpty | None = None
    fallback: NonEmpty | None = None


ModelRoleName = Literal["navigator", "verifier", "healer", "vision_fallback"]


class ModelOverride(StrictModel):
    """A model missing from the pinned price map: what it can do and what it
    costs (ADR-0007 amendment)."""

    capabilities: Annotated[
        Items[Literal["tools", "structured_output", "vision"]], NotEmpty
    ]
    input_usd_per_mtok: _Price
    output_usd_per_mtok: _Price


class Egress(StrictModel):
    """ADR-0026's hosts beyond the allowed origins."""

    subresource_hosts: Items[Host] = ()
    expected_blocked: Items[Host] = ()
    private_origins: Items[Origin] = ()


class RoleField(StrictModel):
    """A field found by its role and accessible name."""

    role: NonEmpty
    name: NonEmpty


def _field(value: object) -> Literal["password"] | RoleField:
    if value == "password":
        return "password"
    if not isinstance(value, str):
        return RoleField.model_validate(value)
    raise ValueError(
        f"'{value}' is not a field: write password, or a role and an accessible name"
    )


def _binding_origin(entry: str) -> str:
    return entry if entry == "start" else parse_origin(entry)


class SecretBinding(StrictModel):
    """Where the browser may fill a test secret (ADR-0026). `start` is the
    run's start origin; any other origin must also be one the run allows."""

    origins: Annotated[
        Items[Annotated[StrictStr, AfterValidator(_binding_origin)]], NotEmpty
    ]
    # `password` is an <input type="password">.
    field: Annotated[Literal["password"] | RoleField, PlainValidator(_field)]


class Budgets(StrictModel):
    """Per explore run; exceeding one is `gave_up` (ADR-0024)."""

    attempts: _PositiveCount = 3
    actions_per_attempt: _PositiveCount = 40
    model_usd: PositiveNumber = 3
    minutes: PositiveNumber = 15
    resolve_seconds: PositiveNumber = 10


class ProjectConfig(StrictModel):
    """The settings every spec under one spec root shares."""

    # The start origin when the invocation gives none.
    base_url: Origin | None = None
    roles: dict[ModelRoleName, ModelRole] = Field(default_factory=dict)
    browser: BrowserOverrides = BrowserOverrides()
    egress: Egress = Egress()
    secrets: dict[SecretName, SecretBinding] = Field(default_factory=dict)
    models: dict[NonEmpty, ModelOverride] = Field(default_factory=dict)
    budgets: Budgets = Budgets()
