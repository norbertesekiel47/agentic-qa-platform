"""The router every model-using mode calls through (ADR-0007 and its
amendments): a model per role, a cost record for every response, and the role's
fallback when a model refuses."""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

from aqa_core.config import Effort, ModelRoleName, ProjectConfig
from aqa_core.model_costs import CostRecord, Mode, Status, Usage, cost_record
from aqa_core.model_roles import ResolvedRole, RoutedModel, resolve_roles
from aqa_core.price_map import vendored
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from aqa_runner.tracing import ignore_ambient_tracing


@dataclass(frozen=True)
class Reply:
    """What a provider's client brings back from one call. `parsed` is the
    answer to a schema, or None when the call gave no schema or the output did
    not parse and validate."""

    message: AIMessage
    usage: Usage
    refused: bool
    parsed: BaseModel | None


class ChatClient(Protocol):
    """One provider's way of calling one model. A call has tools or a schema,
    not both."""

    async def call(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[BaseTool],
        schema: type[BaseModel] | None,
    ) -> Reply: ...


ClientFactory = Callable[[RoutedModel, Effort | None], ChatClient]


@dataclass(frozen=True)
class Routed:
    """The outcome of a routed call: the last response and what was parsed from
    it, whether it is `ok`, a `refusal` or `invalid`, and a cost record for every
    response that arrived, the refused ones included."""

    message: AIMessage
    parsed: BaseModel | None
    outcome: Status
    calls: tuple[CostRecord, ...]


def _status(reply: Reply, schema: type[BaseModel] | None) -> Status:
    if reply.refused:
        return "refusal"
    return "invalid" if schema is not None and reply.parsed is None else "ok"


class ModelRouter:
    def __init__(
        self, roles: Mapping[ModelRoleName, ResolvedRole], client_factory: ClientFactory
    ) -> None:
        # Every model call goes through a router, so an ambient LangChain tracing
        # variable is neutralised before the first one (SECURITY §10).
        ignore_ambient_tracing()
        self._roles = roles
        self._factory = client_factory
        self._clients: dict[tuple[str, str, Effort | None], ChatClient] = {}

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
    ) -> Routed:
        """Call `role`'s model. A refusal is recorded and, if the role has a
        fallback model, answered by calling it. A response that doesn't parse
        against `schema` is recorded as `invalid` and returned, not retried. A
        call that gets no response raises and records nothing."""
        resolved = self._roles[role]
        records: list[CostRecord] = []
        for model in (resolved.model, resolved.fallback):
            if model is None:
                break
            started = time.perf_counter()
            reply = await self._client(model, resolved.effort).call(
                messages, tools, schema
            )
            status = _status(reply, schema)
            records.append(
                cost_record(
                    role=role,
                    mode=mode,
                    model=model,
                    usage=reply.usage,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    status=status,
                )
            )
            if status != "refusal":
                break
        return Routed(reply.message, reply.parsed, status, tuple(records))
