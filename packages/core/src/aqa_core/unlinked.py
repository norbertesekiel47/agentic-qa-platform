"""Reading a file below a directory through no link (#249, #324): each
directory on the way is opened relative to the one before it, never
following a link, and the file is read only when it is a regular one. `aqa
report` reads a run's record this way (`aqa_cli.report_record`), the
loader checks a `pixel_diff` baseline's path with it, and strict replay
reads the baseline with it (`aqa_runner.pixel_diff`)."""

import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Final

# A directory, and a file, each opened only if it isn't a link. The file is
# opened without blocking, so a FIFO never stalls the read.
_FOLDER: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class Unread(Enum):
    """Why `read_below` read nothing: the file or a directory on the way
    doesn't exist; or it is a link, isn't a regular file, or raised another
    `OSError`."""

    ABSENT = "absent"
    BROKEN = "broken"


@contextmanager
def folder(root: Path, folders: Sequence[str]) -> Iterator[int]:
    """The directory `folders` names below `root`, open, each step through no link."""
    handle = os.open(root, _FOLDER)
    try:
        for name in folders:
            inner = os.open(name, _FOLDER, dir_fd=handle)
            os.close(handle)
            handle = inner
        yield handle
    finally:
        os.close(handle)


def read_below(root: Path, name: str) -> bytes | Unread:
    """The bytes of the regular file `name`, a relative path of names below
    the directory `root`, reached through no link; `Unread.ABSENT` when it or
    a directory on the way doesn't exist, and `Unread.BROKEN` for a link,
    anything but a regular file, or an `OSError`."""
    *folders, leaf = name.split("/")
    try:
        with folder(root, folders) as handle:
            file = os.open(leaf, _FILE, dir_fd=handle)
            try:
                if not stat.S_ISREG(os.fstat(file).st_mode):
                    return Unread.BROKEN
                return _whole(file)
            finally:
                os.close(file)
    except FileNotFoundError:
        return Unread.ABSENT
    except OSError:
        return Unread.BROKEN


def _whole(file: int) -> bytes:
    chunks = []
    while chunk := os.read(file, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)
