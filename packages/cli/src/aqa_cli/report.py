"""`aqa report [<run_id>]` (API.md §7; #249): write the run viewer of a finished
`aqa replay`, `aqa run` or `aqa heal` run, one offline HTML file, into its own
record, `<spec root>/.aqa/runs/<run_id>/report.html`, and print its path.

The spec root is found as `aqa heal apply` finds it (`common.spec_root_above`).
Nothing here loads the project or reads a spec, the config or a compiled file:
the page shows only what the run's record holds (`report_record`). It binds no
test secret, starts no browser and builds no model client, so what it prints is
escaped and not scanned (`common.rendered`). Every refusal is one fixed line and
an existing exit code, and writes no report."""

import json
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Final, NoReturn

import typer
from aqa_core.project import SpecError
from aqa_runner.publication import _write_new
from aqa_runner.redaction import NO_SECRETS
from aqa_runner.replay_result import EXIT_CODES

from aqa_cli.common import Failure, say, spec_root_above, stop
from aqa_cli.report_page import page
from aqa_cli.report_record import read_record

# A run's id as `RunRecord.create` names its record: a UUIDv7, lowercase, in
# canonical form. Checked before any path is built from it.
_RUN_ID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
# What an explore run's record holds in place of `replay.json` (#53, #56).
_EXPLORED: Final = ("explore.json", "plan.json", "attempt-1")
# The subject of a refusal when no id was given.
_NEWEST: Final = "report"
_NONE: Final = "no report was written"


def report(
    run_id: Annotated[
        str | None,
        typer.Argument(
            metavar="RUN_ID",
            help="The run to report: its record's directory name under "
            ".aqa/runs/. The newest run with a replay.json if omitted.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Write the run viewer of a finished replay or run, as one offline HTML
    file, into that run's record, and print its path. It opens no browser."""
    if run_id is not None and _RUN_ID.fullmatch(run_id) is None:
        raise typer.BadParameter(
            "a run's id is a lowercase UUIDv7, as its record's directory is named",
            param_hint="'RUN_ID'",
        )
    subject = run_id or _NEWEST
    try:
        root = spec_root_above(Path.cwd())
    except OSError:
        _refuse("record_error", subject, "the working directory could not be read")
    except SpecError as error:
        _refuse("spec_error", subject, error.problems[0])
    runs = _runs(root, subject)
    record = _newest(runs, subject) if run_id is None else _named(runs, run_id)
    written = _written(record, page(read_record(record, _document(record))))
    say(NO_SECRETS, f"report {record.name}: {written}")


def _refuse(outcome: Failure, subject: str, why: str) -> NoReturn:
    stop(outcome, subject, f"{why}, so {_NONE}")


def _runs(root: Path, subject: str) -> Path:
    """`root`'s run records, refused when `.aqa` or `.aqa/runs` is a link, which a
    repository can commit pointing anywhere (`RunRecord.create`)."""
    records = root / ".aqa"
    runs = records / "runs"
    for place in (records, runs):
        if place.is_symlink():
            _refuse(
                "spec_error",
                subject,
                f"{place}: a link, and a run record stays in {root}",
            )
    return runs


def _newest(runs: Path, subject: str) -> Path:
    """The newest run in `runs` with a `replay.json`: the greatest UUIDv7 name,
    since they are time-ordered, skipping a linked run or document."""
    try:
        names = (
            sorted((e.name for e in runs.iterdir()), reverse=True)
            if runs.is_dir()
            else []
        )
    except OSError:
        _refuse("record_error", subject, "the run records could not be read")
    for name in names:
        record = runs / name
        document = record / "replay.json"
        if (
            _RUN_ID.fullmatch(name)
            and not record.is_symlink()
            and not document.is_symlink()
            and document.is_file()
        ):
            return record
    _refuse("spec_error", subject, f"no run record under {runs} has a replay.json")


def _named(runs: Path, run_id: str) -> Path:
    """The record of the run `run_id`, which must be a finished replay's or run's."""
    record = runs / run_id
    if record.is_symlink():
        _refuse(
            "spec_error",
            run_id,
            f"{record}: a link, and a run record stays in {runs.parents[1]}",
        )
    if not record.is_dir():
        _refuse("spec_error", run_id, f"no run record under {runs} has this id")
    document = record / "replay.json"
    if document.is_symlink():
        _refuse(
            "spec_error",
            run_id,
            "its replay.json is a link, which aqa report doesn't follow",
        )
    if not document.is_file():
        if any(os.path.lexists(record / name) for name in _EXPLORED):
            _refuse(
                "spec_error",
                run_id,
                "an explore run's record, which aqa report doesn't read yet",
            )
        _refuse(
            "spec_error",
            run_id,
            "a run that didn't finish: its record has no replay.json",
        )
    return record


def _document(record: Path) -> dict[str, object]:
    """The run's `replay.json`, read through no link, when it is a document aqa
    writes: schema 1, this run's id, a mode, an outcome and a list of verdicts."""
    run_id = record.name
    try:
        handle = os.open(record / "replay.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(handle, "rb") as file:
            document: object = json.loads(file.read().decode("utf-8"))
    # ValueError: not UTF-8, not JSON, or an integer longer than Python reads.
    except OSError, ValueError, RecursionError:
        _refuse("record_error", run_id, "its replay.json could not be read")
    match document:
        case {
            "schema_version": int() as version,
            "run_id": str() as held,
            "mode": "strict" | "run",
            "outcome": str() as outcome,
            "verdicts": list(),
        } if (
            isinstance(document, dict)
            and type(version) is int
            and version == 1
            and held == run_id
            and outcome in EXIT_CODES
        ):
            return document
    _refuse("spec_error", run_id, "its replay.json isn't a document aqa writes")


def _written(record: Path, text: str) -> Path:
    """`text` written whole to a new temporary file in `record`, forced, then
    renamed over its `report.html`, so a link there is replaced, never
    followed, and a failure leaves neither file behind. Text no UTF-8 can hold,
    a lone surrogate from a record's JSON, is written as its escape."""
    target = record / "report.html"
    temporary = record / f".report.{os.urandom(8).hex()}.tmp"
    try:
        _write_new(temporary, text.encode("utf-8", "backslashreplace"))
        temporary.replace(target)
    except OSError:
        with suppress(OSError):
            temporary.unlink()
        stop("record_error", record.name, "the report could not be written")
    return target
