"""The spike's budget.sh and deploy.sh, in the dry run (dry_run.py): the budget
alarm comes before any other resource, and a deploy stops where it fails."""

import pytest
from dry_run import (
    CANDIDATES,
    COMMIT,
    DEPLOYABLE,
    NAME,
    NO_BUDGET,
    ROLES,
    DryRun,
    calls_of,
    changes,
    files,
    option,
    replacing,
    rule,
)

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
