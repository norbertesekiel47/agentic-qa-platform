"""A local stand-in for the provider's API, which the recorder's explore
cases are recorded from in their tests with a fake key (#53 P21a, #57), and
the answers it gives. Imported by its path, as pytest names the runner's
test modules (TESTING.md §1, Shared egress fixtures)."""

import json
import socketserver
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

# Not a credential, and named so (AGENTS.md rule 9).
KEY = "fake-key-for-the-explore-recording"
HELLO = {
    "id": "msg_fake",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "stop_sequence": None,
}

type Answer = tuple[int, bytes]
# An answer, or one made from the request body it answers.
type Answering = Answer | Callable[[bytes], Answer]
# A status that closes the connection without an answer.
DROPPED = 0


def answer(turn: int, content: list[dict[str, Any]], stop: str, **usage: int) -> Answer:
    """A 200 answer; unless `usage` says otherwise, each turn reports 6 more
    input tokens than the one before, so no two cost the same."""
    usage = usage or {"input_tokens": 9 + 6 * turn, "output_tokens": 3}
    body = HELLO | {"content": content, "stop_reason": stop, "usage": usage}
    return 200, json.dumps(body).encode()


def planned(turn: int, plan: dict[str, Any], **usage: int) -> Answer:
    return answer(
        turn, [{"type": "text", "text": json.dumps(plan)}], "end_turn", **usage
    )


def called(turn: int, name: str, args: dict[str, Any], **usage: int) -> Answer:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"notes before {name}"},
        {"type": "tool_use", "id": f"toolu_fake_{turn}", "name": name, "input": args},
    ]
    return answer(turn, content, "tool_use", **usage)


@dataclass
class Provider:
    """A local stand-in for the provider's API: each request gets the next of
    its answers, the last one again after it, with a request ID; every body
    sent is kept."""

    answers: Sequence[Answering]
    sent: list[bytes] = field(default_factory=list)


# `provider(answers)`, what `standing_in` gives.
type Start = Callable[[Sequence[Answering]], Provider]


@contextmanager
def standing_in(monkeypatch: pytest.MonkeyPatch) -> Iterator[Start]:
    """`start(answers)` starts a stand-in and points the recorder at it with
    the fake key; every stand-in started stops when the block ends."""
    servers: list[socketserver.ThreadingTCPServer] = []

    def start(answers: Sequence[Answering]) -> Provider:
        stand_in = Provider(answers)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.rfile.readline()
                length = 0
                while (line := self.rfile.readline().strip()) != b"":
                    name, _, value = line.decode().partition(":")
                    if name.lower() == "content-length":
                        length = int(value)
                stand_in.sent.append(self.rfile.read(length))
                count = len(stand_in.sent)
                entry = stand_in.answers[min(count, len(stand_in.answers)) - 1]
                status, body = entry(stand_in.sent[-1]) if callable(entry) else entry
                if status == DROPPED:
                    return
                self.wfile.write(
                    f"HTTP/1.1 {status} {'OK' if status == 200 else 'Overloaded'}\r\n"
                    "content-type: application/json\r\n"
                    f"request-id: req_fake_{count:02}\r\n"
                    f"content-length: {len(body)}\r\nconnection: close\r\n\r\n".encode()
                    + body
                )

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        monkeypatch.setenv(
            "ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}"
        )
        monkeypatch.delenv("ANTHROPIC_API_URL", raising=False)
        return stand_in

    try:
        yield start
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
