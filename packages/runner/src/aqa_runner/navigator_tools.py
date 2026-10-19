"""The navigator's tools (ADR-0024's #53 P13, P14 and P19 amendments): one
attempt's seven actions through its browser session, each dispatched as
replay dispatches a step (`executor.dispatched`), `describe_parts`, which
lists an element's parts for the next call (`navigator_parts`), and its three
check tools, which bind each planned check to its target, a part included,
or a `probe_equals` check to one of its probe's options (#53 P20), evaluate
it as the confirmation will, and compile the path. A call refused
before its intent is a tool error the attempt survives; a step dispatched
without its completion on disk ends the attempt, and no tool does any work
after it. Every text a tool returns is fixed words, plan text and
`aqa_runner.reasons`' reasons, scanned again, but for the parts a description
lists, which the page chose and `navigator_parts` scans."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from aqa_core.compiled import CompiledScript, Locator, Target
from aqa_core.compiler import (
    CompileError,
    ExploredPath,
    PathStep,
    ProbePath,
    Provenance,
    assertion_for,
    compile_script,
)
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan, PlannedCheck, planned_checks
from aqa_core.project import path_on_origin
from aqa_core.schema import Contract
from aqa_core.spec import Spec
from playwright.async_api import Error

from aqa_runner import evaluation, executor, reasons
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession, RefError, one_key
from aqa_runner.document_origins import DocumentChangedError, PolicyEventError
from aqa_runner.egress import EgressGate
from aqa_runner.evaluation import AssertionResult, Evaluation, evaluate_each
from aqa_runner.explore_result import assertion_words
from aqa_runner.locator_generation import (
    LocatorError,
    Seen,
    TargetUses,
    seen_element,
)
from aqa_runner.navigator import (
    TOOLS,
    AssertCheck,
    Call,
    Click,
    DescribeParts,
    Fill,
    FillSecret,
    Finish,
    Navigate,
    NoteForAbsence,
    Offered,
    Press,
    Reload,
    Select,
)
from aqa_runner.navigator_parts import PART_REF, Described, Parts
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
TOOL_NAMES = {model: name for name, (model, _) in TOOLS.items()}

# The fields an action's intent and `PathStep.fields` name otherwise.
_FIELDS = {"path": "url", "text": "value", "name": "secret"}

# What an action says when the run was interrupted while it was prepared,
# naming neither host nor port.
INTERRUPTED = (
    "the run stopped: the page reached a host the run doesn't allow, or an "
    "allowed host couldn't be reached"
)

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


@dataclass(frozen=True)
class Finished:
    """What `finish` did: the unconfirmed script, when every bound check
    passes on the page now and the path compiled, else None; and the text
    the model reads of it."""

    script: CompiledScript | None
    text: Redacted


@dataclass(frozen=True)
class _Bound:
    """A planned check's binding: what it reads, a target or a probe's JSON
    path, or None for a check that reads neither, and its last evaluation in
    the attempt."""

    reads: Target | ProbePath | None
    result: AssertionResult


def _misfit(call: AssertCheck, check: PlannedCheck) -> str | None:
    """Why `call` can't bind `check` as written."""
    named = call.check_id
    if check.check == "probe_equals_baseline":
        return f"{named} is a {check.check} check, which exploring can't bind yet"
    if check.check == "probe_equals":
        return None  # its ref names an option (`_option`)
    if check.check == "not_visible" and call.ref is not None:
        return (
            f"{named} is a not_visible check: note its element with "
            "note_for_absence while it shows, then call assert_check with no ref "
            "once it is gone"
        )
    if check.target_meaning is None and call.ref is not None:
        return f"{named} reads no element: call it with no ref"
    if check.check != "not_visible" and check.target_meaning and call.ref is None:
        return f"{named} reads an element: call it with the ref of the element it reads"
    return None


@dataclass
class Attempt:
    """One attempt of an explore run: its session, its record part, the
    run's start origin and frozen plan, each planned meaning's contract
    (`contracts_by_meaning`), the secrets it binds and its `budget`, the
    run's `resolve_seconds`, the spec, project config and egress gate its
    checks are evaluated and compiled with, `interrupted`, whether the run
    has an egress block or an infrastructure event, and what the probes its
    checks read offer it (`offered`). It keeps its completed actions by step
    number, as compiling takes them (`ExploredPath`), its last observation,
    and each planned check's binding: what it reads and its last
    evaluation. A run whose admission couldn't keep its secrets
    is `ValueError`, before any use of the browser."""

    session: BrowserSession
    record: RunRecord
    start: str
    plan: CoveragePlan
    contracts: Mapping[str, Contract | None]
    secrets: Mapping[str, BoundSecret]
    budget: float
    spec: Spec
    config: ProjectConfig
    gate: EgressGate
    interrupted: Callable[[], bool]
    offered: Offered = field(default_factory=Offered)
    steps: dict[int, PathStep] = field(init=False, default_factory=dict)
    observation: Redacted | None = field(init=False, default=None)
    _uses: dict[str, TargetUses] = field(init=False, default_factory=dict)
    _bound: dict[str, _Bound] = field(init=False, default_factory=dict)
    parts: Parts = field(init=False, default_factory=Parts)

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

    @property
    def checks(self) -> dict[str, AssertionResult | None]:
        """Each planned check by ID, with its last evaluation in the attempt,
        or None while it was never bound (`ExploreEnd.checks`)."""
        return {
            check_id: self._bound[check_id].result if check_id in self._bound else None
            for check_id in planned_checks(self.plan)
        }

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
        """Run `call`, once the last description's parts are let go: nothing
        once the attempt has ended; then its arguments checked
        (`_unrunnable`, `_unfit`), and everything else before its intent
        (`_prepared`); nothing either once the run is `interrupted` by then;
        then its intent, action, settling and completion. A record that
        can't write its line raises, as does a cancellation."""
        await self.parts.void()
        tool = TOOL_NAMES[type(call)]
        if problem := self._unrunnable(tool, call) or self._unfit(call):
            return self._refused(problem)
        try:
            target, action = await self._prepared(call)
        except _REFUSED as error:
            reason = reasons.reason(error, self.session.redactor)
            return self._refused(f"{tool} did not run: {reason}")
        if self.interrupted():
            # A block or an event while it was prepared: no intent.
            return self._refused(f"{tool} did not run: {INTERRUPTED}")
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

    async def describe(self, call: DescribeParts) -> Described:
        """`describe_parts`: list the parts of the element `call.ref` names
        in the last observation, for the next turn's call. Nothing is
        dispatched and no intent is written; a refusal is a tool error, with
        no parts."""
        problem = self._unrunnable("describe_parts", call)
        if problem is None:
            try:
                return await self.parts.describe(
                    self.session, self.observation or "", call.ref
                )
            except _REFUSED as error:
                reason = reasons.reason(error, self.session.redactor)
                problem = f"describe_parts did not run: {reason}"
        return Described(self._said(problem), self._said(""))

    async def check(self, call: AssertCheck) -> Redacted:
        """`assert_check`: bind the planned check to the element `call.ref`
        names, through its meaning's targets (`uses`), a `probe_equals`
        check to the option it names (`_option`), or a check to none, then
        evaluate it once on the page now. A binding is kept whatever its
        outcome; a refusal binds nothing and changes no earlier binding."""
        if problem := self._unrunnable("assert_check", call):
            return self._said(problem)
        _, check = planned_checks(self.plan)[call.check_id]
        if problem := _misfit(call, check):
            return self._said(f"assert_check did not run: {problem}")
        try:
            reads = (
                self._option(call, check)
                if check.check == "probe_equals"
                else await self._target(call, check)
            )
        except _REFUSED as error:
            reason = reasons.reason(error, self.session.redactor)
            return self._said(f"assert_check did not run: {reason}")
        if isinstance(reads, str):
            return self._said(f"assert_check did not run: {reads}")
        (result,) = await self._kept({call.check_id: reads})
        return self._said(
            f"{call.check_id} is bound, and on this page it is {assertion_words(result)}"
        )

    async def note(self, call: NoteForAbsence) -> Redacted:
        """`note_for_absence`: generate, from the element `call.ref` names
        while it shows, the locators a later `assert_check` of the
        not_visible check starts from, under the check's subject contract."""
        if problem := self._unrunnable("note_for_absence", call):
            return self._said(problem)
        _, check = planned_checks(self.plan)[call.check_id]
        meaning = check.target_meaning
        if check.check != "not_visible" or meaning is None:
            return self._said(
                f"note_for_absence did not run: {call.check_id} is a {check.check} "
                "check, and it notes only a not_visible check's element"
            )
        try:
            seen = await self._seen(call.ref)
            async with self.session.observing_page() as page:
                await self.uses(meaning).see_for_negative_check(page, seen)
        except _REFUSED as error:
            reason = reasons.reason(error, self.session.redactor)
            return self._said(f"note_for_absence did not run: {reason}")
        return self._said(
            f"{call.check_id}'s element is noted where it shows: call assert_check "
            f"for {call.check_id} with no ref once it is gone"
        )

    async def finish(self, call: Finish, by: Provenance) -> Finished:
        """`finish`: evaluate every bound check on the page now, keeping each
        result, then compile the steps `call` keeps and the bound checks
        (`compile_script`). Every check that doesn't pass and every compile
        problem are one tool error the attempt survives."""
        if problem := self._unrunnable("finish", call):
            return Finished(None, self._said(problem))
        bound = {check_id: each.reads for check_id, each in self._bound.items()}
        failing = [
            f"{result.id} is {assertion_words(result)}"
            for result in await self._kept(bound)
            if result.outcome != "pass"
        ]
        problems = [f"on this page {'; '.join(failing)}"] if failing else []
        try:
            script = compile_script(
                self.spec,
                self.config,
                self.plan,
                ExploredPath(self.steps, call.steps, bound),
                by,
            )
        except CompileError as error:
            problems.extend(error.problems)
        if problems:
            text = "; ".join(problems)
            return Finished(None, self._said(f"finish compiled nothing: {text}"))
        return Finished(
            script,
            self._said("finish compiled the path, and every check passes on this page"),
        )

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
                    await asyncio.sleep(evaluation.LOOK_SECONDS)
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

    def _unrunnable(self, tool: str, call: Call) -> str | None:
        """Why `call` can't run at all, never quoting a bound value: the
        attempt has ended, an argument spells a bound value, or it names a
        check the plan doesn't have."""
        if pending := self.record.unresolved():
            return (
                f"{tool} did not run: this attempt has ended, since step "
                f"{pending[0].seq} was dispatched and its completion isn't on disk"
            )
        for name, value in call.model_dump(mode="json").items():
            if self.session.redactor.spells(value):
                hint = "; fill a secret with fill_secret" if name == "text" else ""
                return (
                    f"{tool} did not run: its {name} holds a test secret's value{hint}"
                )
        if isinstance(call, AssertCheck | NoteForAbsence) and (
            call.check_id not in planned_checks(self.plan)
        ):
            return f"{tool} did not run: the plan has no check {call.check_id}"
        return None

    def _unfit(self, call: Action) -> str | None:
        """Why the action `call` can't run as written."""
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

    def _option(self, call: AssertCheck, check: PlannedCheck) -> ProbePath | str:
        """The JSON path of the option `call.ref` names, or why it can't bind
        `check`: it isn't one of the check's probe's options, or its path
        holds a bound value, which no compiled script may."""
        option = self.offered.options.get(call.ref or "")
        if option is None:
            return (
                f"{call.check_id} reads probe {check.probe}: call it with the ref "
                "of one of that probe's options in the probes block"
            )
        if option.probe != check.probe:
            return (
                f"{call.ref} is an option of probe {option.probe}, and "
                f"{call.check_id} reads probe {check.probe}"
            )
        if self.session.redactor.finds(option.json_path):
            return f"{call.ref}'s path holds a test secret's value, so no check can read it"
        return ProbePath(option.json_path)

    async def _target(self, call: AssertCheck, check: PlannedCheck) -> Target | None:
        """The target `check` reads: none for a check that reads no element;
        a not_visible check's, where its element was noted; else the one the
        element `call.ref` names gets."""
        if check.target_meaning is None:
            return None
        uses = self.uses(check.target_meaning)
        if call.ref is None:
            async with self.session.observing_page() as page:
                index = await uses.add_negative_check(page)
        else:
            seen = await self._seen(call.ref)
            async with self.session.observing_page() as page:
                index = await uses.add(page, seen, "assertion")
        return uses.targets[index]

    async def _seen(self, ref: str) -> Seen:
        """The element `ref` names: a part of the last description, by a
        part's ref such as p1.2, or else its element in the last
        observation."""
        if PART_REF.fullmatch(ref):
            return await self.parts.seen(self.session, ref)
        return await seen_element(self.session, self.observation or "", ref)

    async def _kept(
        self, bindings: Mapping[str, Target | ProbePath | None]
    ) -> tuple[AssertionResult, ...]:
        """Evaluate each binding's check once on the page now, in plan order,
        with every locator of its target, as the confirmation does
        (`evaluate_each`), and keep the binding with its result."""
        plan = planned_checks(self.plan)
        ordered = [check_id for check_id in plan if check_id in bindings]
        targets = {
            check_id: target
            for check_id in ordered
            if isinstance(target := bindings[check_id], Target)
        }
        assertions = [
            assertion_for(
                check_id,
                *plan[check_id],
                target=check_id if check_id in targets else None,
                json_path=read.json_path
                if isinstance(read := bindings[check_id], ProbePath)
                else None,
            )
            for check_id in ordered
        ]
        results = await evaluate_each(
            self.session,
            assertions,
            Evaluation(
                targets,
                self.spec,
                self.start,
                self.gate,
                self.budget,
                self.session.redactor,
                every_locator=True,
            ),
        )
        for result in results:
            self._bound[result.id] = _Bound(bindings[result.id], result)
        return results


def _fields(call: Action) -> dict[str, str]:
    """`call` as its intent records it: its action and its own field."""
    own = call.model_dump(exclude={"ref", "meaning"})
    return {"action": TOOL_NAMES[type(call)]} | {
        _FIELDS.get(name, name): value for name, value in own.items()
    }
