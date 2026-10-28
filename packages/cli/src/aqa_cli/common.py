"""What `aqa explore` and `aqa replay` share (API.md §7): resolving a spec,
writing through one output boundary that fails closed (ADR-0026's #53 P17
amendment), stopping with an outcome and its exit code, and starting and
stopping Playwright's driver.

Everything either command prints goes through `rendered`, which scans the
whole text with the invocation's test secrets once they are bound; a text
that still spells one is never written."""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, NoReturn

import typer
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_project, start_origin
from aqa_core.spec import Spec
from aqa_runner.redaction import NO_SECRETS, Redactor
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Playwright

# How a command stops a run it never started, or a plan-only run that doesn't
# plan (ADR-0024's outcomes), and its exit code (API.md §7).
Failure = Literal[
    "gave_up",
    "spec_error",
    "no_response",
    "record_error",
    "secret_unusable",
    "unreachable",
]
EXIT_CODES: Final[Mapping[Failure, int]] = {
    "gave_up": 3,
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
