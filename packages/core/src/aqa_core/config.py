"""The project config, `qa/config.yaml` (DATA_MODEL §9). Every key is
optional, and unknown keys are errors."""

from typing import Annotated, Literal, get_args

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    PlainValidator,
    StrictInt,
    StrictStr,
)

from aqa_core.browser import BrowserOverrides
from aqa_core.price_map import Capability
from aqa_core.schema import (
    AriaRole,
    AtLeastOne,
    DistinctListOf,
    Host,
    NonEmpty,
    Origin,
    PositiveNumber,
    SecretName,
    StrictModel,
    parse_origin,
)

_PositiveCount = Annotated[StrictInt, Field(gt=0)]
_Price = Annotated[float, Field(ge=0, allow_inf_nan=False)]


# The levels ChatAnthropic accepts for a model's effort.
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelRole(StrictModel):
    """Overrides for one model role's defaults (TECH_STACK §3). Only their
    shape is checked here."""

    provider: NonEmpty | None = None
    model: NonEmpty | None = None
    effort: Effort | None = None
    fallback: NonEmpty | None = None


ModelRoleName = Literal["navigator", "verifier", "healer", "vision_fallback"]


class ModelOverride(StrictModel):
    """What a project says about a model: what it can do and what it costs, for
    one the pinned price map lacks or to replace the map's entry for it
    (ADR-0007 amendment)."""

    capabilities: Annotated[DistinctListOf[Capability], AtLeastOne]
    input_usd_per_mtok: _Price
    output_usd_per_mtok: _Price


class Egress(StrictModel):
    """ADR-0026's hosts beyond the allowed origins."""

    subresource_hosts: DistinctListOf[Host] = ()
    expected_blocked: DistinctListOf[Host] = ()
    private_origins: DistinctListOf[Origin] = ()


def _aria_role(value: object) -> object:
    # Checked first, so a typo gets one line rather than every role listed.
    if value not in get_args(AriaRole):
        raise ValueError(
            f"'{value}' is not an ARIA role: write a role Playwright's get_by_role "
            "takes, such as textbox"
        )
    return value


class RoleField(StrictModel):
    """A field found by its role, which no field could have unless it is
    one Playwright's get_by_role takes, and its accessible name."""

    role: Annotated[AriaRole, BeforeValidator(_aria_role)]
    name: NonEmpty


def _field(value: object) -> Literal["password"] | RoleField:
    if value == "password":
        return "password"
    if isinstance(value, dict):
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
        DistinctListOf[Annotated[StrictStr, AfterValidator(_binding_origin)]],
        AtLeastOne,
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
