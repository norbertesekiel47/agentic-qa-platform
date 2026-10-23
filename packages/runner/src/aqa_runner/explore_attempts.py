"""An explore run's attempts (#56; ADR-0024's Reset hook, Path selection and
attempts, and Confirmation replay): how many it may make, whether a new
attempt may start, what a failed confirmation leaves the next attempt, and
when a pass is flaky.

A new attempt loads the start URL again, which may change the app as an
action may, so without a reset hook no new attempt starts: every attempt
loads its start URL before its first turn, so by the time one could end in
a new attempt, its start has always loaded. Nothing here touches a
browser, model or file."""

import json
from dataclasses import dataclass
from typing import Final

from aqa_core.compiled import CompiledScript

from aqa_runner.executor import RunResult
from aqa_runner.explore_result import feedback
from aqa_runner.redaction import Redacted, Redactor

UNRESET: Final = (
    "the spec has no reset hook, and this attempt's steps, its start URL's "
    "load included, may have changed the app, so no new attempt may repeat "
    "them (AGENTS.md §6)"
)


def replayed_as(script: CompiledScript) -> str:
    """What two confirmations compare to call a spec flaky, normalized
    structural identity: `script` as canonical JSON without when and by
    which models it was compiled, whether it was confirmed, and its steps'
    flags, which change what the record says, never what replays. Targets'
    meanings stay, so two scripts that differ only there aren't identical."""
    shown = script.model_dump(
        mode="json", exclude={"compiled_at", "compiled_by", "confirmed"}
    )
    for step in shown["steps"]:
        del step["side_effect"], step["side_effect_basis"]
    return json.dumps(shown, sort_keys=True)


@dataclass
class Attempts:
    """The run's attempts so far: its budget, whether the spec has a reset
    hook, how many attempts it made, how many turn records it wrote, and
    what the next attempt reads of the last failed confirmation."""

    budget: int
    resets: bool
    made: int = 1
    taken: int = 0
    feedback: Redacted | None = None

    def failed_with(
        self, script: CompiledScript, replayed: RunResult, redactor: Redactor
    ) -> None:
        """Keep a failed confirmation of `script`: its feedback."""
        self.feedback = feedback(script, replayed, redactor)
