"""The spike's teardown.sh and collect.sh, in the dry run (dry_run.py): the
teardown deletes everything the scripts create, and only that, and keeps the
budget alarm while anything else remains. The teardown scenario answers a
listing only when it names the spike's resources exactly, so a listing that
asked for the wrong name, or a broader one, would find nothing to delete and
fail the coverage test."""

import json
import re
from collections.abc import Callable

import pytest
from dry_run import (
    ACCOUNT,
    CANDIDATES,
    IMAGE_ARN,
    INVOKABLE,
    LOG_GROUPS,
    NAME,
    ROLES,
    TASK,
    DryRun,
    calls_of,
    changes,
    kind,
    option,
    replacing,
    rule,
)

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
