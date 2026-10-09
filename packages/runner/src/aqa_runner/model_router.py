"""The router every model-using mode calls through (ADR-0007 and its
amendments): a model per role, a cost record for every response, and the role's
fallback when a model refuses."""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import MAX_PREC, Decimal, localcontext
from typing import Self, get_args

from aqa_core.config import Effort, ModelRoleName, ProjectConfig
from aqa_core.model_costs import CostRecord, Mode, Status, cost_record
from aqa_core.model_roles import ResolvedRole, RoutedModel, resolve_roles
from aqa_core.price_map import vendored
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from aqa_runner.chat_client import ChatClient, ClientFactory, Reply
from aqa_runner.tracing import ignore_ambient_tracing


@dataclass(frozen=True)
class Routed:
    """The outcome of a routed call: the last response and what was parsed from
    it, whether it is `ok`, a `refusal` or `invalid`, and a cost record for every
    response that arrived, the refused ones included."""

    message: AIMessage
    parsed: BaseModel | None
    outcome: Status
    calls: tuple[CostRecord, ...]


class ModelCallError(Exception):
    """A call failed after earlier responses were billed: a fallback that raised
    after a refusal. `records` holds what was billed, and `__cause__` the
    failure. A call whose first attempt gets no response raises the failure
    itself, with nothing billed."""

    def __init__(self, records: Sequence[CostRecord]) -> None:
        super().__init__(f"the call failed after {len(records)} billed response(s)")
        self.records = tuple(records)


@dataclass(frozen=True)
class Spend:
    """What a call does with each priced response (ADR-0007's #53 P3 amendment):
    `on_priced` gets its record before any further provider call, and what it
    raises leaves as it came; no fallback once the call's records cost `ceiling_usd`."""

    on_priced: Callable[[CostRecord], None]
    ceiling_usd: Decimal | None = None

    def reached(self, records: Sequence[CostRecord]) -> bool:
        """Whether `records` cost the ceiling or more."""
        left = self.after(records).ceiling_usd
        return left is not None and left <= 0

    def after(self, records: Sequence[CostRecord]) -> Self:
        """This spend, with what `records` cost taken off its ceiling."""
        if self.ceiling_usd is None:
            return self
        # Exact, as each cost is: 28 digits would round a sum under its ceiling.
        with localcontext(prec=MAX_PREC):
            spent = sum((record.cost_usd for record in records), Decimal(0))
            return replace(self, ceiling_usd=self.ceiling_usd - spent)


def _status(reply: Reply, schema: type[BaseModel] | None) -> Status:
    """How a response counts: a refusal; an answer the model didn't finish, or
    one that should have parsed against `schema` and didn't (`invalid`); or
    `ok`. A refusal is not complete either, so it is checked first."""
    if reply.refused:
        return "refusal"
    if not reply.complete or (schema is not None and reply.parsed is None):
        return "invalid"
    return "ok"


class ModelRouter:
    """The model for each role, called one role at a time. Build one per run:
    constructing it switches ambient LangChain tracing off for the process, and
    a LangChain or LangGraph run must start after it (SECURITY §10)."""

    def __init__(
        self, roles: Mapping[ModelRoleName, ResolvedRole], client_factory: ClientFactory
    ) -> None:
        # Every model call goes through a router, so an ambient LangChain tracing
        # variable is neutralised before the first one (SECURITY §10).
        ignore_ambient_tracing()
        self._roles = roles
        self._factory = client_factory
        self._clients: dict[tuple[str, str, Effort | None], ChatClient] = {}
        self._completed_calls: list[CostRecord] = []

    @property
    def completed_calls(self) -> tuple[CostRecord, ...]:
        """The cost record of every response this router's calls received, once
        each and in order. An interruption (Ctrl-C or a cancellation) skips
        `Routed` and `ModelCallError`, so this is what keeps its charges
        (ADR-0007's #161 amendment)."""
        return tuple(self._completed_calls)

    @classmethod
    def from_config(cls, config: ProjectConfig, client_factory: ClientFactory) -> Self:
        """A router for `config`'s roles. No client is built until a call needs
        one, so routing a run that makes no model call (strict replay) builds none."""
        return cls(resolve_roles(config, vendored()), client_factory)

    def _client(self, model: RoutedModel, effort: Effort | None) -> ChatClient:
        key = (model.provider, model.name, effort)
        if key not in self._clients:
            self._clients[key] = self._factory(model, effort)
        return self._clients[key]

    async def call(
        self,
        role: ModelRoleName,
        mode: Mode,
        messages: Sequence[BaseMessage],
        *,
        tools: Sequence[BaseTool] = (),
        schema: type[BaseModel] | None = None,
        spend: Spend | None = None,
    ) -> Routed:
        """Call `role`'s model. A refusal is recorded and, if the role has a
        fallback model, answered by calling it. A response that doesn't parse
        against `schema` is recorded as `invalid` and returned, not retried. A
        call whose first attempt gets no response raises that failure and
        records nothing; a fallback that fails after a billed refusal raises
        `ModelCallError`, which carries the refusal's record. `spend` gets each
        record as it is priced, and may stop a fallback (`Spend`)."""
        if mode not in get_args(Mode):
            # Strict replay makes zero model calls (AGENTS.md §6).
            raise ValueError(
                f"'{mode}' is not a call mode: strict replay makes no model calls"
            )
        resolved = self._roles[role]
        models = [resolved.model]
        if resolved.fallback is not None:
            models.append(resolved.fallback)
        records: list[CostRecord] = []
        for model in models:
            started = time.perf_counter()
            try:
                reply = await self._client(model, resolved.effort).call(
                    messages, tools, schema
                )
            except Exception as error:
                # A fallback's failure must not lose what the refusal cost.
                if records:
                    raise ModelCallError(records) from error
                raise
            status = _status(reply, schema)
            record = cost_record(
                role=role,
                mode=mode,
                model=model,
                usage=reply.usage,
                latency_ms=round((time.perf_counter() - started) * 1000),
                status=status,
            )
            records.append(record)
            self._completed_calls.append(record)
            if spend is not None:
                spend.on_priced(record)
            if status != "refusal" or (spend is not None and spend.reached(records)):
                break
        return Routed(reply.message, reply.parsed, status, tuple(records))
