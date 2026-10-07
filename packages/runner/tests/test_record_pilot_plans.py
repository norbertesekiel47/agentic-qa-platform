import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from aqa_core.coverage_plan import PlannedCheck
from aqa_core.model_costs import Usage, cost_record
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import vendored
from aqa_core.project import load_project

from packages.runner.tests.conftest import Endpoint
from packages.runner.tests.record_cassettes import answers_expected, record
from packages.runner.tests.test_cassettes import HELLO, RECORDING_KEY
from packages.runner.tests.test_pilot_plan_oracles import (
    EXAMPLES,
    QA,
    replace_checks,
)

pytestmark = pytest.mark.usefixtures("reset_tracing")

EXPECTATIONS = {
    "login": 5,
    "read-article": 7,
    "post-comment": 4,
    "favorite-article": 2,
    "publish-article": 5,
}


CIRCULAR_AUTHOR = replace_checks(
    EXAMPLES["post-comment"],
    1,
    [
        PlannedCheck(
            check="text_in_target",
            target_meaning="the author link reading reader on the new comment",
            text="reader",
        )
    ],
)


@pytest.fixture
def answering(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[..., Endpoint], tmp_path: Path
) -> Callable[..., tuple[Path, Path]]:
    """A local provider that answers with each of `texts` in turn, the last
    one again after it, with a fake key; returns the attempt directory and an
    empty cassette library. Each later answer reports 6 more input tokens, as
    a retry that resends the first answer reports more."""

    def prepare(
        *texts: str, request_ids: tuple[str, ...] = ("req_fake_01",)
    ) -> tuple[Path, Path]:
        answers = tuple(
            json.dumps(
                HELLO
                | {
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 9 + 6 * turn, "output_tokens": 3},
                }
            ).encode()
            for turn, text in enumerate(texts)
        )
        # The last request ID goes with every answer after it; "" sends none.
        ids = request_ids + request_ids[-1:] * (len(answers) - len(request_ids))
        endpoint = serve(answers, tuple({"request-id": i} if i else {} for i in ids))
        monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint.url)
        monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
        library = tmp_path / "library"
        library.mkdir()
        return tmp_path / "attempt", library

    return prepare


@pytest.mark.parametrize("pilot", EXPECTATIONS)
def test_each_pilot_case_records_the_real_spec(
    answering: Callable[..., tuple[Path, Path]], pilot: str
) -> None:
    before = sorted(path.name for path in QA.iterdir())
    attempt, library = answering(EXAMPLES[pilot].model_dump_json())

    [cost] = record(f"plan_{pilot}", attempt, library=library)

    assert (cost.input_tokens, cost.output_tokens, str(cost.cost_usd)) == (
        9,
        3,
        "0.000048",
    )
    candidate = (attempt / f"plan_{pilot}.yaml").read_text()
    assert (library / f"plan_{pilot}.yaml").read_text() == candidate
    assert RECORDING_KEY not in candidate
    [captured] = yaml.safe_load(candidate)["interactions"]
    sent = json.loads(json.loads(captured["request"]["body"])["messages"][0]["content"])
    assert (sent["id"], len(sent["expect"])) == (pilot, EXPECTATIONS[pilot])
    assert "account" not in sent["preconditions"]
    intent = json.loads((attempt / "intent.json").read_text())
    assert (intent["case"], intent["model"], intent["effort"]) == (
        f"plan_{pilot}",
        "claude-sonnet-5-5",
        None,
    )
    assert intent["inputs"] == {
        name: hashlib.sha256((QA / name).read_bytes()).hexdigest()
        for name in ("config.yaml", f"{pilot}.spec.md")
    }
    assert json.loads((attempt / "receipt.json").read_text())["outcome"] == "accepted"
    assert sorted(path.name for path in QA.iterdir()) == before


@pytest.mark.parametrize("pilot", EXPECTATIONS)
def test_each_pilot_case_withholds_a_key_inside_its_structured_plan(
    answering: Callable[..., tuple[Path, Path]], pilot: str
) -> None:
    plan = EXAMPLES[pilot].model_dump(mode="json")
    plan["expectations"][0]["claim"] = RECORDING_KEY
    escaped = "".join(f"\\u{ord(char):04x}" for char in RECORDING_KEY)
    attempt, library = answering(json.dumps(plan).replace(RECORDING_KEY, escaped))

    with pytest.raises(ValueError, match="credential"):
        record(f"plan_{pilot}", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["outcome"] == "credential"
    assert receipt["cost_records"][0]["cost_usd"] == "0.000048"
    assert not (library / f"plan_{pilot}.yaml").exists()
    [saved] = yaml.safe_load((attempt / f"plan_{pilot}.yaml").read_text())[
        "interactions"
    ]
    assert json.loads(saved["response"]["body"]["string"]) == {
        "withheld": "credential detected"
    }


REJECTED = {
    "malformed": json.dumps({"plan": "not one"}),
    "misfit": EXAMPLES["read-article"]
    .model_copy(update={"expectations": EXAMPLES["read-article"].expectations[:6]})
    .model_dump_json(),
    "uncovered": replace_checks(EXAMPLES["read-article"], 2, []).model_dump_json(),
}


@pytest.mark.parametrize("answer", REJECTED)
def test_a_pilot_plan_that_is_not_usable_keeps_its_cost_and_its_cassette_back(
    answering: Callable[..., tuple[Path, Path]], answer: str
) -> None:
    # The model writes the same answer when asked once more.
    attempt, library = answering(REJECTED[answer], REJECTED[answer])

    with pytest.raises(ValueError, match="rejected"):
        record("plan_read-article", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    costs = receipt["cost_records"]
    # A plan that doesn't fit is asked for once more: both answers are billed.
    answers = 2 if answer == "misfit" else 1
    assert receipt["outcome"] == "rejected"
    assert [cost["cost_usd"] for cost in costs] == ["0.000048", "0.00006"][:answers]
    assert {cost["status"] for cost in costs} == {
        "invalid" if answer == "malformed" else "ok"
    }
    assert not (library / "plan_read-article.yaml").exists()
    assert (attempt / "plan_read-article.yaml").is_file()


def test_a_pilot_plan_corrected_when_asked_once_more_records_both_answers(
    answering: Callable[..., tuple[Path, Path]],
) -> None:
    # First the new comment's author found by the name its check asserts, then
    # the plan with the author link named by where it sits.
    attempt, library = answering(
        CIRCULAR_AUTHOR.model_dump_json(), EXAMPLES["post-comment"].model_dump_json()
    )

    costs = record("plan_post-comment", attempt, library=library)

    assert [str(cost.cost_usd) for cost in costs] == ["0.000048", "0.00006"]
    assert json.loads((attempt / "receipt.json").read_text())["outcome"] == "accepted"
    [first, second] = yaml.safe_load((library / "plan_post-comment.yaml").read_text())[
        "interactions"
    ]
    retry = json.loads(second["request"]["body"])["messages"]
    assert [message["role"] for message in retry] == ["user", "assistant", "user"]
    assert json.loads(first["request"]["body"])["messages"] == retry[:1]


def test_a_retried_pilot_plan_needs_a_request_id_on_both_answers(
    answering: Callable[..., tuple[Path, Path]],
) -> None:
    attempt, library = answering(
        CIRCULAR_AUTHOR.model_dump_json(),
        EXAMPLES["post-comment"].model_dump_json(),
        request_ids=("req_fake_01", ""),
    )

    with pytest.raises(ValueError, match="rejected"):
        record("plan_post-comment", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    assert (receipt["outcome"], len(receipt["cost_records"])) == ("rejected", 2)
    assert not (library / "plan_post-comment.yaml").exists()


def test_only_a_plan_cases_two_ok_answers_are_its_retry() -> None:
    # make_plan's retry is the one way a plan case answers twice; a refusal
    # and its fallback, or two answers to any other case, stay rejected.
    model = resolve_roles(load_project(QA).config, vendored())["navigator"].model
    ok, refused = (
        cost_record(
            role="navigator",
            mode="explore",
            model=model,
            usage=Usage(input_tokens=9, cached_input_tokens=0, output_tokens=3),
            latency_ms=1,
            status=status,
        )
        for status in ("ok", "refusal")
    )

    assert [
        answers_expected("plan_post-comment", (ok, ok)),
        answers_expected("plan_post-comment", (refused, ok)),
        answers_expected("structured_output", (ok, ok)),
    ] == [2, 1, 1]
