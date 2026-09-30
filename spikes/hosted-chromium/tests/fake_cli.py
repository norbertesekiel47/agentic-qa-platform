"""A stand-in for the aws, docker and curl commands in the spike scripts' dry
runs (test_scripts.py): it records every call and answers from a scenario, so
no test reaches AWS.

Run as `python fake_cli.py <tool> <args...>`.
- FAKE_LOG names a file that gets one JSON array per call: [tool, *args].
- FAKE_SCENARIO names a JSON list of rules. The first rule whose tool matches
  and whose words all appear among the args answers the call:
  - `outputs` and `exits`: stdout and the exit code (default 0) for
    successive matching calls; the last of each repeats;
  - `stderr`: what goes to stderr;
  - `write_last_arg`: text written to the path in the call's last argument,
    as `aws lambda invoke` writes its response.
- A call that no rule matches succeeds with no output.
- A call that reads a password from stdin (`--password-stdin`) reads it, and
  the log records what it read, so a test can tell it never reached argv.
"""

import json
import os
import sys
from pathlib import Path


def main(tool: str, args: list[str]) -> int:
    record = [tool, *args]
    if "--password-stdin" in args:
        record.append(f"<stdin: {sys.stdin.read().strip()}>")
    with Path(os.environ["FAKE_LOG"]).open("a") as log:
        log.write(json.dumps(record) + "\n")
    scenario = Path(os.environ["FAKE_SCENARIO"])
    rules = json.loads(scenario.read_text())
    for rule in rules:
        if rule["tool"] == tool and all(word in args for word in rule["words"]):
            outputs, exits = rule.get("outputs", [""]), rule.get("exits", [0])
            rule["outputs"], rule["exits"] = outputs[1:] or outputs, exits[1:] or exits
            scenario.write_text(json.dumps(rules))
            sys.stdout.write(outputs[0])
            if "write_last_arg" in rule:
                Path(args[-1]).write_text(rule["write_last_arg"])
            sys.stderr.write(rule.get("stderr", ""))
            return int(exits[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
