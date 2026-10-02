"""The refresh script pins a new upstream commit and the sha256 of the map it
downloaded (ADR-0007 amendment, TECH_STACK §7). No test reaches the network."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from aqa_core.price_map import MAP_FILE, PIN_FILE, load_price_map
from aqa_core.price_map_refresh import RefreshError, fetch_https, main, refresh

COMMIT = "6a8e0a270a8a119874c41fa2f479d3dfc965fd9f"
MAP = b'{"sample_spec": {}, "model-a": {"input_cost_per_token": 2e-06}}\n'


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
        if (
            host == "raw.githubusercontent.com"
            and path == f"/BerriAI/litellm/{COMMIT}/{MAP_FILE}"
        ):
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
    ],
    ids=[
        "no-sha",
        "sha-not-a-commit",
        "sha-not-text",
        "41-characters",
        "text-before-the-sha",
        "not-an-object",
        "not-json",
    ],
)
def test_an_answer_that_names_no_commit_is_refused(
    tmp_path: Path, answer: bytes
) -> None:
    github = FakeGitHub(commit_answer=answer)

    with pytest.raises(RefreshError, match="no commit for 'main'"):
        refresh("main", tmp_path, fetch=github)

    assert len(github.requests) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "download", [b"not json", b"[1]"], ids=["not-json", "not-an-object"]
)
def test_a_download_that_is_not_a_json_object_changes_nothing(
    tmp_path: Path, download: bytes
) -> None:
    refresh("main", tmp_path, fetch=FakeGitHub())
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    with pytest.raises(RefreshError, match=COMMIT):
        refresh("main", tmp_path, fetch=FakeGitHub(download=download))

    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


class FakeConnection:
    """Stands in for http.client.HTTPSConnection."""

    opened: list[str]
    status = 200

    def __init__(self, host: str, *, timeout: float) -> None:
        self.opened.append(host)
        self.sent: tuple[str, str, dict[str, str]] | None = None
        assert timeout > 0

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.sent = (method, path, headers)

    def getresponse(self) -> FakeConnection:
        return self

    @property
    def reason(self) -> str:
        return "OK" if self.status == 200 else "Not Found"

    def read(self) -> bytes:
        assert self.sent is not None
        return f"{self.sent[0]} {self.sent[1]}".encode()

    def close(self) -> None:
        pass


def test_fetch_gets_the_path_over_https(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(FakeConnection, "opened", opened, raising=False)
    monkeypatch.setattr("http.client.HTTPSConnection", FakeConnection)

    body = fetch_https("api.github.com", "/repos/BerriAI/litellm/commits/main")

    assert body == b"GET /repos/BerriAI/litellm/commits/main"
    assert opened == ["api.github.com"]


def test_fetch_refuses_an_answer_that_is_not_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FakeConnection, "opened", [], raising=False)
    monkeypatch.setattr(FakeConnection, "status", 404)
    monkeypatch.setattr("http.client.HTTPSConnection", FakeConnection)

    with pytest.raises(RefreshError, match="404 Not Found"):
        fetch_https("raw.githubusercontent.com", "/BerriAI/litellm/x/y.json")


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


@pytest.mark.parametrize("ref", ["main", "v1.2.3", "release_2026-10", COMMIT])
def test_a_commit_branch_or_tag_name_is_accepted(tmp_path: Path, ref: str) -> None:
    pin = refresh(ref, tmp_path, fetch=FakeGitHub(ref=ref))

    assert pin.commit == COMMIT
