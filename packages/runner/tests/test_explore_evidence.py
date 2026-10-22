"""Explore's evidence (#53 P22; ADR-0024's #53 P22 amendment): each step the
attempt dispatches keeps `evidence/<seq>/` in the attempt's part, its
snapshot the observation the next request carried, and the confirmation's
replay keeps its own in the confirmation's part. Saving it changes no
model-visible request, and evidence the record can't take stops the turns
(15). Real Chromium against the executor's fixture app, with a scripted
model."""

import errno
import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from aqa_core.config import ProjectConfig
from aqa_runner import navigator_tools
from aqa_runner.chat_client import Reply
from aqa_runner.explore import Explored
from aqa_runner.navigator_tools import Attempt
from aqa_runner.reasons import UnansweredError
from aqa_runner.redaction import Redacted
from aqa_runner.run_record import RunRecord
from langchain_core.messages import AIMessage, BaseMessage
from playwright.async_api import Page

from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    REFUSED,
    Scripted,
    answer,
    calling,
    record_of,
    turned,
)
from packages.runner.tests.navigator_fixtures import bound
from packages.runner.tests.secret_fixtures import FAKE_VALUE, copies_found
from packages.runner.tests.test_evidence import FILES, holding, kinds
from packages.runner.tests.test_explore_confirm import RESET
from packages.runner.tests.test_explore_graph import (
    ACCOUNT,
    ASSERT,
    FINISH,
    SECRETS,
    UNWRITTEN,
    configured,
    written,
)
from packages.runner.tests.test_explore_graph import explored as attempted
from packages.runner.tests.test_explore_turns import FILL_SECRET

# The path the scripted attempt explores and compiles: fill, save, check.
PATH = (FILL, CLICK, ASSERT, FINISH)
LOGS = FILES[1:]


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)


def sent(client: Scripted) -> list[list[tuple[str, Any]]]:
    """Each request the model was sent, each message as its type and content."""
    return [[(m.type, m.content) for m in request] for request in client.requests]


def snapshot_block(request: Sequence[BaseMessage]) -> str:
    """What the request's snapshot block holds, as the model reads it."""
    found = re.search(r"<snapshot>\n(.*)\n</snapshot>", request[-1].text, re.DOTALL)
    assert found is not None
    return found[1]


def evidence(record: Path | None, part: str = "attempt-1") -> dict[str, bytes]:
    """Each evidence file of the record's `part`, by its path under `evidence/`."""
    assert record is not None
    folder = record / part / "evidence"
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def entries(files: dict[str, bytes], name: str) -> list[dict[str, Any]]:
    """Every entry of each log called `name` among `files`."""
    return [
        entry
        for path, data in files.items()
        if path.endswith(name)
        for entry in json.loads(data)["entries"]
    ]


def explored(app: App, tmp_path: Path, client: Scripted, **setup: Any) -> Explored:
    """How a run of the shop spec under `tmp_path` ended, its config.yaml
    written first, as publication reads it again."""
    configured(tmp_path / "qa", setup.get("config"))
    return attempted(app, tmp_path, client, **setup)


async def unsaved(*_: object) -> None:
    """Evidence off: capture nothing."""


def changing_at(monkeypatch: pytest.MonkeyPatch, look: int) -> list[str]:
    """Make the page's document change while it is looked at the `look`-th
    time (from 1), once; return each look's text."""
    taken: list[str] = []
    aria_snapshot = Page.aria_snapshot

    async def changing(self: Page, *args: Any, **options: Any) -> str:
        if len(taken) + 1 == look:
            async with self.expect_event("framenavigated"):
                await self.evaluate("location.hash = 'moved'")
        taken.append(await aria_snapshot(self, *args, **options))
        return taken[-1]

    monkeypatch.setattr(Page, "aria_snapshot", changing)
    return taken


@pytest.mark.parametrize("case", ["saved", "bound", "retried"])
def test_evidence_capture_leaves_every_model_visible_request_byte_identical(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    setup: dict[str, Any] = (
        {"config": SECRETS, "preconditions": ACCOUNT} if case == "bound" else {}
    )
    runs: dict[bool, tuple[Scripted, Explored, list[str]]] = {}
    for saving in (True, False):
        with monkeypatch.context() as patched:
            looks = changing_at(patched, 2) if case == "retried" else []
            if not saving:
                patched.setattr(navigator_tools, "capture_evidence", unsaved)
            client = Scripted(*PATH)
            run = explored(app, tmp_path / str(saving), client, **setup)
        runs[saving] = client, run, looks
    (on, saved, on_looks), (off, unsaved_run, off_looks) = runs[True], runs[False]

    assert (saved.outcome, len(on.requests)) == ("compiled", 4)
    assert sent(on) == sent(off)
    assert on.offered == off.offered
    assert on_looks == off_looks
    assert len(on_looks) == (5 if case == "retried" else 0)
    # The control: one run saved each step's evidence, the other none.
    assert sorted({name.split("/")[0] for name in evidence(saved.record)}) == [
        "0",
        "1",
        "2",
    ]
    assert unsaved_run.record is not None
    assert not (unsaved_run.record / "attempt-1" / "evidence").exists()
    assert written(saved)["withheld"] is (case == "bound")


NO_TOOL = answer(AIMessage(content="notes before looking again"))


@pytest.mark.parametrize("looked", [True, False], ids=["looked", "unanswered"])
def test_a_steps_evidence_is_the_observation_its_next_request_carried(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looked: bool
) -> None:
    observe = Attempt.observe
    looks: list[int] = []

    async def unanswered_second(attempt: Attempt) -> Redacted:
        looks.append(1)
        if len(looks) == 2 and not looked:
            raise UnansweredError(5)
        return await observe(attempt)

    monkeypatch.setattr(Attempt, "observe", unanswered_second)
    moves = PATH if looked else (FILL, NO_TOOL, CLICK, ASSERT, FINISH)
    client = Scripted(*moves)

    run = explored(app, tmp_path, client)

    assert run.outcome == "compiled"
    files = evidence(run.record)
    seen = [0, 1, 2] if looked else [0, 2]
    assert sorted(files) == sorted(
        [f"{seq}/{name}" for seq in seen for name in FILES]
        + ([] if looked else [f"1/{name}" for name in LOGS])
    )
    # Step n's next request is the (n+1)-th; without a look, the step's
    # evidence is the one after the empty snapshot.
    carried = {0: 0, 1: 1, 2: 2} if looked else {0: 0, 2: 3}
    for seq, index in carried.items():
        saved = files[f"{seq}/a11y_snapshot.yaml"].decode()
        assert saved.replace("<", "&lt;") == snapshot_block(client.requests[index])
        assert saved
    if not looked:
        assert snapshot_block(client.requests[1]) == ""


NAVIGATE_AWAY = calling("navigate", {"path": "/redirect?to=data:text/html,elsewhere"})


def test_no_evidence_file_holds_an_owned_reason_or_result_object(
    app: App, tmp_path: Path
) -> None:
    run = explored(app, tmp_path, Scripted(FILL, NAVIGATE_AWAY))

    assert (run.outcome, run.exit_code) == ("non_resumable", 4)
    reasons = written(run)["reasons"]
    assert any("ERR_UNSAFE_REDIRECT" in reason for reason in reasons)
    files = evidence(run.record)
    # The failed step's evidence: its logs, empty, as its result keeps no
    # window, and no snapshot, since no request followed it.
    assert sorted(files) == [
        *(f"{seq}/{name}" for seq in (0, 1) for name in FILES),
        *(f"2/{name}" for name in LOGS),
    ]
    assert [json.loads(files[f"2/{name}"]) for name in LOGS] == [
        {"entries": [], "total": 0}
    ] * 2
    documents = [
        json.loads(data) for name, data in files.items() if name.endswith(".json")
    ]
    assert {tuple(sorted(document)) for document in documents} == {("entries", "total")}
    assert kinds(entries(files, "console_log.json")) <= {("text", "type")}
    assert kinds(entries(files, "network_log.json")) == {
        ("method", "size", "status", "timing", "url")
    }
    assert (
        holding(files, ["ERR_UNSAFE_REDIRECT", "elsewhere", "outcome", "error"]) == []
    )


@pytest.mark.usefixtures("secret")
@pytest.mark.parametrize("secret_bound", [True, False], ids=["bound", "control"])
def test_every_part_inherits_the_runs_redactor(
    app: App, tmp_path: Path, secret_bound: bool
) -> None:
    setup: dict[str, Any] = (
        {"config": SECRETS, "preconditions": ACCOUNT + RESET}
        if secret_bound
        else {"preconditions": RESET}
    )

    run = explored(app, tmp_path, Scripted(*PATH), **setup)

    assert written(run)["published"] == "confirmed"
    for part in ("attempt-1", "confirmation"):
        files = evidence(run.record, part)
        network = entries(files, "network_log.json")
        if secret_bound:
            assert kinds(network) == {("size", "status", "timing")}
            assert holding(files, [app.origin, "/did/fill", "/write/save"]) == []
        else:
            assert {
                str(entry["url"]).removeprefix(app.origin) for entry in network
            } >= {
                "/did/fill",
                "/write/save",
            }


def test_a_run_binding_a_secret_keeps_no_url_method_or_console_text_in_evidence(
    app: App, tmp_path: Path
) -> None:
    context = turned(
        app,
        tmp_path,
        Scripted(FILL_SECRET, REFUSED),
        page="/page/signin-echoes",
        secrets=[bound(app.origin, FAKE_VALUE)],
    )

    record = record_of(context)
    files = evidence(record)
    assert sorted(files) == [f"1/{name}" for name in FILES]
    console = entries(files, "console_log.json")
    assert (kinds(console), len(console) >= 1) == ({("type",)}, True)
    assert kinds(entries(files, "network_log.json")) <= {("size", "status", "timing")}
    assert "[SECRET:TEST_PASSWORD]" in files["1/a11y_snapshot.yaml"].decode()
    saved = [path for path in record.rglob("*") if path.is_file()]
    assert copies_found(FAKE_VALUE, texts=[], files=saved) == []


def test_a_confirmation_replay_into_an_explore_part_keeps_steps_and_evidence_per_phase(
    app: App, tmp_path: Path
) -> None:
    run = explored(app, tmp_path, Scripted(*PATH), preconditions=RESET)

    assert written(run)["published"] == "confirmed"
    for part in ("attempt-1", "confirmation"):
        files = evidence(run.record, part)
        assert sorted(files) == [f"{seq}/{name}" for seq in range(3) for name in FILES]
        urls = [
            [
                str(entry["url"]).removeprefix(app.origin)
                for entry in json.loads(files[f"{seq}/network_log.json"])["entries"]
            ]
            for seq in range(3)
        ]
        assert urls == [["/page/shop"], ["/did/fill"], ["/write/save"]]


def test_a_step_no_request_followed_keeps_its_logs_and_no_snapshot(
    app: App, tmp_path: Path
) -> None:
    config = ProjectConfig.model_validate({"budgets": {"actions_per_attempt": 2}})

    run = explored(app, tmp_path, Scripted(FILL, CLICK), config=config)

    assert written(run)["reasons"][0] == "the attempt took all 2 of its turns"
    files = evidence(run.record)
    assert sorted(files) == [
        *(f"{seq}/{name}" for seq in (0, 1) for name in FILES),
        *(f"2/{name}" for name in LOGS),
    ]
    network = json.loads(files["2/network_log.json"])["entries"]
    assert [str(entry["url"]).removeprefix(app.origin) for entry in network] == [
        "/write/save"
    ]


def full_disk(monkeypatch: pytest.MonkeyPatch, folder: str) -> None:
    """Make every evidence file under `folder` one the disk refuses."""
    write, write_text = RunRecord.write, RunRecord.write_text

    def refusing(name: str) -> None:
        if name.startswith(folder):
            raise OSError(errno.ENOSPC, "fake full disk")

    def writing(record: RunRecord, name: str, *args: Any, **kwargs: Any) -> Path:
        refusing(name)
        return write(record, name, *args, **kwargs)

    def writing_text(record: RunRecord, name: str, text: str) -> Path:
        refusing(name)
        return write_text(record, name, text)

    monkeypatch.setattr(RunRecord, "write", writing)
    monkeypatch.setattr(RunRecord, "write_text", writing_text)


@pytest.mark.parametrize(
    ("folder", "turns", "calls"),
    [("evidence/1/", 40, 1), ("evidence/2/", 2, 2)],
    ids=["at an observation", "when the turns end"],
)
def test_evidence_the_disk_refuses_stops_the_turns_with_exit_15(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    folder: str,
    turns: int,
    calls: int,
) -> None:
    full_disk(monkeypatch, folder)
    config = ProjectConfig.model_validate({"budgets": {"actions_per_attempt": turns}})
    client = Scripted(*PATH)

    run = explored(app, tmp_path, client, config=config)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    assert UNWRITTEN in written(run)["reasons"]
    assert len(client.requests) == calls


@pytest.mark.parametrize(
    ("linked", "beyond"),
    [
        ("evidence", [f"0/{name}" for name in FILES]),
        ("evidence/1/a11y_snapshot.yaml", []),
    ],
    ids=["its folder", "a file"],
)
def test_evidence_whose_place_a_link_holds_stops_the_turns_with_exit_15(
    app: App, tmp_path: Path, linked: str, beyond: list[str]
) -> None:
    elsewhere = tmp_path / "elsewhere"

    def linking(messages: Sequence[BaseMessage]) -> Reply:
        # Step 0's evidence is saved: a link takes the place of its folder,
        # or of step 1's snapshot, before step 1 runs.
        assert app.record is not None
        place = app.record / linked
        place.parent.mkdir(exist_ok=True)
        if place.exists():
            place.rename(elsewhere)
        place.symlink_to(elsewhere)
        return FILL(messages)

    client = Scripted(linking, CLICK, ASSERT, FINISH)

    run = explored(app, tmp_path, client)

    assert (run.outcome, run.exit_code) == ("record_error", 15)
    document = written(run)
    assert (UNWRITTEN in document["reasons"], document["published"]) == (True, None)
    assert len(client.requests) == 1
    # Nothing was written through the link.
    assert sorted(
        path.relative_to(elsewhere).as_posix()
        for path in elsewhere.rglob("*")
        if path.is_file()
    ) == sorted(beyond)


def test_a_call_refused_at_its_intent_line_keeps_no_evidence(
    app: App, tmp_path: Path
) -> None:
    # Spelled only by the intent line's own JSON around the fill's text.
    client = Scripted(FILL, REFUSED)

    context = turned(
        app, tmp_path, client, secrets=[bound(app.origin, '"value": "Ada"')]
    )

    assert "fill did not run: steps.jsonl:" in client.requests[1][-1].text
    assert not (record_of(context) / "attempt-1" / "evidence").exists()
