"""Capture one named provider case, retaining costs before cassette acceptance."""

import argparse
import asyncio
import hashlib
import json
import os
import runpy
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from re import Pattern
from typing import Any, cast

from aqa_core.config import ModelRole
from aqa_core.model_costs import CostRecord, Usage, cost_record
from aqa_core.model_roles import RoutedModel, resolve_roles
from aqa_core.price_map import vendored
from aqa_core.project import load_project
from aqa_core.spec import Spec
from aqa_runner.anthropic_client import AnthropicClient, ProviderError
from aqa_runner.coverage_plan import make_plan
from aqa_runner.model_router import ModelRouter, Routed
from langchain_core.messages import HumanMessage

from packages.runner.tests.conftest import CASSETTES, REDIRECTS, _canonical, _vcr
from packages.runner.tests.test_anthropic_client import (
    CLICK_PROMPT,
    PLAIN_PROMPT,
    VERDICT_PROMPT,
    Verdict,
    click,
)
from packages.runner.tests.test_coverage_plan import checkout

CASES = ("coverage_plan", "tools", "structured_output", "plain_with_effort")
ROOT = Path(__file__).resolve().parents[3]
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


@dataclass
class Capture:
    """Keep credential-bearing responses out of even the private recording."""

    key: str
    filter_response: Callable[[dict[str, Any]], dict[str, Any]]
    attempt: Path
    withheld: bool = False
    responses: list[dict[str, Any]] = field(default_factory=list)
    previous: float = field(default_factory=time.perf_counter)

    def response(self, response: dict[str, Any]) -> dict[str, Any]:
        response = self.filter_response(response)
        now = time.perf_counter()
        usage = observed_usage(response["body"]["string"])
        text = json.dumps(response, default=str)
        unsafe = self.key in text or any(
            pattern.search(text) for _, pattern in SECRET_FORMATS
        )
        identifiers = response["headers"].get("request-id", [])
        observed = {
            "request_id": None if unsafe or not identifiers else identifiers[0],
            "status_code": response["status"]["code"],
            "usage": usage.model_dump() if usage else None,
            "response_interval_ms": round((now - self.previous) * 1000),
        }
        self.previous = now
        self.responses.append(observed)
        with (self.attempt / "responses.jsonl").open("a") as stream:
            stream.write(json.dumps(observed) + "\n")
        if unsafe:
            self.withheld = True
            return {
                "status": {"code": response["status"]["code"], "message": "withheld"},
                "headers": {},
                "body": {"string": '{"withheld": "credential detected"}'},
            }
        return response


def accounted(
    responses: list[dict[str, Any]], calls: tuple[CostRecord, ...], model: RoutedModel
) -> tuple[CostRecord, ...]:
    """Price SDK responses that did not reach the router, without counting twice."""
    extra = len(responses) - len(calls)
    records = []
    for index, response in enumerate(responses):
        if response["usage"] is None:
            continue
        if index >= extra:
            record = calls[index - extra]
            if (
                record.model_dump(
                    include={"input_tokens", "cached_input_tokens", "output_tokens"}
                )
                != response["usage"]
            ):
                raise ValueError("response usage disagrees with the router record")
            records.append(record)
        else:
            records.append(
                cost_record(
                    role="navigator",
                    mode="explore",
                    model=model,
                    usage=Usage.model_validate(response["usage"]),
                    latency_ms=response["response_interval_ms"],
                    status="invalid",
                )
            )
    return tuple(records)


async def ask(name: str, router: ModelRouter, spec: Spec) -> tuple[Routed, bool]:
    if name == "coverage_plan":
        planned = await make_plan(router, spec)
        return planned.routed, planned.plan is not None and not planned.misfits
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
    calls: tuple[CostRecord, ...],
    model: RoutedModel,
    *,
    outcome: str,
) -> tuple[CostRecord, ...]:
    """Persist returned records even when reconciling observed responses fails."""
    router_records = [item.model_dump(mode="json") for item in calls]
    try:
        calls = accounted(capture.responses, calls, model)
    except ValueError:
        outcome = "accounting_error"
    if outcome == "accepted" and (
        len(capture.responses) != 1 or not capture.responses[0]["request_id"]
    ):
        outcome = "rejected"
    unpriced = [
        index
        for index, response in enumerate(capture.responses)
        if response["usage"] is None
    ]
    if unpriced:
        outcome = "missing_usage"
    if capture.withheld:
        outcome = "credential"
    receipt = {
        "case": name,
        "finished_at": datetime.now(UTC).isoformat(),
        "outcome": outcome,
        "cost_records": [item.model_dump(mode="json") for item in calls],
        "router_records": router_records,
        "responses": capture.responses,
        "unpriced_responses": unpriced,
        "no_response": not capture.responses,
    }
    (attempt / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if outcome != "accepted":
        raise ValueError(f"recording {outcome}; see the attempt receipt")
    return calls


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
    }
    (attempt / "intent.json").write_text(json.dumps(intent, indent=2) + "\n")
    vcr = _vcr(attempt, recording_now=True)
    capture = Capture(key, vcr.before_record_response, attempt)
    vcr.before_record_response = capture.response
    calls: tuple[CostRecord, ...] = ()
    outcome = "error"
    with vcr.use_cassette(f"{name}.yaml") as cassette:
        try:
            routed, accepted = asyncio.run(ask(name, router, spec))
            calls = routed.calls
            outcome = "accepted" if accepted else "rejected"
        except ProviderError:
            outcome = "provider_error"
        except ValueError, UserWarning:
            # Malformed provider usage can fail SDK serialization under the
            # strict warning policy. Keep its observed response and fail capture.
            outcome = "error"
    hashes = [
        hashlib.sha256(_canonical(request.body).encode()).hexdigest()
        for request in cassette.requests
    ]
    (attempt / "request-hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
    calls = finish(name, attempt, capture, calls, role.model, outcome=outcome)
    candidate = attempt / f"{name}.yaml"
    promotion = attempt / "promotion.yaml"
    promotion.write_bytes(candidate.read_bytes())
    promotion.replace(library / candidate.name)
    return calls


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
