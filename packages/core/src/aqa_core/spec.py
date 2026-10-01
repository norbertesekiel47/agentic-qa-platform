"""A spec's frontmatter (DATA_MODEL §6), and the spec as read from its file."""

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    PlainValidator,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from aqa_core.browser import BrowserOverrides
from aqa_core.schema import Items, NonEmpty, Origin, SecretName, StrictModel


class SecretRef(StrictModel):
    """`{ secret: NAME }`: a test secret the project config declares."""

    secret: SecretName


def _credential(value: object) -> str | SecretRef:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return SecretRef.model_validate(value)
    raise ValueError(
        f"{value!r} is not an account value: write a string, or {{ secret: NAME }}"
    )


_Credential = Annotated[str | SecretRef, PlainValidator(_credential)]


class Account(StrictModel):
    email: _Credential | None = None
    password: _Credential | None = None


class Reset(StrictModel):
    """The reset hook, called before every attempt (ADR-0024). How it is
    called is #56's."""

    http: NonEmpty


# One leading slash, then no whitespace, control character or backslash:
# browsers read `/\host` and `/<tab>/host` as `//host`, another origin.
_PATH = re.compile(r"/(?![/\\])[^\s\x00-\x1f\x7f\\]*")


def _start_url(text: str) -> str:
    if not _PATH.fullmatch(text):
        raise ValueError(
            f"'{text}' is not a path: start_url is a path such as /login, and the "
            "origin comes from the run (ADR-0026)"
        )
    return text


class Preconditions(StrictModel):
    start_url: Annotated[StrictStr, AfterValidator(_start_url)]
    account: Account | None = None
    reset: Reset | None = None
    # Read-only GET endpoints, by name; #48 calls them.
    probes: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)


class Expectation(StrictModel):
    """One `expect` item: a plain string, or `{ text, visual }`."""

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
    disable: Items[InvariantName] = ()

    @model_validator(mode="after")
    def _disable_needs_inherit(self) -> Self:
        if not self.inherit and self.disable:
            raise ValueError(
                "disable has no effect when inherit is false: every invariant is already off"
            )
        return self


class SpecFrontmatter(StrictModel):
    id: NonEmpty
    goal: NonEmpty
    preconditions: Preconditions
    steps: Annotated[tuple[NonEmpty, ...], Field(strict=False)] = ()
    expect: Annotated[tuple[Expectation, ...], Field(strict=False, min_length=1)]
    invariants: Invariants = Invariants()
    allowed_origins: Items[Origin] = ()
    browser: BrowserOverrides = BrowserOverrides()
    tags: Items[NonEmpty] = ()


@dataclass(frozen=True)
class Spec:
    path: Path
    frontmatter: SpecFrontmatter
    spec_hash: str


def secret_references(frontmatter: SpecFrontmatter) -> Iterator[tuple[str, str]]:
    """The key and secret name of each test secret the spec references."""
    account = frontmatter.preconditions.account
    if account is None:
        return
    for key, value in (("email", account.email), ("password", account.password)):
        if isinstance(value, SecretRef):
            yield f"preconditions.account.{key}", value.secret


def spec_hash(frontmatter: Mapping[str, object]) -> str:
    """The sha256 of the canonical JSON (sorted keys, no whitespace, Python's
    ASCII escapes) of the frontmatter as parsed, without `tags`. The body never
    counts (DATA_MODEL §7)."""
    hashed = {key: value for key, value in frontmatter.items() if key != "tags"}
    canonical = json.dumps(
        hashed, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
