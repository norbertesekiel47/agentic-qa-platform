"""What a heal patch may change in a compiled script (DATA_MODEL §7, #19): a
heal rebinds targets' locators and inserts replay-safe steps, and changes
nothing else. Product code owns these rules; the benchmark's rebindings pass
through them too. A proposal's parts are here as well: its patch, the blob SHA
of the file it applies to, and its id (ADR-0024's #243 and #244 amendments)."""

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from typing import Final, Literal

from pydantic import JsonValue, StrictStr

from aqa_core.compiled import (
    ByCss,
    ByTestId,
    CompiledScript,
    Locator,
    Step,
    TargetCheck,
    TextInTarget,
    allowed_for_assertion,
    css_strings,
    css_tokens,
)
from aqa_core.project import SpecError, locator_levels
from aqa_core.schema import StrictModel
from aqa_core.text import SearchTimeoutError, any_has_pattern

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


def _strings(level: Locator) -> Iterator[tuple[str, list[str]]]:
    """Each field of `level` that finds its element by text a claim could
    spell, with that text: a testid, a css value's strings and names, and a
    held value. A role's name, a label and a placeholder are refused on an
    assertion's target as they are (`allowed_for_assertion`)."""
    if isinstance(level, ByTestId):
        yield "testid", [level.testid]
    if isinstance(level, ByCss):
        yield "css", list(css_strings(level.css))
        if level.holds_value is not None:
            yield "holds_value/value", [level.holds_value.value]


def _spelling(strings: list[str], claims: Sequence[TextInTarget]) -> str | None:
    """Why an assertion's target may not be found by `strings`, or None: one
    of them meets a claim as its check would, so its element would carry the
    text the check verifies by construction, and any element carrying that
    text could pass it (#261). A pattern is the script's and `strings` the
    heal's, so it runs bounded (ADR-0024), and a search that runs out of
    time or fails refuses them as well: it found neither a match nor its
    absence."""
    try:
        spelled = any(
            any(claim.matches(string) for string in strings)
            if claim.pattern is None
            else any_has_pattern(strings, claim.pattern)
            for claim in claims
        )
    except SearchTimeoutError:
        return (
            "a heal finds no assertion's target by a string where the search "
            "for its text check's pattern ran out of time"
        )
    except RuntimeError:
        return (
            "a heal finds no assertion's target by a string where the search "
            "for its text check's pattern failed"
        )
    if spelled:
        return (
            "a heal finds no assertion's target by a string that spells its "
            "text check's claim"
        )
    return None


def _written(
    path: str, locator: Locator, *, asserted: bool, claims: Sequence[TextInTarget]
) -> Iterator[str]:
    """Problems with a locator the heal wrote at `path`, at each of its
    levels, since a scope finds the element as much as its locator does: a
    css value that finds its element by position, and, for a target an
    assertion reads, a kind the generator never gives one (ADR-0025) or a
    string that spells a text check's claim on its meaning."""
    for where, level in locator_levels(path, locator, scope_key="/scope"):
        if isinstance(level, ByCss) and _positional(level.css):
            yield f"{where}/css: a heal finds no element by its position among its siblings"
        if not asserted:
            continue
        if not allowed_for_assertion(level, checks_text=bool(claims)):
            yield (
                f"{where}: a heal finds no assertion's target by a label or "
                "placeholder, or by a name when its text is checked"
            )
        for field, strings in _strings(level):
            if reason := _spelling(strings, claims):
                yield f"{where}/{field}: {reason}"


def text_claims(script: CompiledScript) -> dict[str, list[TextInTarget]]:
    """Each meaning's text checks. A meaning's text is checked when any
    target of it is, as the generator reads it, so each claim holds for
    every target of it."""
    claims: dict[str, list[TextInTarget]] = {}
    for check in script.assertions:
        if isinstance(check, TextInTarget):
            claims.setdefault(script.targets[check.target].semantic, []).append(check)
    return claims


def _target_problems(script: CompiledScript, healed: CompiledScript) -> Iterator[str]:
    """Targets match by name: each keeps its meaning and contract. Only a
    locator the heal wrote, one its target didn't have, is held to the
    grammar here, so a person's reviewed edit stands."""
    asserted = {
        check.target for check in script.assertions if isinstance(check, TargetCheck)
    }
    claims = text_claims(script)
    for name in healed.targets:
        if name not in script.targets:
            yield f"/targets/{pointer(name)}: a heal adds no target"
    for name, target in script.targets.items():
        path = f"/targets/{pointer(name)}"
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
                    claims=claims.get(target.semantic, []),
                )


def pointer(name: str) -> str:
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


# The actions a heal never inserts, each with why. A navigation or a reload
# could stand in for an interaction a user can't complete (#19).
_NOT_INSERTED = {
    "navigate": "a heal inserts no navigation, which could route around a path "
    "that is broken for its users",
    "reload": "a heal inserts no reload, which loads the page again and could "
    "route around a path that is broken for its users",
    "fill_secret": "a heal inserts no fill_secret, which could steer a bound "
    "secret into another field",
}


def _inserted_steps(steps: Sequence[Step], healed: Sequence[Step]) -> Iterator[str]:
    """A step with a new `seq` is inserted: replay-safe by its flag, not one
    of the actions above, and meeting no required condition, which stays
    with the step that met it. A path indexes the step in `healed`."""
    existing = {step.seq for step in steps}
    for index, step in enumerate(healed):
        if step.seq in existing:
            continue
        if step.side_effect:
            yield f"/steps/{index}/side_effect: a heal inserts only replay-safe steps"
        if step.action in _NOT_INSERTED:
            yield f"/steps/{index}/action: {_NOT_INSERTED[step.action]}"
        if step.satisfies:
            yield (
                f"/steps/{index}/satisfies: a heal marks no inserted step as "
                "meeting a condition"
            )


def heal_patch_problems(script: CompiledScript, healed: CompiledScript) -> list[str]:
    """Why `healed` is no heal of `script`, each problem a fixed reason after
    the JSON pointer of what it names; none when it is one. `healed` must
    have been read by `aqa_core.project.parse_compiled`, so the loader's
    rules already hold for it.

    Nothing here runs the script, so an inserted step's `side_effect: false`
    is only a flag here: the heal step must still show that, after the patch,
    every assertion evaluates and passes and every invariant holds, and the
    replay fails an inserted step that doesn't show, as it runs, that it
    changed nothing (`aqa_runner.repairs.inserted_failure`; DATA_MODEL §7).
    A text check's pattern is searched in a child process,
    though, so a call can block for up to `aqa_core.text.SEARCH_SECONDS`
    a search (ADR-0024)."""
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


def json_patch(
    committed: CompiledScript, final: CompiledScript
) -> list[dict[str, object]]:
    """The RFC 6902 patch from `committed` to `final`: one `replace` of a
    target's locators for each target whose locators differ, in `committed`'s
    order, and nothing else, since a heal changes nothing else that has a
    producer until #246. `final` must hold every target of `committed`, as a
    script the validator took does."""
    return [
        {
            "op": "replace",
            "path": f"/targets/{pointer(name)}/locators",
            "value": final.targets[name].model_dump(mode="json", exclude_none=True)[
                "locators"
            ],
        }
        for name, target in committed.targets.items()
        if final.targets[name].locators != target.locators
    ]


def blob_sha(raw: bytes) -> str:
    """Git's blob SHA of `raw`, as `git hash-object --no-filters` gives it
    (ADR-0024's #244 amendment): over the bytes as they are, a byte order mark
    included, and not for security."""
    header = b"blob %d\0" % len(raw)
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def proposal_id(spec_id: str, base: str, patch: Sequence[dict[str, object]]) -> str:
    """A heal proposal's id, derived from its content, so the same patch on the
    same base gets the same one: `ah_` and the first 12 hex of the sha256 of
    its sorted, compact JSON. 15 characters fit GitHub's 20-character action
    identifier (API.md §6)."""
    content = {"base": base, "patch": list(patch), "spec_id": spec_id}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return f"ah_{hashlib.sha256(canonical.encode()).hexdigest()[:12]}"


def literal_claim_spelled(locator: Locator, claims: Sequence[TextInTarget]) -> bool:
    """Whether a string `locator` finds its element by, at any level, meets a
    literal text claim as its check would: a locator the validator would
    refuse, so a healer's generation drops it, and the next of its kind is
    tried. A pattern claim is left to the validator, whose bounded search
    runs off the loop (#261)."""
    return any(
        claim.matches(string)
        for _, level in locator_levels("", locator, scope_key="/scope")
        for _, strings in _strings(level)
        for string in strings
        for claim in claims
        if claim.pattern is None
    )


# RFC 6901's array index, at most nine digits, as a `json_path`'s is (DATA_MODEL
# §7), and a `~` that escapes nothing.
_INDEX: Final = re.compile(r"0|[1-9][0-9]{0,8}")
_BARE_TILDE: Final = re.compile(r"~(?![01])")


class PatchOp(StrictModel):
    """One change a proposal asks for: RFC 6902's `add` and `replace`. A heal writes
    only `replace` until #246 gives `add` a producer. A proposal file is untrusted
    input, so `remove`, `move`, `copy`, `test` and any other member are refused
    rather than ignored."""

    op: Literal["add", "replace"]
    path: StrictStr
    value: JsonValue


class ProposalBase(StrictModel):
    """The compiled file a proposal applies to. A PR head has no producer until
    M5, and nothing here could say what a fork's head means, so one that isn't
    null is refused."""

    blob_sha: StrictStr
    pr_head_sha: None


class HealProposal(StrictModel):
    """A proposal as `replay.json` holds it (DATA_MODEL §7)."""

    id: StrictStr
    compiled: StrictStr
    base: ProposalBase
    patch: list[PatchOp]


def apply_patch(document: object, patch: Sequence[PatchOp]) -> None:
    """Apply `patch` to `document`, parsed JSON, in place and in order, each path
    read against what the last change left (RFC 6902). Any path is followed:
    `heal_patch_problems` judges the result, so what a path may name isn't decided
    here. A path it can't follow is a `SpecError`, naming that path."""
    for change in patch:
        *parents, last = _parts(change.path)
        at = document
        for part in parents:
            at = _child(at, part, change.path)
        _changed(at, last, change)


def _unfollowed(path: str) -> SpecError:
    return SpecError([f"{path}: a patch can't follow this path"])


def _parts(path: str) -> list[str]:
    """`path`'s parts, decoded in RFC 6901's order, so `~01` is `~1`.
    The empty pointer, the whole document, is no change a heal makes."""
    if not path.startswith("/") or _BARE_TILDE.search(path):
        raise _unfollowed(path)
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _child(at: object, part: str, path: str) -> object:
    if isinstance(at, dict) and part in at:
        return at[part]
    if isinstance(at, list) and (index := _position(part, len(at) - 1)) is not None:
        return at[index]
    raise _unfollowed(path)


def _changed(at: object, part: str, change: PatchOp) -> None:
    if isinstance(at, dict) and (change.op == "add" or part in at):
        at[part] = change.value
    elif isinstance(at, list) and change.op == "add":
        index = len(at) if part == "-" else _position(part, len(at))
        if index is None:
            raise _unfollowed(change.path)
        at.insert(index, change.value)
    elif isinstance(at, list) and (index := _position(part, len(at) - 1)) is not None:
        at[index] = change.value
    else:
        raise _unfollowed(change.path)


def _position(part: str, last: int) -> int | None:
    """The array index `part` spells, RFC 6901's digits and no others, or None
    when it spells none or one past `last`. `int` alone would take `-1`, `1_0`,
    `٣` and a space, and refuse more digits than it reads."""
    if _INDEX.fullmatch(part) is None or int(part) > last:
        return None
    return int(part)
