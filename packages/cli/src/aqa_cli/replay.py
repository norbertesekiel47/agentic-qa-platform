"""`aqa replay` (API.md §7; ADR-0024's #238 and #239 amendments): replay
each spec's committed compiled script in strict mode, with no model and no
heal, say how each run ended in one line, and exit with the runs' outcome
rolled up (`replay_result.rolled_up`).

Every spec is admitted before any run (`common.admitted_runs`): it resolves;
its compiled script, `<spec root>/.compiled/<spec id>.json`, read as explore
reads it, with no link on the way (`publication`), exists and loads, and replay
admits it (`admission.admitted`), each a spec error (exit 5), or 15 for a file
that can't be read; and its test secrets bind (12). Then each runs in argument
order (`aqa_runner.strict_replay`), on one driver (`common.replayed_runs`).

Every line printed goes through `aqa_cli.common.rendered`, scanned with
every test secret the invocation bound, and each run's record scans what
it writes with the same secrets. `--json`'s array is checked with them and
printed as it is, or as `[]` when it spells one."""

import json
from pathlib import Path
from typing import Annotated, Final

import typer
from aqa_runner.replay_result import EXIT_CODES, rolled_up

from aqa_cli.common import admitted_runs, replayed_runs, say, scan_of

_UNPRINTABLE: Final = (
    "the runs' documents are withheld, since they would spell a bound value"
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
    its run record's replay.json."""
    if as_json and quiet:
        raise typer.BadParameter("not with --quiet", param_hint="'--json'")
    try:
        runs = admitted_runs(spec_paths, url)
        scan = scan_of(runs)
        ended = replayed_runs(
            runs,
            scan,
            lines=not as_json,
            quiet=quiet,
            before_screenshots=before_screenshots,
        )
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
