"""`aqa explore` (API.md §7; ADR-0024 and its #53 P17 amendment): explore a
spec end to end through `aqa_runner.explore` and say how it ended in one
line, or, with `--plan-only`, write its coverage plan and stop, with no
browser.

Everything the command line names is resolved first: the spec root, the
spec and the project config, the start origin and each budget `--budget`
lays over the config's. A problem with any of them is a spec error (exit 5).
Then, unless `--force`, a compiled script that is up to date is kept, with
no model call and no browser: it loads, was compiled from the spec and the
subject contracts as they are, agrees with them, as replay's contract check
requires, and is one replay admits. Only a run that will call a model needs
the provider's key. With `--plan-only`, one call on the navigator role writes
the plan into the run record, with every response's cost record.

Everything this module prints goes through `aqa_cli.common.rendered`, which
scans the whole text with the run's test secrets once they are bound; a text
that still spells one is never written, nor are a run's two streams if their
join does (ADR-0026). None is bound with `--plan-only`, whose request leaves
out the account and the reset hook, nor for a script kept up to date, with no
run at all."""

import asyncio
import contextlib
import os
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal

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
from aqa_runner.executor import accepted
from aqa_runner.explore import Drivers, Explored
from aqa_runner.explore import explore as explore_spec
from aqa_runner.model_router import ModelCallError, ModelRouter
from aqa_runner.publication import publication
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.run_record import RunRecord
from playwright.async_api import async_playwright

from aqa_cli.common import (
    WITHHELD,
    Failure,
    UnstartedError,
    fallback,
    reasons_in,
    rendered,
    resolve,
    say,
    started,
    stop,
)

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
_REFUSED: Final = (
    "replay refuses the compiled script, so nothing was explored: correct it, or "
    "pass --force to explore the spec again"
)
_NO_LISTENER: Final = "the egress listener couldn't start"
_UNCONFIRMED: Final = (
    "warning: {path} is unconfirmed: its path has side-effect steps, or "
    "loading its start URL may change the app, and the spec has no reset hook, "
    "so no replay confirmed it (AGENTS.md §6). Declare a reset hook and "
    "explore it again, or explore it again with --force --confirm-repeat to let "
    "one confirmation repeat its side effects"
)


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
            say(NO_SECRETS, _UNWRITTEN, err=True)


def _cost(calls: Sequence[CostRecord]) -> Decimal:
    return sum((call.cost_usd for call in calls), Decimal(0))


def explore(
    spec_path: Annotated[
        Path, typer.Argument(metavar="SPEC", help="The spec file to explore.")
    ],
    *,
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
                "Repeatable, once per name. Without a reset hook, explore makes "
                "one attempt, whatever attempts says."
            ),
        ),
    ] = None,
    confirm_repeat: Annotated[
        bool,
        typer.Option(
            "--confirm-repeat",
            help=(
                "When the spec has no reset hook, let one confirmation replay "
                "repeat the path's side effects."
            ),
        ),
    ] = False,
) -> None:
    """Explore a spec: plan its checks, explore the app in a sandboxed
    browser, confirm the path with a replay that calls no model, and write
    <spec root>/.compiled/<spec id>.json. In M1, a phase whose traffic used
    HTTPS or a WebSocket exits 14: the egress proxy can't see whether a
    tunnel's requests were answered."""
    if not plan_only:
        _explore(
            spec_path,
            url,
            force=force,
            budgets=budget or (),
            confirm_repeat=confirm_repeat,
        )
        return
    if force or budget or confirm_repeat:
        raise typer.BadParameter(
            "only for exploring, not with --plan-only",
            param_hint="'--force', '--budget', '--confirm-repeat'",
        )
    _plan(spec_path, url)


def _plan(spec_path: Path, url: str | None) -> None:
    """Write the spec's coverage plan to a run record, and stop."""
    try:
        root, spec, config, _ = resolve(spec_path, url)
        router = _router(config)
    except SpecError as error:
        stop("spec_error", str(spec_path), "nothing was planned", error.problems)
    spec_id = spec.frontmatter.id
    # Made before the call, so a spec root that can't hold the record stops
    # the run before anything is billed.
    try:
        record = RunRecord.create(root)
    except OSError:
        stop(
            "record_error",
            str(spec_path),
            "the run record could not be created; nothing was planned",
        )
    except ValueError as error:  # a .aqa or .aqa/runs that is a link
        stop("spec_error", str(spec_path), "nothing was planned", (str(error),))
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
        stop("no_response", spec_id, why, (f"cost: ${cost}", f"run record: {path}"))
    except ProviderError as error:
        why = f"the model gave no response: {error}"
        path = _write_record(
            record, spec, outcome="no_response", calls=(), plan=None, reasons=(why,)
        )
        stop("no_response", spec_id, why, (f"run record: {path}",))
    calls = planned.routed.calls
    judged = judge(planned, spec)
    if isinstance(judged, CoveragePlan):
        path = _write_record(
            record, spec, outcome="planned", calls=calls, plan=judged, reasons=()
        )
        say(
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
    stop(
        outcome,
        spec_id,
        why,
        (*reasons, f"cost: ${_cost(calls)}", f"run record: {path}"),
    )


def _explore(
    spec_path: Path,
    url: str | None,
    *,
    force: bool,
    budgets: Sequence[str],
    confirm_repeat: bool,
) -> None:
    """Explore the spec at `spec_path`, unless its compiled script is up to
    date and not `force`, and say how it ended (module docstring)."""
    began = time.monotonic()
    try:
        root, spec, config, start = resolve(spec_path, url)
        config = with_budgets(config, budgets)
        kept = None if force else _kept(root, spec, config)
    except SpecError as error:
        stop("spec_error", str(spec_path), "nothing was explored", error.problems)
    spec_id = spec.frontmatter.id
    target = root / ".compiled" / f"{spec_id}.json"
    if kept is not None:
        # No run, so nothing is bound (D21: no key, no secret, no browser).
        say(NO_SECRETS, f"up to date {spec_id}: {target}")
        if not kept.confirmed:
            say(NO_SECRETS, _UNCONFIRMED.format(path=target), err=True)
        return
    scan = NO_SECRETS  # until the run's test secrets are bound
    try:
        router = _router(config)
        scan = Redactor(bound_secrets(spec, start).values())
        explored = asyncio.run(
            _explored(spec, config, root, start, router, confirm_repeat=confirm_repeat)
        )
    except SpecError as error:
        why = "nothing was explored"
        stop("spec_error", spec_id, why, error.problems, scan=scan)
    except MissingSecretError as error:
        why = "nothing was explored"
        stop("secret_unusable", spec_id, why, error.problems, scan=scan)
    except SecretLoggedError as error:
        why = "nothing was explored"
        stop("secret_unusable", spec_id, why, (str(error),), scan=scan)
    except UnstartedError as error:
        why = f"{error}, so nothing was explored"
        stop("unreachable", spec_id, why, scan=scan)
    _said(scan, spec_id, explored, time.monotonic() - began, target)


def _kept(root: Path, spec: Spec, config: ProjectConfig) -> CompiledScript | None:
    """The spec's compiled script when it is up to date (D21): it loads, and
    was compiled from the spec and the subject contracts as they are, with
    every target agreeing with them and every step and check one replay
    admits (`accepted`), as replay requires. None when there is none, or it
    is stale. Publication compares again under the spec's lock. A link on
    its way is a spec error, as for exploring; one that can't be read stops
    the run (15); one that loads but disagrees, or that replay refuses, is a
    spec error."""
    try:
        at = publication(root, spec)
    except OSError:
        stop("record_error", str(spec.path), _UNREAD)
    if at.found is None:
        return None
    try:
        script = load_compiled(at.target, config)
    except OSError:
        stop("record_error", str(spec.path), _UNREAD)
    except SpecError as error:
        stop("spec_error", str(spec.path), _UNLOADED, error.problems)
    if (script.spec_hash, script.compiled_by.subject_contracts) != (
        spec.spec_hash,
        contracts_fingerprint(config, spec.frontmatter.id),
    ):
        return None
    if problems := contract_problems(script, config):
        stop("spec_error", str(spec.path), _DISAGREES, problems)
    try:
        accepted(script, spec)
    except SpecError as error:
        stop("spec_error", str(spec.path), _REFUSED, error.problems)
    return script


async def _explored(
    spec: Spec,
    config: ProjectConfig,
    root: Path,
    start: str,
    router: ModelRouter,
    *,
    confirm_repeat: bool,
) -> Explored:
    """Explore with Playwright's driver (`started`). An egress listener
    that can't open, for the attempt or the confirmation, is `UnstartedError`,
    exit 13; any other OSError goes on as it came."""
    async with started(async_playwright().start()) as playwright:
        try:
            return await explore_spec(
                spec,
                config,
                root,
                start,
                Drivers(router, playwright),
                confirm_repeat=confirm_repeat,
            )
        except ListenerUnavailableError:
            raise UnstartedError(_NO_LISTENER) from None


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
    withheld = [WITHHELD.format(outcome=explored.outcome, spec_id=spec_id)]
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
    stderr.extend(f"  {reason}" for reason in reasons_in(document))
    if document.get("egress") is not None and explored.record is not None:
        saved = "is" if explored.egress_saved else "couldn't be saved to"
        stderr.append(f"  the egress record {saved} {explored.record / 'egress.json'}")
    out, err = rendered(scan, stdout, withheld), rendered(scan, stderr, withheld)
    if out and err and scan.finds(f"{out}\n{err}"):
        out = err = fallback(scan, withheld)
        err = "" if scan.finds(f"{out}\n{err}") else err
    for text, to_stderr in ((out, False), (err, True)):
        if text:
            typer.echo(text, err=to_stderr)
    if explored.exit_code:
        raise typer.Exit(explored.exit_code)
