"""`aqa heal` (API.md §7; ADR-0024's #247 amendment): `aqa heal <spec>` is `aqa run`
of that one spec, and `aqa heal apply <proposal_id>` writes a proposal a run made to
the compiled script. Nothing here commits it: the person's own commit does that."""

import asyncio
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Final

import typer
from aqa_core.heal_patch import blob_sha
from aqa_core.project import SpecError, load_project
from aqa_runner.heal_proposal import held_proposal, patched_script
from aqa_runner.publication import PublishError, PublishWriteError, publish_text
from aqa_runner.redaction import NO_SECRETS

from aqa_cli.common import found_script, say, spec_root_above, stop
from aqa_cli.run import run

_PROPOSAL_ID: Final = re.compile(r"ah_[0-9a-f]{12}")
_WORDS: Final = "'SPEC | apply PROPOSAL_ID'"
_NOTHING: Final = "nothing was applied"
_UNREAD: Final = (
    "the project or its run records could not be read, so nothing was applied"
)
_STALE: Final = (
    "the compiled script changed since this proposal was made, so nothing was "
    "applied: run aqa run again"
)
_REFORMATTED: Final = (
    "  the compiled script wasn't in the form aqa writes, so its formatting changed too"
)


def heal(
    words: Annotated[
        list[str],
        typer.Argument(
            metavar="SPEC | apply PROPOSAL_ID",
            help="A spec file, or apply and the id of a heal proposal.",
        ),
    ],
    *,
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="The start origin; the project config's base_url if omitted."
        ),
    ] = None,
) -> None:
    """Heal one spec, or apply a heal proposal to its compiled script.

    aqa heal <spec> is aqa run of that one spec.

    aqa heal apply <proposal_id> writes a proposal to the compiled script, and
    commits nothing. A spec file named apply is ./apply."""
    match words:
        case ["apply", proposal] if url is None:
            _apply(proposal)
        case ["apply"]:
            raise typer.BadParameter("apply takes a proposal's id", param_hint=_WORDS)
        case [spec]:
            run([Path(spec)], url=url)
        case _:
            raise typer.BadParameter(
                "give one spec, or apply and a proposal's id with no --url",
                param_hint=_WORDS,
            )


def _apply(proposal: str) -> None:
    """Write the proposal `proposal` names to its spec's compiled script, or stop at
    the first refusal (API.md §7). `Publication.found` comes before the read, so a
    file changed at any point after it is refused under the lock. No test secret is
    bound, so what's printed is escaped and not scanned (`common.rendered`)."""
    if _PROPOSAL_ID.fullmatch(proposal) is None:
        raise typer.BadParameter(
            "a proposal's id is ah_ and 12 lowercase hex digits",
            param_hint="'PROPOSAL_ID'",
        )
    try:
        root = spec_root_above(Path.cwd())
        project = load_project(root)
        spec_id, held = held_proposal(root, proposal)
    except OSError:
        stop("record_error", proposal, _UNREAD)
    except SpecError as error:
        stop("spec_error", proposal, _NOTHING, error.problems)
    spec = project.specs.get(spec_id)
    if spec is None:
        stop("spec_error", proposal, _NOTHING, ("it names no spec of this project",))
    at, script, raw = found_script(root, spec, project.config, NO_SECRETS, _NOTHING)
    if blob_sha(raw) != held.base.blob_sha:
        stop("stale", proposal, _STALE)
    try:
        patched = patched_script(
            raw, script, held, spec=spec, config=project.config, source=at.target
        )
    except SpecError as error:
        stop("spec_error", proposal, _NOTHING, error.problems)
    try:
        # No minutes budget bounds this: `LOCK_SECONDS` bounds the wait.
        asyncio.run(
            publish_text(
                replace(at, own_root=True),
                patched.script,
                patched.text,
                deadline=math.inf,
            )
        )
    except PublishError as error:
        stop("gave_up", proposal, _NOTHING, (str(error),))
    except PublishWriteError as error:
        stop("record_error", proposal, str(error))
    say(
        NO_SECRETS,
        f"applied {proposal} {spec_id}: {at.target}",
        *(f"  {change.path}" for change in held.patch),
        *((_REFORMATTED,) if patched.reformatted else ()),
    )
