"""The dry run of the spike's AWS CLI scripts (ADR-0008 amendment, 2026-09-30):
the harness and the scenarios its tests share. Fake aws, docker, curl and
sleep commands (fake_cli.py) record every call and answer from a scenario, so
the tests reach no AWS account and create nothing. The tests import this
module, which pytest's `pythonpath` and mypy's `mypy_path` find."""

import json
import re
import subprocess
import sys
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPIKE = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_cli.py"

NAME = "aqa-spike-hosted-chromium"
ACCOUNT = "123456789012"
COMMIT = "0123456789ab"
IMAGE_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:microvm-image:{NAME}"
TASK = f"arn:aws:ecs:us-east-1:{ACCOUNT}:task/{NAME}/0f1e2d3c"
ENDPOINT = "https://mvm-fake-1.microvms.example"
FAKE_AUTH = "fake-microvm-auth-3f9a"
LOG_STREAM = "2026/10/01/[$LATEST]0f1e2d3c"
CANDIDATES = ["lambda", "fargate", "microvm"]
ROLES = [f"{NAME}-{role}" for role in ("lambda", "fargate", "microvm-build", "microvm")]
LOG_GROUPS = [f"/aqa-spike/hosted-chromium/{candidate}" for candidate in CANDIDATES]

Rule = dict[str, Any]


def rule(tool: str, *words: str, **answer: Any) -> Rule:
    return {"tool": tool, "words": list(words), **answer}


@dataclass
class DryRun:
    """The spike's scripts, copied to a directory of their own, with the fake
    commands first on PATH. Every call they make is logged in order."""

    root: Path
    calls: list[list[str]] = field(default_factory=list)

    def run(
        self, script: str, *args: str, scenario: list[Rule]
    ) -> subprocess.CompletedProcess[str]:
        """Run one script with `scenario`, and add its calls to `calls`."""
        log, rules = self.root / "calls.log", self.root / "scenario.json"
        log.write_text("")
        rules.write_text(json.dumps(scenario))
        env = {
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin:{Path(sys.executable).parent}",
            "HOME": str(self.root),
            "FAKE_LOG": str(log),
            "FAKE_SCENARIO": str(rules),
        }
        result = subprocess.run(
            ["bash", str(self.root / "spike/scripts" / script), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.calls += [json.loads(line) for line in log.read_text().splitlines()]
        return result


# A call that creates or changes something in the account. Everything else
# the scripts run only reads, or runs locally (docker tag, docker login).
CHANGES = re.compile(
    r"^(create|put|register|run|attach|tag|untag|update|delete|deregister|detach"
    r"|terminate|stop|start|suspend|resume|add|remove|set|publish|associate"
    r"|authorize|revoke|modify|enable|disable|cp|mv|rm|rb|mb|sync)\b"
)


def changes(call: list[str]) -> bool:
    tool, *args = call
    if tool == "docker":
        return args[:1] == ["push"]
    return tool == "aws" and len(args) > 1 and bool(CHANGES.match(args[1]))


def kind(call: list[str]) -> tuple[str, str]:
    tool, *args = call
    return (tool, args[0]) if tool == "docker" else (args[0], args[1])


def calls_of(calls: list[list[str]], *prefix: str) -> list[list[str]]:
    return [call for call in calls if call[1 : 1 + len(prefix)] == list(prefix)]


def files(call: list[str]) -> list[Any]:
    """The JSON the call sent in its file:// arguments, in order."""
    return [
        json.loads(word.removeprefix("<file: ").removesuffix(">"))
        for word in call
        if word.startswith("<file: ")
    ]


def option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


# The account as deploy.sh finds it: the budget alarm exists, the packages
# were built at COMMIT, and every create succeeds.
DEPLOYABLE = [
    rule("aws", "sts", "get-caller-identity", outputs=[ACCOUNT]),
    rule("docker", "image", "inspect", outputs=[COMMIT]),
    rule("aws", "ecr", "get-login-password", outputs=["fake-ecr-password"]),
    rule(
        "aws",
        "iam",
        "create-role",
        outputs=[f"arn:aws:iam::{ACCOUNT}:role/aqa-spike/{NAME}-role"],
    ),
    rule("aws", "ec2", "describe-vpcs", outputs=["vpc-0fake"]),
    rule("aws", "lambda-microvms", "create-microvm-image", outputs=[IMAGE_ARN]),
    rule(
        "aws",
        "lambda-microvms",
        "get-microvm-image",
        "state",
        outputs=["CREATING", "CREATED"],
    ),
]
NO_BUDGET = [
    rule(
        "aws",
        "budgets",
        "describe-budget",
        exits=[254],
        stderr="An error occurred (NotFoundException) when calling the "
        "DescribeBudget operation: fake: no such budget\n",
    ),
]

# What each candidate answers when invoke.sh runs a trial there.
TRIAL = {
    "run_id": "r1",
    "earlier_runs": [],
    "boot_id": "b1",
    "sandbox": {"on": True},
    "ready_seconds": 0.5,
    "ready_at": 1790000031.2,
    "peak_memory_bytes": 400_000_000,
}
LAMBDA_REPORT = (
    "REPORT RequestId: fake\tDuration: 812.30 ms\tBilled Duration: 2313 ms\t"
    "Memory Size: 2048 MB\tMax Memory Used: 420 MB\tInit Duration: 1500.10 ms"
)
TASK_RECORD = {
    "taskArn": TASK,
    "createdAt": "2026-10-01T10:00:00+00:00",
    "pullStartedAt": "2026-10-01T10:00:05+00:00",
    "pullStoppedAt": "2026-10-01T10:00:25+00:00",
    "startedAt": "2026-10-01T10:00:27+00:00",
    "stoppedAt": "2026-10-01T10:00:35+00:00",
    "containers": [{"exitCode": 0}],
}
MICROVM_RECORD = {
    "microvmId": "mvm-fake-1",
    "state": "TERMINATED",
    "startedAt": "2026-10-01T10:00:00+00:00",
    "terminatedAt": "2026-10-01T10:00:09+00:00",
}
REPORT_EVENT = {"timestamp": 1790000031500, "message": json.dumps(TRIAL)}
CHROMIUM_EVENT = {"timestamp": 1790000030000, "message": "a warning from Chromium"}

INVOKABLE = [
    *DEPLOYABLE,
    rule(
        "aws",
        "lambda",
        "invoke",
        write_last_arg=json.dumps({"trial": TRIAL, "log_stream": LOG_STREAM}),
        outputs=[
            json.dumps(
                {
                    "StatusCode": 200,
                    "LogResult": b64encode(LAMBDA_REPORT.encode()).decode(),
                }
            )
        ],
        delay=0.3,
    ),
    rule("aws", "ec2", "describe-subnets", outputs=["subnet-a\tsubnet-b"]),
    rule("aws", "ec2", "describe-security-groups", outputs=["sg-0fake"]),
    rule(
        "aws",
        "ecs",
        "run-task",
        outputs=[json.dumps({"tasks": [{"taskArn": TASK}], "failures": []})],
    ),
    rule("aws", "ecs", "describe-tasks", outputs=[json.dumps(TASK_RECORD)]),
    rule(
        "aws",
        "logs",
        "get-log-events",
        outputs=[json.dumps([CHROMIUM_EVENT, REPORT_EVENT])],
    ),
    rule(
        "aws",
        "lambda-microvms",
        "list-microvm-images",
        outputs=[json.dumps([IMAGE_ARN])],
    ),
    rule("aws", "iam", "get-role", outputs=[f"arn:aws:iam::{ACCOUNT}:role/x"]),
    rule(
        "aws",
        "lambda-microvms",
        "run-microvm",
        outputs=["mvm-fake-1\tmvm-fake-1.microvms.example"],
    ),
    rule("aws", "lambda-microvms", "create-microvm-auth-token", outputs=[FAKE_AUTH]),
    rule("curl", outputs=[json.dumps(TRIAL)]),
    rule("aws", "lambda-microvms", "get-microvm", outputs=[json.dumps(MICROVM_RECORD)]),
]


def replacing(scenario: list[Rule], *rules: Rule) -> list[Rule]:
    """`scenario` with `rules` first, so they answer before its own."""
    return [*rules, *scenario]
