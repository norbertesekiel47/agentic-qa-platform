"""Fixtures the heal's tests share (#244): a run of `Healing` on a scripted model
through `replays.replay_run`, in real Chromium against #240's drift pages, what
the model's moves look like, and the recorder's heal cases, each one `aqa run`
of a drift page (TESTING §4). Imported by its path, as pytest names the runner's
test modules (TESTING.md §1, Shared egress fixtures)."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from aqa_core.compiled import CompiledScript
from aqa_core.heal_patch import blob_sha
from aqa_core.project import contracts_fingerprint, load_config, load_spec
from aqa_core.spec import Spec
from aqa_runner.admission import admitted
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import RunResult
from aqa_runner.heal import Healing
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.model_router import ModelRouter
from aqa_runner.model_turns import Spending
from aqa_runner.publication import published_text
from aqa_runner.redaction import NO_SECRETS, Redactor
from aqa_runner.repairs import Basis, Drift, Offer
from aqa_runner.replay_result import ReplayEnd, present
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.run_record import RunRecord
from aqa_runner.strict_replay import Replayed, StrictRun, strict_replay
from langchain_core.messages import AIMessage, BaseMessage
from playwright.async_api import async_playwright

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.drift_fixtures import (
    BUTTON,
    DETAILS,
    IN_VIEW,
    SHIPPING,
    order_spec,
    page,
    serving,
)
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.explore_graph_fixtures import (
    Move,
    Scripted,
    answer,
    ref,
    tool,
)

MOVED = page("moved")
# Long enough that a heal's reads don't run out under a loaded machine.
BUDGETS = "budgets: { resolve_seconds: 2 }\n"
# The blob SHA a harness's proposals cite: `git hash-object` of `hello\\n`.
BLOB = "ce013625030ba8dba906f756967f9e9ca394464a"


def rebinding(
    role: str, name: str, *, confidence: float = 0.9, rationale: str = "it fits"
) -> Move:
    """A move that picks the element the request's snapshot gives `role` and
    `name`."""
    return tool(
        "rebind",
        lambda messages: {
            "ref": ref(messages, role, name),
            "confidence": confidence,
            "rationale": rationale,
        },
    )


SHOW = rebinding("button", "Show details", confidence=0.9, rationale="it moved")
NO_TOOL = answer(AIMessage(content="nothing on this page fits"))
FOUND_NONE = "the healer found no element for the meaning"


def shipping_at(root: Path) -> Path:
    return order_spec(root, SHIPPING)


def in_view_at(root: Path) -> Path:
    return order_spec(root, IN_VIEW)


def with_budgets(budgets: str) -> str:
    return f"budgets: {{ resolve_seconds: 2, {budgets} }}\n"


@dataclass(frozen=True)
class Healed:
    """A run that healed on a scripted model: its result, its record's
    directory, the model, each request the app saw, the committed script, each
    drift the repair was handed, and how the run ended."""

    result: RunResult
    record: Path
    client: Scripted
    seen: list[tuple[str, str]]
    committed: CompiledScript
    drifts: list[Drift]
    end: ReplayEnd

    @property
    def document(self) -> dict[str, Any]:
        """The run's `replay.json`, as a reader of its JSON gets it."""
        loaded: dict[str, Any] = json.loads(json.dumps(present(self.end)))
        return loaded

    def verdict(self, kind: str, name: object) -> dict[str, Any]:
        """The verdict the document gives the step or assertion `kind` called
        `name`."""
        verdicts: list[dict[str, Any]] = self.document["verdicts"]
        return next(each for each in verdicts if each.get(kind) == name)

    def requests(self) -> list[str]:
        """The text of each request's last message, as sent."""
        return [request[-1].text for request in self.client.requests]

    def asked(self) -> Sequence[Sequence[BaseMessage]]:
        return self.client.requests


def healed(
    tmp_path: Path,
    *moves: Move,
    script: CompiledScript = DETAILS,
    spec_at: Any = shipping_at,
    html: str = MOVED,
    config: str = BUDGETS,
    before_the_heal: Callable[[Drift], Awaitable[object]] | None = None,
) -> Healed:
    """`script` replayed with every locator on `html`, with `Healing` on a model
    that plays `moves`, for the spec `spec_at` writes under a spec root, whose
    config is `config`. `before_the_heal` runs on each drift first."""
    path = spec_at(tmp_path / "qa")
    (path.parent / "config.yaml").write_text(config)
    loaded = load_config(path.parent / "config.yaml")
    spec = load_spec(path, loaded)
    fingerprint = contracts_fingerprint(loaded, script.spec_id)
    by = script.compiled_by.model_copy(update={"subject_contracts": fingerprint})
    committed = fx.for_spec(
        script.model_copy(update={"compiled_by": by}), spec
    ).model_copy(deep=True)
    client = Scripted(*moves)
    drifts: list[Drift] = []
    with serving(html) as app:
        run_gate = gate(allowed=(app.origin,))
        _, _, bound, _ = admitted(committed, spec, loaded, app.origin)
        record = RunRecord.create(path.parent).redacting(Redactor(bound.values()))
        spending = Spending(record, Decimal(str(loaded.budgets.heal_model_usd)))
        healing = Healing(
            ModelRouter.from_config(loaded, client),
            spending,
            loaded.budgets.heal_turns,
            loaded.budgets.resolve_seconds,
        )

        async def repair(drift: Drift) -> Offer:
            drifts.append(drift)
            if before_the_heal is not None:
                await before_the_heal(drift)
            return await healing.repair(drift)

        proxy = EgressProxy(run_gate)

        @asynccontextmanager
        async def opening() -> AsyncIterator[Opened]:
            async with async_playwright() as playwright, proxy:
                await register_identity_engine(playwright)
                yield Opened(playwright.chromium, proxy, record)

        async def scenario() -> RunResult:
            replaying = Replaying(spec, loaded, app.origin, run_gate)
            # No heal test may hang the suite.
            async with asyncio.timeout(60):
                return await replay_run(
                    committed, replaying, opening, every_locator=True, repair=repair
                )

        result = asyncio.run(scenario())
    end = ReplayEnd(
        record,
        spec,
        committed,
        result,
        proxy.egress_blocks(loaded.egress.expected_blocked),
        tuple(run_gate.infrastructure_events),
        base=BLOB,
    )
    return Healed(result, record.path, client, app.seen, committed, drifts, end)


def outcomes(run: Healed) -> list[tuple[int, str]]:
    return [(step.seq, step.outcome) for step in run.result.steps]


def basis(run: Healed, index: int = 0) -> Basis:
    given = run.result.repairs[index].basis
    assert given is not None
    return given


def turn_files(run: Healed, key: str = "1") -> list[str]:
    folder = run.record / f"heals/{key}"
    return sorted(path.name for path in folder.glob("[0-9]*.json"))


# The recording's bounds (ADR-0024's #244 amendment): a drift's one healer
# response, a turn or two to spare, and less than a round's limit a run.
RECORDING_CONFIG = (
    "budgets: { resolve_seconds: 2, heal_turns: 3, heal_model_usd: 0.10 }\n"
)
MOVED_LOCATORS: list[dict[str, Any]] = [
    {"role": "button", "name": "Show details"},
    {"css": "button", "scope": {"css": "#order"}},
]
RENAMED_LOCATORS: list[dict[str, Any]] = [
    {"role": "button", "name": "View details"},
    {"css": "button", "scope": {"css": "#toolbar"}},
]


@dataclass(frozen=True)
class HealCase:
    """One recording: its cassette, the script and the expectation it is compiled
    from, the page it drifts on, and the locators its proposal must bind."""

    cassette: str
    script: CompiledScript
    expectation: str
    html: str
    locators: list[dict[str, Any]]


HEAL_CASES = (
    HealCase("heal_moved", DETAILS, SHIPPING, MOVED, MOVED_LOCATORS),
    HealCase("heal_renamed", DETAILS, SHIPPING, page("renamed"), RENAMED_LOCATORS),
    HealCase("heal_moved_assertion", BUTTON, IN_VIEW, MOVED, MOVED_LOCATORS),
    HealCase(
        "heal_renamed_assertion", BUTTON, IN_VIEW, page("renamed"), RENAMED_LOCATORS
    ),
)


def heal_spec(case: HealCase, root: Path) -> Spec:
    """`case`'s spec under the spec root `root`, with the recording's config."""
    path = order_spec(root, case.expectation)
    (root / "config.yaml").write_text(RECORDING_CONFIG)
    return load_spec(path, load_config(root / "config.yaml"))


def heal_disallowed(case: HealCase, replayed: Replayed) -> str | None:
    """Why `replayed` isn't what `case` records, in fixed words, or None."""
    proposal = replayed.document.get("heal_proposal")
    if not isinstance(proposal, dict):
        return f"it ended {replayed.outcome}, which this case doesn't allow"
    [change] = proposal["patch"]
    if change["value"] != case.locators:
        return "its proposal binds another element than this case's"
    return None


async def heal_recorded(
    case: HealCase, spec: Spec, router: ModelRouter
) -> tuple[Replayed, str | None]:
    """`case`'s `aqa run` with `router`, in real Chromium against the fixture app
    serving its page: how it ended, and why that isn't what `case` records."""
    root = spec.path.parent
    config = load_config(root / "config.yaml")
    script = fx.for_spec(case.script, spec)

    def heal(spending: Spending) -> Healing:
        budgets = config.budgets
        return Healing(router, spending, budgets.heal_turns, budgets.resolve_seconds)

    with serving(case.html) as app:
        blob = blob_sha(published_text(script).encode())
        run = StrictRun(root, spec, config, app.origin, script, {}, blob)
        async with async_playwright() as playwright:
            await register_identity_engine(playwright)
            replayed = await strict_replay(run, playwright, NO_SECRETS, heal=heal)
    return replayed, heal_disallowed(case, replayed)
