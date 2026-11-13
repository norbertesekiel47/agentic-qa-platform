"""`aqa run` (API.md §7; ADR-0024's #244 amendment): `aqa replay`'s strict run of
each spec's committed compiled script, every locator, with a drift handed to the
healer (`aqa_runner.heal`), so a run that heals ends with a proposal a person
applies, exit 7. The model never writes a locator, a step or an assertion, and
nothing here changes the committed file.

A missing provider key is a spec error (5) before any browser. A run with no
drift calls no model and builds no client (AGENTS.md §6). Each spec has budgets
of its own (`heal_turns`, `heal_model_usd`). Every line printed goes through the
same boundary as `aqa replay`'s, and to stderr when the run didn't pass."""

import os
from pathlib import Path
from typing import Annotated, Final

import typer
from aqa_runner.anthropic_client import KEY_VARIABLE, AnthropicClient
from aqa_runner.heal import Healing
from aqa_runner.model_router import ModelRouter
from aqa_runner.model_turns import Spending
from aqa_runner.replay_result import EXIT_CODES, rolled_up
from aqa_runner.strict_replay import StrictRun

from aqa_cli.common import admitted_runs, replayed_runs, scan_of, stop

_UNKEYED: Final = "nothing was replayed, since a drift could only fail without it"


def run(
    spec_paths: Annotated[
        list[Path], typer.Argument(metavar="SPEC...", help="The spec files to run.")
    ],
    *,
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="The start origin; the project config's base_url if omitted."
        ),
    ] = None,
) -> None:
    """Replay specs' compiled scripts with every locator, each in a sandboxed
    browser; a target that drifted goes to the healer, whose rebinding is a
    proposal in the run record's replay.json, for you to accept. Nothing changes
    the compiled script."""
    runs = admitted_runs(spec_paths, url)
    if not os.environ.get(KEY_VARIABLE):
        subject = ", ".join(each.spec.frontmatter.id for each in runs)
        problems = (f"{KEY_VARIABLE}: not set: the healer's model is Anthropic's",)
        stop("spec_error", subject, _UNKEYED, problems, scan=scan_of(runs))
    ended = replayed_runs(runs, scan_of(runs), heal=_repair)
    if code := EXIT_CODES[rolled_up(each.outcome for each in ended)]:
        raise typer.Exit(code)


def _repair(run_of: StrictRun, spending: Spending) -> Healing:
    """The healer of one run. Building its router makes no model client: one
    comes only when a drift needs a call."""
    router = ModelRouter.from_config(run_of.config, AnthropicClient)
    budgets = run_of.config.budgets
    return Healing(router, spending, budgets.heal_turns, budgets.resolve_seconds)
