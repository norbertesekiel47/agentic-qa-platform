"""The coverage plan (ADR-0024; DATA_MODEL §7): for each expectation, its
subject and the checks that establish its claim, or why none can, plus the
conditions the goal or the expectations require. It is written from the spec
alone, before the browser opens, and its hash freezes it for the explore run."""

from collections.abc import Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictInt, model_validator

from aqa_core.compiled import (
    HttpMethod,
    NormalizedText,
    PythonRegex,
    RequiredCondition,
    StatusClass,
)
from aqa_core.schema import (
    AtLeastOne,
    DistinctListOf,
    ListOf,
    NonEmpty,
    StrictModel,
)
from aqa_core.spec import SpecFrontmatter, canonical_hash

# M1's check types (#41). pixel_diff, contrast_min and model_verify arrive in
# M2; until then an expectation that needs one is unsupported.
CheckType = Literal[
    "text_visible",
    "text_in_target",
    "not_visible",
    "url_matches",
    "network_none",
    "network_seen",
    "probe_equals",
    "probe_equals_baseline",
    "visible_unoccluded",
]

# The M2 checks an unsupported expectation may name as what it needs.
M2Check = Literal["pixel_diff", "contrast_min", "model_verify"]

_NETWORK = frozenset({"method", "url_pattern", "status_class"})
_TEXT_OR_PATTERN = frozenset({"text", "pattern"})

# The fields each check type needs beyond `check`. These are DATA_MODEL §7's
# compiled fields, less what compiling adds, with a target named by its
# meaning rather than by an ID.
_NEEDS_FIELDS: Final[Mapping[CheckType, frozenset[str]]] = {
    "text_visible": frozenset(),
    "text_in_target": frozenset({"target_meaning"}),
    "not_visible": frozenset({"target_meaning"}),
    "url_matches": frozenset({"pattern"}),
    "network_none": _NETWORK,
    "network_seen": _NETWORK,
    "probe_equals": frozenset({"probe", "value"}),
    "probe_equals_baseline": frozenset({"probe"}),
    "visible_unoccluded": frozenset({"target_meaning"}),
}
# The check types that also take `text` or `pattern`, exactly one.
_TEXT_CHECKS: Final = frozenset({"text_visible", "text_in_target"})


class PlannedCheck(StrictModel):
    """One check that establishes all or part of an expectation's claim. Its
    type decides which of the other fields it has (DATA_MODEL §7)."""

    check: CheckType
    # What the element the check reads is for and where it sits, never its
    # label (ADR-0025): the expectation's subject, or the part of it the check
    # reads when the claim names several elements. It becomes the meaning of
    # the assertion's target (#52).
    target_meaning: NonEmpty | None = None
    text: NormalizedText | None = None
    pattern: PythonRegex | None = None
    probe: NonEmpty | None = None
    value: StrictInt | NonEmpty | None = None
    method: HttpMethod | None = None
    url_pattern: NonEmpty | None = None
    status_class: StatusClass | None = None

    @model_validator(mode="after")
    def _its_types_fields(self) -> Self:
        needs = _NEEDS_FIELDS[self.check]
        text_check = self.check in _TEXT_CHECKS
        given = {
            name
            for name in type(self).model_fields
            if name != "check" and getattr(self, name) is not None
        }
        takes = needs | (_TEXT_OR_PATTERN if text_check else frozenset())
        problems = [
            f"a {self.check} check needs {name}" for name in sorted(needs - given)
        ]
        problems += [
            f"a {self.check} check takes no {name}" for name in sorted(given - takes)
        ]
        if text_check and len(given & _TEXT_OR_PATTERN) != 1:
            problems.append(f"a {self.check} check takes text or pattern, exactly one")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Unsupported(StrictModel):
    """Why no M1 check can establish an expectation's claim, and the M2 check
    it needs, if one would."""

    reason: NonEmpty
    needs: M2Check | None = None


class PlannedExpectation(StrictModel):
    """One expectation of the spec: what it is about, what it claims, and the
    checks that establish the claim, or why none can."""

    expect_index: Annotated[StrictInt, Field(ge=0)]
    subject: NonEmpty
    claim: NonEmpty
    checks: DistinctListOf[PlannedCheck] = ()
    unsupported: Unsupported | None = None

    @model_validator(mode="after")
    def _checks_or_unsupported(self) -> Self:
        if bool(self.checks) == (self.unsupported is not None):
            raise ValueError(
                "an expectation has checks or is unsupported, never both or neither"
            )
        return self


class CoveragePlan(StrictModel):
    """Every expectation's plan, and the conditions the goal or the
    expectations require, such as a reload before the checks."""

    expectations: Annotated[ListOf[PlannedExpectation], AtLeastOne]
    requires: ListOf[RequiredCondition]

    @model_validator(mode="after")
    def _distinct_condition_ids(self) -> Self:
        ids = [condition.id for condition in self.requires]
        if repeated := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"requires lists condition {repeated[0]} twice")
        return self


def plan_hash(plan: CoveragePlan) -> str:
    """The plan's `plan_hash`: the canonical hash of everything the plan says,
    leaving out the fields it doesn't use (DATA_MODEL §7)."""
    return canonical_hash(plan.model_dump(mode="json", exclude_none=True))


def misfits(plan: CoveragePlan, frontmatter: SpecFrontmatter) -> tuple[str, ...]:
    """Why `plan` doesn't fit the spec it was written for: it must have one
    entry per expectation, in the spec's order, and read only probes the spec
    declares. Empty when it fits."""
    found: list[str] = []
    indexes = [planned.expect_index for planned in plan.expectations]
    count = len(frontmatter.expect)
    if indexes != list(range(count)):
        found.append(
            f"the plan covers expectations {indexes}, but the spec has {count} "
            f"expectations: it covers each once, in order, from 0 to {count - 1}"
        )
    declared = frontmatter.preconditions.probes
    found.extend(
        f"expect[{planned.expect_index}]: probe {check.probe} is not one the spec "
        f"declares ({', '.join(sorted(declared)) or 'none'})"
        for planned in plan.expectations
        for check in planned.checks
        if check.probe is not None and check.probe not in declared
    )
    return tuple(found)


# What an unsupported expectation needs, as its line says it.
_NEEDS: Final[Mapping[M2Check | None, str]] = {
    None: "no M1 check can establish it",
    "pixel_diff": "it needs pixel_diff, which M2 adds",
    "contrast_min": "it needs contrast_min, which M2 adds",
    "model_verify": "it needs a model to judge it (model_verify), which M2 adds",
}


def uncovered(plan: CoveragePlan, frontmatter: SpecFrontmatter) -> tuple[str, ...]:
    """A line naming each expectation `plan` can't cover: its index, its text,
    what it needs and why. Call it on a plan that fits its spec (`misfits`)."""
    return tuple(
        f'expect[{planned.expect_index}] "{frontmatter.expect[planned.expect_index].text}": '
        f"{_NEEDS[planned.unsupported.needs]}: {planned.unsupported.reason}"
        for planned in plan.expectations
        if planned.unsupported is not None
    )
