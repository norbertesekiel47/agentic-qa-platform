"""Capture one named provider case, retaining costs before cassette acceptance."""

import argparse
import asyncio
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from re import Pattern
from typing import Any, cast

from aqa_core.config import ModelRole
from aqa_core.coverage_plan import uncovered
from aqa_core.model_costs import CostRecord, Usage, cost_record
from aqa_core.model_roles import RoutedModel, resolve_roles
from aqa_core.price_map import vendored
from aqa_core.project import load_project
from aqa_core.spec import Spec
from aqa_runner.anthropic_client import AnthropicClient, ProviderError
from aqa_runner.coverage_plan import make_plan
from aqa_runner.model_router import ModelCallError, ModelRouter, Routed
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.utils.json import parse_json_markdown
from vcr.persisters.filesystem import FilesystemPersister

from packages.runner.tests.conftest import CASSETTES, REDIRECTS, _canonical, _vcr
from packages.runner.tests.test_anthropic_client import (
    CLICK_PROMPT,
    PLAIN_PROMPT,
    VERDICT_PROMPT,
    Verdict,
    click,
)
from packages.runner.tests.test_coverage_plan import checkout

ROOT = Path(__file__).resolve().parents[3]
PILOTS = ROOT / "bench/apps/conduit/qa"
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
)
SECRET_FORMATS = cast(
    tuple[tuple[str, Pattern[str]], ...],
    runpy.run_path(str(ROOT / ".claude/hooks/policy_rules.py"))["SECRET_FORMATS"],
)


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
    withheld: str | None = None
    responses: list[dict[str, Any]] = field(default_factory=list)
    previous: float = field(default_factory=time.perf_counter)

    def response(self, response: dict[str, Any]) -> dict[str, Any]:
        response = self.filter_response(response)
        now = time.perf_counter()
        usage = observed_usage(response["body"]["string"])
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


async def ask(name: str, router: ModelRouter, spec: Spec) -> tuple[Routed, bool]:
    if name == "coverage_plan" or name.startswith("plan_"):
        planned = await make_plan(router, spec)
        if planned.plan is None or planned.misfits:
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


def finish(
    name: str,
    attempt: Path,
    capture: Capture,
    state: AttemptState,
    model: RoutedModel,
) -> None:
    """Attempt a priced receipt even when other evidence could not be saved."""
    state.costs, mismatch = accounted(capture.responses, state.calls, model)
    unpriced = [
        index
        for index, response in enumerate(capture.responses)
        if response["usage"] is None
    ]
    if mismatch:
        state.finalization_failures.append("accounting_error")
        state.outcome = "accounting_error"
    if state.outcome == "accepted" and (
        len(capture.responses) != 1 or not capture.responses[0]["request_id"]
    ):
        state.outcome = "rejected"
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
    }
    try:
        (attempt / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        state.receipt_written = True
    finally:
        if not state.receipt_written:
            state.remember_interrupt(sys.exception())
            state.finalization_failures.append("receipt_write")


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
        name in {"coverage_plan", "structured_output"} or name.startswith("plan_"),
    )
    vcr.before_record_response = capture.response
    persister = DeferredPersister()
    vcr.register_persister(persister)
    state = AttemptState()
    config = load_project(spec.path.parent).config
    model = resolve_roles(config, vendored())["navigator"].model
    call_completed = capture_completed = completed = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            try:
                try:
                    with vcr.use_cassette(f"{name}.yaml"):
                        try:
                            routed, accepted = asyncio.run(ask(name, router, spec))
                            state.calls = routed.calls
                            state.outcome = "accepted" if accepted else "rejected"
                            call_completed = True
                        finally:
                            if not call_completed:
                                state.call_failed(sys.exception())
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
    return load_project(root).specs[spec_id]


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
    else:
        spec = checkout(attempt / "qa")
    config = load_project(spec.path.parent).config
    if name == "plain_with_effort":
        config = config.model_copy(
            update={"roles": {"navigator": ModelRole(effort="high")}}
        )
    router = ModelRouter.from_config(config, AnthropicClient)
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
