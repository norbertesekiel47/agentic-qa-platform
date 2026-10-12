"""The navigator's action tools (ADR-0024's #53 P13 amendment): one
attempt's seven actions through its browser session, each dispatched as
replay dispatches a step (`executor.dispatched`). A call refused before its
intent is a tool error the attempt survives; a step dispatched without its
completion on disk ends the attempt. Every text a tool returns is
fixed words and `aqa_runner.reasons`' reasons, scanned again."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from aqa_core.compiled import Locator, Target
from aqa_core.compiler import PathStep
from aqa_core.coverage_plan import CoveragePlan, planned_checks
from aqa_core.project import path_on_origin
from aqa_core.schema import Contract
from playwright.async_api import Error

from aqa_runner import executor, reasons
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession, RefError, one_key
from aqa_runner.document_origins import DocumentChangedError, PolicyEventError
from aqa_runner.locator_generation import LocatorError, TargetUses, seen_element
from aqa_runner.navigator import (
    TOOLS,
    Click,
    Fill,
    FillSecret,
    Navigate,
    Press,
    Reload,
    Select,
)
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import Redacted
from aqa_runner.run_record import RunRecord
from aqa_runner.secret_fields import SecretRefusedError
from aqa_runner.settling import Window

# The navigator's actions, the calls an attempt runs.
type Action = Navigate | Reload | Click | Fill | FillSecret | Select | Press

# How many snapshots `observe` takes at most: one, and three more while the
# page changes under them (ADR-0024's #53 P13 amendment).
LOOKS = 4

# Each action's tool name, as the model calls it.
_TOOL = {model: name for name, (model, _) in TOOLS.items()}

# The fields an action's intent and `PathStep.fields` name otherwise.
_FIELDS = {"path": "url", "text": "value", "name": "secret"}

# What a call refused before its intent raises, Playwright's general error
# type included (a page that broke a look).
_REFUSED = (
    RefError,
    PolicyEventError,
    DocumentChangedError,
    LocatorError,
    SecretRefusedError,
    Error,
)


@dataclass(frozen=True)
class ActionResult:
    """What a call did: its step, or None when it was refused before its
    intent, so nothing was dispatched; and the text the model reads of it."""

    step: executor.StepResult | None
    text: Redacted


@dataclass
class Attempt:
    """One attempt of an explore run: its session, its record part, the
    run's start origin and frozen plan, each planned meaning's contract
    (`contracts_by_meaning`), the secrets it binds and its `budget`, the
    run's `resolve_seconds`. It keeps its completed actions by step number, as
    compiling takes them (`ExploredPath`), and its last observation. A run
    whose admission couldn't keep its secrets is `ValueError`, before any
    use of the browser."""

    session: BrowserSession
    record: RunRecord
    start: str
    plan: CoveragePlan
    contracts: Mapping[str, Contract | None]
    secrets: Mapping[str, BoundSecret]
    budget: float
    steps: dict[int, PathStep] = field(init=False, default_factory=dict)
    observation: Redacted | None = field(init=False, default=None)
    _uses: dict[str, TargetUses] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        redactor = self.session.redactor
        if self.record.redactor is not redactor:
            raise ValueError(
                "an attempt's record must be scanned by its session's redactor"
            )
        if not all(redactor.covers(secret) for secret in self.secrets.values()):
            raise ValueError(
                "an attempt's session redactor must cover every secret it can fill"
            )
        if any(
            contract is not None and redactor.spells(contract.model_dump(mode="json"))
            for contract in self.contracts.values()
        ):
            # Config, which generation hands the browser before any drop.
            raise ValueError(
                "a subject contract spells a bound value, so the run's admission "
                "must refuse its config"
            )
        if redactor.markers_spell_a_value:
            raise ValueError(
                "a secret's marker spells a bound value, so no scan can remove it; "
                "the run's admission must refuse it"
            )

    @property
    def ended(self) -> bool:
        """Whether a step was dispatched with no completion on disk."""
        return bool(self.record.unresolved())

    def uses(self, meaning: str) -> TargetUses:
        """The targets `meaning` gets across its uses, under its subject
        contract, none spelling a bound value."""
        if meaning not in self._uses:
            self._uses[meaning] = TargetUses(
                meaning,
                checks_text=any(
                    check.check == "text_in_target" and check.target_meaning == meaning
                    for _, check in planned_checks(self.plan).values()
                ),
                contract=self.contracts.get(meaning),
                drop=self._spelled,
            )
        return self._uses[meaning]

    async def act(self, call: Action) -> ActionResult:
        """Run `call`: nothing once the attempt has ended; then its arguments
        checked (`_unfit`), and everything else before its intent
        (`_prepared`); then its intent, action, settling and completion. A
        record that can't write its line raises, as does a cancellation."""
        tool = _TOOL[type(call)]
        if pending := self.record.unresolved():
            return self._refused(
                f"{tool} did not run: this attempt has ended, since step "
                f"{pending[0].seq} was dispatched and its completion isn't on disk"
            )
        if problem := self._unfit(tool, call):
            return self._refused(problem)
        try:
            target, action = await self._prepared(call)
        except _REFUSED as error:
            reason = reasons.reason(error, self.session.redactor)
            return self._refused(f"{tool} did not run: {reason}")
        seq = max(self.steps, default=0) + 1
        fields = _fields(call)
        wait = (
            executor.NAVIGATION_SECONDS
            if isinstance(call, Navigate | Reload)
            else self.budget
        )
        step = await executor.dispatched(
            self.session,
            self.record,
            executor.Dispatch(
                seq, fields, True, wait, None if target is None else target.semantic
            ),
            action,
        )
        if step.outcome == "completed":
            self.steps[seq] = PathStep(fields, target)
            settled = (
                "the page settled"
                if step.settled == "idle"
                else "the page was still busy when settling's wait ran out"
            )
            return ActionResult(step, self._said(f"step {seq} ran, and {settled}"))
        if self.ended:
            return ActionResult(
                step,
                self._said(
                    f"step {seq} failed after it was dispatched, so this attempt "
                    f"has ended: {step.error}"
                ),
            )
        return self._refused(f"{tool} did not run: {step.error}")

    async def observe(self) -> Redacted:
        """The page's snapshot, kept as the observation: taken again while
        the page changes under it, at most `LOOKS` times, all within one
        `budget`. Then `DocumentChangedError` if any look saw a change,
        else `UnansweredError`; anything else propagates."""
        limit = asyncio.timeout(self.budget)
        changed = False
        try:
            async with limit:
                for _ in range(LOOKS - 1):
                    try:
                        return await self._observed()
                    except DocumentChangedError:
                        changed = True
                    await asyncio.sleep(executor.LOOK_SECONDS)
                return await self._observed()
        except TimeoutError:
            if not limit.expired():
                raise  # not the budget's own limit
            if changed:
                raise DocumentChangedError from None
            raise UnansweredError(self.budget) from None

    async def _observed(self) -> Redacted:
        self.observation = await self.session.snapshot()
        return self.observation

    def _spelled(self, locator: Locator) -> bool:
        return self.session.redactor.spells(locator.model_dump(mode="json"))

    def _said(self, text: str) -> Redacted:
        return self.session.redactor.redact(text)

    def _refused(self, text: str) -> ActionResult:
        return ActionResult(None, self._said(text))

    def _unfit(self, tool: str, call: Action) -> str | None:
        """Why `call` can't run as written, never quoting a bound value."""
        for name, value in call.model_dump(mode="json").items():
            if self.session.redactor.spells(value):
                hint = "; fill a secret with fill_secret" if name == "text" else ""
                return (
                    f"{tool} did not run: its {name} holds a test secret's value{hint}"
                )
        if isinstance(call, Press) and not one_key(call.key):
            return (
                "press did not run: it takes one key, with only modifiers held "
                "before it, such as Enter or Shift+a"
            )
        if isinstance(call, Select) and not call.option:
            return (
                "select did not run: its option is empty; choose an option by its label"
            )
        if isinstance(call, FillSecret) and call.name not in self.secrets:
            return (
                f"fill_secret did not run: no test secret named {call.name} is bound "
                "for this run"
            )
        return None

    async def _prepared(
        self, call: Action
    ) -> tuple[Target | None, Callable[[], Awaitable[Window]]]:
        """`call`'s target and action, once the page is checked (`navigate`
        checks where it goes), the target generated, and a `fill_secret`'s
        binding checked."""
        session = self.session
        if isinstance(call, Navigate):
            url = path_on_origin(self.start, call.path)
            return None, lambda: session.navigate(url)
        if isinstance(call, Reload | Press):
            # Checked as every observation is, so a page off the allowed
            # origins is refused before the intent.
            async with session.observing_page():
                pass
            key = call.key if isinstance(call, Press) else None
            return None, session.reload if key is None else lambda: session.press(key)
        seen = await seen_element(session, self.observation or "", call.ref)
        uses = self.uses(call.meaning)
        async with session.observing_page() as page:
            index = await uses.add(page, seen, "action")
        target = uses.targets[index]
        element = seen.element
        if isinstance(call, Click):
            return target, lambda: session.click(element)
        if isinstance(call, Fill):
            return target, lambda: session.fill(element, call.text)
        if isinstance(call, Select):
            return target, lambda: session.select(element, call.option)
        secret = self.secrets[call.name]
        await session.secret_refusal(element, secret)
        return target, lambda: session.fill_secret(element, secret)


def _fields(call: Action) -> dict[str, str]:
    """`call` as its intent records it: its action and its own field."""
    own = call.model_dump(exclude={"ref", "meaning"})
    return {"action": _TOOL[type(call)]} | {
        _FIELDS.get(name, name): value for name, value in own.items()
    }
