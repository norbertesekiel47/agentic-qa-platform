"""Fixtures the test-secret tests share (#49): fake values, and a spec that
references test secrets, loaded with its project config. Imported by its
path, as pytest names the runner's test modules (TESTING.md §1, Shared
egress fixtures)."""

from pathlib import Path

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
