"""A spec's frontmatter (DATA_MODEL §6), and the spec as read from its file."""

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self, TypedDict, cast

from pydantic import (
    Field,
    PlainValidator,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

from aqa_core.browser import BrowserOverrides
from aqa_core.config import SecretBinding
from aqa_core.schema import (
    AtLeastOne,
    DistinctListOf,
    ListOf,
    NonEmpty,
    Origin,
    SecretName,
    StartPath,
    StrictModel,
)


class SpecContext(TypedDict):
    """What checking a spec needs beyond its frontmatter. Checked during
    validation, so every problem is reported together."""

    # The spec's file name without `.spec.md`, which its id must equal.
    file_id: str
    # The secrets the project config declares. None skips the check: only
    # load_project passes it, for a config that is itself invalid, and then the
    # load fails whatever the specs hold.
    declared_secrets: frozenset[str] | None


def _context(info: ValidationInfo) -> SpecContext:
    if info.context is None:
        raise TypeError("a spec is validated with a SpecContext: use load_spec")
    return cast(SpecContext, info.context)


class SecretReference(StrictModel):
    """`{ secret: NAME }`: a test secret the project config declares."""

    secret: SecretName

    @field_validator("secret")
    @classmethod
    def _declared(cls, name: str, info: ValidationInfo) -> str:
        declared = _context(info)["declared_secrets"]
        if declared is not None and name not in declared:
            raise ValueError(
                f"secret {name} is not declared in the project config's secrets"
            )
        return name


def _credential(value: object, info: ValidationInfo) -> str | SecretReference:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"secret"}:
        return SecretReference.model_validate(value, context=info.context)
    # Never the value, nor a key: a password typed where a reference goes
    # would otherwise land in a CI log.
    raise ValueError("must be a string, or { secret: NAME }")


_Credential = Annotated[str | SecretReference, PlainValidator(_credential)]


class Account(StrictModel):
    """The test account a spec signs in with, as the agent may read it."""

    email: _Credential | None = None
    password: _Credential | None = None


class Reset(StrictModel):
    """The reset hook, called before every attempt (ADR-0024)."""

    http: NonEmpty


class Preconditions(StrictModel):
    """Where a run starts and the state it starts from."""

    start_url: StartPath
    account: Account | None = None
    reset: Reset | None = None
    # Read-only GET endpoints, by name (DATA_MODEL §6).
    probes: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)


class Expectation(StrictModel):
    """One expectation: a plain string, or `{ text, visual }`."""

    text: NonEmpty
    visual: Literal["deterministic", "model"] = "deterministic"

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, data: object) -> object:
        return {"text": data} if isinstance(data, str) else data

    @field_validator("visual")
    @classmethod
    def _deterministic_in_m1(cls, visual: str) -> str:
        if visual == "model":
            raise ValueError(
                "visual: model is a spec error in M1: a model-assisted check can't pass "
                "the strict confirmation replay that ends explore (ADR-0024)"
            )
        return visual


InvariantName = Literal["console_errors", "js_exceptions", "http_5xx", "broken_images"]


class Invariants(StrictModel):
    """Every invariant applies unless the spec turns it off (DATA_MODEL §6)."""

    inherit: StrictBool = True
    disable: DistinctListOf[InvariantName] = ()

    @model_validator(mode="after")
    def _disable_needs_inherit(self) -> Self:
        if not self.inherit and self.disable:
            raise ValueError(
                "disable has no effect when inherit is false: every invariant is already off"
            )
        return self


class SpecFrontmatter(StrictModel):
    """Everything a run reads from a spec; the body after it is for people."""

    id: NonEmpty
    goal: NonEmpty
    preconditions: Preconditions
    steps: ListOf[NonEmpty] = ()
    expect: Annotated[ListOf[Expectation], AtLeastOne]
    invariants: Invariants = Invariants()
    allowed_origins: DistinctListOf[Origin] = ()
    browser: BrowserOverrides = BrowserOverrides()
    tags: DistinctListOf[NonEmpty] = ()

    @field_validator("id")
    @classmethod
    def _is_the_file_name(cls, spec_id: str, info: ValidationInfo) -> str:
        file_id = _context(info)["file_id"]
        if spec_id != file_id:
            raise ValueError(
                f"'{spec_id}' doesn't match the file name: a spec's id is its file "
                f"name without .spec.md, here '{file_id}'"
            )
        return spec_id


@dataclass(frozen=True)
class Spec:
    """A spec as read from its file, with the hash a compiled script records
    (DATA_MODEL §7), and the bindings of exactly the test secrets it
    references."""

    path: Path
    frontmatter: SpecFrontmatter
    spec_hash: str
    # By secret name, as load_spec and load_project take them from the project
    # config (DATA_MODEL §9), with `start` unresolved and not checked against
    # the run's allowed origins: a run reads them through secret_destinations.
    # Not part of spec_hash.
    secret_bindings: Mapping[str, SecretBinding]

    def __post_init__(self) -> None:
        # Its own copy, so a later change to the mapping it was given can't
        # reach it.
        object.__setattr__(self, "secret_bindings", dict(self.secret_bindings))
        referenced = {name for _, name in secret_references(self.frontmatter)}
        if set(self.secret_bindings) != referenced:
            raise ValueError(
                f"{self.path}: bindings for secrets {sorted(self.secret_bindings)} but "
                f"references to {sorted(referenced)}: a spec carries the bindings of "
                "exactly the secrets it references"
            )


def secret_references(frontmatter: SpecFrontmatter) -> Iterator[tuple[str, str]]:
    """The key and secret name of each test secret the spec references."""
    account = frontmatter.preconditions.account
    if account is None:
        return
    for key, value in account:
        if isinstance(value, SecretReference):
            yield f"preconditions.account.{key}", value.secret


def canonical_hash(value: object) -> str:
    """`sha256:` and the sha256 of `value`'s canonical JSON: sorted keys, no
    whitespace, Python's ASCII escapes (DATA_MODEL §7)."""
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def spec_hash(frontmatter: Mapping[str, object]) -> str:
    """The canonical hash of the frontmatter as parsed, without `tags`. The
    body never counts (DATA_MODEL §7)."""
    return canonical_hash(
        {key: value for key, value in frontmatter.items() if key != "tags"}
    )
