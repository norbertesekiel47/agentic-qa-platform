"""The coverage plan (ADR-0024; DATA_MODEL §7): for each expectation, its
subject and the checks that establish its claim, or why none can, plus the
conditions the goal or the expectations require. It is written from the spec
alone, before the browser opens, and its hash freezes it for the explore run.

These models are also the response format the navigator's model fills, and
their docstrings reach it as the format's descriptions: editing one changes
the plan's request and its cassette (TESTING §4)."""

import re
from collections.abc import Iterator, Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictInt, model_validator

from aqa_core.compiled import (
    ContrastRatio,
    HttpMethod,
    NormalizedText,
    ProbeValue,
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
from aqa_core.spec import Expectation, SpecFrontmatter, canonical_hash

# M1's check types (#41), then the three M2 checks a plan names (#327).
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
    "contrast_min",
    "model_verify",
    "pixel_diff",
]

_NETWORK = frozenset({"method", "url_pattern", "status_class"})
_TEXT_OR_PATTERN = frozenset({"text", "pattern"})

# The fields each check type needs beyond `check`. These are DATA_MODEL §7's
# compiled fields, less what compiling adds, with a target named by its
# meaning rather than by an ID. Compiling sets a `pixel_diff`'s tolerances
# and baseline: a spec states none, and a plan never picks one (ADR-0024's
# #327 amendment).
_FIELDS_OF: Final[Mapping[CheckType, frozenset[str]]] = {
    "text_visible": frozenset(),
    "text_in_target": frozenset({"target_meaning"}),
    "not_visible": frozenset({"target_meaning"}),
    "url_matches": frozenset({"pattern"}),
    "network_none": _NETWORK,
    "network_seen": _NETWORK,
    "probe_equals": frozenset({"probe", "value"}),
    "probe_equals_baseline": frozenset({"probe"}),
    "visible_unoccluded": frozenset({"target_meaning"}),
    "contrast_min": frozenset({"target_meaning", "min_ratio"}),
    "model_verify": frozenset({"target_meaning"}),
    "pixel_diff": frozenset({"target_meaning"}),
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
    value: ProbeValue | None = None
    method: HttpMethod | None = None
    url_pattern: PythonRegex | None = None
    status_class: StatusClass | None = None
    # A `contrast_min`'s ratio: one the expectation states, never one the
    # plan chooses (`misfits`).
    min_ratio: ContrastRatio | None = None

    @model_validator(mode="after")
    def _its_types_fields(self) -> Self:
        required = _FIELDS_OF[self.check]
        text_check = self.check in _TEXT_CHECKS
        given = {
            name
            for name in type(self).model_fields
            if name != "check" and getattr(self, name) is not None
        }
        takes = required | (_TEXT_OR_PATTERN if text_check else frozenset())
        problems = [
            f"a {self.check} check needs {name}" for name in sorted(required - given)
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
    """Why no check can establish an expectation's claim."""

    reason: NonEmpty


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
    leaving out every field whose value is null (DATA_MODEL §7)."""
    return canonical_hash(plan.model_dump(mode="json", exclude_none=True))


def planned_checks(plan: CoveragePlan) -> dict[str, tuple[int, PlannedCheck]]:
    """Each planned check by its ID, with its expectation's index. The IDs are
    `a1`, `a2`, ... in plan order: the navigator names a check by the ID of
    the assertion it compiles to (ADR-0024)."""
    numbered = (
        (planned.expect_index, check)
        for planned in plan.expectations
        for check in planned.checks
    )
    return {f"a{n}": pair for n, pair in enumerate(numbered, start=1)}


# A ratio an expectation's text states, a decimal literal N in ratio form:
# "N:1" or "N to 1", or N directly after "contrast ratio of" or "contrast
# ratio of at least" and not the first term of another ratio, such as "4 to
# 3". So a version such as "WCAG 2.1", a count such as "the 3 buttons", a
# section number or an aspect ratio such as "16:9" states none (ADR-0024's
# #327 amendment, the maintainer's tightening).
_DECIMAL = r"[0-9]+(?:\.[0-9]+)?"
_END = r"(?![0-9]|\.[0-9])"
_RATIO = re.compile(
    rf"(?<![0-9.])(?P<to_one>{_DECIMAL})\s*(?::|\bto\b)\s*1{_END}"
    rf"|\bcontrast\s+ratio\s+of(?:\s+at\s+least)?\s+(?P<worded>{_DECIMAL}){_END}"
    r"(?!\s*(?::|\bto\b)\s*[0-9])",
    re.IGNORECASE,
)
# What a contrast_min misfit tells the plan to do instead.
_STATE_IT = "write the ratio the expectation states, or unsupported when it states none"


def _entry_misfits(
    planned: PlannedExpectation, expectation: Expectation
) -> Iterator[str]:
    """Why one entry doesn't fit its expectation: a `visual: model` one
    takes exactly one `model_verify` check, an `unsupported` entry holding
    none, and no other expectation takes one; and a `contrast_min`'s ratio is
    above 1 and a ratio the expectation's text states (`_RATIO`), so the plan
    never picks a threshold (ADR-0024's #327 amendment)."""
    where = f"expect[{planned.expect_index}]"
    verifying = sum(check.check == "model_verify" for check in planned.checks)
    if expectation.visual == "model" and verifying != 1:
        yield (
            f"{where}: the spec marks it visual: model, so it takes exactly one "
            f"model_verify check, not {verifying}"
        )
    if expectation.visual != "model" and verifying:
        yield (
            f"{where}: only an expectation the spec marks visual: model takes a "
            "model_verify check"
        )
    stated = {
        float(found["to_one"] or found["worded"])
        for found in _RATIO.finditer(expectation.text)
    }
    for ratio in (check.min_ratio for check in planned.checks):
        if ratio is None:
            continue
        if ratio <= 1:
            yield f"{where}: a contrast_min min_ratio of {ratio:g} can never fail: {_STATE_IT}"
        elif ratio not in stated:
            yield (
                f"{where}: a contrast_min min_ratio of {ratio:g} is not a ratio "
                f"the expectation states: {_STATE_IT}"
            )


def misfits(plan: CoveragePlan, frontmatter: SpecFrontmatter) -> tuple[str, ...]:
    """Why `plan` doesn't fit the spec it was written for: it must have one
    entry per expectation, in the spec's order, read only probes the spec
    declares, and fit each expectation's own rules (`_entry_misfits`). Empty
    when it fits."""
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
        f"declares ({', '.join(declared) or 'none'})"
        for planned in plan.expectations
        for check in planned.checks
        if check.probe is not None and check.probe not in declared
    )
    found.extend(
        misfit
        for planned in plan.expectations
        if planned.expect_index < count
        for misfit in _entry_misfits(planned, frontmatter.expect[planned.expect_index])
    )
    return tuple(found)


def uncovered(plan: CoveragePlan, frontmatter: SpecFrontmatter) -> tuple[str, ...]:
    """A line naming each expectation `plan` can't cover: its index, its text
    and why. A plan that doesn't fit its spec (`misfits`) is a ValueError: its
    entries may name no expectation."""
    if found := misfits(plan, frontmatter):
        raise ValueError(f"the plan doesn't fit its spec: {'; '.join(found)}")
    return tuple(
        f'expect[{planned.expect_index}] "{frontmatter.expect[planned.expect_index].text}": '
        f"no check can establish it: {planned.unsupported.reason}"
        for planned in plan.expectations
        if planned.unsupported is not None
    )
