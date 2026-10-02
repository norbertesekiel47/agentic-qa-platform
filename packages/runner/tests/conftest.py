"""Fixtures for the model router's tests: a local stand-in for HTTP APIs, a clean
slate for the tracing switches the tests flip (SECURITY §10), and VCR cassettes
for the Anthropic adapter (TESTING §4)."""

import hashlib
import json
import os
import socketserver
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import langsmith
import pytest
from aqa_core.model_roles import RoutedModel, resolve_roles
from aqa_core.price_map import vendored
from aqa_core.project import load_config
from langsmith.utils import get_env_var
from vcr import VCR
from vcr.errors import CannotOverwriteExistingCassetteException

# Not a credential, and named so (AGENTS.md rule 9).
FAKE_KEY = "fake-key-for-tests"


@dataclass
class Endpoint:
    """A local HTTP server that records every request and answers 200."""

    url: str
    requests: list[tuple[str, str]] = field(default_factory=list)

    def received(self, *, within: float) -> bool:
        """Whether a request arrives within `within` seconds."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if self.requests:
                return True
            time.sleep(0.05)
        return bool(self.requests)


@pytest.fixture
def serve() -> Iterator[Callable[..., Endpoint]]:
    """`serve(body=b"{}", headers={...})` starts a local server that answers every
    request with that body and those headers, and stops it after the test."""
    servers: list[socketserver.ThreadingTCPServer] = []

    def start(
        body: bytes = b"{}", headers: Mapping[str, str] | None = None
    ) -> Endpoint:
        endpoint = Endpoint(url="")
        answer = {"content-type": "application/json", **(headers or {})}
        head = "".join(f"{name}: {value}\r\n" for name, value in answer.items())
        response = (
            f"HTTP/1.1 200 OK\r\n{head}content-length: {len(body)}\r\n"
            "connection: close\r\n\r\n"
        ).encode() + body

        class Handler(socketserver.StreamRequestHandler):
            """Reads one request, writes it down, and answers it."""

            def handle(self) -> None:
                method, path, _ = self.rfile.readline().decode().split(" ", 2)
                length = 0
                while (line := self.rfile.readline().strip()) != b"":
                    name, _, value = line.decode().partition(":")
                    if name.lower() == "content-length":
                        length = int(value)
                self.rfile.read(length)
                endpoint.requests.append((method, path))
                self.wfile.write(response)

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        endpoint.url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return endpoint

    yield start
    for server in servers:
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


@pytest.fixture
def langsmith_endpoint(serve: Callable[..., Endpoint]) -> Endpoint:
    """A stand-in for LangSmith's API."""
    return serve()


@pytest.fixture
def customers_environment(
    langsmith_endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], None]:
    """`customers_environment(switch)` sets `switch` the way a customer's
    environment might, with LangSmith's API at the stand-in."""

    def set_up(switch: str) -> None:
        monkeypatch.setenv(switch, "true")
        monkeypatch.setenv("LANGSMITH_ENDPOINT", langsmith_endpoint.url)
        monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_KEY)

    return set_up


@pytest.fixture
def exported_to_langsmith(langsmith_endpoint: Endpoint) -> Callable[[float], bool]:
    """`exported_to_langsmith(seconds)`: whether the stand-in got a request
    within that long."""
    return lambda within: langsmith_endpoint.received(within=within)


CASSETTES = Path(__file__).parent / "cassettes"
RECORD = "AQA_RECORD_CASSETTES"
# The command is TESTING.md §4's to own; a failing test points there.
RE_RECORD = "to re-record deliberately, follow TESTING.md §4 (Cassettes)"
# The first line of a cassette that a live API won't produce on demand (a refusal,
# an answer that doesn't validate): it is never re-recorded, whatever RECORD says.
BY_DESIGN = "# HAND-WRITTEN RESPONSES, BY DESIGN"
# Where the adapter's requests go, whatever endpoint a recording went through.
ANTHROPIC_HOST = "api.anthropic.com"
# Variables that would send a replayed call somewhere a cassette never saw.
REDIRECTS = ("ANTHROPIC_API_URL", "ANTHROPIC_BASE_URL")


@dataclass
class Recording:
    """What a cassette saw, filled in when the `with` block ends: the body of
    every request the adapter sent and of every response it got. Replay matches a
    request by its prompt hash, so the recorded request each response was played
    for is the one that was sent."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    # The bodies of the responses played, in the same order.
    responses: list[dict[str, Any]] = field(default_factory=list)


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


def _refused_request(error: BaseException | None) -> Any | None:
    """The request that VCR refused because no cassette interaction matches it,
    if `error`, or something it was raised from, is that refusal (the SDK wraps
    it)."""
    while error is not None:
        if isinstance(error, CannotOverwriteExistingCassetteException):
            return error.failed_request
        error = error.__cause__ or error.__context__
    return None


def _keep_headers(*names: str) -> Callable[[Mapping[str, str]], dict[str, str]]:
    """What a recording keeps of a request's or a response's headers: nothing
    that identifies the caller is written down."""
    return lambda headers: {
        key: value for key, value in headers.items() if key.lower() in names
    }


def _in_record_mode(path: Path, *, replay_only: bool) -> bool:
    if replay_only or os.environ.get(RECORD) != "1":
        return False
    return not (path.exists() and path.read_text().startswith(BY_DESIGN))


def _vcr(directory: Path, *, recording_now: bool) -> VCR:
    """VCR set up as TESTING §4 describes: matched by prompt hash, nothing that
    identifies the caller written down."""

    def before_record_request(request: Any) -> Any:
        request.headers = _keep_headers("content-type", "anthropic-version")(
            request.headers
        )
        if recording_now:
            # Through a proxy or a stand-in, the cassette still replays against
            # the API's own address.
            request.uri = (
                urlsplit(request.uri)
                ._replace(scheme="https", netloc=ANTHROPIC_HOST)
                .geturl()
            )
        return request

    def before_record_response(response: dict[str, Any]) -> dict[str, Any]:
        response["headers"] = _keep_headers("content-type", "request-id")(
            response["headers"]
        )
        return response

    vcr = VCR(
        cassette_library_dir=str(directory),
        record_mode="once" if recording_now else "none",
        match_on=["method", "uri", "prompt_hash"],
        before_record_request=before_record_request,
        before_record_response=before_record_response,
        # A compressed answer is kept as the text it was, not as bytes that
        # replay can't decode.
        decode_compressed_response=True,
        # LangSmith's stand-in in the tracing tests is on localhost; a
        # recording's stand-in (in this fixture's own tests) is the API.
        ignore_localhost=not recording_now,
    )
    vcr.register_matcher("prompt_hash", prompt_hash_matches)
    return vcr


@pytest.fixture
def cassette(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., AbstractContextManager[Recording]]:
    """`with cassette("name") as recording:` replays `cassettes/name.yaml`
    against the Anthropic API's host, matching each request by its prompt hash,
    and fails if a request has no match or a recorded interaction is never
    played. With AQA_RECORD_CASSETTES=1 and ANTHROPIC_API_KEY set it records the
    real API's answers instead, into a scratch file that replaces the cassette
    only when the test passes. `replay_only` keeps a test that isn't about the
    API's answers (tracing, the fixture's own failures) from ever recording;
    `library` is the directory the cassette lives in."""

    @contextmanager
    def use(
        name: str, *, replay_only: bool = False, library: Path = CASSETTES
    ) -> Iterator[Recording]:
        path = library / f"{name}.yaml"
        recording_now = _in_record_mode(path, replay_only=replay_only)
        directory = library
        if recording_now:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    f"{RECORD}=1 needs ANTHROPIC_API_KEY in the environment"
                )
            directory = tmp_path / "recording"
            directory.mkdir(exist_ok=True)
        else:
            monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
            for variable in REDIRECTS:
                monkeypatch.delenv(variable, raising=False)
        recording = Recording()
        try:
            with _vcr(directory, recording_now=recording_now).use_cassette(
                f"{name}.yaml"
            ) as played:
                yield recording
        except Exception as error:
            if (refused := _refused_request(error)) is not None:
                raise AssertionError(
                    f"cassettes/{name}.yaml has no response for a request the adapter "
                    f"just sent, so a prompt changed; {RE_RECORD}. The request "
                    f"body was {_canonical(refused.body)}"
                ) from error
            raise
        recording.sent = [json.loads(request.body) for request in played.requests]
        recording.responses = [
            json.loads(response["body"]["string"]) for response in played.responses
        ]
        if recording_now:
            written = directory / path.name
            if not written.exists():
                raise AssertionError(f"{name}: the test made no request to record")
            written.replace(path)
        elif not played.all_played:
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
