"""Publishing a compiled script under its spec's lock (ADR-0024's #53 P16
amendment; DATA_MODEL §7): the text written, the comparison made inside
the lock, the install point and what a failure on either side of it
says. No browser: spec roots under `tmp_path`, faults injected at the
operating system's calls."""

import asyncio
import errno
import fcntl
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_config, load_spec, parse_compiled
from aqa_runner import publication as publication_module
from aqa_runner.publication import (
    Publication,
    PublishError,
    PublishWriteError,
    publication,
    publish,
    published_text,
)

from packages.runner.tests.executor_fixtures import (
    SHOP_TARGETS,
    a_spec,
    compiled,
    for_spec,
)

OLD = '{"old": 1}\n'
SAVE = [
    {"seq": 1, "action": "fill", "target": "name", "value": "Ada", "side_effect": False}
]
SAVED = [
    {
        "id": "a1",
        "expect_index": 0,
        "check": "text_in_target",
        "target": "status",
        "pattern": r"Saved Ada\b",
    }
]


def project(root: Path, *, existing: str | None = None) -> Publication:
    """A spec root holding `replay`'s spec, and its compiled file holding
    `existing` when given, as a run finds it at its start."""
    (root / "config.yaml").write_text("secrets: {}\n")
    config = load_config(root / "config.yaml")
    spec = load_spec(a_spec(root, config, start_url="/page/shop").path, config)
    if existing is not None:
        (root / ".compiled").mkdir()
        (root / ".compiled" / "replay.json").write_text(existing)
    return publication(root, spec)


def script_for(at: Publication, *, confirmed: bool = True) -> CompiledScript:
    config = load_config(at.root / "config.yaml")
    spec = load_spec(at.spec, config)
    script = for_spec(compiled(SAVE, targets=SHOP_TARGETS, assertions=SAVED), spec)
    return script.model_copy(update={"confirmed": confirmed})


def unchecked(_text: str) -> None:
    return None


def published(at: Publication, script: CompiledScript, *, seconds: float = 60) -> None:
    async def scenario() -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        await publish(at, script, check=unchecked, deadline=deadline)

    asyncio.run(scenario())


def temporaries(root: Path) -> list[str]:
    return sorted(path.name for path in (root / ".compiled").glob(".*.tmp"))


def test_the_published_text_reads_back_as_the_script(tmp_path: Path) -> None:
    at = project(tmp_path)
    script = script_for(at)

    published(at, script)

    text = at.target.read_text(encoding="utf-8")
    assert text == published_text(script)
    assert text.startswith('{\n  "schema_version": 1,\n  "spec_id": "replay",\n')
    assert text.endswith("}\n")
    assert "null" not in text
    assert parse_compiled(text, ProjectConfig(), source=at.target) == script
    assert at.target == tmp_path / ".compiled" / "replay.json"
    assert temporaries(tmp_path) == []


def test_an_existing_compiled_script_is_replaced(tmp_path: Path) -> None:
    at = project(tmp_path, existing=OLD)
    script = script_for(at)

    published(at, script)

    assert at.target.read_text(encoding="utf-8") == published_text(script)
    assert temporaries(tmp_path) == []


def test_the_lock_file_is_never_removed(tmp_path: Path) -> None:
    at = project(tmp_path)
    published(at, script_for(at, confirmed=False))
    lock = tmp_path / ".aqa" / "locks" / "replay.lock"
    first = lock.stat().st_ino

    published(project(tmp_path), script_for(at))

    assert lock.stat().st_ino == first
    assert json.loads(at.target.read_text(encoding="utf-8"))["confirmed"] is True


def a_link(root: Path, place: str) -> Path:
    """`place`, under `root`, made a link to a directory or file outside
    it."""
    outside = root.parent / f"outside-{root.name}"
    outside.mkdir(exist_ok=True)
    pointed = outside / Path(place).name
    if place.endswith((".json", ".lock")):
        pointed.write_text(OLD)
    else:
        pointed.mkdir(exist_ok=True)
    link = root / place
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(pointed)
    return link


@pytest.mark.parametrize(
    "place",
    [
        ".compiled",
        ".compiled/replay.json",
        ".aqa",
        ".aqa/locks",
        ".aqa/locks/replay.lock",
    ],
)
def test_a_link_explore_would_publish_through_is_refused_at_the_start(
    tmp_path: Path, place: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    link = a_link(root, place)

    with pytest.raises(SpecError) as refused:
        project(root)

    assert refused.value.problems == (
        f"{link}: a link, and explore writes only in {root}",
    )


@pytest.mark.parametrize("place", [".compiled", ".aqa/locks"])
def test_a_link_that_appears_during_the_run_publishes_nothing(
    tmp_path: Path, place: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    at = project(root)
    link = a_link(root, place)

    with pytest.raises(PublishError) as refused:
        published(at, script_for(at))

    assert str(refused.value) == (
        "a directory or file explore publishes through became a link"
    )
    assert sorted(path.name for path in link.resolve().iterdir()) == []
    assert not at.target.exists()


def test_a_compiled_file_that_cannot_be_read_raises_at_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".compiled" / "replay.json"
    real = os.open

    def unreadable(path: Any, flags: int, *args: Any, **options: Any) -> int:
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(path, flags, *args, **options)

    monkeypatch.setattr(os, "open", unreadable)

    with pytest.raises(PermissionError):
        project(tmp_path, existing=OLD)


CONTRACTED = (
    "secrets: {}\n"
    "subjects:\n"
    "  - spec: replay\n"
    "    expect: 0\n"
    "    region: div.banner\n"
    "    part: a.author\n"
)


@pytest.mark.parametrize("existing", [None, OLD])
def test_a_compiled_script_another_writer_wrote_meanwhile_is_not_overwritten(
    tmp_path: Path, existing: str | None
) -> None:
    at = project(tmp_path, existing=existing)
    at.target.parent.mkdir(exist_ok=True)
    at.target.write_text('{"theirs": 1}\n')

    with pytest.raises(PublishError) as refused:
        published(at, script_for(at))

    assert str(refused.value) == "the compiled script changed during the run"
    assert at.target.read_text() == '{"theirs": 1}\n'
    assert temporaries(tmp_path) == []


def test_a_target_appearing_between_the_comparison_and_the_link_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path)
    real = os.link

    def raced(source: Any, destination: Any, **options: Any) -> None:
        Path(destination).write_text('{"theirs": 1}\n')
        real(source, destination, **options)

    monkeypatch.setattr(os, "link", raced)

    with pytest.raises(PublishError) as refused:
        published(at, script_for(at))

    assert str(refused.value) == "the compiled script changed during the run"
    assert at.target.read_text() == '{"theirs": 1}\n'
    assert temporaries(tmp_path) == []


def test_a_spec_edited_during_the_run_writes_nothing(tmp_path: Path) -> None:
    at = project(tmp_path)
    script = script_for(at)
    a_spec(tmp_path, ProjectConfig(), start_url="/page/form")

    with pytest.raises(PublishError) as refused:
        published(at, script)

    assert str(refused.value) == "the spec changed during the run"
    assert not at.target.exists()


def test_a_subject_contract_changed_during_the_run_writes_nothing(
    tmp_path: Path,
) -> None:
    at = project(tmp_path, existing=OLD)
    script = script_for(at)
    (tmp_path / "config.yaml").write_text(CONTRACTED)

    with pytest.raises(PublishError) as refused:
        published(at, script)

    assert str(refused.value) == (
        "the project's subject contracts changed during the run"
    )
    assert at.target.read_text() == OLD


def test_a_spec_that_no_longer_loads_writes_nothing(tmp_path: Path) -> None:
    at = project(tmp_path)
    script = script_for(at)
    at.spec.unlink()

    with pytest.raises(PublishError) as refused:
        published(at, script)

    assert str(refused.value) == "the spec or its project config no longer loads"
    assert not at.target.exists()


def test_a_script_that_does_not_load_as_written_publishes_nothing(
    tmp_path: Path,
) -> None:
    at = project(tmp_path)
    # Its steps and assertion name targets it no longer has.
    script = script_for(at).model_copy(update={"targets": {}})

    with pytest.raises(PublishError) as refused:
        published(at, script)

    assert str(refused.value) == "the script doesn't load as written"
    assert not at.target.exists()


def holding(at: Publication) -> int:
    """A descriptor of another writer's, holding the spec's lock."""
    at.lock.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(at.lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return holder


def test_publication_gives_up_when_another_writer_holds_the_lock_past_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path)
    holder = holding(at)
    monkeypatch.setattr(publication_module, "LOCK_SECONDS", 0.2)

    try:
        with pytest.raises(PublishError) as refused:
            published(at, script_for(at))
    finally:
        os.close(holder)

    assert str(refused.value) == "another aqa process held this spec's publication lock"
    assert not at.target.exists()


def test_a_lock_acquired_after_its_bound_is_not_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path)
    holder = holding(at)
    monkeypatch.setattr(publication_module, "LOCK_SECONDS", 0.2)
    monkeypatch.setattr(publication_module, "_RETRY_SECONDS", 0.3)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        # Free by the time the retry wakes, which is past the bound.
        loop.call_later(0.25, os.close, holder)
        await publish(at, script_for(at), check=unchecked, deadline=loop.time() + 60)

    with pytest.raises(PublishError) as refused:
        asyncio.run(scenario())

    assert str(refused.value) == "another aqa process held this spec's publication lock"
    assert not at.target.exists()


def test_the_event_loop_runs_while_publication_waits_for_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path)
    holder = holding(at)
    monkeypatch.setattr(publication_module, "LOCK_SECONDS", 0.5)
    ticks: list[float] = []

    async def scenario() -> None:
        async def tick() -> None:
            while True:
                ticks.append(asyncio.get_running_loop().time())
                await asyncio.sleep(0.05)

        ticking = asyncio.create_task(tick())
        try:
            await publish(
                at,
                script_for(at),
                check=unchecked,
                deadline=asyncio.get_running_loop().time() + 60,
            )
        finally:
            ticking.cancel()

    try:
        with pytest.raises(PublishError):
            asyncio.run(scenario())
    finally:
        os.close(holder)

    # About ten ticks in the half second; a blocking wait would allow one.
    assert len(ticks) >= 5


def open_descriptors() -> int:
    return len(list(Path("/dev/fd").iterdir()))


def test_cancelling_a_publication_waiting_for_the_lock_writes_nothing_and_holds_nothing(
    tmp_path: Path,
) -> None:
    at = project(tmp_path)
    holder = holding(at)

    async def scenario() -> tuple[int, int]:
        before = open_descriptors()
        waiting = asyncio.create_task(
            publish(
                at,
                script_for(at),
                check=unchecked,
                deadline=asyncio.get_running_loop().time() + 60,
            )
        )
        await asyncio.sleep(0.2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        return before, open_descriptors()

    try:
        before, after = asyncio.run(scenario())
    finally:
        os.close(holder)

    assert after == before
    assert not at.target.parent.exists()


def outcome(at: Publication, script: CompiledScript) -> str:
    try:
        published(at, script)
    except PublishError as error:
        return f"PublishError: {error}"
    return "published"


def writer(root: str, confirmed: bool, events: dict[str, Any], done: Any) -> None:
    """One process's publication of `replay` over the file both found. The
    confirmed writer pauses right before installing, once it has compared,
    until `go`; the other starts publishing once it has."""
    config = load_config(Path(root) / "config.yaml")
    at = publication(Path(root), load_spec(Path(root, "qa", "replay.spec.md"), config))
    real = os.replace

    def paused(source: Any, destination: Any) -> None:
        events["compared"].set()
        events["go"].wait(30)
        real(source, destination)

    if not confirmed:
        events["begun"].set()
        events["compared"].wait(30)
    with patch.object(os, "replace", paused if confirmed else real):
        said = outcome(at, script_for(at, confirmed=confirmed))
    done.put((confirmed, said, time.monotonic()))


def test_two_writers_interleaved_at_the_comparison_publish_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path, existing=OLD)
    # Each writer's process imports this module by name, from the
    # repository's root, as pytest's importlib mode doesn't put it on the
    # path its processes inherit.
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3]))
    context = multiprocessing.get_context("spawn")
    events = {name: context.Event() for name in ("begun", "compared", "go")}
    done = context.Queue()
    writers = [
        context.Process(target=writer, args=(str(tmp_path), confirmed, events, done))
        for confirmed in (True, False)
    ]
    for each in writers:
        each.start()
    assert events["begun"].wait(60)
    assert events["compared"].wait(60)
    # The other writer is waiting for the lock meanwhile.
    time.sleep(1.0)
    finished_while_held = not done.empty()
    held_text = at.target.read_text()
    events["go"].set()
    results = sorted([done.get(timeout=60), done.get(timeout=60)], key=lambda r: r[2])
    for each in writers:
        each.join(30)

    assert finished_while_held is False
    assert held_text == OLD
    assert [(confirmed, said) for confirmed, said, _ in results] == [
        (True, "published"),
        (False, "PublishError: the compiled script changed during the run"),
    ]
    assert json.loads(at.target.read_text())["confirmed"] is True


UNWRITTEN = "the compiled script couldn't be written"
INSTALLED = (
    "the compiled script was published, but the file system failed after it, "
    "so it may not survive a crash"
)


def failing(code: int) -> OSError:
    return OSError(code, os.strerror(code))


def on_directory(handle: int) -> bool:
    return stat.S_ISDIR(os.fstat(handle).st_mode)


class Faults:
    """Operating system calls made to fail, or to take part of their
    bytes, only where publication makes them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.patch = monkeypatch
        self.write = os.write
        self.fsync = os.fsync
        self.unlink = os.unlink

    def short_writes(self) -> None:
        self.patch.setattr(os, "write", lambda fd, data: self.write(fd, data[:7]))

    def no_progress(self) -> None:
        self.patch.setattr(os, "write", lambda _fd, _data: 0)

    def full_after_five_bytes(self) -> None:
        calls: list[int] = []

        def write(fd: int, data: Any) -> int:
            calls.append(fd)
            if len(calls) == 1:
                return self.write(fd, data[:5])
            raise failing(errno.ENOSPC)

        self.patch.setattr(os, "write", write)

    def full(self) -> None:
        def write(_fd: int, _data: Any) -> int:
            raise failing(errno.ENOSPC)

        self.patch.setattr(os, "write", write)

    def temporary_unforced(self) -> None:
        def fsync(handle: int) -> None:
            if not on_directory(handle):
                raise failing(errno.EIO)
            self.fsync(handle)

        self.patch.setattr(os, "fsync", fsync)

    def directory_unforced(self) -> None:
        def fsync(handle: int) -> None:
            if on_directory(handle):
                raise failing(errno.EIO)
            self.fsync(handle)

        self.patch.setattr(os, "fsync", fsync)

    def rename(self) -> None:
        def replace(*_paths: Any, **_options: Any) -> None:
            raise failing(errno.EIO)

        self.patch.setattr(os, "replace", replace)

    def temporary_kept(self) -> None:
        def unlink(path: Any, **options: Any) -> None:
            if str(path).endswith(".tmp"):
                raise failing(errno.EACCES)
            self.unlink(path, **options)

        self.patch.setattr(os, "unlink", unlink)

    def full_and_temporary_kept(self) -> None:
        self.full()
        self.temporary_kept()


@pytest.mark.parametrize("existing", [None, OLD])
def test_short_writes_publish_the_whole_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str | None
) -> None:
    at = project(tmp_path, existing=existing)
    script = script_for(at)
    Faults(monkeypatch).short_writes()

    published(at, script)

    assert at.target.read_text(encoding="utf-8") == published_text(script)
    assert temporaries(tmp_path) == []


@pytest.mark.parametrize(
    ("fault", "existing", "kept"),
    [
        ("no_progress", OLD, 0),
        ("full_after_five_bytes", None, 0),
        ("temporary_unforced", OLD, 0),
        ("rename", OLD, 0),
        ("full_and_temporary_kept", OLD, 1),
    ],
)
def test_a_fault_before_installing_leaves_the_file_as_it_was(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    existing: str | None,
    kept: int,
) -> None:
    at = project(tmp_path, existing=existing)
    script = script_for(at)
    getattr(Faults(monkeypatch), fault)()

    with pytest.raises(PublishWriteError) as failed:
        published(at, script)

    assert failed.value.installed is False
    assert str(failed.value) == UNWRITTEN
    assert (at.target.read_text() if at.target.exists() else None) == existing
    assert len(temporaries(tmp_path)) == kept


@pytest.mark.parametrize(
    ("fault", "existing", "kept"),
    [
        ("directory_unforced", None, 0),
        ("directory_unforced", OLD, 0),
        ("temporary_kept", None, 1),
    ],
)
def test_a_fault_after_installing_says_the_script_was_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    existing: str | None,
    kept: int,
) -> None:
    at = project(tmp_path, existing=existing)
    script = script_for(at)
    getattr(Faults(monkeypatch), fault)()

    with pytest.raises(PublishWriteError) as failed:
        published(at, script)

    assert failed.value.installed is True
    assert str(failed.value) == INSTALLED
    assert at.target.read_text(encoding="utf-8") == published_text(script)
    assert len(temporaries(tmp_path)) == kept


def refusing_open(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    real = os.open

    def opened(given: Any, flags: int, *args: Any, **options: Any) -> int:
        if Path(given) == path:
            raise failing(errno.EACCES)
        return real(given, flags, *args, **options)

    monkeypatch.setattr(os, "open", opened)


@pytest.mark.parametrize("place", ["lock", "target"])
def test_a_lock_or_compiled_file_that_cannot_be_opened_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: str
) -> None:
    at = project(tmp_path, existing=OLD)
    refusing_open(monkeypatch, getattr(at, place))

    with pytest.raises(PublishWriteError) as failed:
        published(at, script_for(at))

    assert failed.value.installed is False
    assert str(failed.value) == UNWRITTEN
    assert at.target.read_text() == OLD


MINUTES = "the minutes budget ran out before the script was published"


def test_an_expired_deadline_publishes_nothing_even_with_the_lock_free(
    tmp_path: Path,
) -> None:
    at = project(tmp_path)

    with pytest.raises(PublishError) as refused:
        published(at, script_for(at), seconds=-1)

    assert str(refused.value) == MINUTES
    assert not (tmp_path / ".aqa").exists()
    assert not (tmp_path / ".compiled").exists()


def test_the_deadline_passing_while_waiting_for_the_lock_publishes_nothing(
    tmp_path: Path,
) -> None:
    at = project(tmp_path)
    holder = holding(at)

    try:
        with pytest.raises(PublishError) as refused:
            published(at, script_for(at), seconds=0.2)
    finally:
        os.close(holder)

    assert str(refused.value) == MINUTES
    assert not at.target.exists()


def test_the_deadline_is_checked_right_before_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path, existing=OLD)
    script = script_for(at)
    real = os.fsync

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        now = loop.time

        def fsync(handle: int) -> None:
            # The temporary is forced an hour after the deadline was set.
            real(handle)
            monkeypatch.setattr(loop, "time", lambda: now() + 3600)

        monkeypatch.setattr(os, "fsync", fsync)
        await publish(at, script, check=unchecked, deadline=now() + 60)

    with pytest.raises(PublishError) as refused:
        asyncio.run(scenario())

    assert str(refused.value) == MINUTES
    assert at.target.read_text() == OLD
    assert temporaries(tmp_path) == []


def lock_tries(monkeypatch: pytest.MonkeyPatch, error: OSError) -> list[int]:
    """Make every try of a lock fail with `error`, and count the tries."""
    tries: list[int] = []

    def flock(handle: int, _operation: int) -> None:
        tries.append(handle)
        raise error

    monkeypatch.setattr(fcntl, "flock", flock)
    return tries


def lock_releases(monkeypatch: pytest.MonkeyPatch, at: Publication) -> list[int]:
    """Make closing the lock's descriptor fail once it has closed, as
    Linux's `close` does on an I/O error, and count the closes."""
    real_open, real_close = os.open, os.close
    locks: set[int] = set()
    closes: list[int] = []

    def opened(path: Any, flags: int, *args: Any, **options: Any) -> int:
        handle = real_open(path, flags, *args, **options)
        if Path(path) == at.lock:
            locks.add(handle)
        return handle

    def closed(handle: int) -> None:
        real_close(handle)
        if handle in locks:
            locks.discard(handle)
            closes.append(handle)
            raise failing(errno.EIO)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    return closes


def test_a_lock_that_fails_other_than_by_contention_is_a_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path)
    tries = lock_tries(monkeypatch, failing(errno.ENOLCK))

    with pytest.raises(PublishWriteError) as failed:
        published(at, script_for(at))

    assert failed.value.installed is False
    assert str(failed.value) == UNWRITTEN
    assert len(tries) == 1
    assert not at.target.parent.exists()


def test_a_lock_release_that_fails_before_installing_keeps_the_reason_nothing_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path)
    at.target.parent.mkdir()
    at.target.write_text('{"theirs": 1}\n')
    closes = lock_releases(monkeypatch, at)

    with pytest.raises(PublishError) as refused:
        published(at, script_for(at))

    assert str(refused.value) == "the compiled script changed during the run"
    assert at.target.read_text() == '{"theirs": 1}\n'
    assert len(closes) == 1


def test_a_lock_release_that_fails_after_installing_says_the_script_was_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = project(tmp_path, existing=OLD)
    script = script_for(at)
    closes = lock_releases(monkeypatch, at)

    with pytest.raises(PublishWriteError) as failed:
        published(at, script)

    assert failed.value.installed is True
    assert str(failed.value) == INSTALLED
    assert at.target.read_text(encoding="utf-8") == published_text(script)
    assert len(closes) == 1


@pytest.mark.parametrize(
    ("fault", "existing", "installed"),
    [("full", OLD, False), ("directory_unforced", None, True)],
)
def test_a_lock_release_that_fails_while_a_write_error_propagates_keeps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    existing: str | None,
    installed: bool,
) -> None:
    at = project(tmp_path, existing=existing)
    script = script_for(at)
    getattr(Faults(monkeypatch), fault)()
    closes = lock_releases(monkeypatch, at)

    with pytest.raises(PublishWriteError) as failed:
        published(at, script)

    assert failed.value.installed is installed
    assert str(failed.value) == (INSTALLED if installed else UNWRITTEN)
    assert at.target.read_text(encoding="utf-8") == (
        published_text(script) if installed else OLD
    )
    assert len(closes) == 1
