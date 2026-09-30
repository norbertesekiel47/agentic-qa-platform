"""The Lambda function's handler: one trial per invocation. The image runs it
through awslambdaric (the Dockerfile's `lambda` target)."""

from aqa_hosted_chromium_spike.trial import Report, trial_on_this_host


def handler(_event: object, _context: object) -> Report:
    return trial_on_this_host()
