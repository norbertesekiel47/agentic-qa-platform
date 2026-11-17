"""A heal proposal (ADR-0024's #243, #244 and #247 amendments, DATA_MODEL §7): what a
run that healed asks a person to accept, as `replay.json` holds it and as `aqa
run` prints it, and what `aqa heal apply` reads back. A proposal file is untrusted
input, so nothing it says is believed because it says it."""

import difflib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.heal_patch import (
    HealProposal,
    apply_patch,
    heal_patch_problems,
    json_patch,
    proposal_id,
)
from aqa_core.project import SpecError, parse_compiled
from aqa_core.spec import Spec
from pydantic import ValidationError

from aqa_runner.admission import admissible
from aqa_runner.publication import BYTE_ORDER_MARK, document_text, published_text
from aqa_runner.repairs import Repaired

# Why a proposal in a run record isn't applied, in fixed words.
_NOT_ONE: Final = "the proposal isn't one aqa run writes"
_EDITED: Final = "the proposal doesn't match its id, so it was edited after its run"
_DIFFER: Final = (
    "run records hold different proposals under this id, so at least one was edited"
)


def final_script(
    committed: CompiledScript, repairs: tuple[Repaired, ...]
) -> CompiledScript:
    """The script a run went on with last: its last taken repair's, else the
    committed one."""
    taken = [repaired.script for repaired in repairs if repaired.script is not None]
    return taken[-1] if taken else committed


def rebinds(
    committed: CompiledScript, repairs: tuple[Repaired, ...]
) -> Iterator[tuple[str, Repaired]]:
    """Each change a taken repair made to the locators a target had, in order:
    the target's name, and the repair. Adding a target binds nothing anew."""
    previous = committed
    for repaired in repairs:
        if (script := repaired.script) is not None:
            for name, target in script.targets.items():
                was = previous.targets.get(name)
                if was is not None and target.locators != was.locators:
                    yield name, repaired
            previous = script


def proposal_document(
    spec_id: str, base: str, committed: CompiledScript, final: CompiledScript
) -> dict[str, object]:
    """The proposal `replay.json` holds: its content-derived id, the compiled
    file it applies to, the blob SHA that file had, with no PR head locally
    (M5's), and the patch."""
    patch = json_patch(committed, final)
    return {
        "id": proposal_id(spec_id, base, patch),
        "compiled": f".compiled/{spec_id}.json",
        "base": {"blob_sha": base, "pr_head_sha": None},
        "patch": patch,
    }


def proposal_lines(
    spec_id: str,
    proposal: str,
    committed: CompiledScript,
    repairs: tuple[Repaired, ...],
) -> tuple[str, ...]:
    """What `aqa run` prints of a proposal: its id, each rebound target's meaning
    and the element it is bound to, then the diff, which also shows each target
    the heal added and each step it inserted. The role and name are the page's
    own ARIA, so a decoy can match them, and a reviewer's evidence is the diff
    (ADR-0024's #244 and #246 amendments)."""
    final = final_script(committed, repairs)
    bound = {
        name: "" if repaired.basis is None else repaired.basis.said
        for name, repaired in rebinds(committed, repairs)
    }
    return (
        f"heal proposal {proposal}:",
        *(
            f"{name} ({target.semantic}): bound {bound[name]}"
            for name, target in committed.targets.items()
            if final.targets[name].locators != target.locators
        ),
        *diff(spec_id, committed, final),
    )


def diff(spec_id: str, committed: CompiledScript, final: CompiledScript) -> list[str]:
    """The patch as a person reads it: a unified diff of the published file,
    each line its own, which the output boundary scans and escapes."""
    path = f".compiled/{spec_id}.json"
    return list(
        difflib.unified_diff(
            published_text(committed).splitlines(),
            published_text(final).splitlines(),
            f"a/{path}",
            f"b/{path}",
            lineterm="",
        )
    )


@dataclass(frozen=True)
class _Held:
    """A proposal a run record holds under the id asked for, as its file says it:
    the record's directory, the spec it names, and the proposal, not yet read."""

    record: Path
    spec_id: str
    proposal: object


def held_proposal(root: Path, proposal: str) -> tuple[str, HealProposal]:
    """The proposal the run records under the spec root `root` hold under the id
    `proposal`, and the spec it names, or a `SpecError`. Listing the records may
    raise its `OSError`.

    What a file says is believed only once the id the person typed is the id of
    its content, so one edited under its id is refused, and one given a new id
    isn't found. `compiled` and a PR head are outside that id, so they must be what
    `aqa run` writes. Two runs on one drift hold one proposal under one id, which is
    no ambiguity. Records that differ under one id can't both be a run's, and which
    was edited can't be told, so neither is applied."""
    found, unread = _records_holding(root, proposal)
    if not found:
        also = f", and {unread} couldn't be read" if unread else ""
        raise SpecError([f"no run record under {root} holds it{also}"])
    first = found[0]
    if any((h.spec_id, h.proposal) != (first.spec_id, first.proposal) for h in found):
        records = (str(held.record) for held in found)
        raise SpecError([_DIFFER, *records])
    return first.spec_id, _checked(first.spec_id, first.proposal, proposal)


def _records_holding(root: Path, proposal: str) -> tuple[list[_Held], int]:
    """What the run records under `root` hold under the id `proposal`, and how many
    `replay.json` couldn't be read, which stop nothing: an unrelated broken record
    mustn't block a proposal. A link at `.aqa` or `.aqa/runs` is a spec error, as for
    a record being made (`RunRecord.create`), and a linked run or document holds
    nothing, since a repository can commit one pointing anywhere."""
    records = root / ".aqa"
    runs = records / "runs"
    for place in (records, runs):
        if place.is_symlink():
            raise SpecError([f"{place}: a link, and a run record stays in {root}"])
    if not runs.is_dir():
        return [], 0
    found: list[_Held] = []
    unread = 0
    for record in sorted(runs.iterdir()):
        file = record / "replay.json"
        if record.is_symlink() or file.is_symlink() or not file.is_file():
            continue
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        # ValueError: not UTF-8, not JSON, or an integer longer than Python reads.
        except OSError, ValueError, RecursionError:
            unread += 1
            continue
        match document:
            case {
                "spec_id": str() as spec_id,
                "heal_proposal": {"id": held} as raw,
            } if held == proposal:
                found.append(_Held(record, spec_id, raw))
    return found, unread


def _checked(spec_id: str, held: object, proposal: str) -> HealProposal:
    """`held` read as a proposal, once the id typed is the id of its content."""
    try:
        checked = HealProposal.model_validate(held)
    except ValidationError:
        # Its errors quote the file, and a refusal says only what it decides.
        raise SpecError([_NOT_ONE]) from None
    patch = [change.model_dump(mode="json") for change in checked.patch]
    if (
        proposal_id(spec_id, checked.base.blob_sha, patch) != proposal
        or checked.compiled != f".compiled/{spec_id}.json"
    ):
        raise SpecError([_EDITED])
    return checked


@dataclass(frozen=True)
class Patched:
    """What a proposal makes of a compiled script: the script, the text to write,
    and whether that text changes formatting outside the patch."""

    script: CompiledScript
    text: str
    reformatted: bool


def patched_script(
    raw: bytes,
    committed: CompiledScript,
    proposal: HealProposal,
    *,
    spec: Spec,
    config: ProjectConfig,
    source: Path,
) -> Patched:
    """`proposal` applied to the compiled file at `source`, whose bytes are `raw`
    and whose script is `committed`, or a `SpecError`. The result goes through the
    loader, the heal-patch validator, against `committed`, and what replay admits
    before it binds a secret, so nothing in the file decides what it may change.
    Nothing here runs the script: that is the run's, and the next replay's.

    The text keeps a byte order mark. A file as explore publishes it is written as
    `published_text`, so the patched values' lines alone change and the file's diff
    is the one `aqa run` printed, though `replay.json` sorted those values' keys. Any
    other file is written from its own parsed tree, since `published_text` would
    change paths the patch doesn't name: a default it adds, a null it drops."""
    whole = raw.decode()
    text = whole.removeprefix(BYTE_ORDER_MARK)
    document = json.loads(text)
    reformatted = document_text(document) != text
    apply_patch(document, proposal.patch)
    written = document_text(document)
    healed = parse_compiled(written, config, source=source)
    if problems := heal_patch_problems(committed, healed):
        raise SpecError(problems)
    admissible(healed, spec, config)
    if published_text(committed) == text:
        written = published_text(healed)
    mark = BYTE_ORDER_MARK if whole.startswith(BYTE_ORDER_MARK) else ""
    return Patched(healed, mark + written, reformatted)
