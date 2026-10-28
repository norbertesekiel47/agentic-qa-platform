"""`aqa replay` (API.md §7; ADR-0024's #238 and #239 amendments): replay
each spec's committed compiled script in strict mode, with no model and no
heal, say how each run ended in one line, and exit with the runs' outcome
rolled up (`replay_result.rolled_up`).

Every spec is admitted before any run, so before any reset hook is sent
and before Playwright's driver starts, which must inherit no environment a
test secret's binding refused (#215): it resolves; its compiled script,
`<spec root>/.compiled/<spec id>.json`, read as explore reads it, with no
link on the way (`publication`), exists and loads, and replay admits it
(`executor.admitted`), each a spec error (exit 5), or 15 for a file that
can't be read; and its test secrets bind (12). Then each runs in argument
order (`aqa_runner.strict_replay`), on one driver.

Everything printed goes through `aqa_cli.common.rendered`, scanned with
every test secret the invocation bound."""

import asyncio
import json
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Annotated, Final

import typer
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_compiled
from aqa_core.spec import Spec
from aqa_runner.bound_secrets import BoundSecret, MissingSecretError, SecretLoggedError
from aqa_runner.executor import BoundValueSpelledError, admitted
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.publication import publication
from aqa_runner.redaction import Redactor
from aqa_runner.replay_result import EXIT_CODES, rolled_up
from aqa_runner.strict_replay import Replayed, StrictRun, strict_replay
from playwright.async_api import async_playwright

from aqa_cli.common import (
    WITHHELD,
    UnstartedError,
    reasons_in,
    resolve,
    say,
    started,
    stop,
)

_NOTHING: Final = "nothing was replayed"
_MISSING: Final = "no compiled script: explore it first"
_UNREAD: Final = "the compiled script could not be read, so nothing was replayed"
_UNLOADED: Final = "the compiled script doesn't load, so nothing was replayed"
_REFUSED: Final = "replay refuses the compiled script, so nothing was replayed"


def replay(
    spec_paths: Annotated[
        list[Path], typer.Argument(metavar="SPEC...", help="The spec files to replay.")
    ],
    *,
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="The start origin; the project config's base_url if omitted."
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="Print each run's replay.json, in one JSON array, only."
        ),
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", help="Print only the runs that didn't pass.")
    ] = False,
) -> None:
    """Replay specs' compiled scripts in strict mode, with no model call and
    no heal, each in a sandboxed browser, and write each run's verdicts to
    its run record's replay.json."""
    if as_json and quiet:
        raise typer.BadParameter("not with --quiet", param_hint="'--json'")
    try:
        runs = _admitted(spec_paths, url)
        scan = _scan(runs)
        try:
            ended = asyncio.run(_replayed(runs, scan, lines=not as_json, quiet=quiet))
        except UnstartedError as error:
            subject = ", ".join(run.spec.frontmatter.id for run in runs)
            stop("unreachable", subject, f"{error}, so {_NOTHING}", scan=scan)
    except typer.Exit:
        # A stop before any run.
        if as_json:
            typer.echo("[]")
        raise
    if as_json:
        say(scan, *json.dumps([e.document for e in ended], indent=2).splitlines())
    if code := EXIT_CODES[rolled_up(e.outcome for e in ended)]:
        raise typer.Exit(code)


def _admitted(paths: Sequence[Path], url: str | None) -> list[StrictRun]:
    """Each spec at `paths`, admitted for a strict run (module docstring),
    or a stop, scanned with the test secrets the specs before it bound, and
    its own once admission bound them."""
    runs: list[StrictRun] = []
    for path in paths:
        scan = _scan(runs)
        try:
            root, spec, config, start = resolve(path, url)
        except SpecError as error:
            stop("spec_error", str(path), _NOTHING, error.problems, scan=scan)
        spec_id = spec.frontmatter.id
        script = _compiled(root, spec, config, scan)
        try:
            _, _, bound, _ = admitted(script, spec, config, start)
        except BoundValueSpelledError as error:
            scan = _scan(runs, error.bound.values())
            stop("spec_error", spec_id, _REFUSED, error.problems, scan=scan)
        except SpecError as error:
            stop("spec_error", spec_id, _REFUSED, error.problems, scan=scan)
        except MissingSecretError as error:
            stop("secret_unusable", spec_id, _NOTHING, error.problems, scan=scan)
        except SecretLoggedError as error:
            stop("secret_unusable", spec_id, _NOTHING, (str(error),), scan=scan)
        runs.append(StrictRun(root, spec, config, start, script, bound))
    return runs


def _compiled(
    root: Path, spec: Spec, config: ProjectConfig, scan: Redactor
) -> CompiledScript:
    """`spec`'s compiled script, read as explore reads it, or a stop
    scanned with `scan`: none, a link on the way (`publication`) and one
    that doesn't load are spec errors, and one that can't be read is 15."""
    spec_id = spec.frontmatter.id
    try:
        at = publication(root, spec)
    except SpecError as error:
        stop("spec_error", spec_id, _NOTHING, error.problems, scan=scan)
    except OSError:
        stop("record_error", spec_id, _UNREAD, scan=scan)
    if at.found is None:
        stop("spec_error", spec_id, _MISSING, scan=scan)
    try:
        return load_compiled(at.target, config)
    except OSError:
        stop("record_error", spec_id, _UNREAD, scan=scan)
    except SpecError as error:
        stop("spec_error", spec_id, _UNLOADED, error.problems, scan=scan)


def _scan(runs: Sequence[StrictRun], more: Iterable[BoundSecret] = ()) -> Redactor:
    return Redactor([*(s for run in runs for s in run.bound.values()), *more])


async def _replayed(
    runs: Sequence[StrictRun], scan: Redactor, *, lines: bool, quiet: bool
) -> list[Replayed]:
    """Each run, in order, on one Playwright driver (`common.started`), and,
    with `lines`, its line as it ends, unless `quiet` and it passed."""
    ended: list[Replayed] = []
    async with started(async_playwright().start()) as playwright:
        # Once per Playwright: a second registration is refused.
        await register_identity_engine(playwright)
        for run in runs:
            began = time.monotonic()
            replayed = await strict_replay(run, playwright)
            if lines and not (quiet and replayed.outcome == "passed"):
                seconds = time.monotonic() - began
                _said(scan, run.spec.frontmatter.id, replayed, seconds)
            ended.append(replayed)
    return ended


def _said(scan: Redactor, spec_id: str, replayed: Replayed, seconds: float) -> None:
    """Say how the run ended in one line, to stdout when it passed and to
    stderr otherwise: the outcome, the time, what it cost, which is nothing
    with no model, the run record when it didn't pass, and "non-resumable"
    whenever a side effect's outcome is unknown, alone if the rest is
    withheld. Then each reason its document gives, and where its egress
    record is. A run that passed has neither."""
    document = replayed.document
    failed = replayed.outcome != "passed"
    facts = [f"{seconds:.1f}s", "$0"]
    withheld = [WITHHELD.format(outcome=replayed.outcome, spec_id=spec_id)]
    if failed and replayed.record is not None:
        facts.append(f"run record: {replayed.record}")
    if document.get("non_resumable"):
        facts.append("non-resumable")
        withheld.append("non-resumable")
    lines = [f"{replayed.outcome} {spec_id}: {', '.join(facts)}"]
    lines.extend(f"  {reason}" for reason in reasons_in(document))
    if document.get("error_code") == "egress_blocked" and replayed.record is not None:
        egress = replayed.record / "egress.json"
        saved = "is" if egress.is_file() else "couldn't be saved to"
        lines.append(f"  the egress record {saved} {egress}")
    say(scan, *lines, err=failed, withheld=withheld)
