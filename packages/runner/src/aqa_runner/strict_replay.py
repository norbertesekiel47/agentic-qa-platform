"""One strict replay of a spec's compiled script (#239; ADR-0024's #238
amendment): a run record and an egress gate, then the spec's reset hook and
the script replayed with every locator and no model, through
`replays.replay_run`, in a fresh browser on the caller's driver; then the
run's `replay.json` (`replay_result.present`), whose outcome decides.

The caller admits the script and binds its test secrets first, for every
spec before any run (`aqa replay`), so nothing here sends a reset for a
script replay would refuse. What replay raises becomes a stop: a reset that
failed (14), a browser that isn't sandboxed or a selector engine left
unregistered (10), an egress listener that couldn't open (13), and a record
that couldn't be made or took no more lines (15)."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Final

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.spec import Spec
from playwright.async_api import Playwright

from aqa_runner.binding import UnregisteredEngineError
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.egress import EgressGate, egress_policy
from aqa_runner.egress_proxy import EgressProxy, ListenerUnavailableError
from aqa_runner.executor import RunResult
from aqa_runner.redaction import Redactor
from aqa_runner.replay_result import (
    Outcome,
    ReplayEnd,
    Stop,
    final_outcome,
    present,
    unrecorded,
)
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.reset import ResetFailedError
from aqa_runner.run_record import RefusedWriteError, RunRecord, require_clean
from aqa_runner.sandbox import SandboxUnavailableError

# Why a run stopped, in fixed words.
_UNMADE: Final = "the run record could not be created; nothing was replayed"
_UNWRITTEN: Final = "the run record took no more lines, so the replay stopped"
_UNSAVED: Final = "replay.json could not be written to the run record"


@dataclass(frozen=True)
class StrictRun:
    """One spec's strict replay, admitted (`executor.admitted`): its spec
    root, spec, project config and start origin, the compiled script, and
    the test secrets the run binds."""

    root: Path
    spec: Spec
    config: ProjectConfig
    start: str
    script: CompiledScript
    bound: Mapping[str, BoundSecret]


@dataclass(frozen=True)
class Replayed:
    """How one strict run ended: its outcome; its document, `replay.json`
    as written or as the record couldn't take it; and its record, None when
    none could be made."""

    outcome: Outcome
    document: Mapping[str, object]
    record: Path | None


async def strict_replay(run: StrictRun, playwright: Playwright) -> Replayed:
    """Replay `run` once in a new record under its spec root, launching its
    browser from `playwright`, whose selector engines are registered, and
    write its `replay.json` (module docstring)."""
    spec = run.spec
    redactor = Redactor(run.bound.values())
    try:
        record = RunRecord.create(run.root).redacting(redactor)
    except OSError, ValueError:  # ValueError: a .aqa or .aqa/runs that is a link
        return Replayed("record_error", unrecorded(spec, redactor, _UNMADE), None)
    gate = EgressGate(egress_policy(spec, run.config, run.start))
    proxy = EgressProxy(gate)
    replaying = Replaying(spec, run.config, run.start, gate)
    opening = partial(_opened, playwright, proxy, record)
    result: RunResult | None = None
    stops: list[Stop] = []
    try:
        result = await replay_run(run.script, replaying, opening, every_locator=True)
    except ResetFailedError as error:
        stops.append(Stop("reset_unsafe", record.redactor.redact(str(error))))
    except (SandboxUnavailableError, UnregisteredEngineError) as error:
        stops.append(Stop("no_sandbox", record.redactor.redact(str(error))))
    # First: the listener's error is an OSError.
    except ListenerUnavailableError as error:
        stops.append(Stop("unreachable", record.redactor.redact(str(error))))
    except OSError, RefusedWriteError:
        stops.append(Stop("record_error", record.redactor.redact(_UNWRITTEN)))
    # Read from the proxy and gate, not the result: a record that took no
    # more lines, egress.json's included, loses the result, never a block.
    blocks = proxy.egress_blocks(run.config.egress.expected_blocked)
    events = tuple(gate.infrastructure_events)
    end = ReplayEnd(record, spec, run.script, result, blocks, events, tuple(stops))
    return _ended(end)


@asynccontextmanager
async def _opened(
    playwright: Playwright, proxy: EgressProxy, record: RunRecord
) -> AsyncIterator[Opened]:
    """What the replay runs in once its reset has passed: the run's egress
    proxy, its first phase open, and Chromium for the replay to launch its
    browser from."""
    async with proxy:
        yield Opened(playwright.chromium, proxy, record)


def _ended(end: ReplayEnd) -> Replayed:
    """Write the run's `replay.json`, checked as written, and say how the
    run ended. A document the record refuses or can't write adds a record
    error, which decides unless a more decisive outcome holds."""
    document = present(end)
    try:
        end.record.write(
            "replay.json",
            document,
            check=require_clean(end.record.redactor, "replay.json"),
        )
    except OSError, RefusedWriteError:
        unsaved = Stop("record_error", end.record.redactor.redact(_UNSAVED))
        end = replace(end, stops=(*end.stops, unsaved))
        document = present(end)
    return Replayed(final_outcome(end), document, end.record.path)
