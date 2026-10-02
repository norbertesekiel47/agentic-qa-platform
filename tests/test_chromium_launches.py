"""Only `aqa_runner.sandbox.launch` starts Chromium in packages/. Any other
launch of, or connection to, Chromium there fails here, so every browser the
packages use has passed the sandbox check (ADR-0026, #77).
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The one module in packages/ that may call Playwright's launch.
SANDBOX = "packages/runner/src/aqa_runner/sandbox.py"
# ADR-0026's negative controls, which launch without the sandbox on purpose.
NEGATIVE_CONTROLS = "packages/runner/tests/test_sandbox.py"
ALLOWED = [SANDBOX, NEGATIVE_CONTROLS]
ALLOWED_IDS = ["sandbox-module", "negative-controls"]

HOW_TO_FIX = (
    "launch it through aqa_runner.sandbox.launch, which proves the sandbox and "
    "gives the browser an empty environment (ADR-0026)"
)

# The BrowserType methods that start or attach to a browser, in Playwright
# 1.63 (https://playwright.dev/python/docs/api/class-browsertype). No other
# class has the last two, while `launch` and `connect` are common names, so
# those two count only on an object spelled `chromium`.
STARTS = {"launch", "connect"}
BROWSER_TYPE_ONLY = {"launch_persistent_context", "connect_over_cdp"}


def is_chromium(node: ast.expr) -> bool:
    """Whether an expression is spelled as Chromium's BrowserType:
    `playwright.chromium`, a name `chromium`, or `playwright["chromium"]`."""
    match node:
        case (
            ast.Attribute(attr="chromium")
            | ast.Name(id="chromium")
            | ast.Subscript(slice=ast.Constant(value="chromium"))
        ):
            return True
    return False


def chromium_starts(source: str) -> list[ast.Call]:
    """The calls in a module's source that launch or connect to Chromium."""
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and (
            node.func.attr in BROWSER_TYPE_ONLY
            or (node.func.attr in STARTS and is_chromium(node.func.value))
        )
    ]


def refusals(root: Path) -> list[str]:
    """One refusal for each Chromium launch or connection in the packages'
    source under `root`, outside the sandbox module."""
    return [
        f"{path.relative_to(root)}:{call.lineno}: {ast.unparse(call.func)}() "
        f"starts Chromium outside the sandbox check: {HOW_TO_FIX}"
        for path in sorted(root.glob("packages/*/src/**/*.py"))
        if path != root / SANDBOX
        for call in chromium_starts(path.read_text())
    ]


def test_packages_launch_chromium_only_through_the_sandbox_check() -> None:
    refused = refusals(REPO)

    assert not refused, "\n".join(refused)


# The gate's silence on the real tree means something only if it recognises
# the launches it allows.
@pytest.mark.parametrize("allowed", ALLOWED, ids=ALLOWED_IDS)
def test_the_gate_recognises_the_launches_it_allows(allowed: str) -> None:
    assert chromium_starts((REPO / allowed).read_text())


def plant(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


@pytest.mark.parametrize("allowed", ALLOWED, ids=ALLOWED_IDS)
def test_the_sandbox_module_and_package_tests_may_launch_directly(
    tmp_path: Path, allowed: str
) -> None:
    plant(
        tmp_path,
        allowed,
        "async def start(playwright):\n    return await playwright.chromium.launch()\n",
    )

    assert refusals(tmp_path) == []


ROGUE = "packages/runner/src/aqa_runner/rogue.py"

# One call to each method in STARTS and BROWSER_TYPE_ONLY.
FORMS = {
    "launch": "playwright.chromium.launch()",
    "launch_persistent_context": "playwright.chromium.launch_persistent_context(profile)",
    "connect": "playwright.chromium.connect(endpoint)",
    "connect_over_cdp": "playwright.chromium.connect_over_cdp(endpoint)",
}


@pytest.mark.parametrize("call", FORMS.values(), ids=list(FORMS))
def test_a_direct_chromium_call_fails_the_gate(tmp_path: Path, call: str) -> None:
    plant(
        tmp_path,
        ROGUE,
        f"async def start(playwright, profile, endpoint):\n    return await {call}\n",
    )

    refused = refusals(tmp_path)

    assert len(refused) == 1, refused
    assert refused[0].startswith(f"{ROGUE}:2: playwright.chromium."), refused
    assert "aqa_runner.sandbox.launch" in refused[0], refused


# The ways code reaches Chromium's BrowserType: Playwright's `chromium`
# attribute, a name for it, and Playwright's `__getitem__`. Only BrowserType has
# launch_persistent_context and connect_over_cdp, so those count on any object.
SPELLINGS = {
    "sync": "browser = playwright.chromium.launch()",
    "attribute-of-self": "browser = self.chromium.launch()",
    "bare-name": "browser = chromium.launch()",
    "subscript": 'browser = playwright["chromium"].connect(endpoint)',
    "persistent-context-on-any-object": "context = browser_type.launch_persistent_context(profile)",
    "cdp-on-any-object": "browser = browser_type.connect_over_cdp(endpoint)",
}


@pytest.mark.parametrize("statement", SPELLINGS.values(), ids=list(SPELLINGS))
def test_each_way_of_reaching_chromium_is_seen(statement: str) -> None:
    assert [call.lineno for call in chromium_starts(statement)] == [1]


# Other libraries' `connect` and the sandbox's own `launch` aren't Chromium's.
OTHER_CALLS = {
    "sandbox-launch": "sandbox.launch(playwright.chromium)",
    "imported-launch": "launch(playwright.chromium)",
    "sqlite": "sqlite3.connect(path)",
    "websockets": "websockets.connect(url)",
    "socket": "sock.connect(address)",
}


@pytest.mark.parametrize("call", OTHER_CALLS.values(), ids=list(OTHER_CALLS))
def test_other_launch_and_connect_calls_pass(call: str) -> None:
    assert chromium_starts(call) == []
