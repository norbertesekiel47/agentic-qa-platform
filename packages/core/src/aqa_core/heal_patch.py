"""What a heal patch may change in a compiled script (DATA_MODEL §7, #19): a
heal rebinds targets' locators and inserts replay-safe steps, and changes
nothing else. Product code owns these rules; the benchmark's rebindings pass
through them too."""

from collections.abc import Iterator, Sequence

from aqa_core.compiled import (
    ByCss,
    CompiledScript,
    Locator,
    Navigate,
    NotVisible,
    Step,
    TextInTarget,
    VisibleUnoccluded,
    allowed_for_assertion,
    css_tokens,
)
from aqa_core.project import locator_levels

# The child-indexed pseudo-classes, typed or not (Selectors Level 4, §13.3
# and §13.4, https://www.w3.org/TR/selectors-4/#child-index). They find an
# element by its position among its siblings, which the locator grammar
# forbids. The format still reads them, so a person may write one in a
# reviewed edit, but a heal is the model's edit (ADR-0025's #52 amendment).
_POSITIONAL = frozenset(
    {
        "nth-child",
        "nth-last-child",
        "first-child",
        "last-child",
        "only-child",
        "nth-of-type",
        "nth-last-of-type",
        "first-of-type",
        "last-of-type",
        "only-of-type",
    }
)


def _positional(css: str) -> bool:
    """Whether `css` finds its element by its position among its siblings:
    by a child-indexed pseudo-class, or by a sibling combinator (`+` or `~`,
    at any depth, `:not(* ~ *)` being `:first-child`). Inside an attribute
    selector, a `~` is part of an operator (`~=`)."""
    attribute = False
    for part in css_tokens(css):
        if part in ("[", "]"):
            attribute = part == "["
        elif (part.startswith(":") and part[1:] in _POSITIONAL) or (
            part in ("+", "~") and not attribute
        ):
            return True
    return False


def _written(
    path: str, locator: Locator, *, asserted: bool, checks_text: bool
) -> Iterator[str]:
    """Problems with a locator the heal wrote at `path`, at each of its
    levels, since a scope finds the element as much as its locator does: a
    css value that finds its element by position, and, for a target an
    assertion reads, a kind the generator never gives one (ADR-0025)."""
    for where, level in locator_levels(path, locator, scope_key="/scope"):
        if isinstance(level, ByCss) and _positional(level.css):
            yield f"{where}/css: a heal finds no element by its position among its siblings"
        if asserted and not allowed_for_assertion(level, checks_text=checks_text):
            yield (
                f"{where}: a heal finds no assertion's target by a label or "
                "placeholder, or by a name when its text is checked"
            )


def _target_problems(script: CompiledScript, healed: CompiledScript) -> Iterator[str]:
    """Targets match by name: each keeps its meaning and contract. Only a
    locator the heal wrote, one its target didn't have, is held to the
    grammar here, so a person's reviewed edit stands. A meaning's text is
    checked when any target of it is, as the generator reads it."""
    asserted = {
        check.target
        for check in script.assertions
        if isinstance(check, TextInTarget | NotVisible | VisibleUnoccluded)
    }
    checked = {
        script.targets[check.target].semantic
        for check in script.assertions
        if isinstance(check, TextInTarget)
    }
    for name in healed.targets:
        if name not in script.targets:
            yield f"/targets/{_pointer(name)}: a heal adds no target"
    for name, target in script.targets.items():
        path = f"/targets/{_pointer(name)}"
        if name not in healed.targets:
            yield f"{path}: a heal removes no target"
            continue
        rebound = healed.targets[name]
        if rebound.semantic != target.semantic:
            yield f"{path}/semantic: a heal never changes a target's meaning"
        if rebound.contract != target.contract:
            yield (
                f"{path}/contract: a heal never changes a subject contract, which "
                "only the reviewed config gives"
            )
        for index, locator in enumerate(rebound.locators):
            if locator not in target.locators:
                yield from _written(
                    f"{path}/locators/{index}",
                    locator,
                    asserted=name in asserted,
                    checks_text=target.semantic in checked,
                )


def _pointer(name: str) -> str:
    """`name` as one JSON pointer token (RFC 6901)."""
    return name.replace("~", "~0").replace("/", "~1")


def _existing_steps(steps: Sequence[Step], healed: Sequence[Step]) -> Iterator[str]:
    """Steps match by `seq`, which held values, probe baselines and run
    records name: each existing step stays, unchanged and in order, so no
    `side_effect` flag changes and no `satisfies` mark moves. A path indexes
    the step in `steps`."""
    by_seq = {step.seq: step for step in healed}
    for index, step in enumerate(steps):
        if step.seq not in by_seq:
            yield f"/steps/{index}: a heal removes no step"
            continue
        was = step.model_dump(mode="json")
        now = by_seq[step.seq].model_dump(mode="json")
        yield from (
            f"/steps/{index}/{field}: a heal rewrites no existing step"
            for field in dict.fromkeys([*was, *now])
            if was.get(field) != now.get(field)
        )
    kept = [step.seq for step in steps if step.seq in by_seq]
    if [step.seq for step in healed if step.seq in {s.seq for s in steps}] != kept:
        yield "/steps: a heal reorders no step"


def _inserted_steps(steps: Sequence[Step], healed: Sequence[Step]) -> Iterator[str]:
    """A step with a new `seq` is inserted: replay-safe by its flag, and no
    navigation (#19). A path indexes the step in `healed`."""
    existing = {step.seq for step in steps}
    for index, step in enumerate(healed):
        if step.seq in existing:
            continue
        if step.side_effect:
            yield f"/steps/{index}/side_effect: a heal inserts only replay-safe steps"
        if isinstance(step, Navigate):
            yield (
                f"/steps/{index}/action: a heal inserts no navigation, which could "
                "route around a path that is broken for its users"
            )


def heal_patch_problems(script: CompiledScript, healed: CompiledScript) -> list[str]:
    """Why `healed` is no heal of `script`, each problem a fixed reason after
    the JSON pointer of what it names; none when it is one. `healed` must
    have been read by `aqa_core.project.parse_compiled`, so the loader's
    rules already hold for it.

    Nothing here runs: the heal step must still show that, after the patch,
    every assertion evaluates and passes and every invariant holds, and that
    an inserted step is replay-safe, not only flagged so (DATA_MODEL §7)."""
    before = script.model_dump(mode="json")
    after = healed.model_dump(mode="json")
    problems = [
        f"/{field}: a heal changes nothing but target locators and inserted steps"
        for field, value in before.items()
        if field not in ("targets", "steps") and after[field] != value
    ]
    problems.extend(_target_problems(script, healed))
    problems.extend(_existing_steps(script.steps, healed.steps))
    problems.extend(_inserted_steps(script.steps, healed.steps))
    return problems
