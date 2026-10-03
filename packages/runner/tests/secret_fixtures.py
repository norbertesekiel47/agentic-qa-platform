"""Fixtures the test-secret tests share (#49): fake values, and a spec that
references test secrets, loaded with its project config. Imported by its
path, as pytest names the runner's test modules (TESTING.md §1, Shared
egress fixtures)."""

import json
from base64 import b64encode, urlsafe_b64encode
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote, quote_plus

from aqa_core.project import load_config, load_spec
from aqa_core.spec import Spec

# Values no real environment holds, each saying it is fake (AGENTS.md rule
# 9), with characters that URL, base64 and JSON encodings each change.
FAKE_VALUE = "fake-pässword+/= for #49"
OTHER_FAKE_VALUE = "fake-api-token-for-49"

# A config's binding of TEST_PASSWORD to password fields on the start origin.
BOUND_AT_START = "TEST_PASSWORD: { origins: [start], field: password }"


def secret_spec(
    tmp_path: Path,
    secrets: str = BOUND_AT_START,
    *,
    account: str = "{ email: reader@example.test, password: { secret: TEST_PASSWORD } }",
    start_url: str = "/login",
    allowed_origins: tuple[str, ...] = (),
) -> Spec:
    """A spec whose account is `account`, loaded with a project config that
    declares `secrets`, a YAML mapping's entries."""
    root = tmp_path / "qa"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(f"secrets: {{ {secrets} }}\n")
    path = root / "login.spec.md"
    path.write_text(
        "---\n"
        "id: login\n"
        "goal: A reader signs in.\n"
        f"preconditions:\n  start_url: '{start_url}'\n  account: {account}\n"
        "expect:\n  - The home page is shown\n"
        f"allowed_origins: [{', '.join(repr(o) for o in allowed_origins)}]\n"
        "---\n"
    )
    return load_spec(path, load_config(root / "config.yaml"))


def copies(value: str) -> list[str]:
    """`value` as it is, and in the simple encodings a page or a writer could
    give it (SECURITY §5): URL-encoded, base64 and JSON-escaped."""
    raw = value.encode()
    return [
        value,
        quote(value, safe=""),
        quote_plus(value),
        b64encode(raw).decode(),
        urlsafe_b64encode(raw).decode(),
        json.dumps(value)[1:-1],
    ]


def copies_found(
    value: str, *, files: Iterable[Path] = (), texts: Iterable[str] = ()
) -> list[str]:
    """Where a copy of `value` (`copies`) is: each file, by path, and each
    text, by its index, that holds one. A scan for #49's "never stored", and
    for what #50 and #53 save."""
    found: list[str] = []
    for path in files:
        held = path.read_bytes().decode("utf-8", errors="replace")
        found.extend(str(path) for copy in copies(value) if copy in held)
    for index, text in enumerate(texts):
        found.extend(f"text {index}" for copy in copies(value) if copy in text)
    return found
