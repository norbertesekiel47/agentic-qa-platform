"""`aqa explore` (API.md §7; ADR-0024). Until exploring is built (#53), it
writes the spec's coverage plan and stops: `--plan-only` is required.

Everything is resolved before any model call: the spec root, the spec and
the project config, the start origin and the provider's key. A problem with
any of them is a spec error (exit 5). Then one call on the navigator role
writes the plan into the run record, with every response's cost record."""

import asyncio
import os
import re
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, NoReturn

import anthropic
import typer
from aqa_core.coverage_plan import CoveragePlan, plan_hash, uncovered
from aqa_core.model_costs import CostRecord
from aqa_core.project import SpecError, load_project, start_origin
from aqa_core.spec import Spec
from aqa_runner.anthropic_client import (
    KEY_VARIABLE,
    MAX_OUTPUT_TOKENS,
    AnthropicClient,
)
from aqa_runner.coverage_plan import Planned, make_plan
from aqa_runner.model_router import ModelCallError, ModelRouter
from aqa_runner.run_record import RunRecord

# Exit codes (API.md §7).
PLANNED = 0
GAVE_UP = 3
SPEC_ERROR = 5
NO_RESPONSE = 11

# Characters that would move the cursor, recolour the terminal or reorder text
# when printed: C0 and C1 controls, line and paragraph separators, and bidi
# embeddings, overrides and isolates. What the model wrote is printed with
# them escaped.
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _visible(text: str) -> str:
    return _INVISIBLE.sub(lambda char: char[0].encode("unicode_escape").decode(), text)


def _stop(code: int, headline: str, reasons: Sequence[str] = ()) -> NoReturn:
    """Print `headline` and each reason to stderr, and exit with `code`."""
    typer.echo(_visible(headline), err=True)
    for reason in reasons:
        typer.echo(f"  {_visible(reason)}", err=True)
    raise typer.Exit(code)


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
    root: Path,
    spec: Spec,
    outcome: str,
    calls: Sequence[CostRecord],
    plan: CoveragePlan | None = None,
) -> Path:
    """A new run record under `root`, holding the plan, if there is one, and
    the cost record of every response, so a billed response is always kept."""
    record = RunRecord.create(root)
    record.write(
        "plan.json",
        {
            "run_id": record.run_id,
            "spec_id": spec.frontmatter.id,
            "spec_hash": spec.spec_hash,
            "outcome": outcome,
            "plan": None
            if plan is None
            else plan.model_dump(mode="json", exclude_none=True),
            "plan_hash": None if plan is None else plan_hash(plan),
            "calls": [call.model_dump(mode="json") for call in calls],
        },
    )
    return record.path


def _judge(planned: Planned, spec: Spec) -> tuple[str, int, str, tuple[str, ...]]:
    """The outcome of a plan call that got a response: its name, its exit
    code, a headline and the reasons."""
    if planned.plan is None:
        if planned.routed.outcome == "refusal":
            why = "the model refused to write the plan"
        elif (
            planned.routed.message.response_metadata.get("stop_reason") == "max_tokens"
        ):
            why = f"the model's plan was cut off at the {MAX_OUTPUT_TOKENS}-token bound"
        else:
            why = "the model's plan didn't parse as a coverage plan"
        return "gave_up", GAVE_UP, why, ()
    if planned.misfits:
        return (
            "gave_up",
            GAVE_UP,
            "the model's plan doesn't fit the spec",
            planned.misfits,
        )
    if lines := uncovered(planned.plan, spec.frontmatter):
        return (
            "spec_error",
            SPEC_ERROR,
            "an expectation has no establishing check",
            lines,
        )
    return "planned", PLANNED, "", ()


def explore(
    spec: Annotated[Path, typer.Argument(help="The spec file to explore.")],
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
        root, found, router = _resolve(spec, url)
    except SpecError as error:
        _stop(SPEC_ERROR, "spec_error: nothing was planned", error.problems)
    spec_id = found.frontmatter.id
    try:
        planned = asyncio.run(make_plan(router, found))
    except ModelCallError as error:
        # The refusal before the failed fallback was billed: keep its record.
        path = _write_record(root, found, "no_response", error.records)
        _stop(
            NO_RESPONSE,
            f"no_response {spec_id}: the fallback model gave no response: {error.__cause__}",
            (f"run record: {path}",),
        )
    except anthropic.APIError as error:
        # langchain-anthropic's errors are the SDK's, subclassed; nothing was
        # billed, so there is nothing to record.
        _stop(
            NO_RESPONSE, f"no_response {spec_id}: the model gave no response: {error}"
        )
    outcome, code, why, reasons = _judge(planned, found)
    path = _write_record(root, found, outcome, planned.routed.calls, planned.plan)
    plan = planned.plan
    # A plan always comes with PLANNED; the second test only tells mypy so.
    if code != PLANNED or plan is None:
        _stop(code, f"{outcome} {spec_id}: {why}", (*reasons, f"run record: {path}"))
    cost = sum((call.cost_usd for call in planned.routed.calls), Decimal(0))
    typer.echo(
        f"planned {spec_id}: plan {plan_hash(plan)}, "
        f"{len(plan.expectations)} expectations covered, ${cost}, run record: {path}"
    )
