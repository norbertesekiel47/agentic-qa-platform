"""What `aqa explore`, `aqa replay`, `aqa run`, `aqa heal` and `aqa report` share
(API.md §7): resolving a spec, finding the spec root of a command given none,
writing through one output boundary that fails closed (ADR-0026's #53 P17
amendment), stopping with an outcome and its exit code, starting and stopping
Playwright's driver, and the runner `replay` and `run` both are.

Everything a command prints goes through `rendered`, which scans the whole text
with the invocation's test secrets once they are bound; a text that still spells
one is never written."""

import asyncio
import contextlib
import re
from collections.abc import (
    AsyncIterator,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from functools import partial
from pathlib import Path
from typing import Any, Final, Literal, NoReturn

import typer
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.heal_patch import blob_sha
from aqa_core.project import SpecError, load_project, read_compiled, start_origin
from aqa_core.spec import Spec
from aqa_runner.admission import BoundValueSpelledError, admitted
from aqa_runner.bound_secrets import BoundSecret, MissingSecretError, SecretLoggedError
from aqa_runner.heal import Healing
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.model_turns import Spending
from aqa_runner.publication import Publication, publication
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.strict_replay import Replayed, StrictRun, strict_replay
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Playwright, async_playwright

# How a command stops a run it never started, or a plan-only run that doesn't
# plan (ADR-0024's outcomes), and its exit code (API.md §7).
Failure = Literal[
    "gave_up",
    "stale",
    "spec_error",
    "no_response",
    "record_error",
    "secret_unusable",
    "unreachable",
]
EXIT_CODES: Final[Mapping[Failure, int]] = {
    "gave_up": 3,
    "stale": 3,
    "spec_error": 5,
    "no_response": 11,
    "secret_unusable": 12,
    "unreachable": 13,
    "record_error": 15,
}
# What is written instead of a text that still spells a bound value after its
# scans: the outcome and the spec id, then words no run supplies.
WITHHELD: Final = (
    "{outcome} {spec_id}: the rest is withheld, since it would spell a bound value"
)
# Playwright 1.63's words when its driver exits before the handshake
# (`PipeTransport.run`), raised as a bare Exception, with no class of its own.
_CLOSED_EARLY: Final = ("Connection closed while reading from the driver",)
_NO_DRIVER: Final = "the browser driver couldn't start"
_NOTHING: Final = "nothing was replayed"
_MISSING: Final = "no compiled script: explore it first"
_REFUSED: Final = "replay refuses the compiled script, so nothing was replayed"
_LINKED: Final = (
    "a link, which could lead into another checkout: run this inside the project you "
    "mean"
)

# Characters that would move the cursor, recolour the terminal or reorder text
# when printed: C0 and C1 controls, line and paragraph separators, and every
# bidi control (marks, embeddings, overrides and isolates). What the model
# wrote is printed with them escaped.
_INVISIBLE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


class UnstartedError(Exception):
    """The browser driver or the egress listener couldn't start (exit 13),
    in fixed words: never the OS's."""


def _visible(text: str) -> str:
    return _INVISIBLE.sub(
        lambda found: found.group().encode("unicode_escape").decode(), text
    )


def say(
    scan: Redactor, *lines: str, err: bool = False, withheld: Sequence[str] = ()
) -> None:
    """Write `rendered`'s text to stdout, or to stderr with `err`, if any."""
    if text := rendered(scan, lines, withheld):
        typer.echo(text, err=err)


def rendered(scan: Redactor, lines: Sequence[str], withheld: Sequence[str]) -> str:
    """`lines` as one text: each line scanned whole with `scan`, so a bound
    value a join spells is found, then escaped (`_visible`), and the escaped
    text scanned whole again, since escaping and the line breaks can spell
    one too (ADR-0026's A2 amendment, as `redaction.error_text`). A text
    that still spells one, as a marker does when the value is in its
    secret's name, is never returned; `fallback` returns the `withheld`
    lines instead, each escaped before its check, less any that spells one,
    and only the last, the caller's status, if their join does.

    `scan` holds the test secrets the command bound, once `bound_secrets`
    gave them, and is empty where none is bound: before then (usage, the
    spec, the budgets), and where a command binds none (each command
    says)."""
    text: str = scan.redact("\n".join(_visible(scan.redact(line)) for line in lines))
    return fallback(scan, withheld) if scan.finds(text) else text


def fallback(scan: Redactor, withheld: Sequence[str]) -> str:
    kept = [w for w in map(_visible, withheld) if not scan.finds(w)]
    text = "\n".join(kept)
    return kept[-1] if scan.finds(text) else text


def reasons_in(document: Mapping[str, object]) -> list[str]:
    """The reasons a run's document gives for how it ended."""
    given = document.get("reasons", [])
    if not isinstance(given, list) or not all(isinstance(r, str) for r in given):
        raise TypeError("a run's document holds its reasons as a list of text")
    return given


def stop(
    outcome: Failure,
    subject: str,
    why: str,
    reasons: Sequence[str] = (),
    *,
    scan: Redactor = NO_SECRETS,
) -> NoReturn:
    """Print the outcome, what it concerns, why, and each reason to stderr,
    scanned with `scan`, the run's once its test secrets are bound, and exit
    with the outcome's code, whatever `say` withholds. Once they are bound,
    `subject` is the spec id."""
    reasoned = (f"  {reason}" for reason in reasons)
    withheld = (WITHHELD.format(outcome=outcome, spec_id=subject),)
    say(scan, f"{outcome} {subject}: {why}", *reasoned, err=True, withheld=withheld)
    raise typer.Exit(EXIT_CODES[outcome])


def _spec_root(path: Path) -> Path:
    """The spec root for the spec at `path`: the nearest directory, from the
    spec's own up, that holds config.yaml (DATA_MODEL §9)."""
    for directory in path.absolute().parents:
        if (directory / "config.yaml").is_file():
            return directory
    raise SpecError(
        [f"{path}: no config.yaml in its directory or above it, so no spec root"]
    )


def spec_root_above(directory: Path) -> Path:
    """The spec root a command given no spec path runs under: from `directory` up,
    at each level `qa/`, where `aqa init` puts one, and then the level itself, so a
    repository's own `config.yaml` doesn't win over its `qa/`. No person named this
    root, and a repository can commit `qa` as a link into another checkout, where the
    write would land, so a link is refused before anything behind it is read. A level
    holds no link: the working directory is the operating system's physical path."""
    for level in (directory, *directory.parents):
        for root in (level / "qa", level):
            if (root / "config.yaml").is_file():
                if root.is_symlink():
                    raise SpecError([f"{root}: {_LINKED}"])
                return root
    raise SpecError([f"{directory}: no config.yaml in it, in its qa/ or above it"])


def resolve(path: Path, url: str | None) -> tuple[Path, Spec, ProjectConfig, str]:
    """The spec root, the spec, its project's config and the start origin,
    or a SpecError. Nothing here calls a model or starts a browser. The whole
    project is read, so a spec id used twice is caught, and a problem in any
    spec of the project stops the command (ADR-0024's #41 amendment)."""
    if path.is_dir():
        raise SpecError([f"{path}: a directory, not a file"])
    root = _spec_root(path)
    project = load_project(root)
    found = [s for s in project.specs.values() if s.path.resolve() == path.resolve()]
    if not found:
        raise SpecError(
            [f"{path}: not a spec of the project at {root}, whose specs are *.spec.md"]
        )
    return root, found[0], project.config, start_origin(url, project.config)


@contextlib.asynccontextmanager
async def started(
    start: Coroutine[Any, Any, Playwright],
) -> AsyncIterator[Playwright]:
    """Playwright's driver, `start`ed in this frame's own task, which is
    cancelled and joined if still pending, and the driver stopped however
    the block ends; a failed stop changes nothing. A driver that can't start
    is `UnstartedError` (`_driver`)."""
    starting = asyncio.create_task(start)
    try:
        yield await _driver(starting)
    finally:
        if not starting.done():
            starting.cancel()
            await asyncio.wait({starting})
        if not starting.cancelled() and not starting.exception():
            with contextlib.suppress(OSError, PlaywrightError):
                await starting.result().stop()


async def _driver(starting: asyncio.Task[Playwright]) -> Playwright:
    """The driver `starting` started, or `UnstartedError` (exit 13) for an
    OSError (the spawn), Playwright's Error (the handshake) or `_CLOSED_EARLY`
    (a driver that exited first), the start's failure read as a value; any
    other, or a cancellation, goes on as it came. Playwright 1.63 also leaves
    a failed spawn's error in a task nobody reads, which asyncio would log,
    path and all: reports made meanwhile are held, those holding the caught
    error dropped, and the rest passed on."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    held: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _, report: held.append(report))
    try:
        [started] = await asyncio.gather(starting, return_exceptions=True)
        if isinstance(started, OSError | PlaywrightError) or (
            type(started) is Exception and started.args == _CLOSED_EARLY
        ):
            held[:] = [r for r in held if r.get("exception") is not started]
            raise UnstartedError(_NO_DRIVER) from None
        if isinstance(started, BaseException):
            raise started
        return started
    finally:
        loop.set_exception_handler(previous)
        for report in held:
            loop.call_exception_handler(report)


def admitted_runs(paths: Sequence[Path], url: str | None) -> list[StrictRun]:
    """Each spec at `paths`, admitted for a replay, before any reset is sent and
    before Playwright's driver starts, which must inherit no environment a test
    secret's binding refused (#215), or a stop, scanned with the test secrets
    the specs before it bound, and its own once admission bound them. The first
    problem stops the command: 5 for a spec, or for a compiled script missing,
    that doesn't load or that replay refuses (`admission.admitted`), 15 for one
    that can't be read, and 12 for test secrets that can't be used."""
    runs: list[StrictRun] = []
    for path in paths:
        scan = scan_of(runs)
        try:
            root, spec, config, start = resolve(path, url)
        except SpecError as error:
            stop("spec_error", str(path), _NOTHING, error.problems, scan=scan)
        spec_id = spec.frontmatter.id
        _, script, raw = found_script(root, spec, config, scan, _NOTHING)
        try:
            _, _, bound, _ = admitted(script, spec, config, start)
        except BoundValueSpelledError as error:
            scan = scan_of(runs, error.bound.values())
            stop("spec_error", spec_id, _REFUSED, error.problems, scan=scan)
        except SpecError as error:
            stop("spec_error", spec_id, _REFUSED, error.problems, scan=scan)
        except MissingSecretError as error:
            scan = scan_of(runs, error.bound.values())
            stop("secret_unusable", spec_id, _NOTHING, error.problems, scan=scan)
        except SecretLoggedError as error:
            scan = scan_of(runs, error.bound.values())
            stop("secret_unusable", spec_id, _NOTHING, (str(error),), scan=scan)
        runs.append(StrictRun(root, spec, config, start, script, bound, blob_sha(raw)))
    return runs


def found_script(
    root: Path, spec: Spec, config: ProjectConfig, scan: Redactor, nothing: str
) -> tuple[Publication, CompiledScript, bytes]:
    """`spec`'s compiled script, read as explore reads it, where it was found,
    before that read, and the bytes it was read from, or a stop scanned with
    `scan`, saying `nothing` of what the command didn't do: none, a link on the
    way (`publication`) and one that doesn't load are spec errors, and one that
    can't be read is 15."""
    spec_id = spec.frontmatter.id
    unread = f"the compiled script could not be read, so {nothing}"
    try:
        at = publication(root, spec)
    except SpecError as error:
        stop("spec_error", spec_id, nothing, error.problems, scan=scan)
    except OSError:
        stop("record_error", spec_id, unread, scan=scan)
    if at.found is None:
        stop("spec_error", spec_id, _MISSING, scan=scan)
    try:
        return (at, *read_compiled(at.target, config))
    except OSError:
        stop("record_error", spec_id, unread, scan=scan)
    except SpecError as error:
        unloaded = f"the compiled script doesn't load, so {nothing}"
        stop("spec_error", spec_id, unloaded, error.problems, scan=scan)


def scan_of(runs: Sequence[StrictRun], more: Iterable[BoundSecret] = ()) -> Redactor:
    """The scan of every test secret `runs` bind, and `more`."""
    return Redactor([*(s for run in runs for s in run.bound.values()), *more])


def replayed_runs(
    runs: Sequence[StrictRun],
    scan: Redactor,
    *,
    lines: bool = True,
    quiet: bool = False,
    heal: Callable[[StrictRun, Spending], Healing] | None = None,
) -> list[Replayed]:
    """Each run, in order, on one Playwright driver, a drift going to the repair
    of the healing `heal` builds for it when given, and, with `lines`, its line
    as it ends, unless `quiet` and it passed; or a stop (13) when the driver or
    the egress listener couldn't start."""
    try:
        return asyncio.run(_replayed(runs, scan, lines=lines, quiet=quiet, heal=heal))
    except UnstartedError as error:
        subject = ", ".join(run.spec.frontmatter.id for run in runs)
        stop("unreachable", subject, f"{error}, so {_NOTHING}", scan=scan)


async def _replayed(
    runs: Sequence[StrictRun],
    scan: Redactor,
    *,
    lines: bool,
    quiet: bool,
    heal: Callable[[StrictRun, Spending], Healing] | None,
) -> list[Replayed]:
    ended: list[Replayed] = []
    async with started(async_playwright().start()) as playwright:
        # Once per Playwright: a second registration is refused.
        await register_identity_engine(playwright)
        for run in runs:
            replayed = await strict_replay(
                run,
                playwright,
                scan,
                heal=None if heal is None else partial(heal, run),
            )
            if lines and not (quiet and replayed.outcome == "passed"):
                _said(scan, run.spec.frontmatter.id, replayed)
            ended.append(replayed)
    return ended


def _said(scan: Redactor, spec_id: str, replayed: Replayed) -> None:
    """Say how the run ended in one line, to stdout when it passed and to
    stderr otherwise: the outcome, the time its `run.json` records, what its
    model calls cost, which is nothing with no model, the run record when it
    didn't pass, and "non-resumable" whenever a side effect's outcome is
    unknown, alone if the rest is withheld. Then each reason its document
    gives, where its egress record is, the `aqa report` command that writes
    its run viewer once its `replay.json` is written (#249), and what a person
    reads of its heal proposal. A run that passed has only the command."""
    document = replayed.document
    failed = replayed.outcome != "passed"
    facts = [f"{replayed.seconds:.1f}s", f"${replayed.cost.normalize():f}"]
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
    if replayed.record is not None and (replayed.record / "replay.json").is_file():
        lines.append(f"  run viewer: aqa report {replayed.record.name}")
    lines.extend(f"  {line}" for line in replayed.proposal)
    say(scan, *lines, err=failed, withheld=withheld)
