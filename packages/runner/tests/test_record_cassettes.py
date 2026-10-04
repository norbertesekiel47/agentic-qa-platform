"""Recording attempts retain billed failures before accepting a cassette."""

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml
from aqa_core.model_costs import CostRecord

from packages.runner.tests.conftest import CASSETTES, Endpoint, _canonical
from packages.runner.tests.record_cassettes import record
from packages.runner.tests.test_cassettes import HELLO, RECORDING_KEY
from packages.runner.tests.test_coverage_plan import PLAN

pytestmark = pytest.mark.usefixtures("reset_tracing")


def test_recording_keeps_costs_before_a_plan_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    serve: Callable[..., Endpoint],
    tmp_path: Path,
) -> None:
    short = PLAN.model_dump(mode="json")
    short["expectations"] = short["expectations"][:1]
    answer = HELLO | {"content": [{"type": "text", "text": json.dumps(short)}]}
    endpoint = serve(json.dumps(answer).encode(), {"request-id": "req_fake_01"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint.url)
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    library = tmp_path / "library"
    library.mkdir()
    previous = library / "coverage_plan.yaml"
    previous.write_text("previous cassette\n")
    attempt = tmp_path / "attempt"

    with pytest.raises(ValueError, match="rejected"):
        record("coverage_plan", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    [cost] = receipt["cost_records"]
    assert (cost["input_tokens"], cost["output_tokens"], cost["status"]) == (9, 3, "ok")
    assert cost["cost_usd"] == "0.000048"
    assert receipt["outcome"] == "rejected"
    assert previous.read_text() == "previous cassette\n"
    assert (attempt / "coverage_plan.yaml").is_file()


@pytest.fixture
def capture_paths(
    monkeypatch: pytest.MonkeyPatch,
    serve: Callable[..., Endpoint],
    tmp_path: Path,
) -> Callable[[Mapping[str, object]], tuple[Path, Path]]:
    def prepare(answer: Mapping[str, object]) -> tuple[Path, Path]:
        endpoint = serve(json.dumps(answer).encode(), {"request-id": "req_fake_01"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint.url)
        monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
        library = tmp_path / "library"
        library.mkdir()
        (library / "coverage_plan.yaml").write_text("previous cassette\n")
        return tmp_path / "attempt", library

    return prepare


@pytest.mark.parametrize(
    "credential",
    [RECORDING_KEY, "sk-ant-fake-response-credential-with-many-characters"],
)
def test_recording_withholds_credentials_before_any_artifact_is_saved(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]], credential: str
) -> None:
    plan = PLAN.model_dump(mode="json")
    plan["expectations"][0]["claim"] = credential
    answer = HELLO | {"content": [{"type": "text", "text": json.dumps(plan)}]}
    attempt, library = capture_paths(answer)

    with pytest.raises(ValueError, match="credential"):
        record("coverage_plan", attempt, library=library)

    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"
    for path in attempt.rglob("*"):
        if path.is_file():
            assert credential not in path.read_text(), path.name
    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["outcome"] == "credential"
    assert receipt["cost_records"][0]["cost_usd"] == "0.000048"


@pytest.mark.parametrize("stop_reason", ["refusal", "end_turn", "max_tokens"])
def test_recording_keeps_billed_invalid_and_refused_answers(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]], stop_reason: str
) -> None:
    attempt, library = capture_paths(HELLO | {"stop_reason": stop_reason})
    with pytest.raises(ValueError, match="rejected"):
        record("coverage_plan", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    [cost] = receipt["cost_records"]
    assert cost["status"] == ("refusal" if stop_reason == "refusal" else "invalid")
    assert cost["cost_usd"] == "0.000048"
    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"


def test_recording_reports_missing_usage_without_inventing_zero_cost(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]],
) -> None:
    attempt, library = capture_paths(
        {key: value for key, value in HELLO.items() if key != "usage"}
    )
    with pytest.raises(ValueError, match="usage"):
        record("coverage_plan", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["outcome"] == "missing_usage"
    assert receipt["cost_records"] == []
    assert receipt["unpriced_responses"] == [0]
    assert receipt["responses"][0]["request_id"] == "req_fake_01"
    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"


def test_recording_accounts_for_an_sdk_retry_response_with_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = [
        (
            500,
            {
                "type": "error",
                "error": {"type": "api_error", "message": "fake failure"},
                "usage": HELLO["usage"],
            },
        ),
        (200, HELLO | {"content": [{"type": "text", "text": PLAN.model_dump_json()}]}),
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            status, body = answers.pop(0)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("request-id", f"req_fake_{status}")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    library = tmp_path / "library"
    library.mkdir()
    attempt = tmp_path / "attempt"
    try:
        with pytest.raises(ValueError, match="rejected"):
            record("coverage_plan", attempt, library=library)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    receipt = json.loads((attempt / "receipt.json").read_text())
    costs = [
        CostRecord.model_validate_json(json.dumps(value))
        for value in receipt["cost_records"]
    ]
    assert not (library / "coverage_plan.yaml").exists()
    assert [str(cost.cost_usd) for cost in costs] == ["0.000048", "0.000048"]
    assert [cost.status for cost in costs] == ["invalid", "ok"]
    receipt = json.loads((attempt / "receipt.json").read_text())
    assert [item["request_id"] for item in receipt["responses"]] == [
        "req_fake_500",
        "req_fake_200",
    ]
    assert receipt["unpriced_responses"] == []


@pytest.mark.parametrize(
    "name", ["coverage_plan", "tools", "structured_output", "plain_with_effort"]
)
def test_named_cases_match_replay_requests_and_keep_attempts(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]], name: str
) -> None:
    [original] = yaml.safe_load((CASSETTES / f"{name}.yaml").read_text())[
        "interactions"
    ]
    attempt, library = capture_paths(json.loads(original["response"]["body"]["string"]))
    costs = record(name, attempt, library=library)

    assert len(costs) == 1
    candidate = (attempt / f"{name}.yaml").read_text()
    assert (library / f"{name}.yaml").read_text() == candidate
    [captured] = yaml.safe_load(candidate)["interactions"]
    assert json.loads(captured["request"]["body"]) == json.loads(
        original["request"]["body"]
    )
    assert captured["request"]["uri"] == "https://api.anthropic.com/v1/messages"
    assert RECORDING_KEY not in candidate
    receipt = (attempt / "receipt.json").read_text()
    assert json.loads(receipt)["case"] == name
    intent = json.loads((attempt / "intent.json").read_text())
    assert intent["model"] == json.loads(original["request"]["body"])["model"]
    assert intent["effort"] == ("high" if name == "plain_with_effort" else None)
    assert len(intent["source_sha"]) == 40
    assert intent["started_at"] < json.loads(receipt)["finished_at"]
    hashes = json.loads((attempt / "request-hashes.json").read_text())
    assert hashes == [
        hashlib.sha256(_canonical(captured["request"]["body"]).encode()).hexdigest()
    ]
    with pytest.raises(FileExistsError):
        record(name, attempt, library=library)
    assert (attempt / "receipt.json").read_text() == receipt


def test_success_without_a_request_id_is_not_promoted(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[..., Endpoint], tmp_path: Path
) -> None:
    answer = HELLO | {"content": [{"type": "text", "text": PLAN.model_dump_json()}]}
    endpoint = serve(json.dumps(answer).encode())
    monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint.url)
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    attempt = tmp_path / "attempt"
    with pytest.raises(ValueError, match="rejected"):
        record("coverage_plan", attempt, library=tmp_path)
    assert not (tmp_path / "coverage_plan.yaml").exists()
    assert json.loads((attempt / "receipt.json").read_text())["cost_records"]


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1, "output_tokens": 3},
        {"input_tokens": 9, "output_tokens": 3, "cache_read_input_tokens": "bad"},
        {"input_tokens": 9, "output_tokens": 3, "cache_creation_input_tokens": True},
    ],
)
def test_unusable_usage_is_retained_as_unpriced(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]],
    usage: dict[str, object],
) -> None:
    attempt, library = capture_paths(HELLO | {"usage": usage})
    with pytest.raises(ValueError, match="usage"):
        record("coverage_plan", attempt, library=library)
    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["cost_records"] == []
    assert receipt["unpriced_responses"] == [0]
    assert receipt["responses"][0]["request_id"] == "req_fake_01"
    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"


def test_a_missing_key_does_not_create_an_attempt_or_replace_a_cassette(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="needs ANTHROPIC_API_KEY"):
        record("coverage_plan", tmp_path / "attempt", library=tmp_path)
    assert list(tmp_path.iterdir()) == []
