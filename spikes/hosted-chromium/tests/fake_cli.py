"""A stand-in for the aws, docker and curl commands in the spike scripts' dry
runs (test_scripts.py): it records every call and answers from a scenario, so
no test reaches AWS.

Run as `python fake_cli.py <tool> <args...>`.
- FAKE_LOG names a file that gets one JSON array per call: [tool, *args],
  followed by what the call carried outside its arguments:
  - `<stdin: …>` for a password read from stdin (`--password-stdin`);
  - `<file: …>` for the contents of each `file://` argument;
  - `<AWS_MAX_ATTEMPTS=…>` when that variable is set for an aws call.
  A test can so tell that a secret never reached argv, and read what a JSON
  file sent to AWS held.
- FAKE_SCENARIO names a JSON list of rules. The first rule that matches
  answers the call. A rule matches when its tool is the call's, its `words`
  are all among the args, and its `mentions` all appear inside some arg (a
  name within a --query, say). A rule answers with:
  - `outputs` and `exits`: stdout and the exit code (default 0) for
    successive matching calls; the last of each repeats;
  - `stderr`: what goes to stderr;
  - `write_last_arg`: text written to the path in the call's last argument,
    as `aws lambda invoke` writes its response;
  - `http_status`: for curl, the status of the response. At 400 or above,
    curl exits 22 when the call asks `--fail`, and prints the body otherwise;
  - `delay`: seconds the call takes.
- A call that no rule matches succeeds with no output.
"""

import json
import os
import sys
import time
from pathlib import Path


def record(tool: str, args: list[str]) -> list[str]:
    extras = []
    if "--password-stdin" in args:
        extras.append(f"<stdin: {sys.stdin.read().strip()}>")
    extras += [
        f"<file: {Path(arg.removeprefix('file://')).read_text()}>"
        for arg in args
        if arg.startswith("file://")
    ]
    if tool == "aws" and "AWS_MAX_ATTEMPTS" in os.environ:
        extras.append(f"<AWS_MAX_ATTEMPTS={os.environ['AWS_MAX_ATTEMPTS']}>")
    return [tool, *args, *extras]


def matches(rule: dict[str, object], tool: str, args: list[str]) -> bool:
    words = rule.get("words", [])
    mentions = rule.get("mentions", [])
    assert isinstance(words, list)
    assert isinstance(mentions, list)
    return (
        rule["tool"] == tool
        and all(word in args for word in words)
        and all(any(str(m) in arg for arg in args) for m in mentions)
    )


def main(tool: str, args: list[str]) -> int:
    with Path(os.environ["FAKE_LOG"]).open("a") as log:
        log.write(json.dumps(record(tool, args)) + "\n")
    scenario = Path(os.environ["FAKE_SCENARIO"])
    rules = json.loads(scenario.read_text())
    for rule in rules:
        if matches(rule, tool, args):
            outputs, exits = rule.get("outputs", [""]), rule.get("exits", [0])
            rule["outputs"], rule["exits"] = outputs[1:] or outputs, exits[1:] or exits
            scenario.write_text(json.dumps(rules))
            time.sleep(rule.get("delay", 0))
            if rule.get("http_status", 200) >= 400 and "--fail" in args:
                return 22
            sys.stdout.write(outputs[0])
            if "write_last_arg" in rule:
                Path(args[-1]).write_text(rule["write_last_arg"])
            sys.stderr.write(rule.get("stderr", ""))
            return int(exits[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
