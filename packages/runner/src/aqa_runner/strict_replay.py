"""One replay of a spec's compiled script with every locator (#239, #244;
ADR-0024's #238 and #244 amendments): a run record and an egress gate, then the
spec's reset hook and the script replayed through `replays.replay_run`, in a
fresh browser on the caller's driver; then the run's `run.json`, what the run
viewer reads of it (#249), and its `replay.json` (`replay_result.present`),
whose outcome decides. With no `heal`, a strict replay, nothing here builds a
model client. With one, what `aqa run` gives, a drift goes to the repair of the
healing it builds for the run.

The caller admits the script and binds its test secrets first, for every
spec before any run (`aqa replay`), so nothing here sends a reset for a
script replay would refuse. What replay raises becomes a stop: a reset that
failed (14), a browser that isn't sandboxed or a selector engine left
unregistered (10), an egress listener that couldn't open (13), and a record
that couldn't be made or took no more lines (15)."""

import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.spec import Spec
from playwright.async_api import Playwright

from aqa_runner.binding import UnregisteredEngineError
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.egress import EgressGate, egress_policy
from aqa_runner.egress_proxy import EgressProxy, ListenerUnavailableError
from aqa_runner.executor import RunResult
from aqa_runner.heal_proposal import proposal_lines
from aqa_runner.model_turns import Spending
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

if TYPE_CHECKING:
    from aqa_runner.heal import Healing

# Why a run stopped, in fixed words.
_UNMADE: Final = "the run record could not be created; nothing was replayed"
_UNWRITTEN: Final = "the run record took no more lines, so the replay stopped"
_UNSAVED: Final = "replay.json could not be written to the run record"
_UNKEPT: Final = "run.json could not be written to the run record"


@dataclass(frozen=True)
class StrictRun:
    """One spec's replay, admitted (`admission.admitted`): its spec root, spec,
    project config and start origin, the compiled script, the test secrets the
    run binds, and the blob SHA of the bytes the script was read from
    (`heal_patch.blob_sha`)."""

    root: Path
    spec: Spec
    config: ProjectConfig
    start: str
    script: CompiledScript
    bound: Mapping[str, BoundSecret]
    blob_sha: str


@dataclass(frozen=True)
class Replayed:
    """How one run ended: its outcome; its document, `replay.json` as written
    or as the record couldn't take it; its record, None when none could be
    made; how long it took, in seconds, which its `run.json` and its line both
    give; what its model calls cost; and what a person reads of its heal
    proposal, if it made one."""

    outcome: Outcome
    document: Mapping[str, object]
    record: Path | None
    seconds: float
    cost: Decimal = Decimal(0)
    proposal: tuple[str, ...] = ()


async def strict_replay(
    run: StrictRun,
    playwright: Playwright,
    scan: Redactor,
    *,
    heal: Callable[[Spending], Healing] | None = None,
) -> Replayed:
    """Replay `run` once in a new record under its spec root, launching its
    browser from `playwright`, whose selector engines are registered, and
    write its `run.json` and `replay.json` (module docstring). The record
    scans with `scan`, every test secret the caller bound, `run`'s among them,
    so no run's record keeps a value another spec binds. With `heal`, drift
    goes to the repair of the healing it builds, once, for the run's spend
    (`heal_model_usd`). The run's time is taken once, from before its record
    is made."""
    began = time.monotonic()
    spec = run.spec
    try:
        record = RunRecord.create(run.root).redacting(scan)
    except OSError, ValueError:  # ValueError: a .aqa or .aqa/runs that is a link
        document = unrecorded(spec, scan, _UNMADE, healing=heal is not None)
        return Replayed("record_error", document, None, time.monotonic() - began)
    spending = Spending(record, Decimal(str(run.config.budgets.heal_model_usd)))
    healing = None if heal is None else heal(spending)
    repair = None if healing is None else healing.repair
    gate = EgressGate(egress_policy(spec, run.config, run.start))
    proxy = EgressProxy(gate)
    replaying = Replaying(spec, run.config, run.start, gate)
    opening = partial(_opened, playwright, proxy, record)
    result: RunResult | None = None
    stops: list[Stop] = []
    try:
        result = await replay_run(
            run.script, replaying, opening, every_locator=True, repair=repair
        )
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
    base = None if heal is None else run.blob_sha
    end = ReplayEnd(
        record, spec, run.script, result, blocks, events, tuple(stops), base
    )
    seconds = time.monotonic() - began
    end = _kept(end, seconds, _models(healing))
    return replace(
        _ended(end, seconds),
        cost=sum((c.cost_usd for c in spending.calls), Decimal(0)),
    )


@asynccontextmanager
async def _opened(
    playwright: Playwright, proxy: EgressProxy, record: RunRecord
) -> AsyncIterator[Opened]:
    """What the replay runs in once its reset has passed: the run's egress
    proxy, its first phase open, and Chromium for the replay to launch its
    browser from."""
    async with proxy:
        yield Opened(playwright.chromium, proxy, record)


def _models(healing: Healing | None) -> dict[str, object]:
    """The models a run's router resolved, as `run.json` holds them: none in a
    strict replay, and the healer's in a run that heals."""
    if healing is None:
        return {}
    healer = healing.router.roles["healer"]
    fallback = None if healer.fallback is None else healer.fallback.name
    return {
        "healer": {
            "provider": healer.model.provider,
            "model": healer.model.name,
            "effort": healer.effort,
            "fallback": fallback,
        }
    }


def _kept(end: ReplayEnd, seconds: float, models: dict[str, object]) -> ReplayEnd:
    """Write the run's `run.json`, what the run viewer reads besides
    `replay.json` (#249): the spec's goal and expectations, how long the run
    took, the models it routed to and the script it replayed. It is scanned
    as evidence is, and left out if the record refuses it, so no goal decides
    a run; a record that can't write it adds a record error, as for
    `replay.json`."""
    spec = end.spec.frontmatter
    document = {
        "schema_version": 1,
        "run_id": end.record.run_id,
        "spec_id": spec.id,
        "goal": spec.goal,
        "expectations": [expectation.text for expectation in spec.expect],
        "duration_seconds": seconds,
        "models": models,
        "script": end.script.model_dump(mode="json", exclude_none=True),
    }
    try:
        with suppress(RefusedWriteError):
            end.record.write("run.json", document)
    except OSError:
        unkept = Stop("record_error", end.record.redactor.redact(_UNKEPT))
        return replace(end, stops=(*end.stops, unkept))
    return end


def _ended(end: ReplayEnd, seconds: float) -> Replayed:
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
    outcome = final_outcome(end)
    return Replayed(
        outcome, document, end.record.path, seconds, proposal=_said(end, document)
    )


def _said(end: ReplayEnd, document: Mapping[str, object]) -> tuple[str, ...]:
    """What a person reads of the proposal `document` holds, if it holds one."""
    proposal = document.get("heal_proposal")
    if not isinstance(proposal, dict) or end.result is None:
        return ()
    spec_id = end.spec.frontmatter.id
    return proposal_lines(spec_id, proposal["id"], end.script, end.result.repairs)
