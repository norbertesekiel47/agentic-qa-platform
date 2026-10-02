"""The refresh script pins a new upstream commit and the sha256 of the map it
downloaded (ADR-0007 amendment, TECH_STACK §7). No test reaches the network."""

import hashlib
import http.client
import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from aqa_core.price_map import MAP_FILE, PIN_FILE, load_price_map
from aqa_core.price_map_refresh import RefreshError, fetch_https, main, refresh

COMMIT = "6a8e0a270a8a119874c41fa2f479d3dfc965fd9f"
MAP = b'{"sample_spec": {}, "model-a": {"input_cost_per_token": 2e-06}}\n'
RAW_HOST = "raw.githubusercontent.com"


class FakeGitHub:
    """Answers the two requests a refresh makes, and remembers every one."""

    def __init__(
        self,
        *,
        ref: str = "main",
        commit_answer: bytes | None = None,
        download: bytes = MAP,
    ) -> None:
        self.ref = ref
        self.commit_answer = (
            json.dumps({"sha": COMMIT}).encode()
            if commit_answer is None
            else commit_answer
        )
        self.download = download
        self.requests: list[tuple[str, str]] = []

    def __call__(self, host: str, path: str) -> bytes:
        self.requests.append((host, path))
        if (
            host == "api.github.com"
            and path == f"/repos/BerriAI/litellm/commits/{self.ref}"
        ):
            return self.commit_answer
        if host == RAW_HOST and path == f"/BerriAI/litellm/{COMMIT}/{MAP_FILE}":
            return self.download
        raise AssertionError(f"unexpected request: {host}{path}")


def test_refresh_pins_the_resolved_commit_and_the_downloaded_sha256(
    tmp_path: Path,
) -> None:
    pin = refresh("main", tmp_path, fetch=FakeGitHub())

    assert pin.commit == COMMIT
    assert pin.sha256 == hashlib.sha256(MAP).hexdigest()
    assert (tmp_path / MAP_FILE).read_bytes() == MAP
    assert json.loads((tmp_path / PIN_FILE).read_text()) == {
        "upstream": "BerriAI/litellm",
        "commit": COMMIT,
        "sha256": hashlib.sha256(MAP).hexdigest(),
    }
    assert load_price_map(tmp_path).version == COMMIT


@pytest.mark.parametrize("ref", ["main", "v1.2.3", "release_2026-10", COMMIT])
def test_a_commit_branch_or_tag_name_is_accepted(tmp_path: Path, ref: str) -> None:
    pin = refresh(ref, tmp_path, fetch=FakeGitHub(ref=ref))

    assert pin.commit == COMMIT


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "main?per_page=1",
        "a b",
        "../x",
        "feature/x",
        ".",
        "..",
        "-x",
        "a..b",
        "main.",
        "m\u00e4in",
        "main\u2215x",
    ],
)
def test_a_ref_that_is_not_a_plain_name_is_refused_before_any_request(
    tmp_path: Path, ref: str
) -> None:
    github = FakeGitHub()

    with pytest.raises(RefreshError, match="not a commit, branch or tag"):
        refresh(ref, tmp_path, fetch=github)

    assert github.requests == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "answer",
    [
        b"{}",
        b'{"sha": "main"}',
        b'{"sha": 7}',
        json.dumps({"sha": COMMIT + "0"}).encode(),
        json.dumps({"sha": "x" + COMMIT}).encode(),
        b"[]",
        b"not json",
        b'{"sha": "\xff"}',
    ],
    ids=[
        "no-sha",
        "sha-not-a-commit",
        "sha-not-text",
        "41-characters",
        "text-before-the-sha",
        "not-an-object",
        "not-json",
        "not-utf8",
    ],
)
def test_an_answer_that_names_no_commit_is_refused(
    tmp_path: Path, answer: bytes
) -> None:
    github = FakeGitHub(commit_answer=answer)

    with pytest.raises(RefreshError, match="no commit for 'main'"):
        refresh("main", tmp_path, fetch=github)

    assert all(host != RAW_HOST for host, _ in github.requests)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("download", "problem"),
    [
        (b"not json", "is not JSON"),
        (b'{"model-a": "\xff"}', "is not JSON"),
        (b"[1]", "is not a JSON object"),
        (b'{"model-a": 1}', "'model-a' is not an object"),
    ],
    ids=["not-json", "not-utf8", "not-an-object", "entry-is-not-an-object"],
)
def test_a_download_that_is_not_a_map_of_objects_changes_nothing(
    tmp_path: Path, download: bytes, problem: str
) -> None:
    refresh("main", tmp_path, fetch=FakeGitHub())
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    with pytest.raises(RefreshError) as raised:
        refresh("main", tmp_path, fetch=FakeGitHub(download=download))

    assert COMMIT in str(raised.value)
    assert problem in str(raised.value)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


class FakeConnection:
    """Stands in for http.client.HTTPSConnection. A test sets what it answers
    on the subclass the `https` fixture makes."""

    status = 200
    reason = "OK"
    read_error: Exception | None = None
    connections: ClassVar[list[FakeConnection]]

    def __init__(self, host: str, *, timeout: float) -> None:
        assert timeout > 0
        self.host = host
        self.closed = False
        self.sent: tuple[str, str, dict[str, str]] | None = None
        self.connections.append(self)

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.sent = (method, path, headers)

    def getresponse(self) -> FakeConnection:
        return self

    def read(self) -> bytes:
        assert self.sent is not None
        if self.read_error is not None:
            raise self.read_error
        return f"{self.sent[0]} {self.sent[1]}".encode()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def https(monkeypatch: pytest.MonkeyPatch) -> type[FakeConnection]:
    class Connection(FakeConnection):
        connections: ClassVar[list[FakeConnection]] = []

    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    return Connection


def test_fetch_gets_the_path_over_https_and_closes_the_connection(
    https: type[FakeConnection],
) -> None:
    body = fetch_https("api.github.com", "/repos/BerriAI/litellm/commits/main")

    assert body == b"GET /repos/BerriAI/litellm/commits/main"
    (connection,) = https.connections
    assert connection.host == "api.github.com"
    assert connection.closed
    assert connection.sent is not None
    # GitHub's API refuses a request that names no user agent.
    assert connection.sent[2]["User-Agent"]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (204, "No Content"),
        (301, "Moved Permanently"),
        (404, "Not Found"),
        (503, "Service Unavailable"),
    ],
)
def test_fetch_refuses_an_answer_that_is_not_200(
    https: type[FakeConnection], status: int, reason: str
) -> None:
    https.status = status
    https.reason = reason

    with pytest.raises(RefreshError, match=f"{status} {reason}"):
        fetch_https(RAW_HOST, "/BerriAI/litellm/x/y.json")

    assert all(connection.closed for connection in https.connections)


def test_fetch_refuses_a_body_that_stops_short(https: type[FakeConnection]) -> None:
    https.read_error = http.client.IncompleteRead(b"part")

    with pytest.raises(RefreshError, match="IncompleteRead"):
        fetch_https(RAW_HOST, "/BerriAI/litellm/x/y.json")

    assert all(connection.closed for connection in https.connections)


@pytest.mark.parametrize("argv", [[], ["main"]], ids=["default-ref", "named-ref"])
def test_the_command_pins_main_and_reports_what_it_pinned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    status = main(argv, fetch=FakeGitHub(), directory=tmp_path)

    assert status == 0
    assert capsys.readouterr().out == (
        f"pinned BerriAI/litellm {COMMIT}\nsha256 {hashlib.sha256(MAP).hexdigest()}\n"
    )
    assert load_price_map(tmp_path).version == COMMIT


def test_the_command_reports_a_refusal_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["a b"], fetch=FakeGitHub(), directory=tmp_path)

    assert status == 1
    assert "'a b' is not a commit, branch or tag name" in capsys.readouterr().err


def test_the_command_reports_a_network_failure_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def offline(host: str, path: str) -> bytes:
        raise OSError(f"cannot reach {host}{path}")

    status = main([], fetch=offline, directory=tmp_path)

    assert status == 1
    assert (
        "cannot reach api.github.com/repos/BerriAI/litellm/commits/main"
        in capsys.readouterr().err
    )


def test_python_dash_m_runs_the_command() -> None:
    # A ref that is refused before any request, so no network is touched.
    run = subprocess.run(
        [sys.executable, "-m", "aqa_core.price_map_refresh", "a b"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 1
    assert "not a commit, branch or tag name" in run.stderr
