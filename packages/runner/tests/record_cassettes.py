"""Capture one named provider case, retaining costs before cassette acceptance."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import runpy
import shutil
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from re import Pattern
from stat import S_ISDIR
from typing import Any, Literal, TypedDict, cast

import yaml
from aqa_core.config import Effort, ModelRole, ProjectConfig
from aqa_core.coverage_plan import uncovered
from aqa_core.model_costs import CostRecord, Usage, cost_record
from aqa_core.model_roles import RoutedModel, resolve_roles
from aqa_core.price_map import vendored
from aqa_core.project import load_compiled, load_config, load_spec
from aqa_core.spec import Spec
from aqa_runner.anthropic_client import (
    MAX_OUTPUT_TOKENS,
    AnthropicClient,
    ProviderError,
)
from aqa_runner.coverage_plan import make_plan
from aqa_runner.explore import Drivers, Ended, Explored, explore
from aqa_runner.model_router import ModelCallError, ModelRouter, Routed
from aqa_runner.run_record import COSTS
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.utils.json import parse_json_markdown
from playwright.async_api import async_playwright
from vcr.persisters.filesystem import FilesystemPersister

from packages.runner.tests.conftest import CASSETTES, REDIRECTS, _canonical, _vcr
from packages.runner.tests.executor_fixtures import serving_app
from packages.runner.tests.injection_fixtures import CASES as INJECTION_CASES
from packages.runner.tests.injection_fixtures import explored_case
from packages.runner.tests.test_anthropic_client import (
    CLICK_PROMPT,
    PLAIN_PROMPT,
    VERDICT_PROMPT,
    Verdict,
    click,
)
from packages.runner.tests.test_coverage_plan import checkout
from packages.runner.tests.test_explore_confirm import RESET
from packages.runner.tests.test_explore_graph import configured, shop_spec

ROOT = Path(__file__).resolve().parents[3]
PILOTS = ROOT / "bench/apps/conduit/qa"
# The injection pages' explores (#57), by cassette name.
INJECTED = {case.cassette: case for case in INJECTION_CASES}
CASES = (
    "coverage_plan",
    "tools",
    "structured_output",
    "plain_with_effort",
    "plan_login",
    "plan_read-article",
    "plan_post-comment",
    "plan_favorite-article",
    "plan_publish-article",
    "explore_fixture",
    *INJECTED,
)
# The whole explore of the fixture spec: the plan and every turn.
EXPLORE = "explore_fixture"
# The explore's own guard: the run stops calling once it has spent this. One
# attempt, so the run's bound covers its turns (#56).
FIXTURE_CONFIG = ProjectConfig.model_validate(
    {"budgets": {"model_usd": 0.15, "actions_per_attempt": 12, "attempts": 1}}
)
SECRET_FORMATS = cast(
    tuple[tuple[str, Pattern[str]], ...],
    runpy.run_path(str(ROOT / ".claude/hooks/policy_rules.py"))["SECRET_FORMATS"],
)
# The explore recording's spend bound (ADR-0024's #53 P21a amendment): a
# run's limit, and the round's, the $1.00 pre-approval less the program's
# USD 0.402050 already spent, #55's round included (the #53 P21b amendment).
RUN_USD = Decimal("0.20")
ROUND_USD = Decimal("0.597950")
# The plan cases' round (#55): the six plan cassettes, each request
# reserving its worst case as the explore case's do.
PLANS_ROUND_USD = Decimal("0.17")
# Input tokens a request may use beyond its body's bytes: the tool and
# output-format preambles and the framing, which the body doesn't spell.
HEADROOM_TOKENS = 1024
# What makes Playwright print or write what a recording sends or types: its
# protocol trace (printed by its Python side), its driver's debug log and
# debug file, its inspector, and its driver's full error output.
LOUD = ("DEBUGP", "DEBUG", "DEBUG_FILE", "PWDEBUG", "PWDEBUGIMPL")
# What a withheld explore response can reach under the attempt once the SDK
# has it, past the attempt's cassette, whose later requests carry it: the
# run's record and compiled script under its spec root.
SCRUBBED = ("qa/.aqa", "qa/.compiled")
# The injection round's limit (#57), a round of its own; each run's is its
# case config's model_usd.
INJECTION_ROUND_USD = Decimal("0.80")


class SpendRefusedError(Exception):
    """A request the recording's spend bound kept from leaving the process."""

    def __init__(self) -> None:
        super().__init__("the recording's spend bound refused a request")


@dataclass(frozen=True)
class Reservation:
    """What an admitted request holds until its response settles it: its
    body, the input tokens reserved for it, and the price of those with its
    own `max_tokens` as output."""

    body: bytes
    input_tokens: int
    usd: Decimal


@dataclass
class Allowance:
    """A recording's spend bound, the explore case's and the plan cases'
    (`round_limit`). Before a request leaves the process it reserves its
    worst case at the price map's list prices, and is refused if that would
    take the run past `run_usd` or the round past `round_usd`. A response
    settles at its usage's price; one without usage, or a request that got
    none, keeps its worst case."""

    model: RoutedModel
    run_usd: Decimal
    round_usd: Decimal
    spent: Decimal = Decimal(0)
    held: Reservation | None = None
    refused: bool = False
    overrun: bool = False

    def price(self, usage: Usage) -> Decimal:
        return cost_record(
            role="navigator",
            mode="explore",
            model=self.model,
            usage=usage,
            latency_ms=0,
            status="ok",
        ).cost_usd

    def reserve(self, body: bytes) -> Reservation | None:
        """`body`'s worst case, or None when the bound can't price it: not
        JSON, another model's, or without a `max_tokens` the adapter allows."""
        try:
            request = json.loads(body)
        except ValueError:
            return None
        if not isinstance(request, dict) or request.get("model") != self.model.name:
            return None
        output = request.get("max_tokens")
        if type(output) is not int or not 0 < output <= MAX_OUTPUT_TOKENS:
            return None
        tokens = len(body) + HEADROOM_TOKENS
        usage = Usage(input_tokens=tokens, cached_input_tokens=0, output_tokens=output)
        return Reservation(body, tokens, self.price(usage))

    def admit(self, request: Any) -> None:
        body = request.body if isinstance(request.body, bytes) else b""
        # VCR filters the held request again just before keeping its response.
        # It is never sent again unchanged: retries are off, and explore ends
        # its run on a call that got no response.
        if self.held is not None and self.held.body == body:
            return
        # One request at a time: while one is held, unanswered or in flight,
        # another is refused, and the held one keeps its worst case.
        held = self.reserve(body) if self.held is None else None
        if (
            self.overrun
            or held is None
            or self.spent + held.usd > min(self.run_usd, self.round_usd)
        ):
            self.refused = True
            raise SpendRefusedError
        self.held = held

    def settle(self, usage: Usage | None) -> None:
        """Price the held request's response. One billed more input tokens
        than its request reserved shows the bound is unsound: no request is
        admitted after it."""
        if self.held is None:
            return
        if usage is None:
            self.spent += self.held.usd
        else:
            self.spent += self.price(usage)
            self.overrun = self.overrun or usage.input_tokens > self.held.input_tokens
        self.held = None

    def receipt(self) -> dict[str, Any]:
        unsettled = self.held.usd if self.held is not None else Decimal(0)
        return {
            "run_usd": str(self.run_usd),
            "round_left_usd": str(self.round_usd),
            "spent_usd": str(self.spent + unsettled),
            "refused": self.refused,
            "overrun": self.overrun,
        }


@contextmanager
def quiet() -> Iterator[None]:
    """Nothing a recording sends, types or receives reaches a log: every
    Python logger is off (the SDK's, which ANTHROPIC_LOG turns on, httpx's,
    VCR's, LangChain's), and Playwright's switches are cleared before its
    driver starts. Both are restored after."""
    kept = {name: os.environ.pop(name) for name in LOUD if name in os.environ}
    disabled = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(disabled)
        os.environ.update(kept)


def retryless(model: RoutedModel, effort: Effort | None) -> AnthropicClient:
    """The adapter with the SDK's automatic retries off, so each request the
    spend bound admits is sent once."""
    client = AnthropicClient(model, effort)
    client.chat = client.chat.model_copy(update={"max_retries": 0})
    return client


def round_left(attempt: Path, limit: Decimal) -> Decimal:
    """What the round has left: `limit` less what each earlier attempt
    beside `attempt` spent, as its receipt says."""
    spent = Decimal(0)
    for earlier in sorted(attempt.parent.iterdir()):
        if earlier != attempt and earlier.is_dir():
            spent += earlier_spend(earlier)
    return limit - spent


def earlier_spend(earlier: Path) -> Decimal:
    """What an earlier attempt spent. One whose receipt doesn't say, or says
    an amount that isn't finite and non-negative, stops the recording: what
    it spent is unknown. So does one billed past its reservation."""
    try:
        spend = json.loads((earlier / "receipt.json").read_text())["spend"]
        amount, overrun = Decimal(spend["spent_usd"]), spend["overrun"]
    except OSError, ValueError, LookupError, TypeError, ArithmeticError:
        amount, overrun = Decimal("NaN"), False
    if not amount.is_finite() or amount < 0:
        raise ValueError("an earlier attempt in this round has no priced receipt")
    if overrun is not False:
        raise ValueError("an earlier attempt in this round billed past its reservation")
    return amount


def observed_usage(body: bytes | str) -> Usage | None:
    try:
        response = json.loads(body)
    except json.JSONDecodeError:
        return None
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict) or not all(
        type(usage.get(key)) is int for key in ("input_tokens", "output_tokens")
    ):
        return None
    cached = usage.get("cache_read_input_tokens", 0)
    created = usage.get("cache_creation_input_tokens", 0)
    if not all(
        type(count) is int and count >= 0
        for count in (
            usage["input_tokens"],
            usage["output_tokens"],
            cached,
            created,
        )
    ):
        return None
    return Usage(
        input_tokens=usage["input_tokens"] + cached + created,
        cached_input_tokens=cached,
        output_tokens=usage["output_tokens"],
    )


def contains_credential(value: Any, key: str) -> bool:
    if isinstance(value, str):
        return bool(key and key in value) or any(
            pattern.search(value) for _, pattern in SECRET_FORMATS
        )
    if isinstance(value, dict):
        return any(
            contains_credential(item, key) for pair in value.items() for item in pair
        )
    if isinstance(value, list):
        return any(contains_credential(item, key) for item in value)
    return False


def inspect_response(
    response: dict[str, Any], key: str, *, structured: bool
) -> str | None:
    """Inspect wire, adapter text and the structured parser's decoded values."""
    if contains_credential(json.dumps(response, default=str), key):
        return "credential"
    try:
        decoded = json.loads(response["body"]["string"])
        if contains_credential(decoded, key):
            return "credential"
        if not isinstance(decoded, dict) or not isinstance(
            decoded.get("content"), list
        ):
            return "uninspectable"
        return inspect_text(decoded["content"], key, structured=structured)
    except ValueError, TypeError:
        return "uninspectable"


def inspect_text(content: list[Any], key: str, *, structured: bool) -> str | None:
    text = ChatGeneration(message=AIMessage(content=content)).text
    if contains_credential(text, key):
        return "credential"
    if structured:
        parsed = parse_json_markdown(text)
        if contains_credential(parsed, key):
            return "credential"
        if not isinstance(parsed, dict):
            return "uninspectable"
    return None


@dataclass
class Capture:
    """Keep credential-bearing responses out of even the private recording."""

    key: str
    filter_response: Callable[[dict[str, Any]], dict[str, Any]]
    attempt: Path
    structured: bool
    allowance: Allowance | None = None
    withheld: str | None = None
    responses: list[dict[str, Any]] = field(default_factory=list)
    previous: float = field(default_factory=time.perf_counter)
    # While VCR loads the interactions an attempt replays, which a recording
    # already priced and inspected; and the request hashes it replays.
    loading: bool = False
    replayed: frozenset[str] = frozenset()

    def response(self, response: dict[str, Any]) -> dict[str, Any]:
        response = self.filter_response(response)
        if self.loading:
            return response
        now = time.perf_counter()
        usage = observed_usage(response["body"]["string"])
        if self.allowance is not None:
            self.allowance.settle(usage)
        identifiers = response["headers"].get("request-id", [])
        safe_id = identifiers[0] if identifiers else None
        if contains_credential(safe_id, self.key):
            safe_id = None
        observed = {
            "request_id": safe_id,
            "status_code": response["status"]["code"],
            "usage": usage.model_dump() if usage else None,
            "response_interval_ms": round((now - self.previous) * 1000),
        }
        self.previous = now
        self.responses.append(observed)
        unsafe = inspect_response(response, self.key, structured=self.structured)
        if unsafe:
            self.withheld = (
                "credential" if "credential" in (unsafe, self.withheld) else unsafe
            )
        with (self.attempt / "responses.jsonl").open("a") as stream:
            stream.write(json.dumps(observed) + "\n")
        if unsafe:
            return {
                "status": {"code": response["status"]["code"], "message": "withheld"},
                "headers": {},
                "body": {
                    "string": json.dumps(
                        {
                            "withheld": "credential detected"
                            if unsafe == "credential"
                            else "response uninspectable"
                        }
                    )
                },
            }
        return response


def accounted(
    responses: list[dict[str, Any]], calls: tuple[CostRecord, ...], model: RoutedModel
) -> tuple[tuple[CostRecord, ...], bool]:
    """Price every usable observation; ambiguous router associations fail closed."""
    matched: dict[int, CostRecord] = {}
    failed = False
    for call in calls:
        usage = call.model_dump(
            include={"input_tokens", "cached_input_tokens", "output_tokens"}
        )
        candidates = [
            index
            for index, response in enumerate(responses)
            if response["status_code"] == 200 and response["usage"] == usage
        ]
        if len(candidates) != 1 or candidates[0] in matched:
            failed = True
        else:
            matched[candidates[0]] = call
    records = tuple(
        matched[index]
        if index in matched
        else cost_record(
            role="navigator",
            mode="explore",
            model=model,
            usage=Usage.model_validate(response["usage"]),
            latency_ms=response["response_interval_ms"],
            status="invalid",
        )
        for index, response in enumerate(responses)
        if response["usage"] is not None
    )
    return (records if responses else calls), failed


class Ending(TypedDict):
    """How an explore ended, in fields that can't spell a value."""

    exit_code: int
    outcome: Ended
    published: Literal["confirmed", "unconfirmed"] | None


@dataclass(frozen=True)
class Reused:
    """An interaction an injection case replays from the fixture explore's
    cassette: its request's hash, its response's request ID and usage."""

    request: str
    request_id: str | None
    usage: Usage | None


@dataclass
class AttemptState:
    calls: tuple[CostRecord, ...] = ()
    costs: tuple[CostRecord, ...] = ()
    outcome: str = "error"
    primary_failure: str | None = None
    first_interrupt: str | None = None
    finalization_failures: list[str] = field(default_factory=list)
    receipt_attempted: bool = False
    receipt_written: bool = False
    promoting: bool = False
    ending: Ending | None = None
    reused: list[Reused] = field(default_factory=list)
    # Why an injection case's run ended as its attack doesn't allow (#57).
    disallowed: str | None = None

    def remember_interrupt(self, error: BaseException | None) -> None:
        if self.first_interrupt is not None:
            return
        for kind in (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            if isinstance(error, kind):
                self.first_interrupt = kind.__name__
                break

    def call_failed(self, error: BaseException | None) -> None:
        if error is None:
            return
        if isinstance(error, ModelCallError):
            self.calls = error.records
        self.remember_interrupt(error)
        self.primary_failure = self.first_interrupt or "call_error"
        self.outcome = "provider_error" if isinstance(error, ProviderError) else "error"

    def diagnostic(self) -> dict[str, Any]:
        return {
            "primary_failure": self.primary_failure,
            "first_interrupt": self.first_interrupt,
            "finalization_failures": list(self.finalization_failures),
            "receipt_attempted": self.receipt_attempted,
            "receipt_written": self.receipt_written,
            "cost_records": [item.model_dump(mode="json") for item in self.costs],
        }

    def safe_error(self) -> BaseException:
        controls: dict[str, BaseException] = {
            "KeyboardInterrupt": KeyboardInterrupt(),
            "SystemExit": SystemExit(1),
            "CancelledError": asyncio.CancelledError(),
        }
        error = controls.get(
            self.first_interrupt or "",
            ValueError(
                f"recording rejected: {self.outcome}; inspect the attempt receipt"
            ),
        )
        vars(error)["capture_state"] = self.diagnostic()
        return error


@dataclass
class DeferredPersister:
    """Let VCR unpatch transports before serialization or filesystem I/O."""

    pending: tuple[Any, Any, Any] | None = None

    def load_cassette(self, path: str, serializer: Any) -> Any:
        return FilesystemPersister.load_cassette(path, serializer)

    def save_cassette(self, path: str, cassette_dict: Any, serializer: Any) -> None:
        self.pending = path, cassette_dict, serializer

    def flush(self) -> None:
        if self.pending is not None:
            FilesystemPersister.save_cassette(*self.pending)


def plan_case(name: str) -> bool:
    return name == "coverage_plan" or name.startswith("plan_")


def round_limit(name: str) -> Decimal | None:
    """The round limit of a case recorded within the spend bound, with the
    SDK's retries off: the explore case's and the plan cases'. The other
    cases keep the SDK's retries and no bound."""
    if name == EXPLORE:
        return ROUND_USD
    if name in INJECTED:
        return INJECTION_ROUND_USD
    return PLANS_ROUND_USD if plan_case(name) else None


def answers_expected(name: str, calls: tuple[CostRecord, ...]) -> int:
    """One answer per case. A plan case holds two when its plan couldn't be
    used and make_plan asked once more: two answers, both `ok` (ADR-0024's
    #161 amendment). A refusal and its fallback stay rejected. The explore
    case holds one per priced call: the plan's, its retry's, each turn's, and
    so does an injection case, less those it replayed."""
    if name == EXPLORE or name in INJECTED:
        return len(calls)
    retried = plan_case(name) and [call.status for call in calls] == ["ok", "ok"]
    return 2 if retried else 1


async def ask(name: str, router: ModelRouter, spec: Spec) -> tuple[Routed, bool]:
    if plan_case(name):
        planned = await make_plan(router, spec)
        if planned.plan is None or planned.problems:
            return planned.routed, False
        # Every pilot expectation has an M1 check in REVIEW.md.
        return planned.routed, name == "coverage_plan" or not uncovered(
            planned.plan, spec.frontmatter
        )
    prompt = {
        "tools": CLICK_PROMPT,
        "structured_output": VERDICT_PROMPT,
        "plain_with_effort": PLAIN_PROMPT,
    }[name]
    routed = await router.call(
        "navigator",
        "explore",
        [HumanMessage(content=prompt)],
        tools=[click] if name == "tools" else [],
        schema=Verdict if name == "structured_output" else None,
    )
    accepted = routed.outcome == "ok"
    if name == "tools":
        accepted = accepted and any(
            call["name"] == "click" and call["args"] == {"ref": "e12"}
            for call in routed.message.tool_calls
        )
    else:
        accepted = (
            accepted
            and routed.message.response_metadata.get("stop_reason") == "end_turn"
        )
    return routed, accepted


async def explored(
    router: ModelRouter, spec: Spec
) -> tuple[tuple[CostRecord, ...], bool, Ending]:
    """Explore `spec` against the executor's fixture app in real Chromium:
    the run's priced calls, as its own cost lines keep them; whether it
    exited 0 having published a confirmed script; and how it ended."""
    root = spec.path.parent
    config = load_config(root / "config.yaml")
    with serving_app() as app:
        async with async_playwright() as playwright:
            run = await explore(
                spec, config, root, app.origin, Drivers(router, playwright)
            )
    costs, ending = ended(run)
    script = root / ".compiled" / f"{spec.frontmatter.id}.json"
    confirmed = (
        run.exit_code == 0
        and ending["published"] == "confirmed"
        and script.is_file()
        and load_compiled(script, config).confirmed
    )
    return costs, confirmed, ending


def ended(run: Explored) -> tuple[tuple[CostRecord, ...], Ending]:
    """The run's priced calls, as its own cost lines keep them, and how it
    ended."""
    costs = None if run.record is None else run.record / COSTS
    lines = costs.read_text().splitlines() if costs and costs.is_file() else []
    said = run.document.get("published")
    ending = Ending(
        exit_code=run.exit_code,
        outcome=run.outcome,
        published="confirmed"
        if said == "confirmed"
        else "unconfirmed"
        if said == "unconfirmed"
        else None,
    )
    return tuple(map(CostRecord.model_validate_json, lines)), ending


async def explored_injection(
    name: str, router: ModelRouter, spec: Spec
) -> tuple[tuple[CostRecord, ...], bool, Ending, str | None]:
    """Explore an injection case's spec on its page (#57): its priced calls,
    whether it ended as its attack allows, how it ended, and, in fixed
    words, why its attack doesn't allow that, if it doesn't."""
    run, disallowed = await explored_case(INJECTED[name], spec.path.parent, router)
    costs, ending = ended(run)
    return costs, disallowed is None, ending, disallowed


async def answered(
    name: str, router: ModelRouter, spec: Spec
) -> tuple[tuple[CostRecord, ...], bool, Ending | None, str | None]:
    if name == EXPLORE:
        return (*await explored(router, spec), None)
    if name in INJECTED:
        return await explored_injection(name, router, spec)
    routed, accepted = await ask(name, router, spec)
    return routed.calls, accepted, None, None


def admitting(vcr: Any, capture: Capture, allowance: Allowance) -> None:
    """Before an explore request leaves the process, hold its worst case
    against the spend bound; inspect its response as it asked, the plan's as
    structured output, a turn's as notes and tool calls. VCR filters a
    request before sending it and again just before keeping its response
    (vcrpy 8.3.0, `_vcr_request` and `Cassette.append`), and explore makes
    its calls one at a time. An interaction loaded to replay, or a request
    that replays one, never leaves, so it holds nothing: explore asks for its
    plan once, and a retry's body differs."""
    kept = vcr.before_record_request
    capture.allowance = allowance

    def admitted(request: Any) -> Any:
        if capture.loading or hashed(request.body) in capture.replayed:
            return kept(request)
        allowance.admit(request)
        capture.structured = "output_config" in json.loads(request.body)
        return kept(request)

    vcr.before_record_request = admitted


def scrub(attempt: Path, state: AttemptState, case: str) -> list[str]:
    """Remove what a withheld explore response reached once the SDK had it,
    the case's cassette and SCRUBBED, since VCR withholds only the copy it
    keeps. Each name is tried whatever happened to the one before."""
    removed: list[str] = []
    failed = False
    for name in (f"{case}.yaml", *SCRUBBED):
        path = attempt / name
        try:
            # lstat, since is_dir() and exists() read a name they can't look
            # at as absent; a link is removed, never followed.
            if S_ISDIR(path.lstat().st_mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # The rest are still tried and the receipt still written; the
            # failure is its own category.
            failed = True
            continue
        removed.append(name)
    if failed:
        state.finalization_failures.append("scrub")
    return removed


def finish(
    name: str,
    attempt: Path,
    capture: Capture,
    state: AttemptState,
    model: RoutedModel,
) -> None:
    """Attempt a priced receipt even when other evidence could not be saved."""
    explores = name == EXPLORE or name in INJECTED
    scrubbed = scrub(attempt, state, name) if explores and capture.withheld else []
    fresh = unreused(state.calls, state.reused)
    state.costs, mismatch = accounted(capture.responses, fresh, model)
    unpriced = [
        index
        for index, response in enumerate(capture.responses)
        if response["usage"] is None
    ]
    if mismatch:
        state.finalization_failures.append("accounting_error")
        state.outcome = "accounting_error"
    if state.outcome == "accepted" and (
        len(capture.responses) != answers_expected(name, fresh)
        or not all(response["request_id"] for response in capture.responses)
    ):
        state.outcome = "rejected"
    # Each later outcome wins over the ones above it.
    if state.ending is not None and state.ending["outcome"] == "no_response":
        state.outcome = "provider_error"
    if capture.allowance is not None and capture.allowance.refused:
        state.outcome = "spend_refused"
    if capture.allowance is not None and capture.allowance.overrun:
        state.outcome = "overrun"
    if capture.withheld:
        state.outcome = capture.withheld
    if unpriced and capture.withheld != "credential":
        state.outcome = "missing_usage"
    if any(reason != "accounting_error" for reason in state.finalization_failures):
        state.outcome = "error"
    state.receipt_attempted = True
    receipt = {
        "case": name,
        "finished_at": datetime.now(UTC).isoformat(),
        "outcome": state.outcome,
        **state.diagnostic(),
        "receipt_written": True,
        "router_records": [item.model_dump(mode="json") for item in state.calls],
        "responses": capture.responses,
        "unpriced_responses": unpriced,
        "no_response": not capture.responses,
        "withheld": capture.withheld,
        "scrubbed": scrubbed,
        "explore": state.ending,
        "disallowed": state.disallowed,
        "spend": None if capture.allowance is None else capture.allowance.receipt(),
        "reused": [
            {
                "request_id": each.request_id,
                "usage": None if each.usage is None else each.usage.model_dump(),
            }
            for each in state.reused
        ],
    }
    try:
        (attempt / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        state.receipt_written = True
    finally:
        if not state.receipt_written:
            state.remember_interrupt(sys.exception())
            state.finalization_failures.append("receipt_write")


def hashed(body: bytes | str | None) -> str:
    """A request body's prompt hash, as replay matches it."""
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def unreused(
    calls: tuple[CostRecord, ...], reused: list[Reused]
) -> tuple[CostRecord, ...]:
    """`calls` less one priced call for each response `reused` replayed:
    the fixture explore's recording paid for those and keeps their receipt."""
    left = [each.usage for each in reused]
    fresh: list[CostRecord] = []
    for call in calls:
        usage = Usage.model_validate(
            call.model_dump(
                include={"input_tokens", "cached_input_tokens", "output_tokens"}
            )
        )
        if usage in left:
            left.remove(usage)
        else:
            fresh.append(call)
    return tuple(fresh)


def preloaded(name: str, attempt: Path, library: Path) -> list[Reused]:
    """Write the plan interactions of the library's fixture explore cassette
    as the attempt's cassette, for an injection case to replay: its plan
    request is the fixture explore's, its account left out (#57). None
    when the library holds no plan interaction."""
    source = library / f"{EXPLORE}.yaml"
    if not source.is_file():
        return []
    cassette = yaml.safe_load(source.read_text())
    plans = [
        each
        for each in cassette["interactions"]
        if "output_config" in json.loads(each["request"]["body"])
    ]
    if not plans:
        return []
    (attempt / f"{name}.yaml").write_text(
        yaml.safe_dump(cassette | {"interactions": plans})
    )
    return [
        Reused(
            hashed(each["request"]["body"]),
            next(iter(each["response"]["headers"].get("request-id", [])), None),
            observed_usage(each["response"]["body"]["string"]),
        )
        for each in plans
    ]


def persist_capture(
    attempt: Path, persister: DeferredPersister, state: AttemptState
) -> None:
    cassette_written = hashes_written = False
    try:
        try:
            persister.flush()
            cassette_written = True
        finally:
            if not cassette_written:
                state.remember_interrupt(sys.exception())
                state.finalization_failures.append("cassette_write")
    finally:
        try:
            requests = persister.pending[1]["requests"] if persister.pending else []
            hashes = [
                hashlib.sha256(_canonical(request.body).encode()).hexdigest()
                for request in requests
            ]
            (attempt / "request-hashes.json").write_text(
                json.dumps(hashes, indent=2) + "\n"
            )
            hashes_written = True
        finally:
            if not hashes_written:
                state.remember_interrupt(sys.exception())
                state.finalization_failures.append("request_hashes")


def bounded(
    name: str,
    attempt: Path,
    library: Path,
    *,
    vcr: Any,
    capture: Capture,
    config: ProjectConfig,
) -> tuple[list[Reused], dict[str, Any]]:
    """Hold `name`'s requests to its spend bound, when it has one; and give
    the interactions it replays, which the capture neither prices nor
    inspects (an injection case's plan, from the fixture explore, #57), with
    how its cassette records past them and drops any not replayed."""
    reusable = preloaded(name, attempt, library) if name in INJECTED else []
    capture.replayed = frozenset(each.request for each in reusable)
    if (limit := round_limit(name)) is not None:
        model = resolve_roles(config, vendored())["navigator"].model
        # An injection run's limit is its own config's model_usd (#57).
        cap = Decimal(str(config.budgets.model_usd)) if name in INJECTED else RUN_USD
        admitting(vcr, capture, Allowance(model, cap, round_left(attempt, limit)))
    replaying = {"record_mode": "new_episodes", "drop_unused_requests": True}
    return reusable, replaying if reusable else {}


def capture_attempt(
    name: str,
    attempt: Path,
    library: Path,
    router: ModelRouter,
    spec: Spec,
    *,
    key: str,
) -> tuple[CostRecord, ...]:
    vcr = _vcr(attempt, recording_now=True)
    capture = Capture(
        key,
        vcr.before_record_response,
        attempt,
        plan_case(name) or name == "structured_output",
    )
    vcr.before_record_response = capture.response
    persister = DeferredPersister()
    vcr.register_persister(persister)
    state = AttemptState()
    config = load_config(spec.path.parent / "config.yaml")
    model = resolve_roles(config, vendored())["navigator"].model
    reusable, replaying = bounded(
        name, attempt, library, vcr=vcr, capture=capture, config=config
    )
    call_completed = capture_completed = completed = False
    try:
        with warnings.catch_warnings(), quiet():
            warnings.simplefilter("error")
            try:
                try:
                    capture.loading = bool(reusable)
                    with vcr.use_cassette(f"{name}.yaml", **replaying) as cassette:
                        capture.loading = False
                        try:
                            (state.calls, accepted, state.ending, state.disallowed) = (
                                asyncio.run(answered(name, router, spec))
                            )
                            state.outcome = (
                                "accepted"
                                if accepted
                                else "disallowed"
                                if state.disallowed
                                else "rejected"
                            )
                            call_completed = True
                        finally:
                            if not call_completed:
                                state.call_failed(sys.exception())
                            # Loaded first, the replayed interactions lead.
                            state.reused = [
                                each
                                for index, each in enumerate(reusable)
                                if cassette.play_counts[index]
                            ]
                    capture_completed = True
                finally:
                    if not capture_completed:
                        state.remember_interrupt(sys.exception())
                    persist_capture(attempt, persister, state)
            finally:
                finish(name, attempt, capture, state, model)
            if state.outcome == "accepted":
                state.promoting = True
                promotion = attempt / "promotion.yaml"
                promotion.write_bytes((attempt / f"{name}.yaml").read_bytes())
                promotion.replace(library / f"{name}.yaml")
            completed = True
    finally:
        error = sys.exception() if not completed else None
        if error is not None or state.outcome != "accepted":
            state.remember_interrupt(error)
            if error is not None and state.promoting:
                state.finalization_failures.append("promotion_failed")
            raise state.safe_error() from None
    return state.costs


def pilot(spec_id: str, root: Path) -> Spec:
    """A copy of a committed pilot spec and its project config, so the
    recording reads exactly what it hashes and writes nothing under bench."""
    root.mkdir()
    for source in (PILOTS / "config.yaml", PILOTS / f"{spec_id}.spec.md"):
        shutil.copyfile(source, root / source.name)
    # The reviewed subject rows name the other pilot specs, which a project
    # load requires; the recording reads only this spec and its config.
    return load_spec(root / f"{spec_id}.spec.md", load_config(root / "config.yaml"))


def fixture(root: Path) -> Spec:
    """The shop spec with its reset hook under the spec root `root`, which
    explores the executor's fixture app as `test_explore_confirm` does."""
    return shop_spec(
        configured(root, FIXTURE_CONFIG), FIXTURE_CONFIG, "/page/shop", RESET
    )


def record(
    name: str, attempt: Path, *, library: Path = CASSETTES
) -> tuple[CostRecord, ...]:
    """Keep an attempt's evidence before replacing its accepted cassette."""
    if name not in CASES:
        raise ValueError("unknown recording case")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("recording needs ANTHROPIC_API_KEY")
    attempt.mkdir(parents=True)
    if name.startswith("plan_"):
        spec = pilot(name.removeprefix("plan_"), attempt / "qa")
    elif name == EXPLORE:
        spec = fixture(attempt / "qa")
    elif name in INJECTED:
        spec = INJECTED[name].spec(attempt / "qa", FIXTURE_CONFIG)
    else:
        spec = checkout(attempt / "qa")
    config = load_config(spec.path.parent / "config.yaml")
    if name == "plain_with_effort":
        config = config.model_copy(
            update={"roles": {"navigator": ModelRole(effort="high")}}
        )
    router = ModelRouter.from_config(
        config, AnthropicClient if round_limit(name) is None else retryless
    )
    role = resolve_roles(config, vendored())["navigator"]
    intent = {
        "case": name,
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "started_at": datetime.now(UTC).isoformat(),
        "role": "navigator",
        "provider": role.model.provider,
        "model": role.model.name,
        "effort": role.effort,
        "inputs": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(spec.path.parent.iterdir())
        },
    }
    (attempt / "intent.json").write_text(json.dumps(intent, indent=2) + "\n")
    return capture_attempt(name, attempt, library, router, spec, key=key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    attempt = args.attempt.resolve()
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", str(attempt)], cwd=ROOT, check=False
    )
    if not attempt.is_relative_to(ROOT) or ignored.returncode != 0:
        parser.error("attempt must be an ignored directory inside this worktree")
    for variable in REDIRECTS:
        os.environ.pop(variable, None)
    try:
        record(args.case, attempt)
    except ValueError, OSError:
        parser.exit(
            1,
            "Recording stopped; inspect the private attempt receipt before another call.\n",
        )


if __name__ == "__main__":
    main()
