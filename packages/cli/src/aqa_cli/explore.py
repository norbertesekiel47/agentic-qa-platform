"""`aqa explore` (API.md §7; ADR-0024 and its #53 P17 amendment): explore a
spec end to end through `aqa_runner.explore` and say how it ended in one
line, or, with `--plan-only`, write its coverage plan and stop, with no
browser.

Everything the command line names is resolved first: the spec root, the
spec and the project config, the start origin and each budget `--budget`
lays over the config's. A problem with any of them is a spec error (exit 5).
Then, unless `--force`, a compiled script that is up to date is kept, with
no model call and no browser: it loads, was compiled from the spec and the
subject contracts as they are, and agrees with them, as replay's contract
check requires. Only a run that will call a model needs the provider's key. With
`--plan-only`, one call on the navigator role writes the plan into the run
record, with every response's cost record.

Everything this module prints goes through `_rendered`, which scans the whole
text with the run's test secrets once they are bound; a text that still spells
one is never written, nor are a run's two streams if their join does (ADR-0026)."""

import asyncio
import contextlib
import os
import re
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, Literal, NoReturn

import typer
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan, plan_hash
from aqa_core.model_costs import CostRecord
from aqa_core.project import (
    SpecError,
    contract_problems,
    contracts_fingerprint,
    load_compiled,
    load_project,
    start_origin,
    with_budgets,
)
from aqa_core.spec import Spec
from aqa_runner.anthropic_client import (
    KEY_VARIABLE,
    AnthropicClient,
    ProviderError,
)
from aqa_runner.bound_secrets import (
    MissingSecretError,
    SecretLoggedError,
    bound_secrets,
)
from aqa_runner.coverage_plan import judge, make_plan
from aqa_runner.egress_proxy import ListenerUnavailableError
from aqa_runner.explore import Explored
from aqa_runner.explore import explore as explore_spec
from aqa_runner.model_router import ModelCallError, ModelRouter
from aqa_runner.publication import publication
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.run_record import RunRecord
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Playwright, async_playwright

# How a run this module stops ends (ADR-0024's outcomes), and its exit code
# (API.md §7): a plan-only run that doesn't plan, or a run that stops before
# exploring. A run that plans exits 0; one that explores, as it ended.
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
# What the run record says: "planned", a failure, "error", a fault of ours,
# or "interrupted", Ctrl-C or a cancellation while planning. "error" and
# "interrupted" are raised as they came.
Outcome = Literal["planned", "error", "interrupted"] | Failure

# Said when an interrupted run's record can't be written: the interruption
# goes on regardless, and the error's own text stays out, as for exit 15.
_UNWRITTEN: Final = (
    "interrupted: the run record could not be written, so what the plan call "
    "was billed is not kept"
)
# Why a run stopped before exploring, in fixed words, and what a published
# script with no confirmation says each time it is published or kept.
_UNREAD: Final = "the compiled script could not be read; nothing was planned"
_UNLOADED: Final = (
    "the compiled script doesn't load, so nothing was explored: correct it, or "
    "pass --force to explore the spec again"
)
_DISAGREES: Final = (
    "the compiled script disagrees with the project's subject contracts, so "
    "nothing was explored: correct them, or pass --force to explore the spec again"
)
_NO_DRIVER: Final = "the browser driver couldn't start, so nothing was explored"
_NO_LISTENER: Final = "the egress listener couldn't start, so nothing was explored"
# What is written instead of a text that still spells a bound value after its
# scans: the outcome and the spec id, then words no run supplies.
_WITHHELD: Final = (
    "{outcome} {spec_id}: the rest is withheld, since it would spell a bound value"
)
# Playwright 1.63's words when its driver exits before the handshake
# (`PipeTransport.run`), raised as a bare Exception, with no class of its own.
_CLOSED_EARLY: Final = ("Connection closed while reading from the driver",)
_UNCONFIRMED: Final = (
    "warning: {path} is unconfirmed: its path has side-effect steps, or "
    "loading its start URL may change the app, and the spec has no reset hook, "
    "so no replay confirmed it (AGENTS.md §6). Declare a reset hook, and "
    "explore it again"
)

# Characters that would move the cursor, recolour the terminal or reorder text
# when printed: C0 and C1 controls, line and paragraph separators, and every
# bidi control (marks, embeddings, overrides and isolates). What the model
# wrote is printed with them escaped.
_INVISIBLE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


class _UnstartedError(Exception):
    """The browser driver or the egress listener couldn't start (exit 13),
    in fixed words: never the OS's."""


def _visible(text: str) -> str:
    return _INVISIBLE.sub(
        lambda found: found.group().encode("unicode_escape").decode(), text
    )


def _say(
    scan: Redactor, *lines: str, err: bool = False, withheld: Sequence[str] = ()
) -> None:
    """Write `_rendered`'s text to stdout, or to stderr with `err`, if any."""
    if text := _rendered(scan, lines, withheld):
        typer.echo(text, err=err)


def _rendered(scan: Redactor, lines: Sequence[str], withheld: Sequence[str]) -> str:
    """`lines` as one text: each line scanned whole with `scan`, so a bound
    value a join spells is found, then escaped (`_visible`), and the escaped
    text scanned whole again, since escaping and the line breaks can spell
    one too (ADR-0026's A2 amendment, as `redaction.error_text`). A text
    that still spells one, as a marker does when the value is in its
    secret's name, is never returned; `_fallback` returns the `withheld`
    lines instead, each escaped before its check, less any that spells one,
    and only the last, the caller's status, if their join does.

    `scan` holds the run's test secrets once `bound_secrets` gave them. It
    is empty where none is bound: before then (usage, the spec, the
    budgets); with `--plan-only`, which binds none and whose request leaves
    out the account and the reset hook; and for a script kept up to date,
    with no run at all."""
    text: str = scan.redact("\n".join(_visible(scan.redact(line)) for line in lines))
    return _fallback(scan, withheld) if scan.finds(text) else text


def _fallback(scan: Redactor, withheld: Sequence[str]) -> str:
    kept = [w for w in map(_visible, withheld) if not scan.finds(w)]
    text = "\n".join(kept)
    return kept[-1] if scan.finds(text) else text


def _stop(
    outcome: Failure,
    subject: str,
    why: str,
    reasons: Sequence[str] = (),
    *,
    scan: Redactor = NO_SECRETS,
) -> NoReturn:
    """Print the outcome, what it concerns, why, and each reason to stderr,
    scanned with `scan`, the run's once its test secrets are bound, and exit
    with the outcome's code, whatever `_say` withholds. Once they are bound,
    `subject` is the spec id."""
    reasoned = (f"  {reason}" for reason in reasons)
    withheld = (_WITHHELD.format(outcome=outcome, spec_id=subject),)
    _say(scan, f"{outcome} {subject}: {why}", *reasoned, err=True, withheld=withheld)
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


def _resolve(path: Path, url: str | None) -> tuple[Path, Spec, ProjectConfig, str]:
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


def _router(config: ProjectConfig) -> ModelRouter:
    """A router for the project, or a SpecError when the key is missing:
    M1's only provider is Anthropic (DATA_MODEL §9), and its key comes from
    the environment, so without one a call could only fail."""
    if not os.environ.get(KEY_VARIABLE):
        raise SpecError([f"{KEY_VARIABLE}: not set: the plan's model is Anthropic's"])
    return ModelRouter.from_config(config, AnthropicClient)


def _write_record(
    record: RunRecord,
    spec: Spec,
    *,
    outcome: Outcome,
    calls: Sequence[CostRecord],
    plan: CoveragePlan | None,
    reasons: Sequence[str],
) -> Path:
    """Write the run's plan.json: the outcome and why, the plan the model
    wrote, if any, and the cost record of every response, so a billed
    response is always kept."""
    record.write(
        "plan.json",
        {
            "run_id": record.run_id,
            "spec_id": spec.frontmatter.id,
            "spec_hash": spec.spec_hash,
            "outcome": outcome,
            "reasons": list(reasons),
            "plan": None
            if plan is None
            else plan.model_dump(mode="json", exclude_none=True),
            "plan_hash": None if plan is None else plan_hash(plan),
            "calls": [call.model_dump(mode="json") for call in calls],
        },
    )
    return record.path


def _write_interrupted(
    record: RunRecord, spec: Spec, router: ModelRouter, interruption: BaseException
) -> None:
    """Write an interrupted run's plan.json: no plan, and the cost record of
    every response the router received before Ctrl-C or the cancellation.
    The caller raises the interruption afterwards, so a record that can't be
    written gets a note and a warning, never an error of its own."""
    try:
        _write_record(
            record,
            spec,
            outcome="interrupted",
            calls=router.completed_calls,
            plan=None,
            reasons=("planning interrupted",),
        )
    except OSError, ValueError:
        # What `RunRecord.write` raises: a filesystem failure, or a write the
        # record refuses. Neither may replace the interruption.
        interruption.add_note(_UNWRITTEN)
        # A broken stderr raises OSError; a closed one, ValueError.
        with contextlib.suppress(OSError, ValueError):
            _say(NO_SECRETS, _UNWRITTEN, err=True)


def _cost(calls: Sequence[CostRecord]) -> Decimal:
    return sum((call.cost_usd for call in calls), Decimal(0))


def explore(
    spec_path: Annotated[
        Path, typer.Argument(metavar="SPEC", help="The spec file to explore.")
    ],
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="The start origin; the project config's base_url if omitted."
        ),
    ] = None,
    plan_only: Annotated[
        bool,
        typer.Option(
            "--plan-only",
            help="Write the coverage plan to the run record and stop, with no browser.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Explore the spec again when its compiled script is up to date.",
        ),
    ] = False,
    budget: Annotated[
        list[str] | None,
        typer.Option(
            "--budget",
            metavar="NAME=VALUE",
            help=(
                "Lay one of config.yaml's budgets over it for this run: attempts, "
                "actions_per_attempt, model_usd, minutes or resolve_seconds. "
                "Repeatable, once per name. M1 makes one attempt, whatever "
                "attempts says."
            ),
        ),
    ] = None,
) -> None:
    """Explore a spec: plan its checks, explore the app in a sandboxed
    browser, confirm the path with a replay that calls no model, and write
    <spec root>/.compiled/<spec id>.json. In M1, a phase whose traffic used
    HTTPS or a WebSocket exits 14: the egress proxy can't see whether a
    tunnel's requests were answered."""
    if not plan_only:
        _explore(spec_path, url, force=force, budgets=budget or ())
        return
    if force or budget:
        raise typer.BadParameter(
            "only for exploring, not with --plan-only",
            param_hint="'--force', '--budget'",
        )
    _plan(spec_path, url)


def _plan(spec_path: Path, url: str | None) -> None:
    """Write the spec's coverage plan to a run record, and stop."""
    try:
        root, spec, config, _ = _resolve(spec_path, url)
        router = _router(config)
    except SpecError as error:
        _stop("spec_error", str(spec_path), "nothing was planned", error.problems)
    spec_id = spec.frontmatter.id
    # Made before the call, so a spec root that can't hold the record stops
    # the run before anything is billed.
    try:
        record = RunRecord.create(root)
    except OSError:
        _stop(
            "record_error",
            str(spec_path),
            "the run record could not be created; nothing was planned",
        )
    except ValueError as error:  # a .aqa or .aqa/runs that is a link
        _stop("spec_error", str(spec_path), "nothing was planned", (str(error),))
    try:
        planned = asyncio.run(make_plan(router, spec))
    except (asyncio.CancelledError, KeyboardInterrupt) as interruption:
        # Keep what was billed, then let the interruption go on as it came:
        # Ctrl-C still exits 130, and a cancellation still propagates.
        _write_interrupted(record, spec, router, interruption)
        raise
    except ModelCallError as error:
        # An answer before the failed call was billed (a refusal before its
        # fallback, or a plan before its one retry): keep its record.
        if not isinstance(error.__cause__, ProviderError):
            _write_record(
                record,
                spec,
                outcome="error",
                calls=error.records,
                plan=None,
                reasons=(str(error),),
            )
            raise  # not the provider's failure, so not "no response"
        why = f"the model gave no response after a billed answer: {error.__cause__}"
        path = _write_record(
            record,
            spec,
            outcome="no_response",
            calls=error.records,
            plan=None,
            reasons=(why,),
        )
        cost = _cost(error.records)
        _stop("no_response", spec_id, why, (f"cost: ${cost}", f"run record: {path}"))
    except ProviderError as error:
        why = f"the model gave no response: {error}"
        path = _write_record(
            record, spec, outcome="no_response", calls=(), plan=None, reasons=(why,)
        )
        _stop("no_response", spec_id, why, (f"run record: {path}",))
    calls = planned.routed.calls
    judged = judge(planned, spec)
    if isinstance(judged, CoveragePlan):
        path = _write_record(
            record, spec, outcome="planned", calls=calls, plan=judged, reasons=()
        )
        _say(
            NO_SECRETS,
            f"planned {spec_id}: plan {plan_hash(judged)}, "
            f"{len(judged.expectations)} expectations covered, "
            f"${_cost(calls)}, run record: {path}",
        )
        return
    outcome, why, reasons = judged
    path = _write_record(
        record,
        spec,
        outcome=outcome,
        calls=calls,
        plan=planned.plan,
        reasons=(why, *reasons),
    )
    _stop(
        outcome,
        spec_id,
        why,
        (*reasons, f"cost: ${_cost(calls)}", f"run record: {path}"),
    )


def _explore(
    spec_path: Path, url: str | None, *, force: bool, budgets: Sequence[str]
) -> None:
    """Explore the spec at `spec_path`, unless its compiled script is up to
    date and not `force`, and say how it ended (module docstring)."""
    began = time.monotonic()
    try:
        root, spec, config, start = _resolve(spec_path, url)
        config = with_budgets(config, budgets)
        kept = None if force else _kept(root, spec, config)
    except SpecError as error:
        _stop("spec_error", str(spec_path), "nothing was explored", error.problems)
    spec_id = spec.frontmatter.id
    target = root / ".compiled" / f"{spec_id}.json"
    if kept is not None:
        # No run, so nothing is bound (D21: no key, no secret, no browser).
        _say(NO_SECRETS, f"up to date {spec_id}: {target}")
        if not kept.confirmed:
            _say(NO_SECRETS, _UNCONFIRMED.format(path=target), err=True)
        return
    scan = NO_SECRETS  # until the run's test secrets are bound
    try:
        router = _router(config)
        scan = Redactor(bound_secrets(spec, start).values())
        explored = asyncio.run(_explored(spec, config, root, start, router))
    except SpecError as error:
        why = "nothing was explored"
        _stop("spec_error", spec_id, why, error.problems, scan=scan)
    except MissingSecretError as error:
        why = "nothing was explored"
        _stop("secret_unusable", spec_id, why, error.problems, scan=scan)
    except SecretLoggedError as error:
        why = "nothing was explored"
        _stop("secret_unusable", spec_id, why, (str(error),), scan=scan)
    except _UnstartedError as error:
        _stop("unreachable", spec_id, str(error), scan=scan)
    _said(scan, spec_id, explored, time.monotonic() - began, target)


def _kept(root: Path, spec: Spec, config: ProjectConfig) -> CompiledScript | None:
    """The spec's compiled script when it is up to date (D21): it loads, and
    was compiled from the spec and the subject contracts as they are, with
    every target agreeing with them, as replay requires. None when there is
    none, or it is stale. Publication compares again under the spec's lock.
    A link on its way is a spec error, as for exploring; one that can't be
    read stops the run (15); one that loads but disagrees is a spec error."""
    try:
        at = publication(root, spec)
    except OSError:
        _stop("record_error", str(spec.path), _UNREAD)
    if at.found is None:
        return None
    try:
        script = load_compiled(at.target, config)
    except OSError:
        _stop("record_error", str(spec.path), _UNREAD)
    except SpecError as error:
        _stop("spec_error", str(spec.path), _UNLOADED, error.problems)
    if (script.spec_hash, script.compiled_by.subject_contracts) != (
        spec.spec_hash,
        contracts_fingerprint(config, spec.frontmatter.id),
    ):
        return None
    if problems := contract_problems(script, config):
        _stop("spec_error", str(spec.path), _DISAGREES, problems)
    return script


async def _explored(
    spec: Spec, config: ProjectConfig, root: Path, start: str, router: ModelRouter
) -> Explored:
    """Explore with Playwright's driver, started in this frame's own task,
    which is cancelled and joined if still pending, and its driver stopped
    however the run ends; a failed stop changes nothing. An egress listener
    that can't open, for the attempt or the confirmation, is `_UnstartedError`,
    exit 13; any other OSError goes on as it came."""
    starting = asyncio.create_task(async_playwright().start())
    try:
        playwright = await _driver(starting)
        return await explore_spec(
            spec, config, root, start, router=router, playwright=playwright
        )
    except ListenerUnavailableError:
        raise _UnstartedError(_NO_LISTENER) from None
    finally:
        if not starting.done():
            starting.cancel()
            await asyncio.wait({starting})
        if not starting.cancelled() and not starting.exception():
            with contextlib.suppress(OSError, PlaywrightError):
                await starting.result().stop()


async def _driver(starting: asyncio.Task[Playwright]) -> Playwright:
    """The driver `starting` started, or `_UnstartedError` (exit 13) for an
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
            raise _UnstartedError(_NO_DRIVER) from None
        if isinstance(started, BaseException):
            raise started
        return started
    finally:
        loop.set_exception_handler(previous)
        for report in held:
            loop.call_exception_handler(report)


def _said(
    scan: Redactor, spec_id: str, explored: Explored, seconds: float, target: Path
) -> None:
    """Say how the run ended in one line, to stdout when it compiled and to
    stderr otherwise: the outcome, the time, what the model calls cost, the
    compiled script whenever one was published, the run record when the run
    didn't compile, and "non-resumable" whenever a side effect's outcome is
    unknown, whatever the exit (D42), alone if the rest is withheld. Then,
    on stderr, a published script's warning, each reason the run's document
    gives and where its egress record is, and exit with the run's code. If
    the two streams' texts, joined as `2>&1` writes them, spell a bound value,
    each gets the fallback, and stderr nothing if the fallbacks' join does."""
    document = explored.document
    published = document.get("published")
    facts = [f"{seconds:.1f}s", f"${explored.cost}"]
    withheld = [_WITHHELD.format(outcome=explored.outcome, spec_id=spec_id)]
    if published is not None:
        facts.append(str(target))
    if explored.exit_code and explored.record is not None:
        facts.append(f"run record: {explored.record}")
    if document.get("non_resumable"):
        facts.append("non-resumable")
        withheld.append("non-resumable")
    status = [f"{explored.outcome} {spec_id}: {', '.join(facts)}"]
    stdout, stderr = ([], status) if explored.exit_code else (status, [])
    if published == "unconfirmed":
        stderr.append(_UNCONFIRMED.format(path=target))
    stderr.extend(f"  {reason}" for reason in _reasons(document))
    if document.get("egress") is not None and explored.record is not None:
        saved = "is" if explored.egress_saved else "couldn't be saved to"
        stderr.append(f"  the egress record {saved} {explored.record / 'egress.json'}")
    out, err = _rendered(scan, stdout, withheld), _rendered(scan, stderr, withheld)
    if out and err and scan.finds(f"{out}\n{err}"):
        out = err = _fallback(scan, withheld)
        err = "" if scan.finds(f"{out}\n{err}") else err
    for text, to_stderr in ((out, False), (err, True)):
        if text:
            typer.echo(text, err=to_stderr)
    if explored.exit_code:
        raise typer.Exit(explored.exit_code)


def _reasons(document: Mapping[str, object]) -> list[str]:
    reasons = document.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise TypeError("a run's document holds its reasons as a list of text")
    return reasons
