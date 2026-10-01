"""The spike's invoke.sh, in the dry run (dry_run.py): one trial per run, with
the platform's own record, and nothing left running when it fails."""

import json

import pytest
from dry_run import (
    CANDIDATES,
    CHROMIUM_EVENT,
    ENDPOINT,
    FAKE_AUTH,
    IMAGE_ARN,
    INVOKABLE,
    LAMBDA_REPORT,
    LOG_STREAM,
    MICROVM_RECORD,
    REPORT_EVENT,
    TASK,
    TASK_RECORD,
    TRIAL,
    DryRun,
    Rule,
    calls_of,
    changes,
    option,
    replacing,
    rule,
)

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
