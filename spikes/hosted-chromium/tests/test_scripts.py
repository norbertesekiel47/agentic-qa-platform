"""The spike's AWS CLI scripts, in a dry run (ADR-0008 amendment, 2026-09-30):
fake aws, docker, curl and sleep commands (fake_cli.py) record every call and
answer from a scenario, so these tests reach no AWS account and create nothing.

They prove what #37 asks of the scripts: the budget alarm comes before any
other resource, and the teardown deletes everything the scripts create. The
teardown scenario answers a listing only when it names the spike's resources
exactly, so a listing that asked for the wrong name, or for a broader one,
would find nothing to delete and fail the coverage test."""

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


# --- The budget alarm comes first -------------------------------------------------


def test_budget_sh_creates_only_the_budget_alarm_it_promises(dry_run: DryRun) -> None:
    result = dry_run.run("budget.sh", "spike@example.com", scenario=DEPLOYABLE)

    assert result.returncode == 0, result.stderr
    changed = [call for call in dry_run.calls if changes(call)]
    assert [call[:3] for call in changed] == [["aws", "budgets", "create-budget"]]
    budget, notifications = files(changed[0])
    assert budget["BudgetType"] == "COST"
    assert budget["TimeUnit"] == "MONTHLY"
    assert budget["BudgetLimit"] == {"Amount": "10", "Unit": "USD"}
    alerts = [n["Notification"] for n in notifications]
    assert [(a["NotificationType"], a["Threshold"]) for a in alerts] == [
        ("ACTUAL", 50),
        ("ACTUAL", 100),
    ]
    assert all(
        n["Subscribers"]
        == [{"SubscriptionType": "EMAIL", "Address": "spike@example.com"}]
        for n in notifications
    )


@pytest.mark.parametrize("email", ["", "not-an-address", "a@b@c"])
def test_budget_sh_needs_an_email_address(dry_run: DryRun, email: str) -> None:
    result = dry_run.run("budget.sh", email, scenario=DEPLOYABLE)

    assert result.returncode == 2
    assert [call for call in dry_run.calls if changes(call)] == []


# With the alarm there, each deploy looks for it before it changes anything.
@pytest.mark.parametrize("candidate", CANDIDATES)
def test_a_deploy_checks_the_budget_alarm_before_its_first_change(
    dry_run: DryRun, candidate: str
) -> None:
    result = dry_run.run("deploy.sh", candidate, scenario=DEPLOYABLE)

    assert result.returncode == 0, result.stderr
    calls = dry_run.calls
    first_change = next(i for i, call in enumerate(calls) if changes(call))
    assert calls_of(calls[:first_change], "budgets", "describe-budget")
    # A deploy's first change is its log group, the mark of a deployed candidate.
    assert calls[first_change][1:3] == ["logs", "create-log-group"]


@pytest.mark.parametrize("script", ["deploy.sh", "invoke.sh"])
@pytest.mark.parametrize("candidate", CANDIDATES)
def test_the_scripts_refuse_to_run_without_the_budget_alarm(
    dry_run: DryRun, script: str, candidate: str
) -> None:
    result = dry_run.run(script, candidate, "cold", scenario=NO_BUDGET + DEPLOYABLE)

    assert result.returncode == 1
    assert "run budget.sh first" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


# --- deploy.sh --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "failing"),
    [
        ("lambda", ["ecr", "create-repository"]),
        ("lambda", ["iam", "put-role-policy"]),
        ("fargate", ["iam", "create-role"]),
        ("fargate", ["ecs", "create-cluster"]),
        ("microvm", ["s3api", "create-bucket"]),
        ("microvm", ["iam", "put-role-policy"]),
    ],
)
def test_a_deploy_stops_at_the_first_change_that_fails(
    dry_run: DryRun, candidate: str, failing: list[str]
) -> None:
    fails = rule("aws", *failing, exits=[254], stderr="fake: AccessDenied\n")

    result = dry_run.run("deploy.sh", candidate, scenario=replacing(DEPLOYABLE, fails))

    assert result.returncode != 0
    calls = [call[1:3] for call in dry_run.calls]
    assert [c for c in dry_run.calls[calls.index(failing) + 1 :] if changes(c)] == []


def test_a_deploy_stops_when_the_account_lookup_fails(dry_run: DryRun) -> None:
    expired = rule("aws", "sts", "get-caller-identity", exits=[255], stderr="expired\n")

    result = dry_run.run(
        "deploy.sh", "microvm", scenario=replacing(DEPLOYABLE, expired)
    )

    assert result.returncode != 0
    assert [call for call in dry_run.calls if changes(call)] == []


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

    result = dry_run.run("deploy.sh", candidate, scenario=replacing(DEPLOYABLE, there))

    assert result.returncode == 1
    assert f"{candidate} is already deployed: run teardown.sh first" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


@pytest.mark.parametrize("label", ["<no value>", "", "0123456"])
def test_a_deploy_refuses_an_image_that_names_no_commit(
    dry_run: DryRun, label: str
) -> None:
    unlabelled = rule("docker", "image", "inspect", outputs=[label])

    result = dry_run.run(
        "deploy.sh", "lambda", scenario=replacing(DEPLOYABLE, unlabelled)
    )

    assert result.returncode == 1
    assert "names no commit" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


def test_a_microvm_deploy_refuses_a_missing_zip(dry_run: DryRun) -> None:
    (dry_run.root / f"spike/build/microvm-{COMMIT}.zip").unlink()

    result = dry_run.run("deploy.sh", "microvm", scenario=DEPLOYABLE)

    assert result.returncode == 1
    assert "run package.sh microvm first" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


def test_a_fargate_deploy_needs_a_default_vpc(dry_run: DryRun) -> None:
    none = rule("aws", "ec2", "describe-vpcs", outputs=["None"])

    result = dry_run.run("deploy.sh", "fargate", scenario=replacing(DEPLOYABLE, none))

    assert result.returncode == 1
    assert "no default VPC" in result.stderr


@pytest.mark.parametrize("state", ["CREATE_FAILED", "CREATION_FAILED"])
def test_a_microvm_deploy_fails_when_its_image_build_does(
    dry_run: DryRun, state: str
) -> None:
    failed = rule(
        "aws", "lambda-microvms", "get-microvm-image", "state", outputs=[state]
    )

    result = dry_run.run("deploy.sh", "microvm", scenario=replacing(DEPLOYABLE, failed))

    assert result.returncode == 1
    assert f"the MicroVM image build ended {state}" in result.stderr


def test_only_the_microvm_roles_may_tag_their_session(dry_run: DryRun) -> None:
    for candidate in CANDIDATES:
        dry_run.run("deploy.sh", candidate, scenario=DEPLOYABLE)

    trusts = {
        option(call, "--role-name"): files(call)[0]["Statement"][0]
        for call in calls_of(dry_run.calls, "iam", "create-role")
    }
    assert sorted(trusts) == sorted(ROLES)
    assert {role: trust["Action"] for role, trust in trusts.items()} == {
        f"{NAME}-lambda": ["sts:AssumeRole"],
        f"{NAME}-fargate": ["sts:AssumeRole"],
        f"{NAME}-microvm-build": ["sts:AssumeRole", "sts:TagSession"],
        f"{NAME}-microvm": ["sts:AssumeRole", "sts:TagSession"],
    }
    assert trusts[f"{NAME}-fargate"]["Principal"] == {
        "Service": "ecs-tasks.amazonaws.com"
    }


def test_the_fargate_task_holds_no_credentials_and_ends_by_itself(
    dry_run: DryRun,
) -> None:
    dry_run.run("deploy.sh", "fargate", scenario=DEPLOYABLE)

    [register] = calls_of(dry_run.calls, "ecs", "register-task-definition")
    [task] = files(register)
    assert "taskRoleArn" not in task
    [container] = task["containerDefinitions"]
    assert container["command"] == [
        "timeout",
        "300",
        "python",
        "-m",
        "aqa_hosted_chromium_spike",
    ]
    assert (task["cpu"], task["memory"]) == ("1024", "2048")
    assert task["runtimePlatform"]["cpuArchitecture"] == "ARM64"


# --- invoke.sh --------------------------------------------------------------------

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


def test_a_lambda_invoke_is_timed_around_the_call_and_never_retried(
    dry_run: DryRun,
) -> None:
    result = dry_run.run("invoke.sh", "lambda", "warm", scenario=INVOKABLE)

    measured = json.loads(result.stdout)
    # The fake's invoke takes 0.3 s, which the timing must span.
    assert measured["answered_at"] - measured["requested_at"] >= 0.3
    [invoke] = calls_of(dry_run.calls, "lambda", "invoke")
    assert "<AWS_MAX_ATTEMPTS=1>" in invoke


def test_a_cold_lambda_invoke_starts_a_new_environment(dry_run: DryRun) -> None:
    dry_run.run("invoke.sh", "lambda", "cold", scenario=INVOKABLE)
    cold = [call[1:3] for call in calls_of(dry_run.calls, "lambda")]
    dry_run.calls.clear()
    dry_run.run("invoke.sh", "lambda", "warm", scenario=INVOKABLE)
    warm = [call[1:3] for call in calls_of(dry_run.calls, "lambda")]

    # A configuration change makes Lambda start the next invocation afresh.
    assert cold == [
        ["lambda", "update-function-configuration"],
        ["lambda", "wait"],
        ["lambda", "invoke"],
    ]
    assert warm == [["lambda", "invoke"]]


def test_a_failed_lambda_trial_prints_no_measurement(dry_run: DryRun) -> None:
    error = rule(
        "aws",
        "lambda",
        "invoke",
        write_last_arg='{"errorMessage": "fake crash"}',
        outputs=[json.dumps({"StatusCode": 200, "FunctionError": "Unhandled"})],
    )

    result = dry_run.run(
        "invoke.sh", "lambda", "cold", scenario=replacing(INVOKABLE, error)
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "fake crash" in result.stderr


def test_a_fargate_task_has_no_warm_start(dry_run: DryRun) -> None:
    result = dry_run.run("invoke.sh", "fargate", "warm", scenario=INVOKABLE)

    assert result.returncode == 2
    assert "every Fargate task starts cold" in result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


def test_a_fargate_task_is_stopped_when_its_wait_fails(dry_run: DryRun) -> None:
    # `ecs wait tasks-stopped` gives up after 100 checks, about ten minutes.
    gave_up = rule("aws", "ecs", "wait", exits=[255], stderr="Max attempts exceeded\n")

    result = dry_run.run(
        "invoke.sh", "fargate", "cold", scenario=replacing(INVOKABLE, gave_up)
    )

    assert result.returncode != 0
    [stopped] = calls_of(dry_run.calls, "ecs", "stop-task")
    assert TASK in stopped


def test_a_task_fargate_cannot_place_is_reported(dry_run: DryRun) -> None:
    unplaced = rule(
        "aws",
        "ecs",
        "run-task",
        outputs=[
            json.dumps({"tasks": [], "failures": [{"reason": "fake: no capacity"}]})
        ],
    )

    result = dry_run.run(
        "invoke.sh", "fargate", "cold", scenario=replacing(INVOKABLE, unplaced)
    )

    assert result.returncode == 1
    assert "fake: no capacity" in result.stderr
    assert calls_of(dry_run.calls, "ecs", "wait") == []


def test_a_fargate_task_whose_trial_failed_prints_no_measurement(
    dry_run: DryRun,
) -> None:
    failed = rule(
        "aws",
        "ecs",
        "describe-tasks",
        outputs=[json.dumps({**TASK_RECORD, "containers": [{"exitCode": 1}]})],
    )

    result = dry_run.run(
        "invoke.sh", "fargate", "cold", scenario=replacing(INVOKABLE, failed)
    )

    assert result.returncode == 1
    assert result.stdout == ""


def test_a_fargate_task_without_a_report_prints_no_measurement(
    dry_run: DryRun,
) -> None:
    silent = rule(
        "aws", "logs", "get-log-events", outputs=[json.dumps([CHROMIUM_EVENT])]
    )

    result = dry_run.run(
        "invoke.sh", "fargate", "cold", scenario=replacing(INVOKABLE, silent)
    )

    assert result.returncode == 1
    assert "holds no report" in result.stderr
    assert result.stdout == ""


def test_a_fargate_report_is_found_among_chromium_lines(dry_run: DryRun) -> None:
    around = rule(
        "aws",
        "logs",
        "get-log-events",
        outputs=[json.dumps([CHROMIUM_EVENT, REPORT_EVENT, CHROMIUM_EVENT])],
    )

    result = dry_run.run(
        "invoke.sh", "fargate", "cold", scenario=replacing(INVOKABLE, around)
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["trial"] == TRIAL


def test_a_fargate_invoke_stops_when_its_subnets_cannot_be_read(
    dry_run: DryRun,
) -> None:
    denied = rule(
        "aws", "ec2", "describe-subnets", exits=[254], stderr="fake: denied\n"
    )

    result = dry_run.run(
        "invoke.sh", "fargate", "cold", scenario=replacing(INVOKABLE, denied)
    )

    assert result.returncode != 0
    assert calls_of(dry_run.calls, "ecs", "run-task") == []


def test_a_microvm_runs_one_trial_once_its_server_answers(dry_run: DryRun) -> None:
    # Lambda answers 502 until the MicroVM's /run hook has returned.
    health = rule("curl", f"{ENDPOINT}/health", exits=[22, 22, 0])

    result = dry_run.run(
        "invoke.sh", "microvm", "cold", scenario=replacing(INVOKABLE, health)
    )

    assert result.returncode == 0, result.stderr
    urls = [call[-1] for call in dry_run.calls if call[0] == "curl"]
    assert urls == [f"{ENDPOINT}/health"] * 3 + [f"{ENDPOINT}/trial"]


def test_a_microvm_lives_a_capped_time(dry_run: DryRun) -> None:
    dry_run.run("invoke.sh", "microvm", "cold", scenario=INVOKABLE)

    [run] = calls_of(dry_run.calls, "lambda-microvms", "run-microvm")
    assert option(run, "--maximum-duration-in-seconds") == "900"
    idle = json.loads(option(run, "--idle-policy"))
    assert idle == {
        "autoResumeEnabled": False,
        "maxIdleDurationSeconds": 60,
        "suspendedDurationSeconds": 0,
    }


def test_a_microvm_is_terminated_before_its_record_is_read(dry_run: DryRun) -> None:
    dry_run.run("invoke.sh", "microvm", "cold", scenario=INVOKABLE)
    microvm = [call[2] for call in calls_of(dry_run.calls, "lambda-microvms")]

    assert microvm.index("run-microvm") < microvm.index("terminate-microvm")
    assert microvm.index("terminate-microvm") < microvm.index("get-microvm")
    assert microvm.count("terminate-microvm") == 1


@pytest.mark.parametrize(
    "trial",
    [
        rule("curl", f"{ENDPOINT}/trial", exits=[28], stderr="curl: (28)\n"),
        rule(
            "curl",
            f"{ENDPOINT}/trial",
            http_status=502,
            outputs=['{"message": "Bad Gateway"}'],
        ),
    ],
    ids=["timeout", "server-error"],
)
def test_a_microvm_trial_that_fails_is_not_run_again(
    dry_run: DryRun, trial: Rule
) -> None:
    result = dry_run.run(
        "invoke.sh", "microvm", "cold", scenario=replacing(INVOKABLE, trial)
    )

    assert result.returncode != 0
    assert result.stdout == ""
    urls = [call[-1] for call in dry_run.calls if call[0] == "curl"]
    assert urls.count(f"{ENDPOINT}/trial") == 1
    terminated = calls_of(dry_run.calls, "lambda-microvms", "terminate-microvm")
    assert ["mvm-fake-1" in call for call in terminated] == [True]


def test_a_microvm_that_never_answers_is_still_terminated(dry_run: DryRun) -> None:
    silent = rule("curl", exits=[22], stderr="curl: (22) 502\n")

    result = dry_run.run(
        "invoke.sh", "microvm", "cold", scenario=replacing(INVOKABLE, silent)
    )

    assert result.returncode == 1
    assert "never answered" in result.stderr
    terminated = calls_of(dry_run.calls, "lambda-microvms", "terminate-microvm")
    assert ["mvm-fake-1" in call for call in terminated] == [True]


def test_a_failed_token_request_stops_the_invoke(dry_run: DryRun) -> None:
    denied = rule(
        "aws",
        "lambda-microvms",
        "create-microvm-auth-token",
        exits=[254],
        stderr="fake\n",
    )

    result = dry_run.run(
        "invoke.sh", "microvm", "cold", scenario=replacing(INVOKABLE, denied)
    )

    assert result.returncode != 0
    assert [call for call in dry_run.calls if call[0] == "curl"] == []
    assert calls_of(dry_run.calls, "lambda-microvms", "terminate-microvm")


@pytest.mark.parametrize(
    "images",
    [[], [IMAGE_ARN, f"{IMAGE_ARN}-copy"]],
    ids=["none", "two"],
)
def test_a_microvm_invoke_needs_exactly_one_ready_image(
    dry_run: DryRun, images: list[str]
) -> None:
    found = rule(
        "aws", "lambda-microvms", "list-microvm-images", outputs=[json.dumps(images)]
    )

    result = dry_run.run(
        "invoke.sh", "microvm", "cold", scenario=replacing(INVOKABLE, found)
    )

    assert result.returncode == 1
    assert f"found {len(images)}" in result.stderr
    assert calls_of(dry_run.calls, "lambda-microvms", "run-microvm") == []


def test_the_microvm_image_lookup_reads_every_page(dry_run: DryRun) -> None:
    dry_run.run("invoke.sh", "microvm", "cold", scenario=INVOKABLE)

    # With text output, the CLI applies the query to each page on its own.
    [lookup] = calls_of(dry_run.calls, "lambda-microvms", "list-microvm-images")
    assert option(lookup, "--output") == "json"


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


# --- teardown.sh ------------------------------------------------------------------

# Each kind of resource the scripts create, and the calls teardown.sh must make
# to delete it: the words each deleting call must hold. A create that isn't
# listed here or in PART_OF fails the coverage test until someone decides what
# deletes it.
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
    # A MicroVM or task whose invoke.sh died before its cleanup ran.
    ("lambda-microvms", "run-microvm"): lambda _: [
        ["lambda-microvms", "terminate-microvm", "mvm-fake-1"]
    ],
    ("ecs", "run-task"): lambda _: [
        ["ecs", "stop-task", TASK],
        ["ecs", "wait", "tasks-stopped", TASK],
    ],
}
# Calls that change a resource above, or make something that ends by itself.
PART_OF = {
    ("logs", "put-retention-policy"): "the log group",
    ("s3api", "put-public-access-block"): "the bucket",
    ("s3api", "put-bucket-tagging"): "the bucket",
    ("s3", "cp"): "the bucket, which rb --force empties",
    ("lambda", "update-function-configuration"): "the function",
    ("lambda-microvms", "create-microvm-auth-token"): "a token that expires by itself",
    ("lambda-microvms", "terminate-microvm"): "invoke.sh's own cleanup",
    ("docker", "push"): "the ECR repository, which delete-repository --force empties",
}
DELETES = re.compile(r"^(delete|deregister|detach|terminate|stop|rb)\b")


def quoted(*names: str) -> list[str]:
    return [f"'{name}'" for name in names]


# The account after every candidate was deployed and a trial was interrupted
# on each, as teardown.sh lists it. A listing answers only when it names the
# spike's resources exactly; each shows what exists, then nothing once deleted.
DEPLOYED = [
    rule("aws", "sts", "get-caller-identity", outputs=[ACCOUNT]),
    rule(
        "aws",
        "lambda-microvms",
        "list-microvms",
        mentions=[f"imageArn=='{IMAGE_ARN}'"],
        outputs=["mvm-fake-1", ""],
    ),
    rule(
        "aws",
        "lambda-microvms",
        "list-microvm-images",
        mentions=[f"name=='{NAME}'"],
        outputs=[IMAGE_ARN, ""],
    ),
    rule(
        "aws",
        "lambda",
        "list-functions",
        mentions=[f"FunctionName=='{NAME}'"],
        outputs=[NAME, ""],
    ),
    rule("aws", "ecs", "describe-clusters", "--clusters", NAME, outputs=[NAME, ""]),
    rule("aws", "ecs", "list-tasks", "--cluster", NAME, outputs=[TASK]),
    rule(
        "aws",
        "ecs",
        "list-task-definitions",
        "--family-prefix",
        NAME,
        "ACTIVE",
        mentions=[f":task-definition/{NAME}:"],
        outputs=[f"{NAME}:1", ""],
    ),
    rule(
        "aws",
        "ecs",
        "list-task-definitions",
        "--family-prefix",
        NAME,
        "INACTIVE",
        mentions=[f":task-definition/{NAME}:"],
        outputs=[f"{NAME}:1", ""],
    ),
    rule(
        "aws",
        "ec2",
        "describe-security-groups",
        f"Name=group-name,Values={NAME}",
        "Name=tag:aqa-spike,Values=hosted-chromium",
        outputs=["sg-0fake", ""],
    ),
    rule(
        "aws",
        "ecr",
        "describe-repositories",
        mentions=quoted(f"{NAME}-lambda", f"{NAME}-fargate"),
        outputs=[f"{NAME}-lambda\t{NAME}-fargate", ""],
    ),
    rule(
        "aws",
        "s3api",
        "list-buckets",
        mentions=[f"Name=='{NAME}-{ACCOUNT}'"],
        outputs=[f"{NAME}-{ACCOUNT}", ""],
    ),
    rule(
        "aws",
        "logs",
        "describe-log-groups",
        "--log-group-name-prefix",
        "/aqa-spike/hosted-chromium/",
        mentions=quoted(*LOG_GROUPS),
        outputs=["\t".join(LOG_GROUPS), ""],
    ),
    rule(
        "aws",
        "iam",
        "list-roles",
        "--path-prefix",
        "/aqa-spike/",
        mentions=quoted(*ROLES),
        outputs=["\t".join(ROLES), ""],
    ),
    *[
        rule("aws", "iam", "list-role-policies", "--role-name", role, outputs=[role])
        for role in ROLES
    ],
    rule(
        "aws",
        "budgets",
        "describe-budget",
        "--budget-name",
        NAME,
        outputs=[NAME, ""],
        exits=[0, 254],
        stderr="An error occurred (NotFoundException) when calling the "
        "DescribeBudget operation: fake\n",
    ),
]
# The kinds teardown.sh checks, as it names them when one remains.
KINDS = [
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
    start = len(dry_run.calls)

    teardown = dry_run.run("teardown.sh", scenario=DEPLOYED)

    assert teardown.returncode == 0, teardown.stderr
    assert "teardown complete" in teardown.stdout
    unclassified = {kind(c) for c in created} - DELETED_BY.keys() - PART_OF.keys()
    assert unclassified == set(), "add these to DELETED_BY or PART_OF"
    teardown_calls = dry_run.calls[start:]
    expected = [
        words
        for call in created
        if kind(call) in DELETED_BY
        for words in DELETED_BY[kind(call)](call[1:])
    ]
    missing = [
        words
        for words in expected
        if not any(all(word in call for word in words) for call in teardown_calls)
    ]
    assert missing == [], "teardown.sh deletes none of these"


def test_the_teardown_deletes_the_budget_alarm_only_after_the_rest(
    dry_run: DryRun,
) -> None:
    dry_run.run("teardown.sh", scenario=DEPLOYED)
    calls = dry_run.calls

    budget = calls.index(next(iter(calls_of(calls, "budgets", "delete-budget"))))
    others = [i for i, call in enumerate(calls) if changes(call) and i != budget]
    assert others
    assert max(others) < budget


def test_the_teardown_succeeds_on_an_empty_account(dry_run: DryRun) -> None:
    # Only the budget lookup says anything: NotFoundException, for no budget.
    empty = [DEPLOYED[0], {**DEPLOYED[-1], "outputs": [""], "exits": [254]}]

    result = dry_run.run("teardown.sh", scenario=empty)

    assert result.returncode == 0, result.stderr
    assert [call for call in dry_run.calls if changes(call)] == []


def test_a_teardown_stops_when_a_listing_fails(dry_run: DryRun) -> None:
    expired = rule(
        "aws", "lambda", "list-functions", exits=[255], stderr="ExpiredTokenException\n"
    )

    result = dry_run.run("teardown.sh", scenario=replacing(DEPLOYED, expired))

    assert result.returncode != 0
    assert "teardown complete" not in result.stdout
    failed = dry_run.calls.index(
        next(iter(calls_of(dry_run.calls, "lambda", "list-functions")))
    )
    assert [c for c in dry_run.calls[failed + 1 :] if changes(c)] == []


def test_a_teardown_stops_when_the_account_lookup_fails(dry_run: DryRun) -> None:
    expired = rule("aws", "sts", "get-caller-identity", exits=[255], stderr="expired\n")

    result = dry_run.run("teardown.sh", scenario=replacing(DEPLOYED, expired))

    assert result.returncode != 0
    assert [call for call in dry_run.calls if changes(call)] == []


def test_a_teardown_stops_when_a_listing_fails_during_its_check(
    dry_run: DryRun,
) -> None:
    # The roles are listed and deleted, then their listing fails in the check.
    flaky = {
        **next(r for r in DEPLOYED if "list-roles" in r["words"]),
        "exits": [0, 255],
        "stderr": "fake: throttled\n",
    }

    result = dry_run.run("teardown.sh", scenario=replacing(DEPLOYED, flaky))

    assert result.returncode != 0
    assert "teardown complete" not in result.stdout
    assert calls_of(dry_run.calls, "budgets", "delete-budget") == []


def test_the_teardown_keeps_the_budget_alarm_while_anything_remains(
    dry_run: DryRun,
) -> None:
    # Every resource still shows up in its listing after its delete.
    stuck = [{**r, "outputs": [r["outputs"][0]]} for r in DEPLOYED]

    result = dry_run.run("teardown.sh", scenario=stuck)

    assert result.returncode == 1
    assert "the budget alarm stays" in result.stderr
    assert calls_of(dry_run.calls, "budgets", "delete-budget") == []
    assert [k for k in KINDS if f"  {k}: " not in result.stderr] == []


def test_the_teardown_fails_when_the_budget_alarm_remains(dry_run: DryRun) -> None:
    budget = {**DEPLOYED[-1], "outputs": [NAME], "exits": [0]}

    result = dry_run.run("teardown.sh", scenario=replacing(DEPLOYED, budget))

    assert result.returncode == 1
    assert "budgets" in result.stderr


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


def test_the_teardown_fails_when_a_task_definition_is_not_deleted(
    dry_run: DryRun,
) -> None:
    refused = rule("aws", "ecs", "delete-task-definitions", outputs=[f"{NAME}:1"])

    result = dry_run.run("teardown.sh", scenario=replacing(DEPLOYED, refused))

    assert result.returncode != 0
    assert f"{NAME}:1" in result.stderr
    assert calls_of(dry_run.calls, "budgets", "delete-budget") == []


def test_the_teardown_waits_for_a_security_group_in_use(dry_run: DryRun) -> None:
    # A stopped task's network interface holds the group for a while.
    busy = rule(
        "aws",
        "ec2",
        "delete-security-group",
        exits=[254, 254, 0],
        stderr="An error occurred (DependencyViolation): fake\n",
    )

    result = dry_run.run("teardown.sh", scenario=replacing(DEPLOYED, busy))

    assert result.returncode == 0, result.stderr
    assert len(calls_of(dry_run.calls, "ec2", "delete-security-group")) == 3


def test_the_teardown_stops_when_a_security_group_cannot_be_deleted(
    dry_run: DryRun,
) -> None:
    denied = rule(
        "aws",
        "ec2",
        "delete-security-group",
        exits=[254],
        stderr="fake: UnauthorizedOperation\n",
    )

    result = dry_run.run("teardown.sh", scenario=replacing(DEPLOYED, denied))

    assert result.returncode != 0
    assert "UnauthorizedOperation" in result.stderr
    assert len(calls_of(dry_run.calls, "ec2", "delete-security-group")) == 1


# --- collect.sh -------------------------------------------------------------------


def test_collect_asks_cost_explorer_for_the_usage_by_type(dry_run: DryRun) -> None:
    usage = rule("aws", "ce", "get-cost-and-usage", outputs=['{"ResultsByTime": []}'])

    result = dry_run.run("collect.sh", "2026-10-01", "2026-10-03", scenario=[usage])

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ResultsByTime": []}
    [call] = calls_of(dry_run.calls, "ce", "get-cost-and-usage")
    assert "Start=2026-10-01,End=2026-10-03" in call
    assert "Type=DIMENSION,Key=USAGE_TYPE" in call
    assert json.loads(option(call, "--filter")) == {
        "Dimensions": {"Key": "REGION", "Values": ["us-east-1"]}
    }


@pytest.mark.parametrize(
    "days",
    [
        [],
        ["2026-10-01"],
        ["2026-10-01", "tomorrow"],
        ["2026-10-03", "2026-10-01"],
        ["2026-10-01", "2026-10-03", "extra"],
    ],
    ids=["none", "one", "malformed", "reversed", "three"],
)
def test_collect_asks_nothing_without_two_days_in_order(
    dry_run: DryRun, days: list[str]
) -> None:
    result = dry_run.run("collect.sh", *days, scenario=[])

    assert result.returncode == 2
    assert dry_run.calls == []
