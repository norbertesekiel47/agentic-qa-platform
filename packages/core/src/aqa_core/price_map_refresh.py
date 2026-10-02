"""Refresh the vendored price map from LiteLLM's repository, in a reviewed pull
request (TECH_STACK §7). Run: `uv run python -m aqa_core.price_map_refresh [ref]`."""

import argparse
import hashlib
import http.client
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from aqa_core.price_map import (
    COMMIT,
    MAP_FILE,
    PIN_FILE,
    UPSTREAM,
    VENDORED,
    Pin,
    PriceMapError,
    load_price_map,
)

# Fetch(host, path): the body of an HTTPS GET.
Fetch = Callable[[str, str], bytes]

# A commit, a branch or a tag without a slash: letters, digits, `_` and `-`,
# with single dots between them. Nothing in it can end the request's path,
# start a query or step up a directory.
_REF = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]*(?:\.[0-9A-Za-z_-]+)*")


class RefreshError(Exception):
    """The refresh was refused; nothing was written."""


def _resolve_commit(ref: str, fetch: Fetch) -> str:
    if not _REF.fullmatch(ref):
        raise RefreshError(f"'{ref}' is not a commit, branch or tag name")
    try:
        answer = json.loads(fetch("api.github.com", f"/repos/{UPSTREAM}/commits/{ref}"))
    except ValueError:
        answer = None
    commit = answer.get("sha") if isinstance(answer, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(COMMIT, commit):
        raise RefreshError(f"GitHub gave no commit for '{ref}'")
    return commit


def refresh(ref: str, directory: Path = VENDORED, *, fetch: Fetch) -> Pin:
    """Copy the map at upstream's `ref` into `directory` and pin the commit
    that `ref` names, with the copy's sha256. Both files are written only
    after the pair loads."""
    commit = _resolve_commit(ref, fetch)
    data = fetch("raw.githubusercontent.com", f"/{UPSTREAM}/{commit}/{MAP_FILE}")
    pin = Pin(upstream=UPSTREAM, commit=commit, sha256=hashlib.sha256(data).hexdigest())
    with tempfile.TemporaryDirectory() as scratch:
        staged = Path(scratch)
        (staged / MAP_FILE).write_bytes(data)
        (staged / PIN_FILE).write_text(pin.model_dump_json(indent=2) + "\n")
        try:
            load_price_map(staged)
        except PriceMapError as error:
            raise RefreshError(
                f"the download at {commit} is unusable: {error}"
            ) from None
        shutil.copyfile(staged / MAP_FILE, directory / MAP_FILE)
        shutil.copyfile(staged / PIN_FILE, directory / PIN_FILE)
    return pin


def fetch_https(host: str, path: str) -> bytes:
    """The body of `https://<host><path>`. The host is fixed by the caller's
    constants, so only HTTPS is spoken; certificates are verified by default."""
    connection = http.client.HTTPSConnection(host, timeout=60)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "User-Agent": "aqa-price-map-refresh",
                "Accept": "application/vnd.github+json",
            },
        )
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise RefreshError(
            f"https://{host}{path} answered {response.status} {response.reason}"
        )
    return body


def main(
    argv: Sequence[str], *, fetch: Fetch = fetch_https, directory: Path = VENDORED
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aqa_core.price_map_refresh",
        description="Pin the price map to a commit of BerriAI/litellm.",
    )
    parser.add_argument("ref", nargs="?", default="main", help="commit, branch or tag")
    args = parser.parse_args(argv)
    try:
        pin = refresh(args.ref, directory, fetch=fetch)
    except (RefreshError, OSError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(f"pinned {pin.upstream} {pin.commit}\nsha256 {pin.sha256}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
