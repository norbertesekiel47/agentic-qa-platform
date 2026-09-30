"""`python -m aqa_hosted_chromium_spike`: one trial, its report printed as a
line of JSON. The Fargate task runs this."""

import json

from aqa_hosted_chromium_spike.trial import trial_on_this_host

print(json.dumps(trial_on_this_host()))
