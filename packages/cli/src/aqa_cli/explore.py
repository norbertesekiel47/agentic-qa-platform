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

# How a plan-only run ends (ADR-0024's outcomes), and its exit code (API.md §7).
Outcome = Literal["planned", "gave_up", "spec_error", "no_response"]
EXIT_CODES: Final[Mapping[Outcome, int]] = {
    "planned": 0,
    "gave_up": 3,
    "spec_error": 5,
    "no_response": 11,
}

# Characters that would move the cursor, recolour the terminal or reorder text
# when printed: C0 and C1 controls, line and paragraph separators, and bidi
# embeddings, overrides and isolates. What the model wrote is printed with
# them escaped.
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _visible(text: str) -> str:
    return _INVISIBLE.sub(lambda char: char[0].encode("unicode_escape").decode(), text)


def _stop(
    outcome: Outcome, subject: str, why: str, reasons: Sequence[str] = ()
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
    root: Path,
    spec: Spec,
    outcome: Outcome,
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


def _judge(planned: Planned, spec: Spec) -> tuple[Outcome, str, tuple[str, ...]]:
    """The outcome of a plan call that got a response, why, and the
    reasons."""
    if planned.plan is None:
        if planned.routed.outcome == "refusal":
            why = "the model refused to write the plan"
        elif (
            planned.routed.message.response_metadata.get("stop_reason") == "max_tokens"
        ):
            why = f"the model's plan was cut off at the {MAX_OUTPUT_TOKENS}-token bound"
        else:
            why = "the model's plan didn't parse as a coverage plan"
        return "gave_up", why, ()
    if planned.misfits:
        return "gave_up", "the model's plan doesn't fit the spec", planned.misfits
    if lines := uncovered(planned.plan, spec.frontmatter):
        return "spec_error", "an expectation has no establishing check", lines
    return "planned", "", ()


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
    try:
        planned = asyncio.run(make_plan(router, spec))
    except ModelCallError as error:
        # The refusal before the failed fallback was billed: keep its record.
        path = _write_record(root, spec, "no_response", error.records)
        if not isinstance(error.__cause__, anthropic.APIError):
            raise  # not the provider's failure, so not "no response"
        why = f"the fallback model gave no response: {error.__cause__}"
        _stop("no_response", spec_id, why, (f"run record: {path}",))
    except anthropic.APIError as error:
        # langchain-anthropic raises the SDK's errors, subclassed. Nothing was
        # billed, so there is nothing to record.
        _stop("no_response", spec_id, f"the model gave no response: {error}")
    outcome, why, reasons = _judge(planned, spec)
    path = _write_record(root, spec, outcome, planned.routed.calls, planned.plan)
    plan = planned.plan
    # A plan always comes with "planned"; the second test only tells mypy so.
    if outcome != "planned" or plan is None:
        _stop(outcome, spec_id, why, (*reasons, f"run record: {path}"))
    cost = sum((call.cost_usd for call in planned.routed.calls), Decimal(0))
    typer.echo(
        f"planned {spec_id}: plan {plan_hash(plan)}, "
        f"{len(plan.expectations)} expectations covered, ${cost}, run record: {path}"
    )
