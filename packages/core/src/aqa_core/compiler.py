"""Compiling an explored path into a compiled script (DATA_MODEL §7,
"Compilation rules"; ADR-0025's #53 P2 amendment). Pure: no clock, no file,
no browser and no model.

A problem names a check, a step number, a condition or a row, and may quote
plan or spec text; never a step's fields, a bound target's meaning or its
locators, which the attempt and the page chose."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from aqa_core.compiled import (
    Assertion,
    CompiledBy,
    CompiledScript,
    Coverage,
    ExpectationCoverage,
    FillSecret,
    Step,
    Target,
)
from aqa_core.config import ModelRoleName, ProjectConfig
from aqa_core.coverage_plan import (
    CoveragePlan,
    PlannedCheck,
    plan_hash,
    planned_checks,
    uncovered,
)
from aqa_core.project import (
    SpecError,
    contract_problems,
    contracts_fingerprint,
    effective_browser,
    parse_compiled,
    subject_contracts,
)
from aqa_core.schema import Contract, NonEmpty, StrictModel
from aqa_core.spec import Spec

_ASSERTION: Final[TypeAdapter[Assertion]] = TypeAdapter(Assertion)
_PROBES: Final = frozenset({"probe_equals", "probe_equals_baseline"})
_STEP: Final[TypeAdapter[Step]] = TypeAdapter(Step)
# Until #54 infers the flag from evidence, every compiled step is a
# side-effect step (ADR-0025: false needs positive evidence).
_BASIS: Final = "no positive evidence that the step changes nothing"
# The step fields a problem may name: the ones the actions take, and the
# ones compiling owns. Any other key came from the caller and is never shown.
_ACTION_FIELDS: Final = frozenset({"url", "value", "secret", "option", "key", "target"})
_COMPILER_FIELDS: Final = frozenset(
    {"seq", "target", "side_effect", "side_effect_basis", "satisfies"}
)
_NOT_ITS_CONTRACT: Final = "its target's subject contract isn't its meaning's"
_LOADER: Final = "the compiled script doesn't pass the loader's checks (DATA_MODEL §7, \"Checked by the loader\")"


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
    viewport and have a size (DATA_MODEL §7); a `probe_equals` needs the JSON
    path exploring doesn't bind yet (#53 P20)."""
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
    return (
        *(
            f"{check_id} (expect[{index}], {check.check}): exploring can't compile a {check.check} check yet"
            for check_id, (index, check) in planned_checks(plan).items()
            if check.check in _PROBES
        ),
        *(
            f'requires {condition.id} ("{condition.condition}"): exploring can\'t keep a required condition in the path yet'
            for condition in plan.requires
        ),
    )


def _contracts(
    plan: CoveragePlan, rows: Mapping[int, Contract]
) -> tuple[dict[str, Contract | None], list[str]]:
    """Each planned target meaning's contract, and why `rows` can't govern the
    plan's meanings."""
    problems: list[str] = []
    contracts: dict[str, Contract | None] = {}
    first: dict[str, int] = {}
    clashed: set[str] = set()
    for planned in plan.expectations:
        index = planned.expect_index
        meanings = [c.target_meaning for c in planned.checks if c.target_meaning]
        if index in rows and not meanings:
            how = (
                f"leaves expectation {index} unsupported"
                if planned.unsupported
                else f"checks expectation {index} with no element"
            )
            problems.append(
                f"subjects expect {index}: the plan {how}, so no target can carry its subject contract"
            )
        for meaning in meanings:
            contract = rows.get(index)
            known = contracts.setdefault(meaning, contract)
            if known != contract and meaning not in clashed:
                clashed.add(meaning)
                problems.append(
                    f'meaning "{meaning}" is planned under different subject contracts, for expect {first[meaning]} and expect {index}: one meaning has one contract or none'
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
    `fields` are `action` and the action's own field (`url`, `value`,
    `secret`, `option` or `key`; a reload and a click have none), never what
    compiling owns: `seq`, `target`, `side_effect`, `side_effect_basis` and
    `satisfies` (DATA_MODEL §7)."""

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


def _carries(target: Target, contracts: Mapping[str, Contract | None]) -> bool:
    """Whether `target` carries exactly its meaning's contract. A meaning no
    planned check reads, such as an action's, has none."""
    return target.contract == contracts.get(target.semantic)


def _named(names: dict[Target, str], target: Target) -> str:
    """`target`'s name, `t1`, `t2`, ... in order of first use. Equal targets
    are one target."""
    return names.setdefault(target, f"t{len(names) + 1}")


def _steps(
    path: ExploredPath,
    taken: Sequence[int],
    contracts: Mapping[str, Contract | None],
    names: dict[Target, str],
    secrets: Collection[str],
) -> tuple[list[Step], list[str]]:
    """The kept steps, `taken`, compiled in order, and what's wrong with them.
    A secret the spec doesn't bind has no binding at replay (`secrets`)."""
    steps: list[Step] = []
    problems: list[str] = []
    for seq, number in enumerate(taken, start=1):
        step = path.steps[number]
        if step.target is not None and not _carries(step.target, contracts):
            problems.append(f"step {number}: {_NOT_ITS_CONTRACT}")
        compiled = _step(number, step, seq, names)
        if isinstance(compiled, str):
            problems.append(compiled)
        elif isinstance(compiled, FillSecret) and compiled.secret not in secrets:
            problems.append(f"step {number}: fills a secret the spec doesn't bind")
        else:
            steps.append(compiled)
    return steps, problems


def _step(
    number: int, step: PathStep, seq: int, names: dict[Target, str]
) -> Step | str:
    """Step `number` of the attempt compiled as step `seq`, or the problem with
    it, which names only a known action, a listed field or a kind of mistake,
    never a value: pydantic's messages quote their input."""
    if owned := sorted(_COMPILER_FIELDS & step.fields.keys()):
        return f"step {number}: sets {owned[0]}, which compiling owns"
    fields: dict[str, object] = {
        **step.fields,
        "seq": seq,
        "side_effect": True,
        "side_effect_basis": _BASIS,
    }
    if step.target is not None:
        fields["target"] = _named(names, step.target)
    try:
        return _STEP.validate_python(fields)
    except ValidationError as error:
        first = error.errors(include_input=False)[0]
    return f"step {number}: {_invalid(first['type'], first['loc'])}"


def _invalid(error: str, loc: tuple[int | str, ...]) -> str:
    """What's wrong with a step, from pydantic's first error type and its
    location (the action, then the field), never its message."""
    if not loc:  # no action, or one this format doesn't know
        return "names no action this format knows"
    kind, field = loc[0], loc[1] if len(loc) > 1 else None
    # Compiling's own fields are valid, so any other is the caller's key.
    if field not in _ACTION_FIELDS:
        return f"a {kind} step takes a field it doesn't know"
    if error == "missing":
        return f"a {kind} step needs {field}"
    if error == "extra_forbidden":
        return f"a {kind} step takes no {field}"
    return f"its {field} isn't valid for a {kind} step"


def _binding_problem(
    check: PlannedCheck, target: Target | None, contracts: Mapping[str, Contract | None]
) -> str | None:
    """What's wrong with binding `check` to `target`, if anything."""
    if check.target_meaning is None:
        return None if target is None else "reads no element, but is bound to one"
    if target is None:
        return "bound to no element, but it reads one"
    if target.semantic != check.target_meaning:
        return f'bound to a target of another meaning than "{check.target_meaning}"'
    return None if _carries(target, contracts) else _NOT_ITS_CONTRACT


def _assertions(
    plan: CoveragePlan,
    path: ExploredPath,
    contracts: Mapping[str, Contract | None],
    names: dict[Target, str],
    has_steps: bool,
) -> tuple[list[Assertion], list[str]]:
    """Each bound check's assertion, in plan order, and what's wrong with the
    checks. Probe checks are `unsupported_features`' to name."""
    assertions: list[Assertion] = []
    problems: list[str] = []
    if path.bound.keys() - planned_checks(plan).keys():
        problems.append("the path binds a check ID the plan doesn't have")
    for check_id, (index, check) in planned_checks(plan).items():
        where = f"{check_id} (expect[{index}], {check.check})"
        if check.check in _PROBES:
            if check.check == "probe_equals_baseline" and not has_steps:
                problems.append(
                    f"{where}: a path with no steps has no step to capture its baseline before"
                )
            continue
        if check_id not in path.bound:
            problems.append(f"{where}: never bound")
            continue
        target = path.bound[check_id]
        if problem := _binding_problem(check, target, contracts):
            problems.append(f"{where}: {problem}")
            continue
        name = None if target is None else _named(names, target)
        assertions.append(assertion_for(check_id, index, check, target=name))
    return assertions, problems


def _loads(script: CompiledScript, config: ProjectConfig) -> bool:
    """Whether the strict loader reads `script`'s JSON back. Its problems can
    quote a name the model wrote, so the caller reports one fixed line."""
    try:
        parse_compiled(script.model_dump_json(), config, source=Path("compiled.json"))
    except SpecError:
        return False
    return True


def compile_script(
    spec: Spec,
    config: ProjectConfig,
    plan: CoveragePlan,
    path: ExploredPath,
    by: Provenance,
) -> CompiledScript:
    """The unconfirmed script for the steps `finish` kept, in the attempt's
    order, and the bound checks (DATA_MODEL §7), or CompileError naming every
    problem. `plan` must fit `spec` (`coverage_plan.misfits`)."""
    plan_problems = [*uncovered(plan, spec.frontmatter), *unsupported_features(plan)]
    spec_id = spec.frontmatter.id
    contracts, meaning_problems = _contracts(plan, subject_contracts(config, spec_id))
    kept = sorted(set(path.kept))
    taken = [number for number in kept if number in path.steps]
    names: dict[Target, str] = {}
    steps, step_problems = _steps(path, taken, contracts, names, spec.secret_bindings)
    assertions, check_problems = _assertions(plan, path, contracts, names, bool(taken))
    problems = [
        *plan_problems,
        *meaning_problems,
        *(
            f"finish names step {number}, which the attempt never took"
            for number in kept
            if number not in path.steps
        ),
        *step_problems,
        *check_problems,
    ]
    if problems:
        raise CompileError(problems) from None
    script = CompiledScript(
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
                    assertions=tuple(
                        a.id
                        for a in assertions
                        if a.expect_index == planned.expect_index
                    ),
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
    problems = [
        *(() if _loads(script, config) else (_LOADER,)),
        *contract_problems(script, config),
    ]
    if problems:
        raise CompileError(problems) from None
    return script
