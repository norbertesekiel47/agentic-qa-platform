"""Publishing a compiled script (ADR-0024's #53 P16 amendment; DATA_MODEL
§7) at `<spec root>/.compiled/<spec id>.json`, only under the spec's
publication lock, `<spec root>/.aqa/locks/<spec id>.lock`.

The lock is taken without blocking the event loop, before the run's
deadline and within `LOCK_SECONDS`. Inside it, with no `await` until it is
released, the spec and the project config are read again, and the script
is installed only while its `spec_hash` and subject-contract fingerprint
are still theirs, the file is as the run found it, and the deadline hasn't
passed. A writer that takes no lock (an editor, `git checkout`) can still
change the file between the comparison and the install.

Installing is one `link` (the run found no file) or `rename` (it found
one) of a temporary file beside the target holding the whole text, forced
to disk. A failure before it leaves the file as it was. After it, the file
holds the script whatever fails next, so a failure there, forcing the
directory, removing the temporary or releasing the lock, says so. Nothing
is rolled back or retried.

Links are refused by path where checked, at the run's start and inside the
lock, as the run record's are: a local process that swaps a checked
directory between the check and a write can still redirect it. macOS and
Linux only, as M1 is (`fcntl.flock`)."""

import asyncio
import errno
import fcntl
import hashlib
import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Final

from aqa_core.compiled import CompiledScript
from aqa_core.project import (
    SpecError,
    contracts_fingerprint,
    load_config,
    load_spec,
    parse_compiled,
)
from aqa_core.spec import Spec

from aqa_runner.run_record import LineCheck

# How long publication waits for another writer's lock, at most, and how
# often it tries it meanwhile.
LOCK_SECONDS: Final = 10.0
_RETRY_SECONDS: Final = 0.05
_CHANGED: Final = "the compiled script changed during the run"


class PublishError(Exception):
    """Nothing was published, and why, in fixed words: no path, no value."""


class PublishWriteError(Exception):
    """A file failed while publishing, and whether the script was installed
    before it did. Fixed words: no path, never the OS error."""

    def __init__(self, *, installed: bool) -> None:
        super().__init__(
            "the compiled script was published, but the file system failed after "
            "it, so it may not survive a crash"
            if installed
            else "the compiled script couldn't be written"
        )
        self.installed = installed


@dataclass(frozen=True)
class Found:
    """The compiled file as a run found it: its content's digest and what
    the file system says of it."""

    digest: str
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Publication:
    """Where a run publishes and what it found there when it began: the spec
    root, the spec's file and ID, and the compiled file, None when absent."""

    root: Path
    spec: Path
    spec_id: str
    found: Found | None

    @property
    def target(self) -> Path:
        return self.root / ".compiled" / f"{self.spec_id}.json"

    @property
    def lock(self) -> Path:
        return self.root / ".aqa" / "locks" / f"{self.spec_id}.lock"


def publication(root: Path, spec: Spec) -> Publication:
    """Where `spec`, of the project at `root`, publishes, and the compiled
    file as found there now, at the run's start. A link on the way, or at
    the file or its lock, is a spec error: a repository can commit one
    pointing anywhere. A file that can't be read raises its `OSError`."""
    at = Publication(root, spec.path, spec.frontmatter.id, None)
    if (link := _link(at)) is not None:
        raise SpecError([f"{link}: a link, and explore writes only in {root}"])
    return replace(at, found=_found(at.target))


def published_text(script: CompiledScript) -> str:
    """The exact text a compiled script is published as: JSON without null
    fields, indented for reviewable diffs, with a final newline."""
    document = script.model_dump(mode="json", exclude_none=True)
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


async def publish(
    at: Publication, script: CompiledScript, *, check: LineCheck, deadline: float
) -> None:
    """Publish `script` at `at` as `published_text` gives it, once `check`
    passes that exact text and the spec's lock is held, before `deadline`,
    a time on the running loop's clock (module docstring). `check`'s
    refusal raises as it came, before any file is touched. A file that
    failed, the lock's included, is `PublishWriteError`, which says whether
    the script was installed; any other reason nothing was published is
    `PublishError`."""
    _due(deadline)
    text = published_text(script)
    check(text)
    lock = await _locked(at, deadline)
    try:
        _due(deadline)
        _replace(at, script, text, deadline)
    except BaseException:
        _quietly(partial(os.close, lock))
        raise
    try:
        os.close(lock)
    except OSError:
        # `_replace` returned, so the script is installed.
        raise PublishWriteError(installed=True) from None


async def _locked(at: Publication, deadline: float) -> int:
    """A descriptor holding the spec's publication lock. The lock file is
    made if missing and never removed, so every writer locks one inode.
    Closing the descriptor releases the lock."""
    _unlinked(at)
    try:
        at.lock.parent.mkdir(parents=True, exist_ok=True)
        lock = os.open(at.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except OSError:
        raise PublishWriteError(installed=False) from None
    try:
        await _acquired(lock, deadline)
    except BaseException:
        _quietly(partial(os.close, lock))
        raise
    return lock


async def _acquired(lock: int, deadline: float) -> None:
    """Take the lock on `lock`, tried at once, then every `_RETRY_SECONDS`
    while neither `LOCK_SECONDS` nor `deadline` has passed, both checked
    before each try, so no late acquisition is taken."""
    loop = asyncio.get_running_loop()
    held = loop.time() + LOCK_SECONDS
    while True:
        _due(deadline)
        if loop.time() >= held:
            raise PublishError("another aqa process held this spec's publication lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            await asyncio.sleep(_RETRY_SECONDS)
        except OSError:
            raise PublishWriteError(installed=False) from None
        else:
            return


def _due(deadline: float) -> None:
    if asyncio.get_running_loop().time() >= deadline:
        raise PublishError("the minutes budget ran out before the script was published")


def _replace(
    at: Publication, script: CompiledScript, text: str, deadline: float
) -> None:
    """Compare, then install: no `await`, so no cancellation lands in
    between and nothing else on the loop runs."""
    try:
        config = load_config(at.root / "config.yaml")
        spec = load_spec(at.spec, config)
    except SpecError, OSError:
        raise PublishError("the spec or its project config no longer loads") from None
    if spec.spec_hash != script.spec_hash:
        raise PublishError("the spec changed during the run")
    if (
        contracts_fingerprint(config, at.spec_id)
        != script.compiled_by.subject_contracts
    ):
        raise PublishError("the project's subject contracts changed during the run")
    try:
        parse_compiled(text, config, source=at.target)
    except SpecError:
        raise PublishError("the script doesn't load as written") from None
    _unlinked(at)
    try:
        found = _found(at.target)
    except OSError:
        raise PublishWriteError(installed=False) from None
    if found != at.found:
        raise PublishError(_CHANGED)
    _install(at, text.encode(), deadline)


def _install(at: Publication, data: bytes, deadline: float) -> None:
    """Write `data` whole to a new temporary file beside the target,
    forced, then install it: `link` when the run found no file, which fails
    if one appeared, else `rename`. A failure before installing removes the
    temporary and keeps that failure; one after says the script is
    installed."""
    directory = at.target.parent
    temporary = directory / f".{at.spec_id}.{os.urandom(8).hex()}.tmp"
    try:
        directory.mkdir(exist_ok=True)
        _write_new(temporary, data)
        _due(deadline)
        if at.found is None:
            os.link(temporary, at.target)
        else:
            temporary.replace(at.target)
    except FileExistsError:
        _quietly(temporary.unlink)
        raise PublishError(_CHANGED) from None
    except OSError:
        _quietly(temporary.unlink)
        raise PublishWriteError(installed=False) from None
    except PublishError:
        _quietly(temporary.unlink)
        raise
    try:
        if at.found is None:
            temporary.unlink()
        _sync(directory)
    except OSError:
        raise PublishWriteError(installed=True) from None


def _write_new(path: Path, data: bytes) -> None:
    """Make `path` holding all of `data`, forced to disk. A write that
    takes no byte is an error, as one that fails is."""
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        left = memoryview(data)
        while left:
            taken = os.write(handle, left)
            if taken == 0:
                raise OSError(errno.EIO, "a write took none of its bytes")
            left = left[taken:]
        os.fsync(handle)
    finally:
        os.close(handle)


def _sync(directory: Path) -> None:
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _quietly(cleanup: Callable[[], object]) -> None:
    # Cleanup after the failure to report: if it fails too, it never
    # replaces that failure, and it isn't tried again.
    with suppress(OSError):
        cleanup()


def _found(target: Path) -> Found | None:
    """The file at `target` as it is now, None when absent."""
    try:
        handle = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(handle, "rb") as file:
        stat = os.fstat(file.fileno())
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    return Found(digest, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _link(at: Publication) -> Path | None:
    compiled, locks = at.target.parent, at.lock.parent
    places = (compiled, at.target, locks.parent, locks, at.lock)
    return next((place for place in places if place.is_symlink()), None)


def _unlinked(at: Publication) -> None:
    if _link(at) is not None:
        raise PublishError(
            "a directory or file explore publishes through became a link"
        )
