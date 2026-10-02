"""Fixtures for the model router's tests: a local stand-in for LangSmith's API,
and a clean slate for the tracing switches the tests flip (SECURITY §10)."""

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

import langsmith
import pytest
from langsmith.utils import get_env_var


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

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            self.rfile.read(int(self.headers.get("content-length") or 0))
            endpoint.requests.append((self.command, self.path))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({}).encode())

        do_GET = do_POST = do_PATCH = do_PUT = _answer  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    endpoint.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
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
