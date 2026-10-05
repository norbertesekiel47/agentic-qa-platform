"""The selected pilots' inputs, each admitted before any of them is used
(ADR-0023, #51). Manifest answers never enter."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from aqa_core.compiled import (
    CompiledScript,
    FillSecret,
    Press,
    ProbeEquals,
    ProbeEqualsBaseline,
    VisibleUnoccluded,
)
from aqa_core.config import ProjectConfig
from aqa_core.project import (
    SpecError,
    load_compiled,
    load_project,
    path_on_origin,
    start_origin,
)
from aqa_core.schema import StartPath
from aqa_core.spec import Spec, secret_references
from aqa_runner.bound_secrets import (
    MissingSecretError,
    SecretLoggedError,
    bound_secrets,
)
from aqa_runner.browser_session import one_key
from pydantic import TypeAdapter

_PATH: TypeAdapter[str] = TypeAdapter(StartPath)


@dataclass(frozen=True)
class ResetRequest:
    """The spec's reset hook: a POST to this URL on the run's start origin."""

    url: str


@dataclass(frozen=True)
class PilotInput:
    """One selected pilot, admitted for replay."""

    spec: Spec
    config: ProjectConfig
    script: CompiledScript
    start: str
    reset: ResetRequest | None


def load_pilots(
    qa_root: Path, compiled_dir: Path, selected_ids: Sequence[str]
) -> tuple[PilotInput, ...]:
    """Every selected pilot, or every pilot by ID when none is selected, each
    from `<compiled_dir>/<id>.json`. Raises ValueError naming the file and a
    fixed category, never the problem's text, unless all are admitted."""
    try:
        project = load_project(qa_root)
    except (OSError, SpecError):
        raise ValueError(f"{qa_root}: invalid pilot input") from None
    ids = tuple(selected_ids) or tuple(sorted(project.specs))
    if not ids or len(set(ids)) < len(ids) or not set(ids) <= project.specs.keys():
        raise ValueError(f"{qa_root}: invalid selection")
    root = compiled_dir.resolve()
    pilots: list[PilotInput] = []
    for spec_id in ids:
        source = compiled_dir / f"{spec_id}.json"
        refused = ValueError(f"{source}: invalid compiled input")
        # Read where the check looked, so a link can't lead outside the root.
        path = source.resolve()
        if not path.is_relative_to(root):
            raise refused
        try:
            script = load_compiled(path, project.config)
        except (OSError, SpecError):
            raise refused from None
        spec = project.specs[spec_id]
        pilots.append(validate_pilot(spec, project.config, script, source=source))
    return tuple(pilots)


def validate_pilot(
    spec: Spec, config: ProjectConfig, script: CompiledScript, *, source: Path
) -> PilotInput:
    """`script`, already read strictly, admitted for `spec`: compiled from the
    spec as it is now, covering each of its expectations, runnable by the
    executor, reset by a valid hook when a step has side effects, and with
    every test secret the spec references bound for this run. The secrets'
    values are read to check them, never kept. Otherwise raises ValueError
    naming `source` and a fixed category."""
    refused = ValueError(f"{source}: invalid pilot input")
    try:
        start = start_origin(None, config)
        reset = _reset(spec, start)
        bound_secrets(spec, start)
    # ValueError covers _reset's refusals, and a secret value os.environ decoded
    # with surrogates, which fails to encode with a message quoting part of it.
    except (SpecError, ValueError, MissingSecretError, SecretLoggedError):
        raise refused from None
    side_effects = any(step.side_effect for step in script.steps)
    if (side_effects and reset is None) or not _admitted(spec, script):
        raise refused
    return PilotInput(spec, config, script, start, reset)


def _reset(spec: Spec, start: str) -> ResetRequest | None:
    """The spec's reset hook: POST and a path held to start_url's rules,
    ASCII and without a fragment, as a request line carries it, on `start`.
    Raises ValueError for any other hook."""
    hook = spec.frontmatter.preconditions.reset
    if hook is None:
        return None
    method, _, path = hook.http.partition(" ")
    if method != "POST" or "#" in path or not path.isascii():
        raise ValueError("a reset hook is POST and a path")
    return ResetRequest(path_on_origin(start, _PATH.validate_python(path)))


def _admitted(spec: Spec, script: CompiledScript) -> bool:
    """Whether `script` records `spec` as it is now, lists each expectation
    once, in order, with exactly the assertions that name it, realizes every
    required condition, and holds only the steps and checks the executor
    runs (`aqa_runner.executor`, #48)."""
    frontmatter = spec.frontmatter
    probes = frontmatter.preconditions.probes
    referenced = {name for _, name in secret_references(frontmatter)}
    expect = range(len(frontmatter.expect))
    rows = script.coverage.expectations
    listed = [set(row.assertions) for row in rows]
    named = [{a.id for a in script.assertions if a.expect_index == i} for i in expect]
    satisfied = {name for step in script.steps for name in step.satisfies}
    steps, checks = script.steps, script.assertions
    return (
        (script.spec_id, script.spec_hash) == (frontmatter.id, spec.spec_hash)
        and [row.expect_index for row in rows] == list(expect)
        and listed == named
        and {condition.id for condition in script.coverage.requires} <= satisfied
        and all(one_key(s.key) for s in steps if isinstance(s, Press))
        and all(s.secret in referenced for s in steps if isinstance(s, FillSecret))
        and all(
            c.probe in probes
            for c in checks
            if isinstance(c, ProbeEquals | ProbeEqualsBaseline)
        )
        and script.probe_baselines.keys() <= probes.keys()
        and all(c.in_viewport for c in checks if isinstance(c, VisibleUnoccluded))
    )
