"""AGENTS.md §4 runs every step CI runs, and every command CONSTRAINTS.md names.

A green local run predicts a green CI run only while §4 keeps up with
.github/workflows/ci.yml (#61), so a CI step missing from §4 fails here. The
fallow job runs an action rather than a step, and §4 runs its CLI.
"""

import re
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# CI downloads its tools in steps named like this; locally they are §4's
# prerequisites, not commands.
TOOL_DOWNLOAD = "(checksum-verified)"


def as_local(command: str) -> str:
    """A CI command as §4 writes it: the tools CI downloads are on PATH, and
    setup-python's bare `python` is `python3`."""
    if command.startswith("'") and command.endswith("'"):
        command = command[1:-1].replace("''", "'")
    command = re.sub(r'"\$RUNNER_TEMP/([\w.-]+)"', r"\1", command)
    return re.sub(r"^python ", "python3 ", command)


def ci_commands(workflow: str) -> list[str]:
    """Every `run:` step of the workflow except the tool downloads, as §4 would
    write it. A block step comes back whole, so it matches no §4 line."""
    commands: list[str] = []
    name = ""
    lines = workflow.splitlines()
    for number, line in enumerate(lines):
        if step := re.match(r"\s*- name: (.+)", line):
            name = step.group(1)
        elif re.match(r"\s*- ", line):
            name = ""
        run = re.match(r"(\s*(?:- )?)run: (.*)", line)
        if run is None or name.endswith(TOOL_DOWNLOAD):
            continue
        value, indent = run.group(2).strip(), len(run.group(1))
        if value.startswith(("|", ">")):
            block = []
            for body in lines[number + 1 :]:
                if body.strip() and len(body) - len(body.lstrip()) <= indent:
                    break
                block.append(body)
            value = textwrap.dedent("\n".join(block)).strip()
        commands.append(as_local(value))
    return commands


def section4_commands(agents: str) -> set[str]:
    """The command lines in AGENTS.md §4's bash block, without their comments."""
    section = agents.split("\n## 4. Commands\n", 1)[1].split("\n## ", 1)[0]
    block = section.split("```bash\n", 1)[1].split("```", 1)[0]
    return {
        re.sub(r"\s+# .*$", "", line)
        for line in block.splitlines()
        if line.strip() and not line.startswith("#")
    }


def constraints_commands(constraints: str) -> set[str]:
    """The `uv run` commands CONSTRAINTS.md names, except the Planned ones."""
    current = re.sub(
        r"\n## Planned\n.*?(?=\n## |\Z)", "\n", constraints, flags=re.DOTALL
    )
    return set(re.findall(r"`(uv run [^`]+)`", current))


def read(path: str) -> str:
    return (REPO / path).read_text()


def test_section4_runs_every_ci_step() -> None:
    local = section4_commands(read("AGENTS.md"))
    ci = ci_commands(read(".github/workflows/ci.yml"))
    assert ci, "found no run steps in ci.yml"
    missing = [command for command in ci if command not in local]
    assert not missing, f"AGENTS.md §4 doesn't run these CI steps: {missing}"


def test_section4_runs_every_command_constraints_names() -> None:
    named = constraints_commands(read("CONSTRAINTS.md"))
    assert named, "found no commands in CONSTRAINTS.md"
    missing = sorted(named - section4_commands(read("AGENTS.md")))
    assert not missing, (
        f"AGENTS.md §4 doesn't run these CONSTRAINTS.md commands: {missing}"
    )


WORKFLOW = """\
jobs:
  guardrails:
    steps:
      - uses: actions/checkout@3d3c42e5 # v7.0.1
      - name: Install gitleaks (checksum-verified)
        run: |
          curl -sSfL -o "$RUNNER_TEMP/gitleaks.tar.gz" https://example.invalid/g
      - name: Guard tests
        run: python -m unittest discover -s .claude/hooks
      - name: Scan full history
        run: '"$RUNNER_TEMP/gitleaks" git --no-banner --redact .'
      - run: .github/scripts/audit-lockfile.sh "$RUNNER_TEMP/osv-scanner" uv.lock
"""


def test_ci_steps_read_as_section4_writes_them() -> None:
    assert ci_commands(WORKFLOW) == [
        "python3 -m unittest discover -s .claude/hooks",
        "gitleaks git --no-banner --redact .",
        ".github/scripts/audit-lockfile.sh osv-scanner uv.lock",
    ]


def test_a_new_block_step_is_one_command_that_section4_lacks() -> None:
    step = "      - name: Browser tests\n        run: |\n          sudo sysctl -w a=0\n          uv run pytest -m browser\n  next:\n"
    commands = ci_commands(WORKFLOW + step)
    assert commands[-1] == "sudo sysctl -w a=0\nuv run pytest -m browser"


def test_planned_commands_are_not_named_yet() -> None:
    text = "## Thresholds\n| Lint | `uv run ruff check .` |\n\n## Planned\n| Test-first | `uv run coverage report --fail-under=100` |\n\n## Exceptions\n"
    assert constraints_commands(text) == {"uv run ruff check ."}
