"""The cassette fixture itself (conftest.py): what it replays, what it refuses,
and how a re-recording is written (TESTING §4). A failing replay must say so
loudly, and a recording must neither keep a credential nor lose a committed
cassette."""

import asyncio
import gzip
import http.client
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

import pytest
import yaml
from aqa_core.model_roles import RoutedModel
from aqa_runner.anthropic_client import AnthropicClient
from langchain_core.messages import HumanMessage
from vcr.errors import CannotOverwriteExistingCassetteException

CASSETTES = Path(__file__).parent / "cassettes"
Cassette = Callable[..., AbstractContextManager[Any]]
API = "api.anthropic.com"
MESSAGES = "/v1/messages"
RECORD = "AQA_RECORD_CASSETTES"
KEY = "ANTHROPIC_API_KEY"
COMMITTED = "# the cassette as committed\n"


class Stub(Protocol):
    """A local server standing in for the Anthropic API."""

    url: str


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def recorded_request(name: str) -> dict[str, Any]:
    """The body of the one request that `cassettes/<name>.yaml` holds."""
    (interaction,) = yaml.safe_load((CASSETTES / f"{name}.yaml").read_text())[
        "interactions"
    ]
    body: dict[str, Any] = json.loads(interaction["request"]["body"])
    return body


def ask(model: RoutedModel, prompt: str) -> str | list[str | dict[Any, Any]]:
    """A plain call through the adapter."""
    client = AnthropicClient(model, None)
    reply = asyncio.run(client.call([HumanMessage(content=prompt)], [], None))
    return reply.message.content


def send(
    body: dict[str, Any],
    *,
    host: str = API,
    method: str = "POST",
    path: str = MESSAGES,
) -> dict[str, Any]:
    """Send `body` the way an SDK would, over a connection the cassette patches
    (so nothing reaches the network), and return the answer."""
    connection = http.client.HTTPSConnection(host, timeout=5)
    connection.request(
        method, path, json.dumps(body), {"content-type": "application/json"}
    )
    answer: dict[str, Any] = json.loads(connection.getresponse().read())
    return answer


def test_a_request_with_the_recorded_prompt_gets_the_recorded_answer(
    cassette: Cassette,
) -> None:
    with cassette("structured_output", replay_only=True) as recording:
        answer = send(recorded_request("structured_output"))

    assert answer["type"] == "message"
    assert recording.responses == [answer]


def test_the_order_of_a_bodys_keys_is_not_a_changed_prompt(cassette: Cassette) -> None:
    body = recorded_request("structured_output")
    reordered = dict(reversed(body.items()))
    assert list(reordered) != list(body)

    with cassette("structured_output", replay_only=True):
        send(reordered)


def test_a_changed_prompt_fails_the_cassette_loudly(cassette: Cassette) -> None:
    body = recorded_request("structured_output")
    body["messages"][0]["content"] = "another prompt"

    with (
        pytest.raises(AssertionError, match="a prompt changed") as failure,
        cassette("structured_output", replay_only=True),
    ):
        send(body)

    # What a hand-written cassette's request must become (TESTING §4).
    assert _canonical(body) in str(failure.value)


def test_a_changed_prompt_fails_loudly_through_the_sdk_too(
    cassette: Cassette, sonnet: RoutedModel
) -> None:
    # The SDK wraps the refusal in its own error, which the fixture sees through.
    with (
        pytest.raises(AssertionError, match="a prompt changed"),
        cassette("structured_output", replay_only=True),
    ):
        ask(sonnet, "another prompt")


@pytest.mark.parametrize(
    "different",
    [{"method": "PUT"}, {"path": "/v1/other"}, {"host": "example.invalid"}],
    ids=["method", "path", "host"],
)
def test_a_request_must_match_the_method_and_the_address_as_well(
    cassette: Cassette, different: dict[str, str]
) -> None:
    body = recorded_request("structured_output")

    with cassette("structured_output", replay_only=True):
        with pytest.raises(CannotOverwriteExistingCassetteException):
            send(body, **different)
        # The right request still plays, so nothing is left unused.
        send(body)


def test_a_failure_that_is_not_a_missing_response_passes_through_as_itself(
    cassette: Cassette,
) -> None:
    with (
        pytest.raises(KeyError, match="not a cassette problem"),
        cassette("structured_output", replay_only=True),
    ):
        raise KeyError("not a cassette problem")


def test_a_recorded_response_that_is_never_played_fails_loudly(
    cassette: Cassette,
) -> None:
    with (
        pytest.raises(AssertionError, match="never used"),
        cassette("structured_output", replay_only=True),
    ):
        pass


def test_re_recording_needs_a_provider_key(
    monkeypatch: pytest.MonkeyPatch, cassette: Cassette
) -> None:
    monkeypatch.setenv(RECORD, "1")
    monkeypatch.delenv(KEY, raising=False)
    before = (CASSETTES / "tools.yaml").read_text()

    with (
        pytest.raises(RuntimeError, match="needs ANTHROPIC_API_KEY"),
        cassette("tools"),
    ):
        pass

    assert (CASSETTES / "tools.yaml").read_text() == before


def test_a_scenario_a_live_api_cannot_produce_is_never_re_recorded(
    monkeypatch: pytest.MonkeyPatch, cassette: Cassette
) -> None:
    # No key is needed and none is used: the hand-written refusal replays, and
    # the file is as it was.
    monkeypatch.setenv(RECORD, "1")
    monkeypatch.delenv(KEY, raising=False)
    before = (CASSETTES / "refusal.yaml").read_text()

    with cassette("refusal"):
        answer = send(recorded_request("refusal"))

    assert answer["stop_reason"] == "refusal"
    assert (CASSETTES / "refusal.yaml").read_text() == before


def test_a_test_that_is_not_about_the_answers_replays_even_when_recording(
    monkeypatch: pytest.MonkeyPatch, cassette: Cassette
) -> None:
    monkeypatch.setenv(RECORD, "1")
    monkeypatch.delenv(KEY, raising=False)

    with cassette("structured_output", replay_only=True):
        send(recorded_request("structured_output"))


# A recording, made here against a local stand-in for the API.

HELLO = {
    "id": "msg_stub_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "Hello."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 9, "output_tokens": 3},
}
RECORDING_KEY = "fake-key-for-the-recording-test"


def say_hello(model: RoutedModel) -> str:
    answer = ask(model, "Say hello.")
    assert isinstance(answer, str)
    return answer


def say_hello_and_fail(model: RoutedModel) -> None:
    say_hello(model)
    raise ZeroDivisionError


@pytest.fixture
def recording_against_a_stub(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[..., Stub], tmp_path: Path
) -> Path:
    """Records through the fixture to a stand-in for the API that answers with a
    compressed body and headers a cassette must not keep. Returns the library
    the recording goes to."""
    stub = serve(
        gzip.compress(json.dumps(HELLO).encode()),
        {
            "content-encoding": "gzip",
            "request-id": "req_stub_01",
            "set-cookie": "session=fake-cookie",
        },
    )
    monkeypatch.setenv(RECORD, "1")
    monkeypatch.setenv(KEY, RECORDING_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", stub.url)
    library = tmp_path / "library"
    library.mkdir()
    return library


def test_a_recording_keeps_no_credential_and_the_api_address(
    cassette: Cassette, sonnet: RoutedModel, recording_against_a_stub: Path
) -> None:
    with cassette("hello", library=recording_against_a_stub):
        assert say_hello(sonnet) == "Hello."

    text = (recording_against_a_stub / "hello.yaml").read_text()
    for secret in (
        RECORDING_KEY,
        "x-api-key",
        "set-cookie",
        "fake-cookie",
        "stainless",
    ):
        assert secret not in text.lower()
    (interaction,) = yaml.safe_load(text)["interactions"]
    assert interaction["request"]["uri"] == f"https://{API}{MESSAGES}"
    assert set(interaction["request"]["headers"]) <= {
        "content-type",
        "anthropic-version",
    }
    assert {name.lower() for name in interaction["response"]["headers"]} == {
        "content-type",
        "request-id",
    }
    # The compressed answer is kept as the text it was.
    assert json.loads(interaction["response"]["body"]["string"]) == HELLO


def test_a_recording_replays_against_the_apis_own_address(
    monkeypatch: pytest.MonkeyPatch,
    cassette: Cassette,
    sonnet: RoutedModel,
    recording_against_a_stub: Path,
) -> None:
    with cassette("hello", library=recording_against_a_stub):
        say_hello(sonnet)
    monkeypatch.delenv(RECORD)

    with cassette("hello", library=recording_against_a_stub) as replay:
        assert say_hello(sonnet) == "Hello."

    assert replay.responses == [HELLO]


def test_a_recording_replaces_the_cassette_only_when_the_test_passes(
    cassette: Cassette, sonnet: RoutedModel, recording_against_a_stub: Path
) -> None:
    committed = recording_against_a_stub / "hello.yaml"
    committed.write_text(COMMITTED)

    with (
        pytest.raises(ZeroDivisionError),
        cassette("hello", library=recording_against_a_stub),
    ):
        say_hello_and_fail(sonnet)

    assert committed.read_text() == COMMITTED


def test_a_recording_run_that_makes_no_request_says_so_and_keeps_the_cassette(
    cassette: Cassette, recording_against_a_stub: Path
) -> None:
    committed = recording_against_a_stub / "hello.yaml"
    committed.write_text(COMMITTED)

    with (
        pytest.raises(AssertionError, match="no request to record"),
        cassette("hello", library=recording_against_a_stub),
    ):
        pass

    assert committed.read_text() == COMMITTED
