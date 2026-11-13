"""What `aqa report` reads of a finished run's record (API.md §7; #249). Every
file below the run's directory is untrusted input, since a repository can commit
a record and a person can edit one. So each is read through no link, parsed
defensively and type-checked before use. A file that is missing, linked,
unreadable or malformed, or an entry in one that is, gives what it would show a
fixed state (`Gap`), and never stops the report. Nothing here reads the spec, the
config or a compiled file: what the run's own record doesn't hold is "not
recorded"."""

import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

from aqa_core.compiled import CompiledScript
from aqa_core.heal_patch import (
    HealProposal,
    apply_patch,
    heal_patch_problems,
    proposal_id,
)
from aqa_core.model_costs import CostRecord
from aqa_core.project import SpecError
from aqa_core.schema import StrictModel
from aqa_runner.heal_proposal import diff
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError


class Gap(StrEnum):
    """Why a field, a tab or a diff shows nothing of the record: the words it
    shows instead."""

    ABSENT = "not recorded"
    BROKEN = "couldn't be read"
    NOT_ONE = "the proposal isn't one aqa run writes"


# A directory below the run's, and a file, each opened only if it isn't a link.
# The file is opened without blocking, so a FIFO in a record never stalls it.
_FOLDER: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# The paths `replay.json` may cite (DATA_MODEL §7), and the files a link names in
# each: an evidence folder's three, and a heal's turns and first look.
_EVIDENCE: Final = re.compile(r"evidence/[0-9]+")
_REPAIR: Final = re.compile(r"repairs/(?:assertion-)?[0-9]+\.json")
_HEAL: Final = re.compile(r"heals/(?:assertion-)?[0-9]+")
_EVIDENCE_FILES: Final = ("a11y_snapshot.yaml", "console_log.json", "network_log.json")
_HEAL_FILE: Final = re.compile(
    r"[0-9]+\.json|a11y_snapshot\.yaml|console_log\.json|network_log\.json"
)
# What a step's intent line leaves out of its compiled step (`admission.intent_action`).
_NOT_IN_AN_INTENT: Final = {"seq", "side_effect", "side_effect_basis", "satisfies"}
VERDICTS: Final = ("pass", "expectation_violated", "inconclusive", "drift_consistent")


@contextmanager
def _folder(run: Path, folders: Sequence[str]) -> Iterator[int]:
    """The directory `folders` names below `run`, open, each step through no link."""
    handle = os.open(run, _FOLDER)
    try:
        for folder in folders:
            inner = os.open(folder, _FOLDER, dir_fd=handle)
            os.close(handle)
            handle = inner
        yield handle
    finally:
        os.close(handle)


def read_below(run: Path, name: str) -> bytes | Gap:
    """The bytes of the regular file `name`, a relative path of names below the
    run's directory `run`, reached through no link; `Gap.ABSENT` when it or a
    directory on the way doesn't exist, and `Gap.BROKEN` for a link, anything but a
    regular file, or an `OSError`."""
    *folders, leaf = name.split("/")
    try:
        with _folder(run, folders) as folder:
            file = os.open(leaf, _FILE, dir_fd=folder)
            try:
                if not stat.S_ISREG(os.fstat(file).st_mode):
                    return Gap.BROKEN
                return _whole(file)
            finally:
                os.close(file)
    except FileNotFoundError:
        return Gap.ABSENT
    except OSError:
        return Gap.BROKEN


def _whole(file: int) -> bytes:
    chunks = []
    while chunk := os.read(file, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)


def _regular(run: Path, name: str) -> bool:
    """Whether `name` below `run` is a regular file reached through no link."""
    *folders, leaf = name.split("/")
    try:
        with _folder(run, folders) as folder:
            found = os.stat(leaf, dir_fd=folder, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(found.st_mode)


def _files(run: Path, name: str) -> list[str]:
    """The names of the regular files, not links, in the directory `name` below
    `run`, sorted; none when it can't be listed."""
    try:
        with _folder(run, name.split("/")) as folder, os.scandir(folder) as found:
            return sorted(e.name for e in found if e.is_file(follow_symlinks=False))
    except OSError:
        return []


def _json_below(run: Path, name: str) -> object | Gap:
    """The JSON `name` below `run` holds; an empty file is `Gap.ABSENT`, and one
    that isn't UTF-8 JSON is `Gap.BROKEN`."""
    raw = read_below(run, name)
    if isinstance(raw, Gap):
        return raw
    if not raw.strip():
        return Gap.ABSENT
    try:
        loaded: object = json.loads(raw.decode("utf-8"))
    # ValueError: not UTF-8, not JSON, or an integer longer than Python reads.
    except ValueError, RecursionError:
        return Gap.BROKEN
    return loaded


type Cited = tuple[str, ...] | None
"""What a verdict cites: the files a link may name, each a relative path to a
regular file inside the run's directory; none (an empty tuple) when it cites a
path that isn't one of the record's, which shows as "evidence not found"; or
None when it cites nothing."""


def _evidence(run: Path, path: object) -> Cited:
    if path is None:
        return None
    if not isinstance(path, str) or _EVIDENCE.fullmatch(path) is None:
        return ()
    return tuple(
        f"{path}/{name}" for name in _EVIDENCE_FILES if _regular(run, f"{path}/{name}")
    )


def _repair(run: Path, path: object) -> Cited:
    if path is None:
        return None
    if isinstance(path, str) and _REPAIR.fullmatch(path) and _regular(run, path):
        return (path,)
    return ()


def _heal_record(run: Path, path: object) -> Cited:
    if path is None:
        return None
    if not isinstance(path, str) or _HEAL.fullmatch(path) is None:
        return ()
    return tuple(
        f"{path}/{name}" for name in _files(run, path) if _HEAL_FILE.fullmatch(name)
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if type(value) is int else None


def _number(value: object) -> float | None:
    """`value` as a finite float, or None: JSON's integers have no bound, so
    one may hold no float."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(each for each in value if isinstance(each, str))


@dataclass(frozen=True)
class Finding:
    """A violation's finding: its category and title, each None when it isn't
    text."""

    category: str | None
    title: str | None


@dataclass(frozen=True)
class Heal:
    """What a verdict in a run that heals cites of its heal: the repair, its file
    if the record holds one, the heal's own files, what it bound, the page's ARIA
    role and name, and what the model reported of that binding."""

    repair_path: str | None
    repair: Cited
    record: Cited
    bound: str | None
    confidence: float | None
    rationale: str | None


@dataclass(frozen=True)
class Verdict:
    """One verdict of `replay.json`: what it is on, `kind` None for an entry that
    isn't one; the verdict, None when it isn't one of `VERDICTS`; why; each
    locator's miss; the locator that found its target; the evidence folder it
    cites, if it is one of the record's, and the files a link may name in it; its
    finding; and its heal."""

    kind: str | None
    name: str | None
    seq: int | None
    verdict: str | None
    reason: str | None
    misses: tuple[str, ...]
    locator_index: int | None
    evidence_path: str | None
    evidence: Cited
    finding: Finding | None
    heal: Heal | None


_UNREADABLE: Final = Verdict(
    None, None, None, None, None, (), None, None, None, None, None
)


def _subject(entry: Mapping[str, object]) -> tuple[str, str, int | None] | None:
    if (seq := _integer(entry.get("step"))) is not None:
        return "step", str(seq), seq
    for kind in ("assertion", "invariant"):
        if (name := _text(entry.get(kind))) is not None:
            return kind, name, None
    return None


def _finding(value: object) -> Finding | None:
    if not isinstance(value, dict):
        return None
    return Finding(_text(value.get("category")), _text(value.get("title")))


def _heal(run: Path, value: object) -> Heal | None:
    if not isinstance(value, dict):
        return None
    reported = value.get("model_reported")
    if not isinstance(reported, dict):
        reported = {}
    path = value.get("repair")
    return Heal(
        path if isinstance(path, str) and _REPAIR.fullmatch(path) else None,
        _repair(run, path),
        _heal_record(run, value.get("record")),
        _text(value.get("bound")),
        _number(reported.get("confidence")),
        _text(reported.get("rationale")),
    )


def _verdict(run: Path, entry: object) -> Verdict:
    """The verdict `entry` gives, each field type-checked on its own, so one that
    is malformed leaves the rest."""
    if not isinstance(entry, dict) or (subject := _subject(entry)) is None:
        return _UNREADABLE
    kind, name, seq = subject
    given = entry.get("verdict")
    evidence = entry.get("evidence")
    path = evidence.get("path") if isinstance(evidence, dict) else evidence
    cited = _evidence(run, path)
    return Verdict(
        kind,
        name,
        seq,
        given if given in VERDICTS else None,
        _text(entry.get("reason")),
        _texts(entry.get("misses")),
        _integer(entry.get("locator_index")),
        path if isinstance(path, str) and cited else None,
        cited,
        _finding(entry.get("finding")),
        _heal(run, entry.get("heal")),
    )


class _Healer(StrictModel):
    provider: StrictStr
    model: StrictStr
    effort: StrictStr | None
    fallback: StrictStr | None


class _Models(StrictModel):
    healer: _Healer | None = None


class Kept(StrictModel):
    """`run.json` (DATA_MODEL §7), as `strict_replay` writes it."""

    schema_version: StrictInt
    run_id: StrictStr
    spec_id: StrictStr
    goal: StrictStr
    expectations: tuple[StrictStr, ...]
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    models: _Models
    script: CompiledScript


def _kept(run: Path, spec_id: str | None) -> Kept | Gap:
    """The run's `run.json`, only when it validates and is this run's and spec's.
    Any other, like a record made before #249 wrote one, is `Gap.ABSENT`: what it
    would give is not recorded."""
    raw = read_below(run, "run.json")
    if isinstance(raw, Gap):
        return Gap.ABSENT
    try:
        kept = Kept.model_validate_json(raw)
    except ValidationError:
        return Gap.ABSENT
    if (kept.schema_version, kept.run_id, kept.spec_id) != (1, run.name, spec_id):
        return Gap.ABSENT
    return kept


def _cost(run: Path) -> Decimal | Gap:
    """What the run's priced calls cost: the sum of `costs.jsonl`'s, nothing when
    there is none, and `Gap.BROKEN` when a line isn't a `CostRecord` or the sum
    is past what a `Decimal` holds (`decimal.Overflow`, an `ArithmeticError`):
    a `CostRecord` takes any finite `Decimal`, such as 1e999999999."""
    raw = read_below(run, "costs.jsonl")
    if raw is Gap.ABSENT:
        return Decimal(0)
    if isinstance(raw, Gap):
        return raw
    try:
        lines = raw.decode("utf-8").splitlines()
        calls = [CostRecord.model_validate_json(line) for line in lines]
        return sum((call.cost_usd for call in calls), Decimal(0))
    except ValueError, ArithmeticError:
        return Gap.BROKEN


@dataclass(frozen=True)
class Intent:
    """A step's intent line in `steps.jsonl`: its action as compiled, and the
    target it acted on."""

    action: Mapping[str, object]
    target: str | None


def _intents(run: Path) -> dict[int, Intent]:
    """Each step's first intent line that reads as one, by seq; none from a
    journal that can't be read."""
    raw = read_below(run, "steps.jsonl")
    if isinstance(raw, Gap):
        return {}
    intents: dict[int, Intent] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError, RecursionError:
            continue
        if not isinstance(entry, dict) or entry.get("state") != "intent":
            continue
        seq, action = _integer(entry.get("seq")), entry.get("action")
        if seq is not None and isinstance(action, dict) and seq not in intents:
            intents[seq] = Intent(action, _text(entry.get("target_used")))
    return intents


@dataclass(frozen=True)
class Record:
    """What the report shows of one run's record: `replay.json`'s fields, checked
    as `aqa report` checks them, its verdicts, its proposal as written, the run's
    `run.json`, its cost, and its steps' intents."""

    run: Path
    run_id: str
    spec_id: str | None
    mode: str
    outcome: str
    withheld: bool
    reasons: tuple[str | None, ...]
    verdicts: tuple[Verdict, ...]
    proposal: object
    kept: Kept | Gap
    cost: Decimal | Gap
    intents: Mapping[int, Intent]


def read_record(run: Path, document: Mapping[str, object]) -> Record:
    """The record of the run in the directory `run`, whose `replay.json` is
    `document`, already checked to be one aqa writes (`aqa_cli.report`): its
    `run_id`, `mode` and `outcome` are text, and its `verdicts` a list."""
    spec_id = _text(document.get("spec_id"))
    reasons = document.get("reasons")
    verdicts = document["verdicts"]
    return Record(
        run,
        str(document["run_id"]),
        spec_id,
        str(document["mode"]),
        str(document["outcome"]),
        document.get("withheld") is True,
        tuple(map(_text, reasons)) if isinstance(reasons, list) else (),
        tuple(_verdict(run, each) for each in _items(verdicts)),
        document.get("heal_proposal"),
        _kept(run, spec_id),
        _cost(run),
        _intents(run),
    )


def _items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class ConsoleEntry:
    """A console message: its type, and its text, None when it holds none."""

    type: str
    text: str | None


@dataclass(frozen=True)
class Request:
    """A request: its method and URL, each None when it holds none, and its
    response's status, None when it had none."""

    method: str | None
    url: str | None
    status: int | None


@dataclass(frozen=True)
class Log[T]:
    """A step's console or network log: each entry, None for one that isn't one,
    and how many the window saw, None when the log doesn't say."""

    entries: tuple[T | None, ...]
    total: int | None


def _console_entry(entry: object) -> ConsoleEntry | None:
    if not isinstance(entry, dict) or (kind := _text(entry.get("type"))) is None:
        return None
    return ConsoleEntry(kind, _text(entry.get("text")))


def _request(entry: object) -> Request | None:
    if not isinstance(entry, dict):
        return None
    return Request(
        _text(entry.get("method")),
        _text(entry.get("url")),
        _integer(entry.get("status")),
    )


def _log[T](
    run: Path, evidence: str | None, name: str, entry: Callable[[object], T | None]
) -> Log[T] | Gap:
    """The log `name` of the evidence folder `evidence`, if it has one: an empty
    one is `Gap.ABSENT`, and one with no list of entries `Gap.BROKEN`."""
    if evidence is None:
        return Gap.ABSENT
    document = _json_below(run, f"{evidence}/{name}")
    if isinstance(document, Gap):
        return document
    match document:
        case {"entries": list() as entries, **rest}:
            return Log(tuple(map(entry, entries)), _integer(rest.get("total")))
    return Gap.BROKEN


def console_log(run: Path, evidence: str | None) -> Log[ConsoleEntry] | Gap:
    """The console log of the evidence folder `evidence`, if it has one."""
    return _log(run, evidence, "console_log.json", _console_entry)


def network_log(run: Path, evidence: str | None) -> Log[Request] | Gap:
    """The network log of the evidence folder `evidence`, if it has one."""
    return _log(run, evidence, "network_log.json", _request)


def snapshot(run: Path, evidence: str | None) -> str | Gap:
    """The accessibility snapshot of the evidence folder `evidence`, if it has
    one: an empty one is `Gap.ABSENT`, and one that isn't UTF-8 `Gap.BROKEN`."""
    if evidence is None:
        return Gap.ABSENT
    raw = read_below(run, f"{evidence}/a11y_snapshot.yaml")
    if isinstance(raw, Gap):
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Gap.BROKEN
    return text if text.strip() else Gap.ABSENT


class _RepairFile(BaseModel):
    """What the report reads of a repair's file: the script it offered, if any."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    script: CompiledScript | None


def repair_script(run: Path, path: str) -> CompiledScript | Gap | None:
    """The script the repair at `path`, a path `_REPAIR` matches, offered: None
    when it offered none, and `Gap` when its file is missing or isn't one."""
    raw = read_below(run, path)
    if isinstance(raw, Gap):
        return raw
    try:
        return _RepairFile.model_validate_json(raw).script
    except ValidationError:
        return Gap.BROKEN


def compiled_action(script: CompiledScript, seq: int) -> dict[str, object] | None:
    """Step `seq` of `script` as its intent line records it
    (`admission.intent_action`), or None when it has none, as seq 0 hasn't."""
    for step in script.steps:
        if step.seq == seq:
            return step.model_dump(mode="json", exclude=_NOT_IN_AN_INTENT)
    return None


def heal_diff(record: Record) -> tuple[str, ...] | Gap | None:
    """The diff of the record's heal proposal, the lines `aqa run` printed, only
    for a proposal `aqa run` writes: one `heal apply` would take, whose patch the
    heal-patch validator allows on `run.json`'s script. None with no proposal,
    `Gap.ABSENT` with no `run.json`, and `Gap.NOT_ONE` for any other."""
    if record.proposal is None:
        return None
    proposal = _proposal(record.spec_id, record.proposal)
    if proposal is None or record.spec_id is None:
        return Gap.NOT_ONE
    if isinstance(record.kept, Gap):
        return Gap.ABSENT
    recorded = record.kept.script
    patched = _patched(recorded, proposal)
    if patched is None:
        return Gap.NOT_ONE
    return tuple(diff(record.spec_id, recorded, patched))


def _proposal(spec_id: str | None, held: object) -> HealProposal | None:
    """`held` read as `heal apply` reads a proposal (`heal_proposal._checked`):
    the strict shape, an id that is its content's, and `compiled` the spec's own
    file; None for any other."""
    try:
        proposal = HealProposal.model_validate(held)
    except ValidationError:
        return None
    patch = [change.model_dump(mode="json") for change in proposal.patch]
    if (
        spec_id is None
        or proposal.id != proposal_id(spec_id, proposal.base.blob_sha, patch)
        or proposal.compiled != f".compiled/{spec_id}.json"
    ):
        return None
    return proposal


def _patched(recorded: CompiledScript, proposal: HealProposal) -> CompiledScript | None:
    """`proposal` applied to the script the run replayed, when the result is a
    compiled script and the heal-patch validator allows it; None otherwise."""
    document = recorded.model_dump(mode="json", exclude_none=True)
    try:
        apply_patch(document, proposal.patch)
        patched = CompiledScript.model_validate_json(json.dumps(document))
    except SpecError, ValidationError, RecursionError:
        return None
    return None if heal_patch_problems(recorded, patched) else patched
