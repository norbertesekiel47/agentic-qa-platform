"""Fixtures for the model router's tests: a local stand-in for LangSmith's API,
a clean slate for the tracing switches the tests flip (SECURITY §10), and VCR
cassettes for the Anthropic adapter (TESTING §4)."""

import hashlib
import json
import os
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import langsmith
import pytest
from aqa_core.model_roles import RoutedModel, resolve_roles
from aqa_core.price_map import vendored
from aqa_core.project import load_config
from langsmith.utils import get_env_var
from vcr import VCR
from vcr.errors import CannotOverwriteExistingCassetteException


@dataclass
class Endpoint:
    """A local HTTP server that records every request and answers 200 `{}`."""

    url: str
    requests: list[tuple[str, str]] = field(default_factory=list)

    def exported(self, *, within: float) -> bool:
        """Whether a request arrives within `within` seconds."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if self.requests:
                return True
            time.sleep(0.05)
        return bool(self.requests)


@pytest.fixture
def langsmith_endpoint() -> Iterator[Endpoint]:
    endpoint = Endpoint(url="")

    class Handler(socketserver.StreamRequestHandler):
        """Reads one request, writes it down, and answers 200 `{}`."""

        def handle(self) -> None:
            method, path, _ = self.rfile.readline().decode().split(" ", 2)
            length = 0
            while (line := self.rfile.readline().strip()) != b"":
                name, _, value = line.decode().partition(":")
                if name.lower() == "content-length":
                    length = int(value)
            self.rfile.read(length)
            endpoint.requests.append((method, path))
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                b"content-length: 2\r\nconnection: close\r\n\r\n{}"
            )

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    endpoint.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield endpoint
    server.shutdown()
    server.server_close()


def clear_env_cache() -> None:
    # An lru_cache that langsmith's overloaded signature hides from the types.
    cast(Any, get_env_var).cache_clear()


@pytest.fixture
def reset_tracing() -> Iterator[None]:
    """LangSmith reads its environment through a cache, keeps a process-wide
    switch and builds one client for the whole process (all through
    `langsmith.configure`), so a test that sets any of them starts and ends from
    the unset state: a client built for one test's endpoint would send the next
    test's traces there."""
    langsmith.configure(enabled=None, client=None)
    clear_env_cache()
    yield
    langsmith.configure(enabled=None, client=None)
    clear_env_cache()


CASSETTES = Path(__file__).parent / "cassettes"
RECORD = "AQA_RECORD_CASSETTES"
# Not a credential, and named so (AGENTS.md rule 9).
FAKE_KEY = "fake-key-for-tests"
RE_RECORD = (
    f"to re-record every cassette deliberately, put your provider key in a "
    f"gitignored .scratch/provider.env as ANTHROPIC_API_KEY=..., then run "
    f"`set -a; . .scratch/provider.env; set +a; {RECORD}=1 uv run pytest "
    f"packages/runner/tests/test_anthropic_wire.py packages/runner/tests/test_anthropic_client.py "
    f"packages/runner/tests/test_model_router.py`"
)


@dataclass
class Recording:
    """What a cassette saw, filled in when the `with` block ends: the body of
    every request the adapter sent. Replay matches a request by its prompt hash,
    so the recorded request each response was played for is the one that was
    sent."""

    sent: list[dict[str, Any]] = field(default_factory=list)


def _canonical(body: bytes | str | None) -> str:
    """A request body as one stable string: the prompt hash's input."""
    return json.dumps(
        json.loads(body or b"null"), sort_keys=True, separators=(",", ":")
    )


def prompt_hash_matches(sent: Any, recorded: Any) -> bool:
    """Whether two requests have the same prompt (model, messages, tools, output
    format): their canonical bodies hash alike. A changed prompt matches
    nothing, and replay fails."""
    return (
        hashlib.sha256(_canonical(sent.body).encode()).hexdigest()
        == hashlib.sha256(_canonical(recorded.body).encode()).hexdigest()
    )


def _cannot_replay(error: BaseException | None) -> bool:
    """Whether `error`, or something it was raised from, is VCR refusing a
    request that no cassette interaction matches (the SDK wraps it)."""
    while error is not None:
        if isinstance(error, CannotOverwriteExistingCassetteException):
            return True
        error = error.__cause__ or error.__context__
    return False


@pytest.fixture
def cassette(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], AbstractContextManager[Recording]]:
    """`with cassette("name") as recording:` replays `cassettes/name.yaml`
    against the Anthropic API's host, matching each request by its prompt hash,
    and fails if a request has no match or a recorded interaction is never
    played. With AQA_RECORD_CASSETTES=1 and ANTHROPIC_API_KEY set it records
    the real API's answers instead."""

    @contextmanager
    def use(name: str) -> Iterator[Recording]:
        recording = Recording()
        recording_now = os.environ.get(RECORD) == "1"
        path = CASSETTES / f"{name}.yaml"
        if recording_now:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    f"{RECORD}=1 needs ANTHROPIC_API_KEY in the environment"
                )
            path.unlink(missing_ok=True)
        else:
            monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

        def before_record_request(request: Any) -> Any:
            # Nothing that identifies the caller is written down.
            request.headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() in {"content-type", "anthropic-version"}
            }
            return request

        def before_record_response(response: dict[str, Any]) -> dict[str, Any]:
            response["headers"] = {
                key: value
                for key, value in response["headers"].items()
                if key.lower() in {"content-type", "request-id"}
            }
            return response

        vcr = VCR(
            cassette_library_dir=str(CASSETTES),
            record_mode="once" if recording_now else "none",
            match_on=["method", "uri", "prompt_hash"],
            before_record_request=before_record_request,
            before_record_response=before_record_response,
            # LangSmith's stand-in in the tracing tests is on localhost.
            ignore_localhost=True,
        )
        vcr.register_matcher("prompt_hash", prompt_hash_matches)
        try:
            with vcr.use_cassette(f"{name}.yaml") as played:
                yield recording
        except Exception as error:
            if _cannot_replay(error):
                raise AssertionError(
                    f"cassettes/{name}.yaml has no response for a request the adapter "
                    f"just sent, so a prompt changed; {RE_RECORD}"
                ) from error
            raise
        recording.sent = [json.loads(request.body) for request in played.requests]
        if not recording_now and not played.all_played:
            raise AssertionError(
                f"cassettes/{name}.yaml has a recorded response that was never "
                f"used, so the adapter now sends different requests; {RE_RECORD}"
            )

    return use


@pytest.fixture
def sonnet(tmp_path: Path) -> RoutedModel:
    """The default model of every role: Claude Sonnet 5.5, from the pinned map."""
    path = tmp_path / "config.yaml"
    path.write_text("")
    return resolve_roles(load_config(path), vendored())["navigator"].model
