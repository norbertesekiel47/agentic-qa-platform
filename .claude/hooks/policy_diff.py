"""What ``policy_guard.py --diff`` reads from git, and what it asks beyond an edit.

policy_guard.py judges each changed file with the check an edit gets, and holds
the ``--diff`` entry point. This module finds the files the working tree changes
since the merge base, reads both sides as git stores them, and adds the asks
that only a whole-file view can raise: a deleted test file, a gate config file
that comes or goes or can't be read, a changed file that proves the bar, a
symbolic link. Like the guard, it runs on whatever python3 Claude Code finds,
so it needs only the standard library.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from policy_rules import (
    BAR_TESTS,
    GATE_WHOLE_FILE,
    TEST_FILE,
    binary_or_lockfile,
    is_exempt,
)

LINK_MODE = "120000"  # git's mode for a symbolic link


@dataclass(frozen=True)
class Change:
    """One file the working tree changes since the merge base."""

    rel: str
    added: bool  # absent at the merge base: new, or untracked
    deleted: bool  # absent from the working tree
    link: bool  # a symbolic link on either side


def git(cwd: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, check=True, timeout=20
    ).stdout


def names(output: bytes) -> list[str]:
    """git's -z output as file names: each one ends with a NUL."""
    return os.fsdecode(output).split("\0")[:-1]


def merge_base(cwd: Path, base: str) -> tuple[Path, str]:
    """The repository's top level, and the merge base of `base` and HEAD there."""
    # git names files from the top level, whichever directory it runs in.
    root = Path(os.fsdecode(git(cwd, "rev-parse", "--show-toplevel")).strip())
    commit = git(
        root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"
    )
    return root, git(
        root, "merge-base", commit.decode().strip(), "HEAD"
    ).decode().strip()


def changed_files(root: Path, since: str) -> list[Change]:
    """Each file the working tree changes since `since`, untracked files included."""
    # --raw: ":<old mode> <new mode> <old blob> <new blob> <status>", then the path.
    fields = names(git(root, "diff", "--raw", "--no-renames", "-z", since))
    changes: dict[str, Change] = {}
    for meta, rel in zip(fields[0::2], fields[1::2], strict=True):
        old_mode, new_mode, _, _, status = meta.lstrip(":").split()
        link = LINK_MODE in {old_mode, new_mode}
        changes[rel] = Change(rel, status == "A", status == "D", link)
    for rel in names(git(root, "ls-files", "-z", "--others", "--exclude-standard")):
        # Dropped from the index but still on disk, a file has changed content,
        # if any, not gone.
        tracked = changes.get(rel)
        link = (root / rel).is_symlink() or bool(tracked and tracked.link)
        changes[rel] = Change(rel, tracked is None, False, link)
    return list(changes.values())


def texts(root: Path, since: str, change: Change) -> tuple[bytes, bytes] | None:
    """The file at `since` and in the working tree, as git stores it: a link is
    the name it points to, never the file behind it. None for a binary or a
    lockfile, whose content no rule reads."""
    if binary_or_lockfile(change.rel):
        return None
    blob = (
        b"" if change.added else git(root, "cat-file", "blob", f"{since}:{change.rel}")
    )
    path = root / change.rel
    if change.deleted:
        after = b""
    elif path.is_symlink():
        after = os.fsencode(path.readlink())
    else:
        # A read error ends the run (exit 2): judged as emptied, the file
        # would go unchecked.
        after = path.read_bytes()
    return blob, after


def diff_asks(
    change: Change, judged: list[str], *, read: bool, rewritten: bool
) -> list[str]:
    """What --diff asks about beyond an edit's judgment (`judged`), which never
    sees a whole file come or go, or a link. `read`: its content was read;
    `rewritten`: its bytes changed, or weren't read."""
    rel = change.rel
    if is_exempt(rel):
        return []
    asks = []
    if change.link:
        asks.append(
            f"{rel} is or was a symbolic link. --diff judges a link by the name it "
            "points to, so a later change to the file behind it goes unseen."
        )
    if judged:  # the judgment already explains this file
        return asks
    if change.deleted and TEST_FILE.search(rel):
        asks.append(
            f"{rel}: this change deletes a test file. Approve only if its "
            "checks are obsolete, not inconvenient."
        )
    elif GATE_WHOLE_FILE.search(rel) and (change.added or change.deleted or not read):
        asks.append(
            f"{rel} is a quality-gate config file that this change adds, deletes or "
            "--diff can't read: the tools read it, even empty."
        )
    elif not change.added and rewritten and BAR_TESTS.search(rel):
        asks.append(
            f"{rel} proves the bar: a change there can weaken an expectation "
            "without removing an assertion."
        )
    return asks
