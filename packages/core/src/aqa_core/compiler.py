"""Compiling an explored path into a compiled script (DATA_MODEL §7,
"Compilation rules"; ADR-0025's #53 P2 amendment). Pure: no clock, no file,
no browser and no model.

A problem names a check by its ID and expectation, a step by the attempt's
number, a condition by its ID, and a meaning only as the plan wrote it. None
quotes a step's fields, a target's meaning or its locators, which the model and
the page chose."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import AwareDatetime, TypeAdapter

from aqa_core.compiled import (
    Assertion,
    Click,
    CompiledBy,
    CompiledScript,
    Coverage,
    ExpectationCoverage,
    Fill,
    FillSecret,
    Navigate,
    Press,
    Reload,
    Select,
    Step,
    Target,
)
from aqa_core.config import ModelRoleName, ProjectConfig
from aqa_core.coverage_plan import (
    CoveragePlan,
    PlannedCheck,
    plan_hash,
    planned_checks,
)
from aqa_core.project import contracts_fingerprint, effective_browser
from aqa_core.schema import Contract, NonEmpty, StrictModel
from aqa_core.spec import Spec

_ASSERTION: Final[TypeAdapter[Assertion]] = TypeAdapter(Assertion)
_PROBES: Final = frozenset({"probe_equals", "probe_equals_baseline"})
_ACTIONS: Final[
    Mapping[str, type[Navigate | Reload | Click | Fill | FillSecret | Select | Press]]
] = {
    "navigate": Navigate,
    "reload": Reload,
    "click": Click,
    "fill": Fill,
    "fill_secret": FillSecret,
    "select": Select,
    "press": Press,
}
# Until #54 infers the flag from evidence, every compiled step is a
# side-effect step (ADR-0025: false needs positive evidence).
BASIS: Final = "no positive evidence that the step changes nothing"


class CompileError(Exception):
    """Why an explored path can't compile: each problem, all at once."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = tuple(problems)


def assertion_for(
    check_id: str, expect_index: int, check: PlannedCheck, *, target: str | None = None
) -> Assertion:
    """The assertion `check` compiles to, reading the target named `target`
    when its type reads an element. A `visible_unoccluded` check must fit the
    viewport and have a size (DATA_MODEL §7)."""
    fields: dict[str, object] = {
        "id": check_id,
        "expect_index": expect_index,
        **check.model_dump(exclude_none=True, exclude={"target_meaning"}),
    }
    if target is not None:
        fields["target"] = target
    if check.check == "visible_unoccluded":
        fields |= {"min_size_px": (1, 1), "in_viewport": True}
    return _ASSERTION.validate_python(fields)


def unsupported_features(plan: CoveragePlan) -> tuple[str, ...]:
    """A line for each probe check and each required condition in `plan`,
    which exploring can't compile yet (ADR-0024's D35): explore refuses such
    a plan before the browser opens."""
    probes = [
        f"{check_id} (expect[{index}], {check.check}): exploring can't compile a "
        f"{check.check} check yet"
        for check_id, (index, check) in planned_checks(plan).items()
        if check.check in _PROBES
    ]
    conditions = [
        f'requires {condition.id} ("{condition.condition}"): exploring can\'t keep a '
        "required condition in the path yet"
        for condition in plan.requires
    ]
    return (*probes, *conditions)


def _contracts(
    plan: CoveragePlan, rows: Mapping[int, Contract]
) -> tuple[dict[str, Contract | None], list[str]]:
    """Each planned target meaning's contract, and why `rows` can't govern the
    plan's meanings."""
    problems: list[str] = []
    contracts: dict[str, Contract | None] = {}
    first: dict[str, int] = {}
    for planned in plan.expectations:
        index = planned.expect_index
        meanings = [c.target_meaning for c in planned.checks if c.target_meaning]
        if index in rows and planned.unsupported is not None:
            problems.append(
                f"subjects expect {index}: the plan leaves expectation {index} "
                "unsupported, so no target can carry its subject contract"
            )
        elif index in rows and not meanings:
            problems.append(
                f"subjects expect {index}: the plan checks expectation {index} with "
                "no element, so no target can carry its subject contract"
            )
        for meaning in meanings:
            contract = rows.get(index)
            if contracts.setdefault(meaning, contract) != contract:
                problems.append(
                    f'meaning "{meaning}" is planned under different subject contracts, '
                    f"for expect {first[meaning]} and expect {index}: one meaning has "
                    "one contract or none"
                )
            first.setdefault(meaning, index)
    return contracts, problems


def contracts_by_meaning(
    plan: CoveragePlan, rows: Mapping[int, Contract]
) -> dict[str, Contract | None]:
    """Each target meaning `plan` checks, with its expectation's contract in
    `rows` (`subject_contracts`), or None. An action whose meaning isn't here
    is unlisted. CompileError when a row's expectation is unsupported or
    checked with no element, or a meaning is planned under two contracts,
    listed and unlisted counting as two (ADR-0024's D35)."""
    contracts, problems = _contracts(plan, rows)
    if problems:
        raise CompileError(problems)
    return contracts


@dataclass(frozen=True)
class PathStep:
    """An action the attempt dispatched, and the target its element got.

    `fields` are the step's compiled fields that the action chose: `action`,
    then `url` for a navigate, `value` for a fill, `secret` for a
    fill_secret, `option` for a select and `key` for a press; a reload and a
    click take none. A click, fill, fill_secret or select has a `target`;
    the others have none. Compiling owns `seq`, `target` (the name),
    `side_effect`, `side_effect_basis` and `satisfies` (DATA_MODEL §7)."""

    fields: Mapping[str, str]
    target: Target | None = None


@dataclass(frozen=True)
class ExploredPath:
    """What one attempt explored: its steps by the number it gave each, the
    numbers `finish` kept, and each planned check it bound, by ID, to the
    target the check reads, or to None for a check that reads no element."""

    steps: Mapping[int, PathStep]
    kept: Sequence[int]
    bound: Mapping[str, Target | None]


class Provenance(StrictModel):
    """Who compiled the script and when: each model role's model the run
    called, the pinned price map's upstream commit, and the time."""

    models: dict[ModelRoleName, NonEmpty]
    price_map: NonEmpty
    at: AwareDatetime


def _named(names: dict[Target, str], target: Target) -> str:
    """`target`'s name, `t1`, `t2`, ... in order of first use. Equal targets
    are one target."""
    return names.setdefault(target, f"t{len(names) + 1}")


def _steps(path: ExploredPath, names: dict[Target, str]) -> list[Step]:
    steps: list[Step] = []
    for seq, number in enumerate(sorted(set(path.kept)), start=1):
        taken = path.steps[number]
        fields: dict[str, object] = {
            **taken.fields,
            "seq": seq,
            "side_effect": True,
            "side_effect_basis": BASIS,
        }
        if taken.target is not None:
            fields["target"] = _named(names, taken.target)
        steps.append(_ACTIONS[taken.fields["action"]].model_validate(fields))
    return steps


def compile_script(
    spec: Spec,
    config: ProjectConfig,
    plan: CoveragePlan,
    path: ExploredPath,
    by: Provenance,
) -> CompiledScript:
    """The compiled script for the steps of `path` that `finish` kept and the
    checks it bound, unconfirmed (DATA_MODEL §7)."""
    names: dict[Target, str] = {}
    steps = _steps(path, names)
    checks = planned_checks(plan)
    assertions = []
    for check_id, (index, check) in checks.items():
        target = path.bound[check_id]
        name = None if target is None else _named(names, target)
        assertions.append(assertion_for(check_id, index, check, target=name))
    ids: dict[int, list[str]] = {}
    for check_id, (index, _) in checks.items():
        ids.setdefault(index, []).append(check_id)
    spec_id = spec.frontmatter.id
    return CompiledScript(
        schema_version=1,
        spec_id=spec_id,
        spec_hash=spec.spec_hash,
        compiled_at=by.at,
        compiled_by=CompiledBy(
            mode="explore",
            models=dict(by.models),
            price_map=by.price_map,
            subject_contracts=contracts_fingerprint(config, spec_id),
        ),
        confirmed=False,
        browser=effective_browser(spec, config),
        coverage=Coverage(
            plan_hash=plan_hash(plan),
            expectations=tuple(
                ExpectationCoverage(
                    expect_index=planned.expect_index,
                    subject=planned.subject,
                    claim=planned.claim,
                    assertions=tuple(ids[planned.expect_index]),
                )
                for planned in plan.expectations
            ),
            requires=plan.requires,
        ),
        targets={name: target for target, name in names.items()},
        probe_baselines={},
        steps=tuple(steps),
        assertions=tuple(assertions),
    )
