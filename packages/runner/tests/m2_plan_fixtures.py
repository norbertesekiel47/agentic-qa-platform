"""The M2 checks' plan case (#327; ADR-0024's #327 amendment): a spec with
one expectation for each check the plan gained, which the recorder records
as `plan_m2-checks`, and the oracle its plan is held to. The oracle compares
each check's type and the fields the spec fixes, never the wording of a
target's meaning, and says what differs in fixed words. Imported by its path,
as pytest names the runner's test modules (TESTING.md §1, Shared egress
fixtures)."""

from pathlib import Path
from typing import Any, Final

from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import load_config, load_spec
from aqa_core.spec import Spec
from aqa_runner.coverage_plan import Planned

SPEC_ID: Final = "m2-checks"
CASE: Final = f"plan_{SPEC_ID}"
# One start URL, no probes, account or reset, and three expectations, each
# about its own element: (0) a contrast that states its ratio, (1) a claim
# the spec leaves to a model, (2) an element that looks unchanged.
SPEC: Final = """\
---
id: m2-checks
goal: A customer opens their order page.
preconditions:
  start_url: /page/order
expect:
  - The shipping notice's text has a contrast of at least 4.5 to 1 against its background
  - text: The product photo shows a pair of red running shoes
    visual: model
  - The store logo in the page header looks unchanged from its reviewed baseline
---
"""
# What each expectation's entry holds: exactly this one check, compared
# without its target's meaning.
WANTED: Final[tuple[dict[str, Any], ...]] = (
    {"check": "contrast_min", "min_ratio": 4.5},
    {"check": "model_verify"},
    {"check": "pixel_diff"},
)
# Why an answer gave no plan the case can use, in fixed words.
UNUSABLE: Final = f"{SPEC_ID}: the model gave no usable plan"


def m2_spec(root: Path) -> Spec:
    """The case's spec and an empty project config, under the spec root
    `root`, read as the loader reads them."""
    root.mkdir(parents=True)
    (root / "config.yaml").write_text("")
    path = root / f"{SPEC_ID}.spec.md"
    path.write_text(SPEC)
    return load_spec(path, load_config(root / "config.yaml"))


def plan_problems(plan: CoveragePlan) -> tuple[str, ...]:
    """Why `plan` isn't the oracle's, in fixed words: one entry per
    expectation, in order, each holding exactly its one check of `WANTED`,
    none unsupported, and no required condition. Empty when it is."""
    if [planned.expect_index for planned in plan.expectations] != [0, 1, 2]:
        return (f"{SPEC_ID}: the plan doesn't cover each expectation once, in order",)
    problems = []
    for planned, wanted in zip(plan.expectations, WANTED, strict=True):
        where = f"{SPEC_ID}/{planned.expect_index}"
        held = [
            check.model_dump(exclude_none=True, exclude={"target_meaning"})
            for check in planned.checks
        ]
        if planned.unsupported is not None:
            problems.append(f"{where}: planned unsupported")
        elif held != [wanted]:
            problems.append(f"{where}: establishing checks differ")
    if plan.requires:
        problems.append(f"{SPEC_ID}: the plan requires a condition")
    return tuple(problems)


def disallowed(planned: Planned) -> str | None:
    """Why the case's answer, after its one retry if it had one, isn't
    promoted, in fixed words: the oracle's problems with the plan it parsed,
    and `UNUSABLE` when there is none or it can't be used. None when it is
    the oracle's plan and usable."""
    problems = () if planned.plan is None else plan_problems(planned.plan)
    if planned.plan is None or planned.problems:
        problems = (*problems, UNUSABLE)
    return "; ".join(problems) or None
