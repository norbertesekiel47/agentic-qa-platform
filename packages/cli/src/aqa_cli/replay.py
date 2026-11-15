"""`aqa replay` (API.md §7; ADR-0024's #238, #239 and #256 amendments): replay
each spec's committed compiled script in strict mode, with no model and no
heal, say how each run ended in one line, and exit with the runs' outcome
rolled up (`replay_result.rolled_up`).

Every spec is admitted before any run (`common.admitted_runs`): it resolves;
its compiled script, `<spec root>/.compiled/<spec id>.json`, read as explore
reads it, with no link on the way (`publication`), exists and loads, and replay
admits it (`admission.admitted`), each a spec error (exit 5), or 15 for a file
that can't be read; and its test secrets bind (12). Then each runs in argument
order (`aqa_runner.strict_replay`), on one driver (`common.replayed_runs`).

With `--mode verified`, admission is verified admission, a missing provider
key is then a spec error (5) before any browser, as for `aqa run`, and each
run's `model_verify` checks go to the verifier role, on a router of the run's
own that builds no client until a check needs a call. Each spec has its own
`verified_model_usd`, and its line gives what its calls cost as `verified $`.

Every line printed goes through `aqa_cli.common.rendered`, scanned with
every test secret the invocation bound, and each run's record scans what
it writes with the same secrets. `--json`'s array is checked with them and
printed as it is, or as `[]` when it spells one."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Final, Literal

import typer
from aqa_runner.anthropic_client import KEY_VARIABLE, AnthropicClient
from aqa_runner.model_router import ModelRouter
from aqa_runner.model_turns import Spending
from aqa_runner.replay_result import EXIT_CODES, rolled_up
from aqa_runner.strict_replay import StrictRun
from aqa_runner.verification import Verifying

from aqa_cli.common import Asking, admitted_runs, replayed_runs, say, scan_of, stop

_UNPRINTABLE: Final = (
    "the runs' documents are withheld, since they would spell a bound value"
)
_UNKEYED: Final = (
    "nothing was replayed, since a verifier call could only fail without it"
)


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
    mode: Annotated[
        Literal["strict", "verified"],
        typer.Option(
            "--mode",
            help=(
                "strict calls no model. verified also has the verifier role judge "
                "each model_verify check on the page's masked screenshot."
            ),
        ),
    ] = "strict",
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="Print each run's replay.json, in one JSON array, only."
        ),
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", help="Print only the runs that didn't pass.")
    ] = False,
    before_screenshots: Annotated[
        bool,
        typer.Option(
            "--before-screenshots",
            help=(
                "Capture the page before each step, and save it beside the "
                "screenshot of a step that fails."
            ),
        ),
    ] = False,
) -> None:
    """Replay specs' compiled scripts in strict mode, with no model call and
    no heal, each in a sandboxed browser, and write each run's verdicts to
    its run record's replay.json. With --mode verified, a model_verify check
    is judged by the verifier role."""
    if as_json and quiet:
        raise typer.BadParameter("not with --quiet", param_hint="'--json'")
    try:
        runs = admitted_runs(spec_paths, url, mode=mode)
        scan = scan_of(runs)
        if mode == "verified" and not os.environ.get(KEY_VARIABLE):
            subject = ", ".join(each.spec.frontmatter.id for each in runs)
            problems = (
                f"{KEY_VARIABLE}: not set: the verifier's model is Anthropic's",
            )
            stop("spec_error", subject, _UNKEYED, problems, scan=scan)
        asking = Asking(
            verify=_verifier if mode == "verified" else None,
            before_screenshots=before_screenshots,
        )
        ended = replayed_runs(runs, scan, lines=not as_json, quiet=quiet, asking=asking)
    except typer.Exit:
        # A stop before any run.
        if as_json:
            typer.echo("[]")
        raise
    if as_json:
        # Checked, never scanned: a marker in place of a value would break
        # the JSON, so an array that spells one is printed as none.
        text = json.dumps([e.document for e in ended], indent=2)
        if scan.finds(text):
            text = "[]"
            say(scan, _UNPRINTABLE, err=True)
        typer.echo(text)
    if code := EXIT_CODES[rolled_up(e.outcome for e in ended)]:
        raise typer.Exit(code)


def _verifier(
    run_of: StrictRun, spending: Spending, interrupted: Callable[[], bool]
) -> Verifying:
    """The verifier of one run. Building its router makes no model client: one
    comes only when a `model_verify` check needs a call."""
    router = ModelRouter.from_config(run_of.config, AnthropicClient)
    seconds = run_of.config.budgets.resolve_seconds
    return Verifying(router, spending, run_of.spec, run_of.script, interrupted, seconds)
