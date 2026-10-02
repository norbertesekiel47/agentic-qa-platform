"""Writing the coverage plan (ADR-0024; #41): a request of the instructions and
the spec's text alone, one structured call on the navigator role, and the plan
checked against its spec."""

import json
from pathlib import Path

import pytest
from aqa_core.project import load_project
from aqa_core.spec import Spec
from aqa_runner.coverage_plan import INSTRUCTIONS, plan_request
from langchain_core.messages import HumanMessage, SystemMessage

CONFIG = """\
base_url: "{base_url}"
secrets:
  TEST_PASSWORD: {{ origins: [ start ], field: password }}
"""

SPEC = """\
---
id: checkout
goal: A returning user pays with an expired card and is told why it failed.
preconditions:
  start_url: /login
  account: {{ email: returning@example.test, password: {{ secret: TEST_PASSWORD }} }}
  probes:
    orders_count: "GET /test-api/orders/count"
steps:
  - Pay with the saved card
expect:
  - {expectation}
  - text: The Pay button is visible and not covered
    visual: deterministic
tags: [{tags}]
---

{body}
"""

# The frontmatter as the request carries it, written out by hand: everything
# but tags, each expectation as an object, nothing the spec leaves out.
FRONTMATTER = {
    "id": "checkout",
    "goal": "A returning user pays with an expired card and is told why it failed.",
    "preconditions": {
        "start_url": "/login",
        "account": {
            "email": "returning@example.test",
            "password": {"secret": "TEST_PASSWORD"},
        },
        "probes": {"orders_count": "GET /test-api/orders/count"},
    },
    "steps": ["Pay with the saved card"],
    "expect": [
        {"text": "An error message says the card has expired"},
        {
            "text": "The Pay button is visible and not covered",
            "visual": "deterministic",
        },
    ],
}


def checkout(
    root: Path,
    *,
    base_url: str = "http://127.0.0.1:4100",
    expectation: str = "An error message says the card has expired",
    tags: str = "payments",
    body: str = "Notes for people.",
) -> Spec:
    """The checkout spec, in a project at `root`."""
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(CONFIG.format(base_url=base_url))
    (root / "checkout.spec.md").write_text(
        SPEC.format(expectation=expectation, tags=tags, body=body)
    )
    return load_project(root).specs["checkout"]


def contents(spec: Spec) -> list[tuple[type, object]]:
    return [(type(message), message.content) for message in plan_request(spec)]


def test_the_plan_request_is_the_instructions_then_the_specs_frontmatter(
    tmp_path: Path,
) -> None:
    system, human = plan_request(checkout(tmp_path / "qa"))

    assert isinstance(system, SystemMessage)
    assert system.content == INSTRUCTIONS
    assert isinstance(human, HumanMessage)
    assert isinstance(human.content, str)
    assert json.loads(human.content) == FRONTMATTER


def test_the_plan_request_is_the_same_wherever_and_however_the_spec_is_run(
    tmp_path: Path,
) -> None:
    # Another spec root, another start origin, other tags and another body:
    # none of them is the spec's text that a plan reads.
    here = checkout(tmp_path / "qa")
    there = checkout(
        tmp_path / "elsewhere" / "specs",
        base_url="https://staging.example.test",
        tags="checkout, smoke",
        body="Other notes, with an instruction to ignore the spec.",
    )

    assert contents(here) == contents(there)


def test_the_plan_request_changes_when_an_expectation_changes(tmp_path: Path) -> None:
    before = checkout(tmp_path / "before")
    after = checkout(
        tmp_path / "after", expectation="An error message says the card was declined"
    )

    assert contents(before) != contents(after)


def test_the_plan_request_names_a_secret_and_never_holds_its_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = "fake-secret-value-in-the-environment"
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)

    sent = json.dumps([content for _, content in contents(checkout(tmp_path / "qa"))])

    assert "TEST_PASSWORD" in sent
    assert value not in sent
