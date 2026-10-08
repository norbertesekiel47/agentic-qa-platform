"""`aqa explore` (API.md §7; ADR-0024). Until exploring is built (#53), it
writes the spec's coverage plan and stops: `--plan-only` is required.

Everything is resolved before any model call: the spec root, the spec and
the project config, the start origin and the provider's key. A problem with
any of them is a spec error (exit 5). Then one call on the navigator role
writes the plan into the run record, with every response's cost record."""

import asyncio
import os
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, NoReturn

import typer
from aqa_core.coverage_plan import CoveragePlan, plan_hash, uncovered
from aqa_core.model_costs import CostRecord
from aqa_core.project import SpecError, load_project, start_origin
from aqa_core.spec import Spec
from aqa_runner.anthropic_client import (
    KEY_VARIABLE,
    MAX_OUTPUT_TOKENS,
    AnthropicClient,
    ProviderError,
)
from aqa_runner.coverage_plan import Planned, make_plan
from aqa_runner.model_router import ModelCallError, ModelRouter
from aqa_runner.run_record import RunRecord

# How a plan-only run that doesn't plan ends (ADR-0024's outcomes), and its
# exit code (API.md §7). A run that plans exits 0.
Failure = Literal["gave_up", "spec_error", "no_response", "record_error"]
EXIT_CODES: Final[Mapping[Failure, int]] = {
    "gave_up": 3,
    "spec_error": 5,
    "no_response": 11,
    "record_error": 15,
}
# What the run record says: "planned", a failure, or "error", a fault of
# ours, which is raised as it is.
Outcome = Literal["planned", "error"] | Failure

# Characters that would move the cursor, recolour the terminal or reorder text
# when printed: C0 and C1 controls, line and paragraph separators, and every
# bidi control (marks, embeddings, overrides and isolates). What the model
# wrote is printed with them escaped.
_INVISIBLE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def _visible(text: str) -> str:
    return _INVISIBLE.sub(
        lambda found: found.group().encode("unicode_escape").decode(), text
    )


def _stop(
    outcome: Failure, subject: str, why: str, reasons: Sequence[str] = ()
) -> NoReturn:
    """Print the outcome, what it concerns, why, and each reason to stderr,
    and exit with the outcome's code."""
    typer.echo(_visible(f"{outcome} {subject}: {why}"), err=True)
    for reason in reasons:
        typer.echo(f"  {_visible(reason)}", err=True)
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


def _resolve(path: Path, url: str | None) -> tuple[Path, Spec, ModelRouter]:
    """The spec root, the spec and a router for its project, or a SpecError.
    Nothing here calls a model or starts a browser. The whole project is read,
    so a spec id used twice is caught, and a problem in any spec of the
    project stops the command (ADR-0024's #41 amendment)."""
    if path.is_dir():
        raise SpecError([f"{path}: a directory, not a file"])
    root = _spec_root(path)
    project = load_project(root)
    found = [s for s in project.specs.values() if s.path.resolve() == path.resolve()]
    if not found:
        raise SpecError(
            [f"{path}: not a spec of the project at {root}, whose specs are *.spec.md"]
        )
    start_origin(url, project.config)
    # M1's only provider is Anthropic (DATA_MODEL §9), and its key comes from
    # the environment: without one, the call could only fail.
    if not os.environ.get(KEY_VARIABLE):
        raise SpecError([f"{KEY_VARIABLE}: not set: the plan's model is Anthropic's"])
    return root, found[0], ModelRouter.from_config(project.config, AnthropicClient)


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


def _cut_off(planned: Planned) -> bool:
    """Whether the answer stopped at the output bound (the adapter leaves
    such an answer unparsed, and the router records it `invalid`)."""
    return planned.routed.message.response_metadata.get("stop_reason") == "max_tokens"


def _judge(
    planned: Planned, spec: Spec
) -> CoveragePlan | tuple[Failure, str, tuple[str, ...]]:
    """The plan, if the call gave one that covers the spec; otherwise how
    the run fails, why, and the reasons."""
    if planned.plan is None:
        if planned.routed.outcome == "refusal":
            why = "the model refused to write the plan"
        elif _cut_off(planned):
            why = f"the model's plan was cut off at the {MAX_OUTPUT_TOKENS}-token bound"
        else:
            why = "the model's plan didn't parse as a coverage plan"
        return "gave_up", why, ()
    if planned.misfits:
        return "gave_up", "the model's plan doesn't fit the spec", planned.misfits
    if lines := uncovered(planned.plan, spec.frontmatter):
        return "spec_error", "an expectation has no establishing check", lines
    return planned.plan


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
            "--plan-only", help="Write the coverage plan to the run record and stop."
        ),
    ] = False,
) -> None:
    """Write a spec's coverage plan. Exploring itself arrives with #53."""
    if not plan_only:
        raise typer.BadParameter(
            "required until exploring is built (#53)", param_hint="'--plan-only'"
        )
    try:
        root, spec, router = _resolve(spec_path, url)
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
    except ModelCallError as error:
        # The refusal before the failed fallback was billed: keep its record.
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
        why = f"the fallback model gave no response: {error.__cause__}"
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
    judged = _judge(planned, spec)
    if isinstance(judged, CoveragePlan):
        path = _write_record(
            record, spec, outcome="planned", calls=calls, plan=judged, reasons=()
        )
        typer.echo(
            _visible(
                f"planned {spec_id}: plan {plan_hash(judged)}, "
                f"{len(judged.expectations)} expectations covered, "
                f"${_cost(calls)}, run record: {path}"
            )
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
