"""What exploring a spec refuses before anything is made (ADR-0026's #53
P15c amendment): every place where a bound value could reach the model, the
browser or the record, each line scanned with the run's redactor, as a
spec error (5)."""

from collections.abc import Iterator, Mapping
from typing import Final

from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, subject_contracts
from aqa_core.spec import Spec

from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.navigator import account_problems, spec_view
from aqa_runner.redaction import Redactor

UNSCANNABLE: Final = (
    "test secrets: a marker, [SECRET:<name>], spells or completes a bound value, "
    "so no scan could take it out: rename the secret or change its value"
)


def admit(
    spec: Spec,
    config: ProjectConfig,
    secrets: Mapping[str, BoundSecret],
    redactor: Redactor,
) -> None:
    """`SpecError` naming each place where a bound value could reach the model,
    the browser or the record, each line scanned (ADR-0026's #53 P15c)."""
    spec_id = spec.frontmatter.id
    problems = account_problems(spec, redactor)
    problems.extend(
        f"{spec.path}: {place}: a bound value's spelling is in it"
        for place, value in _places(spec)
        if redactor.spells(value)
    )
    problems.extend(
        f"subjects ({spec_id}, expect {expect}): a bound value's spelling is in "
        "its region or part"
        for expect, contract in subject_contracts(config, spec_id).items()
        if redactor.spells(contract.model_dump(mode="json"))
    )
    problems.extend(
        f"test secret {name}: a bound value's spelling is in its field's name"
        for name, secret in secrets.items()
        if secret.destination.field != "password"
        and redactor.spells(secret.destination.field.name)
    )
    shown: list[str] = [redactor.redact(problem) for problem in problems]
    if redactor.markers_spell_a_value or any(map(redactor.finds, shown)):
        shown = [UNSCANNABLE]
    if shown:
        raise SpecError(shown)


def _places(spec: Spec) -> Iterator[tuple[str, object]]:
    """Each place of the spec as the model reads it (`navigator.spec_view`),
    by name, keys beside values (a probe's name); the start path whole, as
    the browser and step 0's intent take it, and each probe's endpoint
    whole, as exploring's capture reads it. The account is checked apart."""
    view = spec_view(spec)
    preconditions = view.pop("preconditions")
    preconditions.pop("account", None)
    preconditions["start_url"] = spec.frontmatter.preconditions.start_url
    if "probes" in preconditions:
        preconditions["probes"] = dict(spec.frontmatter.preconditions.probes)
    for name, value in preconditions.items():
        view[f"preconditions.{name}"] = value
    for place, value in view.items():
        yield place, _keyed(value)


def _keyed(value: object) -> object:
    """`value`, a JSON value, with each mapping's keys among its values."""
    if isinstance(value, dict):
        return [[key, _keyed(item)] for key, item in value.items()]
    if isinstance(value, list):
        return [_keyed(item) for item in value]
    return value
