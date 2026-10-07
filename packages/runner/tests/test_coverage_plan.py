"""Writing the coverage plan (ADR-0024; #41): a request of the instructions and
the spec's text alone, one structured call on the navigator role, and the plan
checked against its spec."""

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, get_args

import pytest
from aqa_core.coverage_plan import CheckType, CoveragePlan, M2Check, PlannedCheck
from aqa_core.project import load_project
from aqa_core.spec import Spec
from aqa_runner import text_search
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.coverage_plan import INSTRUCTIONS, Planned, make_plan, plan_request
from aqa_runner.model_router import ModelCallError, ModelRouter
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

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
  start_url: {start_url}
  account: {account}
  reset: {{ http: "{reset}" }}
  probes:
    orders_count: "{probe}"
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
# but tags, the account and the reset hook, each expectation as an object,
# nothing the spec leaves out.
FRONTMATTER = {
    "id": "checkout",
    "goal": "A returning user pays with an expired card and is told why it failed.",
    "preconditions": {
        "start_url": "/login",
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


# What each place in CONFIG and SPEC holds unless a test says otherwise.
DEFAULTS = {
    "base_url": "http://127.0.0.1:4100",
    "expectation": "An error message says the card has expired",
    "tags": "payments",
    "body": "Notes for people.",
    "account": "{ email: returning@example.test, password: { secret: TEST_PASSWORD } }",
    "reset": "POST /test-api/reset?fixture=expired-card",
    "start_url": "/login",
    "probe": "GET /test-api/orders/count",
}


def checkout(root: Path, **overrides: str) -> Spec:
    """The checkout spec, in a project at `root`, with `overrides` of
    DEFAULTS."""
    values = DEFAULTS | overrides
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(CONFIG.format(**values))
    (root / "checkout.spec.md").write_text(SPEC.format(**values))
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


def test_the_plan_request_leaves_out_the_account_and_the_reset_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A plan needs neither, and either may hold a credential the spec writes
    # out: a literal password, or a token in the hook's URL (AGENTS.md §6).
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "fake-secret-in-the-environment")
    spec = checkout(
        tmp_path / "qa",
        account="{ email: returning@example.test, password: fake-literal-password }",
        reset="POST /test-api/reset?token=fake-reset-token",
    )

    sent = json.dumps([content for _, content in contents(spec)])

    for left_out in (
        "fake-literal-password",
        "fake-reset-token",
        "returning@example.test",
        "fake-secret-in-the-environment",
    ):
        assert left_out not in sent
    # The rest of the preconditions are still there.
    assert "GET /test-api/orders/count" in sent


def test_the_plan_request_leaves_out_the_query_of_each_url(tmp_path: Path) -> None:
    # A query can carry a token the spec writes out; the path says what the
    # page or the probe is.
    spec = checkout(
        tmp_path / "qa",
        start_url="/login?token=fake-start-token#fake-fragment",
        probe="GET /test-api/orders/count?api_key=fake-probe-token",
    )

    _, human = plan_request(spec)

    preconditions = json.loads(str(human.content))["preconditions"]
    assert preconditions == {
        "start_url": "/login",
        "probes": {"orders_count": "GET /test-api/orders/count"},
    }


# The benchmark pilot's spec root (bench/apps/conduit/qa).
PILOT = Path(__file__).resolve().parents[3] / "bench" / "apps" / "conduit" / "qa"


def test_a_spec_that_declares_no_probe_sends_none() -> None:
    _, human = plan_request(load_project(PILOT).specs["read-article"])

    assert json.loads(str(human.content))["preconditions"] == {"start_url": "/"}


def test_the_plan_request_keeps_the_specs_own_characters(tmp_path: Path) -> None:
    # Written as the author wrote it, not as \u escapes.
    spec = checkout(
        tmp_path / "qa", expectation="Le paiement a échoué, la carte a expiré"
    )

    _, human = plan_request(spec)

    assert "Le paiement a échoué, la carte a expiré" in str(human.content)


def test_the_instructions_name_every_check_type_and_every_need() -> None:
    # The prompt states what the response format allows; a check type added
    # to the plan model must be added here too.
    for name in (*get_args(CheckType), *get_args(M2Check)):
        assert name in INSTRUCTIONS, name


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
    # One entry for a spec of two expectations, written again when asked.
    short = CoveragePlan(expectations=PLAN.expectations[:1], requires=())
    client = FakeClient(reply(parsed=short), reply(parsed=short))

    planned, _ = plan_with(checkout(tmp_path / "qa"), client)

    assert planned.plan == short
    assert len(planned.misfits) == 1
    assert "2 expectations" in planned.misfits[0]


SHORT = CoveragePlan(expectations=PLAN.expectations[:1], requires=())
# What the one retry says after the first answer, for SHORT.
SHORT_CORRECTION = (
    "That plan can't be used:\n"
    "- the plan covers expectations [0], but the spec has 2 expectations: it "
    "covers each once, in order, from 0 to 1\n"
    "Write the whole plan again: fix each of these, and keep the rest as it is."
)


def test_a_plan_that_cannot_be_used_is_asked_for_once_more_with_its_reasons(
    tmp_path: Path,
) -> None:
    spec = checkout(tmp_path / "qa")
    client = FakeClient(reply(parsed=SHORT), reply(parsed=PLAN))

    planned, _ = plan_with(spec, client)

    assert (planned.plan, planned.misfits) == (PLAN, ())
    [first, second] = client.calls
    assert first == (plan_request(spec), (), CoveragePlan)
    assert second == (
        [
            *plan_request(spec),
            AIMessage(content=SHORT.model_dump_json(exclude_none=True)),
            HumanMessage(content=SHORT_CORRECTION),
        ],
        (),
        CoveragePlan,
    )
    assert [record.status for record in planned.routed.calls] == ["ok", "ok"]


def test_the_second_answer_is_final_and_both_are_costed(tmp_path: Path) -> None:
    # Asked once more, never twice.
    client = FakeClient(reply(parsed=SHORT), reply(parsed=SHORT), reply(parsed=PLAN))

    planned, _ = plan_with(checkout(tmp_path / "qa"), client)

    assert planned.plan == SHORT
    assert len(client.calls) == 2
    assert len(planned.routed.calls) == 2


def test_a_retry_that_fails_keeps_the_first_answers_cost(tmp_path: Path) -> None:
    # The first answer was billed, so its record survives the retry's failure,
    # as a refusal's survives a failed fallback.
    failure = ValueError("no second answer")
    client = FakeClient(reply(parsed=SHORT), failure)

    with pytest.raises(ModelCallError) as raised:
        plan_with(checkout(tmp_path / "qa"), client)

    assert [record.status for record in raised.value.records] == ["ok"]
    assert raised.value.__cause__ is failure


def with_first_check(check: dict[str, str]) -> CoveragePlan:
    """PLAN, with expectation 0 established by `check` alone."""
    first = PLAN.expectations[0].model_copy(
        update={"checks": (PlannedCheck.model_validate(check),)}
    )
    return PLAN.model_copy(update={"expectations": (first, *PLAN.expectations[1:])})


def test_a_target_that_holds_its_checks_text_does_not_fit(tmp_path: Path) -> None:
    # Finding the element by the text the check must verify is circular: a
    # wrong text would find no element, drift instead of a failure (ADR-0025).
    circular = with_first_check(
        {
            "check": "text_in_target",
            "target_meaning": "the error message that says the card has expired",
            "text": "Card has expired",
        }
    )
    # The model writes the same target when asked again.
    client = FakeClient(reply(parsed=circular), reply(parsed=circular))

    planned, _ = plan_with(checkout(tmp_path / "qa"), client)

    assert planned.plan == circular
    assert planned.misfits == (
        (
            'expect[0]: the target "the error message that says the card has '
            'expired" holds "Card has expired", the text its check asserts: a '
            "target says what the element is for and where it sits, never what "
            "it says"
        ),
    )


def test_a_target_its_checks_pattern_matches_does_not_fit(tmp_path: Path) -> None:
    circular = with_first_check(
        {
            "check": "text_in_target",
            "target_meaning": "the message saying the card has expired",
            "pattern": r"(?i)card has expired|card is no longer valid",
        }
    )
    client = FakeClient(reply(parsed=circular), reply(parsed=circular))

    planned, _ = plan_with(checkout(tmp_path / "qa"), client)

    assert planned.misfits == (
        (
            'expect[0]: the target "the message saying the card has expired" '
            'matches "(?i)card has expired|card is no longer valid", the pattern '
            "its check asserts: a target says what the element is for and where "
            "it sits, never what it says"
        ),
    )


def test_a_target_that_names_no_asserted_text_fits(tmp_path: Path) -> None:
    # Whole words, as the check itself reads text: "Pay" is not in "payment".
    plan = with_first_check(
        {
            "check": "text_in_target",
            "target_meaning": "the payment step's error message under the card form",
            "text": "Pay",
        }
    )

    planned, _ = plan_with(checkout(tmp_path / "qa"), FakeClient(reply(parsed=plan)))

    assert (planned.plan, planned.misfits) == (plan, ())


def test_a_pattern_that_cannot_search_its_target_in_time_does_not_fit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The pattern is the model's, so it is searched in a child bounded in time,
    # as every pattern is (ADR-0024's bounded text searches).
    monkeypatch.setattr(text_search, "SEARCH_SECONDS", 0.5)
    slow = with_first_check(
        {
            "check": "text_in_target",
            "target_meaning": "a" * 32 + "b",
            "pattern": "(a+)+$",
        }
    )
    client = FakeClient(reply(parsed=slow), reply(parsed=slow))

    planned, _ = plan_with(checkout(tmp_path / "qa"), client)

    assert planned.misfits == (
        (
            'expect[0]: the pattern "(a+)+$" could not be searched in its own '
            "target within 0.5 s"
        ),
    )


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
