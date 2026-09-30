"""The Lambda function's handler: one trial per invocation. The image runs it
through awslambdaric (the Dockerfile's `lambda` target)."""

from typing import Protocol, TypedDict

from aqa_hosted_chromium_spike.trial import Report, trial_on_this_host


class LambdaContext(Protocol):
    """The part of Lambda's context object the handler reads.
    https://docs.aws.amazon.com/lambda/latest/dg/python-context.html"""

    @property
    def log_stream_name(self) -> str: ...


class Answer(TypedDict):
    """The trial's report, and the log stream of the execution environment it
    ran in: Lambda gives each environment a stream of its own, so a stream
    that shows up again names an environment used again."""

    trial: Report
    log_stream: str


def handler(_event: object, context: LambdaContext) -> Answer:
    return Answer(trial=trial_on_this_host(), log_stream=context.log_stream_name)
