"""The aqa command's entry point (API.md §7)."""

from importlib import metadata
from typing import Annotated

import typer

# Tracebacks never show local variables, which can hold provider keys and
# test-secret values (AGENTS.md rule 9). This is Typer's default since 0.23;
# it is stated here so an upgrade or a debugging edit can't flip it silently.
# No shell-completion options: API.md §7 lists the whole command surface.
app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"aqa {metadata.version('aqa-cli')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Agentic QA: explore a spec once, compile it into a script, then replay that
    script with no model calls.
    """
