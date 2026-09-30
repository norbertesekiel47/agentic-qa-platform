"""The spike's AWS CLI scripts, in a dry run (ADR-0008 amendment, 2026-09-30):
fake aws, docker and curl commands (fake_cli.py) record every call and answer
from a scenario, so these tests reach no AWS account and create nothing.

They prove what #37 asks of the scripts: the budget alarm comes before any
other resource, and the teardown deletes everything the scripts create."""

import json
import re
import shutil
import subprocess
import sys
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

SPIKE = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_cli.py"

NAME = "aqa-spike-hosted-chromium"
ACCOUNT = "123456789012"
COMMIT = "0123456789ab"
IMAGE_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:microvm-image:{NAME}"
CANDIDATES = ["lambda", "fargate", "microvm"]

Rule = dict[str, Any]


def rule(tool: str, *words: str, **answer: Any) -> Rule:
    return {"tool": tool, "words": list(words), **answer}


# The account as the scripts find it: the budget alarm exists, the packages
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


@pytest.fixture
def dry_run(tmp_path: Path) -> DryRun:
    shutil.copytree(SPIKE / "scripts", tmp_path / "spike/scripts")
    (tmp_path / "spike/build").mkdir()
    (tmp_path / f"spike/build/microvm-{COMMIT}.zip").write_bytes(b"fake zip")
    (tmp_path / "bin").mkdir()
    # sleep too, so a poll or an IAM settle takes no time.
    for tool in ("aws", "docker", "curl", "sleep"):
        shim = tmp_path / "bin" / tool
        # -S: the fake needs only the standard library, and starts faster.
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "{FAKE}" {tool} "$@"\n')
        shim.chmod(0o755)
    return DryRun(tmp_path)


# A call that creates or changes something in the account. Everything else
# the scripts run only reads, or runs locally (docker tag, docker login).
CHANGES = re.compile(
    r"^(create|put|register|run|attach|tag|update|delete|deregister|detach"
    r"|terminate|stop|cp|rb)\b"
)


def changes(call: list[str]) -> bool:
    tool, *args = call
    if tool == "docker":
        return args[:1] == ["push"]
    return tool == "aws" and len(args) > 1 and bool(CHANGES.match(args[1]))


def test_budget_sh_creates_only_the_budget_alarm(dry_run: DryRun) -> None:
    result = dry_run.run("budget.sh", "spike@example.com", scenario=DEPLOYABLE)

    assert result.returncode == 0, result.stderr
    changed = [call[:3] for call in dry_run.calls if changes(call)]
    assert changed == [["aws", "budgets", "create-budget"]]


# With the alarm there, each deploy looks for it before it changes anything.
@pytest.mark.parametrize("candidate", CANDIDATES)
def test_a_deploy_checks_the_budget_alarm_before_its_first_change(
    dry_run: DryRun, candidate: str
) -> None:
    result = dry_run.run("deploy.sh", candidate, scenario=DEPLOYABLE)

    assert result.returncode == 0, result.stderr
    calls = dry_run.calls
    first_change = next(i for i, call in enumerate(calls) if changes(call))
    assert ["aws", "budgets", "describe-budget"] in [
        c[:3] for c in calls[:first_change]
    ]


@pytest.mark.parametrize("script", ["deploy.sh", "invoke.sh"])
@pytest.mark.parametrize("candidate", CANDIDATES)
def test_the_scripts_refuse_to_run_without_the_budget_alarm(
    dry_run: DryRun, script: str, candidate: str
) -> None:
    result = dry_run.run(script, candidate, "cold", scenario=NO_BUDGET + DEPLOYABLE)

    assert result.returncode == 1
    assert "run budget.sh first" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


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
FAKE_AUTH = "fake-microvm-auth-3f9a"
LOG_STREAM = "2026/10/01/[$LATEST]0f1e2d3c"
TASK = f"arn:aws:ecs:us-east-1:{ACCOUNT}:task/{NAME}/0f1e2d3c"
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
    ),
    rule("aws", "ec2", "describe-subnets", outputs=["subnet-a\tsubnet-b"]),
    rule("aws", "ec2", "describe-security-groups", outputs=["sg-0fake"]),
    rule("aws", "ecs", "run-task", outputs=[TASK]),
    rule("aws", "ecs", "describe-tasks", outputs=[json.dumps(TASK_RECORD)]),
    rule(
        "aws",
        "logs",
        "get-log-events",
        outputs=[
            json.dumps(
                [
                    {"timestamp": 1790000030000, "message": "a warning from Chromium"},
                    {"timestamp": 1790000031500, "message": json.dumps(TRIAL)},
                ]
            )
        ],
    ),
    rule("aws", "lambda-microvms", "list-microvm-images", outputs=[IMAGE_ARN]),
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

# The platform's own record of the run, as invoke.sh reports it.
PLATFORM = {
    "lambda": {"report": LAMBDA_REPORT, "log_stream": LOG_STREAM},
    "fargate": {"task": TASK_RECORD, "reported_at": 1790000031.5},
    "microvm": {"microvm": MICROVM_RECORD},
}


@pytest.mark.parametrize("candidate", CANDIDATES)
def test_invoke_reports_the_trial_with_the_platforms_record(
    dry_run: DryRun, candidate: str
) -> None:
    result = dry_run.run("invoke.sh", candidate, "cold", scenario=INVOKABLE)

    assert result.returncode == 0, result.stderr
    [line] = result.stdout.splitlines()
    measured = json.loads(line)
    assert measured["candidate"] == candidate
    assert measured["start"] == "cold"
    assert measured["trial"] == TRIAL
    assert measured["platform"] == PLATFORM[candidate]
    assert measured["requested_at"] <= measured["answered_at"]


def test_a_cold_lambda_invoke_starts_a_new_environment(dry_run: DryRun) -> None:
    dry_run.run("invoke.sh", "lambda", "cold", scenario=INVOKABLE)
    cold = [call[1:3] for call in dry_run.calls if call[1:2] == ["lambda"]]
    dry_run.calls.clear()
    dry_run.run("invoke.sh", "lambda", "warm", scenario=INVOKABLE)
    warm = [call[1:3] for call in dry_run.calls if call[1:2] == ["lambda"]]

    # A configuration change makes Lambda start the next invocation afresh.
    assert cold == [
        ["lambda", "update-function-configuration"],
        ["lambda", "wait"],
        ["lambda", "invoke"],
    ]
    assert warm == [["lambda", "invoke"]]


def test_a_fargate_task_has_no_warm_start(dry_run: DryRun) -> None:
    result = dry_run.run("invoke.sh", "fargate", "warm", scenario=INVOKABLE)

    assert result.returncode == 2
    assert "every Fargate task starts cold" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


def test_a_microvm_is_terminated_after_its_trial(dry_run: DryRun) -> None:
    dry_run.run("invoke.sh", "microvm", "cold", scenario=INVOKABLE)
    microvm = [call[2] for call in dry_run.calls if call[1:2] == ["lambda-microvms"]]

    assert microvm.index("run-microvm") < microvm.index("terminate-microvm")


def test_a_microvm_that_never_answers_is_still_terminated(dry_run: DryRun) -> None:
    silent = [rule("curl", exits=[22], stderr="curl: (22) 502\n"), *INVOKABLE]

    result = dry_run.run("invoke.sh", "microvm", "cold", scenario=silent)

    assert result.returncode == 1
    assert "never answered" in result.stderr
    terminated = [
        c for c in dry_run.calls if c[1:3] == ["lambda-microvms", "terminate-microvm"]
    ]
    assert [call[-1] for call in terminated] == ["mvm-fake-1"]


def test_no_secret_reaches_a_command_line(dry_run: DryRun) -> None:
    for candidate in CANDIDATES:
        dry_run.run("deploy.sh", candidate, scenario=INVOKABLE)
        dry_run.run("invoke.sh", candidate, "cold", scenario=INVOKABLE)

    arguments = [
        word
        for call in dry_run.calls
        for word in call
        if not word.startswith("<stdin:")
    ]
    assert [word for word in arguments if FAKE_AUTH in word] == []
    assert [word for word in arguments if "fake-ecr-password" in word] == []
    # The password went to docker login on stdin.
    assert any("<stdin: fake-ecr-password>" in call for call in dry_run.calls)


def option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


# Each kind of resource the scripts create, and the calls that must delete it:
# the words each deleting call must hold. A create that isn't listed here or
# in PART_OF fails the teardown test until someone decides what deletes it.
DELETED_BY: dict[tuple[str, str], Callable[[list[str]], list[list[str]]]] = {
    ("budgets", "create-budget"): lambda _: [["budgets", "delete-budget", NAME]],
    ("ecr", "create-repository"): lambda a: [
        ["ecr", "delete-repository", option(a, "--repository-name"), "--force"]
    ],
    ("iam", "create-role"): lambda a: [
        ["iam", "delete-role", option(a, "--role-name")]
    ],
    ("iam", "put-role-policy"): lambda a: [
        [
            "iam",
            "delete-role-policy",
            option(a, "--role-name"),
            option(a, "--policy-name"),
        ]
    ],
    ("logs", "create-log-group"): lambda a: [
        ["logs", "delete-log-group", option(a, "--log-group-name")]
    ],
    ("lambda", "create-function"): lambda a: [
        ["lambda", "delete-function", option(a, "--function-name")]
    ],
    ("ecs", "create-cluster"): lambda a: [
        ["ecs", "delete-cluster", option(a, "--cluster-name")]
    ],
    ("ecs", "register-task-definition"): lambda _: [
        ["ecs", "deregister-task-definition", f"{NAME}:1"],
        ["ecs", "delete-task-definitions", f"{NAME}:1"],
    ],
    ("ec2", "create-security-group"): lambda _: [
        ["ec2", "delete-security-group", "sg-0fake"]
    ],
    ("s3api", "create-bucket"): lambda a: [
        ["s3", "rb", f"s3://{option(a, '--bucket')}", "--force"]
    ],
    ("lambda-microvms", "create-microvm-image"): lambda _: [
        ["lambda-microvms", "delete-microvm-image", IMAGE_ARN]
    ],
    ("lambda-microvms", "run-microvm"): lambda _: [
        ["lambda-microvms", "terminate-microvm", "mvm-fake-1"]
    ],
    # The trial's task ends when the trial does, and invoke.sh waits for it.
    ("ecs", "run-task"): lambda _: [["ecs", "wait", "tasks-stopped", TASK]],
}
# Calls that change a resource above, or make something that ends by itself.
PART_OF = {
    ("logs", "put-retention-policy"): "the log group",
    ("s3api", "put-public-access-block"): "the bucket",
    ("s3api", "put-bucket-tagging"): "the bucket",
    ("s3", "cp"): "the bucket, which rb --force empties",
    ("lambda", "update-function-configuration"): "the function",
    ("lambda-microvms", "create-microvm-auth-token"): "a token that expires by itself",
    ("docker", "push"): "the ECR repository, which delete-repository --force empties",
}
DELETES = re.compile(r"^(delete|deregister|detach|terminate|stop|rb)\b")


def kind(call: list[str]) -> tuple[str, str]:
    tool, *args = call
    return (tool, args[0]) if tool == "docker" else (args[0], args[1])


# The account after every candidate was deployed and invoked, as teardown.sh
# lists it: each list shows what exists, then nothing once it is deleted.
ROLES = [f"{NAME}-{role}" for role in ("lambda", "fargate", "microvm-build", "microvm")]
DEPLOYED = [
    rule("aws", "sts", "get-caller-identity", outputs=[ACCOUNT]),
    rule("aws", "lambda-microvms", "list-microvms", outputs=["mvm-fake-1", ""]),
    rule("aws", "lambda-microvms", "list-microvm-images", outputs=[IMAGE_ARN, ""]),
    rule("aws", "lambda", "list-functions", outputs=[NAME, ""]),
    rule("aws", "ecs", "describe-clusters", outputs=[NAME, ""]),
    rule("aws", "ecs", "list-tasks", outputs=[""]),
    rule("aws", "ecs", "list-task-definitions", "ACTIVE", outputs=[f"{NAME}:1", ""]),
    rule("aws", "ecs", "list-task-definitions", "INACTIVE", outputs=[f"{NAME}:1", ""]),
    rule("aws", "ec2", "describe-security-groups", outputs=["sg-0fake", ""]),
    rule(
        "aws",
        "ecr",
        "describe-repositories",
        outputs=[f"{NAME}-lambda\t{NAME}-fargate", ""],
    ),
    rule("aws", "s3api", "list-buckets", outputs=[f"{NAME}-{ACCOUNT}", ""]),
    rule(
        "aws",
        "logs",
        "describe-log-groups",
        outputs=[
            "\t".join(f"/aqa-spike/hosted-chromium/{c}" for c in CANDIDATES),
            "",
        ],
    ),
    rule("aws", "iam", "list-roles", outputs=["\t".join(ROLES), ""]),
    *[rule("aws", "iam", "list-role-policies", role, outputs=[role]) for role in ROLES],
    rule("aws", "budgets", "describe-budgets", outputs=[NAME, ""]),
]


def test_the_teardown_deletes_everything_the_scripts_create(dry_run: DryRun) -> None:
    assert (
        dry_run.run("budget.sh", "spike@example.com", scenario=INVOKABLE).returncode
        == 0
    )
    for candidate in CANDIDATES:
        assert dry_run.run("deploy.sh", candidate, scenario=INVOKABLE).returncode == 0
        assert (
            dry_run.run("invoke.sh", candidate, "cold", scenario=INVOKABLE).returncode
            == 0
        )
    created = [
        call
        for call in dry_run.calls
        if changes(call) and not DELETES.match(kind(call)[1])
    ]

    teardown = dry_run.run("teardown.sh", scenario=DEPLOYED)

    assert teardown.returncode == 0, teardown.stderr
    unclassified = {kind(c) for c in created} - DELETED_BY.keys() - PART_OF.keys()
    assert unclassified == set(), "add these to DELETED_BY or PART_OF"
    expected = [
        words
        for call in created
        if kind(call) in DELETED_BY
        for words in DELETED_BY[kind(call)](call[1:])
    ]
    missing = [
        words
        for words in expected
        if not any(all(word in call for word in words) for call in dry_run.calls)
    ]
    assert missing == [], "nothing deletes these"


def test_the_teardown_fails_while_anything_remains(dry_run: DryRun) -> None:
    # Every resource still shows up in its listing after its delete.
    stuck = [{**r, "outputs": [r["outputs"][0]]} for r in DEPLOYED]

    result = dry_run.run("teardown.sh", scenario=stuck)

    assert result.returncode == 1
    assert "still there" in result.stderr
    # The check names every kind of resource the scripts create.
    kinds = [
        "microvms",
        "microvm_images",
        "functions",
        "clusters",
        "task_definitions ACTIVE",
        "task_definitions INACTIVE",
        "security_groups",
        "repositories",
        "buckets",
        "log_groups",
        "roles",
        "budgets",
    ]
    assert [kind for kind in kinds if f"  {kind}: " not in result.stderr] == []


def test_collect_asks_cost_explorer_for_the_usage_by_type(dry_run: DryRun) -> None:
    usage = rule("aws", "ce", "get-cost-and-usage", outputs=['{"ResultsByTime": []}'])

    result = dry_run.run("collect.sh", "2026-10-01", "2026-10-03", scenario=[usage])

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ResultsByTime": []}
    [call] = dry_run.calls
    assert "Start=2026-10-01,End=2026-10-03" in call
    assert "Type=DIMENSION,Key=USAGE_TYPE" in call


@pytest.mark.parametrize("days", [[], ["2026-10-01"], ["2026-10-01", "tomorrow"]])
def test_collect_needs_two_days(dry_run: DryRun, days: list[str]) -> None:
    result = dry_run.run("collect.sh", *days, scenario=[])

    assert result.returncode == 2
    assert dry_run.calls == []


# Failures stop a script where they happen. set -e doesn't reach a command
# inside `$(…)` or `for … in $(…)`, so the scripts never run one there.


def test_a_teardown_stops_when_a_listing_fails(dry_run: DryRun) -> None:
    expired = rule(
        "aws", "lambda", "list-functions", exits=[255], stderr="ExpiredTokenException\n"
    )

    result = dry_run.run("teardown.sh", scenario=[expired, *DEPLOYED])

    assert result.returncode != 0
    assert "teardown complete" not in result.stdout
    assert ["aws", "budgets", "delete-budget"] not in [c[:3] for c in dry_run.calls]


@pytest.mark.parametrize(
    ("candidate", "failing"),
    [
        ("lambda", ["ecr", "create-repository"]),
        ("fargate", ["iam", "create-role"]),
        ("microvm", ["iam", "put-role-policy"]),
    ],
)
def test_a_deploy_stops_at_the_first_change_that_fails(
    dry_run: DryRun, candidate: str, failing: list[str]
) -> None:
    fails = rule("aws", *failing, exits=[254], stderr="fake: AccessDenied\n")

    result = dry_run.run("deploy.sh", candidate, scenario=[fails, *DEPLOYABLE])

    assert result.returncode != 0
    calls = [call[1:3] for call in dry_run.calls]
    assert [c for c in dry_run.calls[calls.index(failing) + 1 :] if changes(c)] == []


@pytest.mark.parametrize("candidate", CANDIDATES)
def test_a_deploy_refuses_a_candidate_already_deployed(
    dry_run: DryRun, candidate: str
) -> None:
    there = rule(
        "aws",
        "logs",
        "describe-log-groups",
        outputs=[f"/aqa-spike/hosted-chromium/{candidate}"],
    )

    result = dry_run.run("deploy.sh", candidate, scenario=[there, *DEPLOYABLE])

    assert result.returncode == 1
    assert "already deployed: run teardown.sh first" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


ENDPOINT = "https://mvm-fake-1.microvms.example"


def test_a_microvm_runs_one_trial_once_its_server_answers(dry_run: DryRun) -> None:
    # Lambda answers 502 until the MicroVM's /run hook has returned.
    health = rule("curl", f"{ENDPOINT}/health", exits=[22, 22, 0])

    result = dry_run.run("invoke.sh", "microvm", "cold", scenario=[health, *INVOKABLE])

    assert result.returncode == 0, result.stderr
    urls = [call[-1] for call in dry_run.calls if call[0] == "curl"]
    assert urls == [f"{ENDPOINT}/health"] * 3 + [f"{ENDPOINT}/trial"]


def test_a_microvm_trial_that_fails_is_not_run_again(dry_run: DryRun) -> None:
    timeout = rule("curl", f"{ENDPOINT}/trial", exits=[28], stderr="curl: (28)\n")

    result = dry_run.run("invoke.sh", "microvm", "cold", scenario=[timeout, *INVOKABLE])

    assert result.returncode != 0
    urls = [call[-1] for call in dry_run.calls if call[0] == "curl"]
    assert urls.count(f"{ENDPOINT}/trial") == 1
    terminated = [c for c in dry_run.calls if "terminate-microvm" in c]
    assert [call[-1] for call in terminated] == ["mvm-fake-1"]


def test_a_failed_token_request_stops_the_invoke(dry_run: DryRun) -> None:
    denied = rule(
        "aws",
        "lambda-microvms",
        "create-microvm-auth-token",
        exits=[254],
        stderr="fake\n",
    )

    result = dry_run.run("invoke.sh", "microvm", "cold", scenario=[denied, *INVOKABLE])

    assert result.returncode != 0
    assert [call for call in dry_run.calls if call[0] == "curl"] == []
    assert any("terminate-microvm" in call for call in dry_run.calls)


def test_a_fargate_task_is_stopped_when_its_wait_fails(dry_run: DryRun) -> None:
    # `ecs wait tasks-stopped` gives up after 100 checks, about ten minutes.
    gave_up = rule("aws", "ecs", "wait", exits=[255], stderr="Max attempts exceeded\n")

    result = dry_run.run("invoke.sh", "fargate", "cold", scenario=[gave_up, *INVOKABLE])

    assert result.returncode != 0
    stopped = [c for c in dry_run.calls if c[1:3] == ["ecs", "stop-task"]]
    assert len(stopped) == 1
    assert TASK in stopped[0]


def test_the_teardown_fails_when_a_task_definition_is_not_deleted(
    dry_run: DryRun,
) -> None:
    refused = rule("aws", "ecs", "delete-task-definitions", outputs=[f"{NAME}:1"])

    result = dry_run.run("teardown.sh", scenario=[refused, *DEPLOYED])

    assert result.returncode != 0
    assert f"{NAME}:1" in result.stderr


def test_the_teardown_waits_for_deletions_to_finish(dry_run: DryRun) -> None:
    # A MicroVM image is DELETING for a while after its delete, and IAM's
    # listings lag: both show up in a listing or two after the delete.
    slow = [
        {**r, "outputs": [r["outputs"][0]] * 3 + [""]}
        if {"list-microvm-images", "list-roles"} & set(r["words"])
        else r
        for r in DEPLOYED
    ]

    result = dry_run.run("teardown.sh", scenario=slow)

    assert result.returncode == 0, result.stderr
    assert "teardown complete" in result.stdout
