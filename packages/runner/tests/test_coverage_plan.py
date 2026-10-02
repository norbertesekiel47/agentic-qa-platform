"""Writing the coverage plan (ADR-0024; #41): a request of the instructions and
the spec's text alone, one structured call on the navigator role, and the plan
checked against its spec."""

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import load_project
from aqa_core.spec import Spec
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.coverage_plan import INSTRUCTIONS, Planned, make_plan, plan_request
from aqa_runner.model_router import ModelRouter
from langchain_core.messages import HumanMessage, SystemMessage

from packages.runner.tests.test_model_router import Factory, FakeClient, reply

pytestmark = pytest.mark.usefixtures("reset_tracing")

Cassette = Callable[..., AbstractContextManager[Any]]

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


# A plan for the checkout spec, as the model might write it.
PLAN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": "the payment error message",
                "claim": "says the card has expired",
                "checks": [{"check": "text_visible", "text": "card has expired"}],
            },
            {
                "expect_index": 1,
                "subject": "the payment step's submit button",
                "claim": "visible and not covered",
                "checks": [
                    {
                        "check": "visible_unoccluded",
                        "target_meaning": "the payment step's submit button",
                    }
                ],
            },
        ],
        "requires": [],
    }
)


def plan_with(spec: Spec, client: FakeClient) -> tuple[Planned, Factory]:
    """`spec`'s plan, with the navigator's model scripted by `client`."""
    factory = Factory(**{"claude-sonnet-5-5": client})
    config = load_project(spec.path.parent).config
    router = ModelRouter.from_config(config, factory)
    return asyncio.run(make_plan(router, spec)), factory


def test_the_plan_is_asked_of_the_navigator_role_in_explore_mode(
    tmp_path: Path,
) -> None:
    spec = checkout(tmp_path / "qa")
    client = FakeClient(reply(parsed=PLAN))

    planned, factory = plan_with(spec, client)

    assert factory.built == [("anthropic", "claude-sonnet-5-5", None)]
    assert client.calls == [(plan_request(spec), (), CoveragePlan)]
    assert (planned.plan, planned.misfits) == (PLAN, ())
    [record] = planned.routed.calls
    assert (record.role, record.mode, record.status) == ("navigator", "explore", "ok")


def test_a_refused_plan_comes_back_as_a_refusal_with_its_cost(tmp_path: Path) -> None:
    planned, _ = plan_with(checkout(tmp_path / "qa"), FakeClient(reply(refused=True)))

    assert planned.plan is None
    assert planned.routed.outcome == "refusal"
    assert [record.status for record in planned.routed.calls] == ["refusal"]


def test_a_plan_that_does_not_parse_comes_back_invalid_with_its_cost(
    tmp_path: Path,
) -> None:
    planned, _ = plan_with(checkout(tmp_path / "qa"), FakeClient(reply(parsed=None)))

    assert planned.plan is None
    assert planned.routed.outcome == "invalid"
    assert [record.status for record in planned.routed.calls] == ["invalid"]


def test_a_plan_that_does_not_fit_its_spec_comes_back_with_its_misfits(
    tmp_path: Path,
) -> None:
    # One entry for a spec of two expectations.
    short = CoveragePlan(expectations=PLAN.expectations[:1], requires=())

    planned, _ = plan_with(checkout(tmp_path / "qa"), FakeClient(reply(parsed=short)))

    assert planned.plan == short
    assert len(planned.misfits) == 1
    assert "2 expectations" in planned.misfits[0]


def plan_through_the_adapter(spec: Spec) -> Planned:
    """`spec`'s plan, through the router and the real Anthropic adapter."""
    config = load_project(spec.path.parent).config
    return asyncio.run(
        make_plan(ModelRouter.from_config(config, AnthropicClient), spec)
    )


def test_a_plan_through_the_real_adapter_is_parsed_and_costed(
    tmp_path: Path, cassette: Cassette
) -> None:
    spec = checkout(tmp_path / "qa")

    with cassette("coverage_plan") as recording:
        planned = plan_through_the_adapter(spec)

    # What was sent: the request alone, structured output, and no tools, so no
    # tool choice at all.
    [sent] = recording.sent
    _, human = plan_request(spec)
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["system"] == INSTRUCTIONS
    assert sent["messages"] == [{"role": "user", "content": human.content}]
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert not {"tools", "tool_choice"} & sent.keys()
    # What came back, read against the recorded answer itself, so a real
    # recording passes too (TESTING §4).
    [answer] = recording.responses
    assert planned.plan == CoveragePlan.model_validate_json(
        answer["content"][0]["text"]
    )
    assert planned.misfits == ()
    [record] = planned.routed.calls
    assert (record.role, record.mode, record.status) == ("navigator", "explore", "ok")
    assert (record.input_tokens, record.output_tokens) == (
        answer["usage"]["input_tokens"],
        answer["usage"]["output_tokens"],
    )


def test_the_plan_cassette_replays_for_the_spec_run_from_anywhere(
    tmp_path: Path, cassette: Cassette
) -> None:
    # The cassette is keyed by the request's hash, so the same spec explored
    # from another spec root, at another start origin, replays the same answer.
    places = [
        (tmp_path / "qa", "http://127.0.0.1:4100"),
        (tmp_path / "elsewhere" / "specs", "https://staging.example.test"),
    ]
    plans = []
    for root, base_url in places:
        spec = checkout(root, base_url=base_url, body="Other notes.")
        with cassette("coverage_plan", replay_only=True):
            plans.append(plan_through_the_adapter(spec).plan)

    assert plans[0] is not None
    assert plans[0] == plans[1]
