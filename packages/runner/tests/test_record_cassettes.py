import asyncio
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import traceback
import warnings
from collections.abc import Callable, Mapping
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from aqa_core.model_costs import CostRecord
from aqa_core.spec import Spec
from aqa_runner.model_router import ModelCallError, ModelRouter, Routed

from packages.runner.tests import record_cassettes as recorder
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


@pytest.fixture
def wire_capture(
    monkeypatch: pytest.MonkeyPatch,
    serve: Callable[..., Endpoint],
    tmp_path: Path,
) -> Callable[[bytes], tuple[Path, Path]]:
    def prepare(body: bytes) -> tuple[Path, Path]:
        endpoint = serve(body, {"request-id": "req_fake_wire"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint.url)
        monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
        library = tmp_path / "library"
        library.mkdir()
        for case in (
            "coverage_plan",
            "tools",
            "structured_output",
            "plain_with_effort",
        ):
            (library / f"{case}.yaml").write_text("previous cassette\n")
        return tmp_path / "attempt", library

    return prepare


@pytest.mark.parametrize(
    "credential",
    [RECORDING_KEY, "sk-ant-fake-response-credential-with-many-characters"],
)
@pytest.mark.parametrize(
    "form",
    ["wire", "structured", "fenced", "partial", "split", "wire_key", "tool_input"],
)
def test_recording_withholds_decoded_credentials(
    wire_capture: Callable[[bytes], tuple[Path, Path]], credential: str, form: str
) -> None:
    encoded = "".join(f"\\u{ord(char):04x}" for char in credential)
    text = json.dumps({"ok": True, "reason": credential}).replace(credential, encoded)
    case = "structured_output"
    if form == "fenced":
        text = f"```json\n{text}\n```"
    if form == "partial":
        text = text[:-1]
    if form in {"wire", "split"}:
        text, case = credential, "plain_with_effort"
    content = [{"type": "text", "text": text}]
    if form == "split":
        content = [{"type": "text", "text": part} for part in (text[:9], text[9:])]
    answer: dict[str, Any] = HELLO | {"content": content}
    if form == "wire_key":
        answer[credential] = "fake metadata"
        case = "plain_with_effort"
    if form == "tool_input":
        answer["content"] = [
            {
                "type": "tool_use",
                "id": "fake_tool",
                "name": "click",
                "input": {"ref": credential},
            }
        ]
        case = "tools"
    body = json.dumps(answer)
    if form in {"wire", "wire_key", "tool_input"}:
        body = body.replace(credential, encoded)
    attempt, library = wire_capture(body.encode())

    with pytest.raises(ValueError, match="credential"):
        record(case, attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["outcome"] == "credential"
    assert receipt["cost_records"][0]["cost_usd"] == "0.000048"
    assert receipt["unpriced_responses"] == []
    assert (library / f"{case}.yaml").read_text() == "previous cassette\n"
    [saved] = yaml.safe_load((attempt / f"{case}.yaml").read_text())["interactions"]
    assert json.loads(saved["response"]["body"]["string"]) == {
        "withheld": "credential detected"
    }
    assert saved["response"]["headers"] == {}


@pytest.mark.parametrize("shape", ["null", "missing", "invalid_json"])
def test_uninspectable_answers_keep_their_priced_receipt(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]], shape: str
) -> None:
    answer = HELLO | {"content": None}
    if shape == "missing":
        answer.pop("content")
    if shape == "invalid_json":
        answer["content"] = [{"type": "text", "text": "not structured JSON"}]
    attempt, library = capture_paths(answer)

    with pytest.raises(ValueError, match="rejected"):
        record("coverage_plan", attempt, library=library)

    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["outcome"] == "uninspectable"
    assert receipt["cost_records"][0]["cost_usd"] == "0.000048"
    assert receipt["unpriced_responses"] == []
    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"


def fail_capture_writes(
    monkeypatch: pytest.MonkeyPatch, attempt: Path, failure: str
) -> None:
    original_write = Path.write_text

    def write(path: Path, text: str, *args: Any, **kwargs: Any) -> int:
        if path.name == "receipt.json" and failure in {"receipt", "both"}:
            raise OSError(RECORDING_KEY)
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    original_open = Path.open

    def open_path(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == attempt / "coverage_plan.yaml":
            if failure in {"cassette", "both"}:
                raise OSError(RECORDING_KEY)
            if failure == "warning":
                warnings.warn(RECORDING_KEY, UserWarning, stacklevel=2)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)


@pytest.mark.parametrize(
    "primary",
    [
        "none",
        "type",
        "keyboard",
        "cancel",
        "exit_none",
        "exit_zero",
        "exit_seven",
        "exit_text",
    ],
)
@pytest.mark.parametrize("failure", ["none", "cassette", "receipt", "both", "warning"])
def test_capture_failure_boundaries_restore_and_account(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    primary: str,
    failure: str,
) -> None:
    attempt, library = capture_paths(
        HELLO | {"content": [{"type": "text", "text": PLAN.model_dump_json()}]}
    )
    original_ask = recorder.ask
    observed: list[CostRecord] = []

    async def interrupted(
        name: str, router: ModelRouter, spec: Spec
    ) -> tuple[Routed, bool]:
        routed, accepted = await original_ask(name, router, spec)
        observed.extend(routed.calls)
        errors = {
            "type": TypeError(RECORDING_KEY),
            "keyboard": KeyboardInterrupt(RECORDING_KEY),
            "cancel": asyncio.CancelledError(RECORDING_KEY),
            "exit_none": SystemExit(),
            "exit_zero": SystemExit(0),
            "exit_seven": SystemExit(7),
            "exit_text": SystemExit(RECORDING_KEY),
        }
        if primary != "none":
            raise errors[primary]
        return routed, accepted

    monkeypatch.setattr(recorder, "ask", interrupted)
    fail_capture_writes(monkeypatch, attempt, failure)
    transports = (
        http.client.HTTPConnection,
        httpx.HTTPTransport.handle_request,
        httpx.AsyncHTTPTransport.handle_async_request,
    )
    filters = list(warnings.filters)
    expected: type[BaseException] = ValueError
    if primary == "keyboard":
        expected = KeyboardInterrupt
    elif primary == "cancel":
        expected = asyncio.CancelledError
    elif primary.startswith("exit"):
        expected = SystemExit
    if primary == failure == "none":
        assert record("coverage_plan", attempt, library=library) == tuple(observed)
        assert (library / "coverage_plan.yaml").read_bytes() == (
            attempt / "coverage_plan.yaml"
        ).read_bytes()
    else:
        with pytest.raises(expected) as raised:
            record("coverage_plan", attempt, library=library)
        error = raised.value
        assert error.__traceback__ is not None
        assert error.__suppress_context__
        assert RECORDING_KEY not in "".join(traceback.format_exception(error))
        if isinstance(error, SystemExit):
            assert error.code == 1
        if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
            assert error.args == ()
        state = vars(error)["capture_state"]
        assert state["primary_failure"] == {
            "none": None,
            "type": "call_error",
            "keyboard": "KeyboardInterrupt",
            "cancel": "CancelledError",
        }.get(primary, "SystemExit")
        assert state["receipt_attempted"]
        assert state["receipt_written"] == (failure not in {"receipt", "both"})
        expected_failures = {
            "none": [],
            "cassette": ["cassette_write"],
            "receipt": ["receipt_write"],
            "both": ["cassette_write", "receipt_write"],
            "warning": ["cassette_write"],
        }[failure]
        assert state["finalization_failures"] == expected_failures
        assert [item["cost_usd"] for item in state["cost_records"]] == ["0.000048"]
        assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"
    assert transports == (
        http.client.HTTPConnection,
        httpx.HTTPTransport.handle_request,
        httpx.AsyncHTTPTransport.handle_async_request,
    )
    assert filters == warnings.filters
    if failure not in {"receipt", "both"}:
        receipt = json.loads((attempt / "receipt.json").read_text())
        assert [item["cost_usd"] for item in receipt["cost_records"]] == ["0.000048"]
        assert receipt["unpriced_responses"] == []


@pytest.mark.parametrize(
    "phase", ["hash", "promotion_write", "promotion_replace", "model_call"]
)
def test_returned_records_survive_later_failures(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    attempt, library = capture_paths(
        HELLO | {"content": [{"type": "text", "text": PLAN.model_dump_json()}]}
    )
    actual: list[CostRecord] = []
    original = recorder.ask

    async def ask(name: str, router: ModelRouter, spec: Spec) -> tuple[Routed, bool]:
        routed, accepted = await original(name, router, spec)
        actual.extend(routed.calls)
        if phase == "model_call":
            raise ModelCallError(routed.calls) from TypeError(RECORDING_KEY)
        return routed, accepted

    monkeypatch.setattr(recorder, "ask", ask)
    original_write = Path.write_text
    original_bytes = Path.write_bytes
    original_replace = Path.replace

    def text(path: Path, value: str, *args: Any, **kwargs: Any) -> int:
        if phase == "hash" and path.name == "request-hashes.json":
            raise OSError(RECORDING_KEY)
        return original_write(path, value, *args, **kwargs)

    def write(path: Path, value: bytes) -> int:
        if phase == "promotion_write" and path.name == "promotion.yaml":
            raise OSError(RECORDING_KEY)
        return original_bytes(path, value)

    def replace(path: Path, target: Path) -> Path:
        if phase == "promotion_replace" and path.name == "promotion.yaml":
            raise OSError(RECORDING_KEY)
        return original_replace(path, target)

    monkeypatch.setattr(Path, "write_text", text)
    monkeypatch.setattr(Path, "write_bytes", write)
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(ValueError, match="rejected") as raised:
        record("coverage_plan", attempt, library=library)
    assert RECORDING_KEY not in "".join(traceback.format_exception(raised.value))
    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["router_records"] == [
        item.model_dump(mode="json") for item in actual
    ]
    assert receipt["cost_records"] == receipt["router_records"]
    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"
    assert receipt["outcome"] == (
        "accepted" if phase.startswith("promotion") else "error"
    )


@pytest.mark.parametrize("failure", ["sdk", "finalization", "promotion"])
def test_cli_default_warnings_never_print_response_credentials(
    serve: Callable[..., Endpoint],
    tmp_path: Path,
    failure: str,
) -> None:
    answer = HELLO | {
        "content": RECORDING_KEY
        if failure == "sdk"
        else [{"type": "text", "text": PLAN.model_dump_json()}]
    }
    endpoint = serve(json.dumps(answer).encode(), {"request-id": "req_fake_cli"})
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    (workspace / ".gitignore").write_text(".scratch/\n")
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fake@example.test",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        cwd=workspace,
        check=True,
    )
    library = tmp_path / "library"
    library.mkdir()
    (library / "coverage_plan.yaml").write_text("previous cassette\n")
    script = tmp_path / "invoke.py"
    script.write_text("""import socket, sys, warnings
from pathlib import Path
from packages.runner.tests import record_cassettes as recorder
from aqa_runner import anthropic_client
from packages.runner.tests.test_cassettes import RECORDING_KEY
workspace, endpoint, library, failure = sys.argv[1:]
original_connect = socket.socket.connect
def local_only(sock, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
        raise AssertionError('nonlocal connection forbidden')
    return original_connect(sock, address)
socket.socket.connect = local_only
recorder.ROOT = Path(workspace)
anthropic_client.API_URL = endpoint
original_record = recorder.record
def record(name, attempt):
    return original_record(name, attempt, library=Path(library))
recorder.record = record
original_open = Path.open
def open_path(path, *args, **kwargs):
    if path.name == 'coverage_plan.yaml' and path.parent.name == 'attempt' and failure == 'finalization':
        warnings.warn(RECORDING_KEY, UserWarning)
    return original_open(path, *args, **kwargs)
Path.open = open_path
original_replace = Path.replace
def replace(path, target):
    if path.name == 'promotion.yaml' and failure == 'promotion':
        raise OSError(RECORDING_KEY)
    return original_replace(path, target)
Path.replace = replace
sys.argv = [sys.argv[0], 'coverage_plan', str(recorder.ROOT / '.scratch' / 'attempt')]
recorder.main()
""")
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(recorder.ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ANTHROPIC_API_KEY": RECORDING_KEY,
    }
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(workspace),
            endpoint.url,
            str(library),
            failure,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert RECORDING_KEY not in result.stdout + result.stderr
    assert "Recording stopped" in result.stderr
    receipt = json.loads((workspace / ".scratch/attempt/receipt.json").read_text())
    assert receipt["cost_records"][0]["cost_usd"] == "0.000048"
    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"


@pytest.mark.parametrize("mismatch", [False, True])
def test_every_retry_response_is_priced_after_later_formatting_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mismatch: bool,
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
        (
            200,
            HELLO
            | {
                "content": [{"type": "text", "text": PLAN.model_dump_json()}]
                if mismatch
                else None
            },
        ),
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            status, answer = answers.pop(0)
            body = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("request-id", f"req_fake_{status}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", RECORDING_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
    original = recorder.ask

    async def ask(name: str, router: ModelRouter, spec: Spec) -> tuple[Routed, bool]:
        routed, accepted = await original(name, router, spec)
        if mismatch:
            calls = tuple(
                item.model_copy(update={"input_tokens": 100}) for item in routed.calls
            )
            routed = replace(routed, calls=calls)
        return routed, accepted

    monkeypatch.setattr(recorder, "ask", ask)
    attempt = tmp_path / "attempt"
    library = tmp_path / "library"
    library.mkdir()
    old = library / "coverage_plan.yaml"
    old.write_text("previous cassette\n")
    try:
        with pytest.raises(ValueError, match="rejected"):
            record("coverage_plan", attempt, library=library)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    receipt = json.loads((attempt / "receipt.json").read_text())
    assert [item["cost_usd"] for item in receipt["cost_records"]] == [
        "0.000048",
        "0.000048",
    ]
    assert [item["input_tokens"] for item in receipt["cost_records"]] == [9, 9]
    assert receipt["unpriced_responses"] == []
    assert [item["request_id"] for item in receipt["responses"]] == [
        "req_fake_500",
        "req_fake_200",
    ]
    assert (
        receipt["router_records"] == []
        if not mismatch
        else receipt["router_records"][0]["input_tokens"] == 100
    )
    assert ("accounting_error" in receipt["finalization_failures"]) == mismatch
    assert old.read_text() == "previous cassette\n"


def test_no_response_is_distinct_from_an_unpriced_observation(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, library = capture_paths(HELLO)

    async def fail(
        _name: str, _router: ModelRouter, _spec: Spec
    ) -> tuple[Routed, bool]:
        raise TypeError(RECORDING_KEY)

    monkeypatch.setattr(recorder, "ask", fail)
    with pytest.raises(ValueError, match="rejected"):
        record("coverage_plan", attempt, library=library)
    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["no_response"]
    assert receipt["responses"] == []
    assert receipt["cost_records"] == []
    assert receipt["unpriced_responses"] == []
    assert receipt["primary_failure"] == "call_error"
    assert receipt["receipt_written"]
    assert (library / "coverage_plan.yaml").read_text() == "previous cassette\n"


def test_successful_capture_ignores_a_callers_active_exception(
    capture_paths: Callable[[Mapping[str, object]], tuple[Path, Path]],
) -> None:
    answer = HELLO | {"content": [{"type": "text", "text": PLAN.model_dump_json()}]}
    attempt, library = capture_paths(answer)
    try:
        int("fake caller error")
    except ValueError:
        costs = record("coverage_plan", attempt, library=library)
    assert [str(item.cost_usd) for item in costs] == ["0.000048"]
    receipt = json.loads((attempt / "receipt.json").read_text())
    assert receipt["outcome"] == "accepted"
    assert receipt["primary_failure"] is None
    assert (library / "coverage_plan.yaml").read_bytes() == (
        attempt / "coverage_plan.yaml"
    ).read_bytes()
