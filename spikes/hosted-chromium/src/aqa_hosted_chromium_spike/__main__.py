"""`python -m aqa_hosted_chromium_spike`: one trial, its report printed as a
line of JSON. The Fargate task runs this."""

import json
import sys

from aqa_hosted_chromium_spike.trial import measure

sys.stdout.write(json.dumps(measure()) + "\n")
