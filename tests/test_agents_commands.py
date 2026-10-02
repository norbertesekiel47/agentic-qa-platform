"""AGENTS.md §4 runs every gate CI runs, and every `uv run` command CONSTRAINTS.md names.

A green local run predicts a green CI run only while §4 keeps up with the
workflows in .github/workflows/ (#61), so a CI step or action with no §4 line
fails here. §4 must also type-check as Linux, which a plain run on macOS
doesn't (#82).
"""

import itertools
import re
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The §4 line each action needs, by the start of that line; None for the setup
# actions, which §4's prerequisites replace. An action missing here fails the
# test until someone decides what §4 runs for it.
ACTIONS: dict[str, str | None] = {
    "actions/checkout": None,
    "actions/setup-python": None,
    "fallow-rs/fallow": "fallow audit ",
}

HOW_TO_FIX = (
    "add each to AGENTS.md §4. A step that downloads a tool is exempt when its "
    "name ends in '(checksum-verified)', it checks a sha256 and every command in "
    "it works in $RUNNER_TEMP; any other step that only prepares the CI runner "
    "needs an exemption here, with the maintainer's approval"
)


def is_download(name: str, body: str) -> bool:
    """A step that only fetches a tool and checks it, so §4 lists the tool as a
    prerequisite instead of running the step. A gate added to it fails this."""
    commands = [line for line in body.replace("\\\n", " ").splitlines() if line.strip()]
    return (
        name.endswith("(checksum-verified)")
        and "sha256sum --check" in body
        and all("$RUNNER_TEMP" in command for command in commands)
    )


def as_local(command: str) -> str:
    """A CI command as §4 writes it: the tools CI downloads are on PATH, and
    setup-python's bare `python` is `python3`."""
    if command.startswith("'") and command.endswith("'"):
        command = command[1:-1].replace("''", "'")
    command = re.sub(r'"\$RUNNER_TEMP/([\w.-]+)"', r"\1", command)
    return re.sub(r"^python ", "python3 ", command)


def run_value(first: str, rest: list[str], indent: int) -> str:
    """A `run:` value from its first line and the lines after it. A block comes
    back whole, so it matches no §4 line; a plain value continued on deeper
    lines is folded into one, as YAML reads it."""
    body = list(
        itertools.takewhile(
            lambda line: not line.strip() or len(line) - len(line.lstrip()) > indent,
            rest,
        )
    )
    if first.startswith(("|", ">")):
        return textwrap.dedent("\n".join(body)).strip()
    return " ".join([first, *(line.strip() for line in body if line.strip())])


def ci_commands(workflow: str) -> list[str]:
    """Every `run:` step, as §4 would write it, except the tool downloads."""
    commands: list[str] = []
    name = ""
    lines = workflow.splitlines()
    for number, line in enumerate(lines):
        if step := re.match(r"\s*- name: (.+)", line):
            name = step.group(1)
        elif re.match(r"\s*- ", line):
            name = ""
        if run := re.match(r"(\s*(?:- )?)run: (.*)", line):
            value = run_value(
                run.group(2).strip(), lines[number + 1 :], len(run.group(1))
            )
            if not is_download(name, value):
                commands.append(as_local(value))
    return commands


def ci_actions(workflow: str) -> set[str]:
    """The actions the workflow uses, by name, without their pinned ref."""
    return set(re.findall(r"^\s*(?:- )?uses: ([^@\s]+)", workflow, flags=re.MULTILINE))


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


def workflows() -> list[str]:
    return [
        path.read_text() for path in sorted((REPO / ".github/workflows").glob("*.y*ml"))
    ]


def section4() -> set[str]:
    return section4_commands((REPO / "AGENTS.md").read_text())


def test_section4_runs_every_ci_step() -> None:
    commands = [
        command for workflow in workflows() for command in ci_commands(workflow)
    ]
    assert commands, "found no run steps in .github/workflows/"
    missing = [command for command in commands if command not in section4()]
    assert not missing, f"CI runs {missing} and §4 doesn't: {HOW_TO_FIX}"


def test_section4_has_a_line_for_every_ci_action() -> None:
    actions = set().union(*map(ci_actions, workflows()))
    unknown = sorted(actions - ACTIONS.keys())
    assert not unknown, f"CI uses {unknown}: say in ACTIONS which §4 line runs each"
    local = section4()
    missing = [
        prefix
        for action in sorted(actions)
        if (prefix := ACTIONS[action])
        and not any(line.startswith(prefix) for line in local)
    ]
    assert not missing, f"§4 has no line starting {missing}: {HOW_TO_FIX}"


def test_section4_runs_every_uv_command_constraints_names() -> None:
    named = constraints_commands((REPO / "CONSTRAINTS.md").read_text())
    assert named, "found no commands in CONSTRAINTS.md"
    missing = sorted(named - section4())
    assert not missing, f"CONSTRAINTS.md names {missing} and §4 doesn't run them"


def test_section4_type_checks_as_linux() -> None:
    assert "uv run mypy --platform linux" in section4(), (
        "§4 must run `uv run mypy --platform linux`, so that a green local run "
        "predicts CI's type check"
    )


WORKFLOW = """\
jobs:
  guardrails:
    steps:
      - uses: actions/checkout@3d3c42e5 # v7.0.1
      - name: Install gitleaks (checksum-verified)
        run: |
          curl -sSfL -o "$RUNNER_TEMP/gitleaks.tar.gz" https://example.invalid/g
          echo "abc  $RUNNER_TEMP/gitleaks.tar.gz" | sha256sum --check --strict
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
    assert ci_actions(WORKFLOW) == {"actions/checkout"}


def test_a_block_step_comes_back_whole() -> None:
    step = "      - name: Browser tests\n        run: |\n          sudo sysctl -w a=0\n          uv run pytest -m browser\n  next:\n"
    assert (
        ci_commands(WORKFLOW + step)[-1]
        == "sudo sysctl -w a=0\nuv run pytest -m browser"
    )


def test_a_plain_value_continued_on_the_next_line_is_folded() -> None:
    step = "      - name: Lint\n        run: uv run ruff check .\n          --select S\n      - name: Next\n"
    assert ci_commands(WORKFLOW + step)[-1] == "uv run ruff check . --select S"


@pytest.mark.parametrize(
    ("body", "command"),
    [
        ("run: uv run pytest -m browser\n", "uv run pytest -m browser"),
        (
            (
                'run: |\n          echo "abc  $RUNNER_TEMP/t" | sha256sum --check\n'
                "          uv run pytest -m browser\n"
            ),
            'echo "abc  $RUNNER_TEMP/t" | sha256sum --check\nuv run pytest -m browser',
        ),
    ],
    ids=["no-download", "gate-inside-a-download"],
)
def test_a_checksum_named_step_that_does_more_than_download_is_a_command(
    body: str, command: str
) -> None:
    step = f"      - name: Install browsers (checksum-verified)\n        {body}"
    assert ci_commands(WORKFLOW + step)[-1] == command


def test_planned_commands_are_not_named_yet() -> None:
    text = "## Thresholds\n| Lint | `uv run ruff check .` |\n\n## Planned\n| Test-first | `uv run coverage report --fail-under=100` |\n\n## Exceptions\n"
    assert constraints_commands(text) == {"uv run ruff check ."}
