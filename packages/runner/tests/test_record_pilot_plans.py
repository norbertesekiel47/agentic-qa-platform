import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from packages.runner.tests.conftest import Endpoint
from packages.runner.tests.record_cassettes import record
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


@pytest.fixture
def answering(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[..., Endpoint], tmp_path: Path
) -> Callable[[str], tuple[Path, Path]]:
    """A local provider that answers `text` once, with a fake key; returns the
    attempt directory and an empty cassette library."""

    def prepare(text: str) -> tuple[Path, Path]:
        answer = HELLO | {"content": [{"type": "text", "text": text}]}
        endpoint = serve(json.dumps(answer).encode(), {"request-id": "req_fake_01"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint.url)
        monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
        library = tmp_path / "library"
        library.mkdir()
        return tmp_path / "attempt", library

    return prepare


@pytest.mark.parametrize("pilot", EXPECTATIONS)
def test_each_pilot_case_records_the_real_spec(
    answering: Callable[[str], tuple[Path, Path]], pilot: str
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
    answering: Callable[[str], tuple[Path, Path]], pilot: str
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
    answering: Callable[[str], tuple[Path, Path]], answer: str
) -> None:
    attempt, library = answering(REJECTED[answer])

    with pytest.raises(ValueError, match="rejected"):
        record("plan_read-article", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    [cost] = receipt["cost_records"]
    assert (receipt["outcome"], cost["cost_usd"]) == ("rejected", "0.000048")
    assert cost["status"] == ("invalid" if answer == "malformed" else "ok")
    assert not (library / "plan_read-article.yaml").exists()
    assert (attempt / "plan_read-article.yaml").is_file()
