"""Only `aqa_runner.sandbox.launch` starts Chromium in the packages' source
(`packages/*/src`). Any other launch of, or connection to, Chromium there, as
far as its spelling shows, fails here, so every browser the packages use has
passed the sandbox check (ADR-0026, #77).
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The one module in packages/*/src that may start Chromium, through launch.
SANDBOX = "packages/runner/src/aqa_runner/sandbox.py"
# ADR-0026's negative controls, which launch without the sandbox on purpose.
# The gate scans no package tests.
NEGATIVE_CONTROLS = "packages/runner/tests/test_sandbox.py"

HOW_TO_FIX = (
    "launch Chromium through aqa_runner.sandbox.launch, which proves the sandbox "
    "and gives the browser an empty environment. The check can't prove a browser "
    "it didn't launch, so a connection can't pass it (ADR-0026)"
)

# The BrowserType methods that start or attach to a browser, in Playwright
# 1.63 (https://playwright.dev/python/docs/api/class-browsertype). No other
# class has launch_persistent_context or connect_over_cdp. `launch` and
# `connect` are common names, so they count only on an object spelled
# `chromium`.
ON_ANY_OBJECT = {"launch_persistent_context", "connect_over_cdp"}
ON_CHROMIUM = {"launch", "connect"}


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


def chromium_starts(source: str) -> list[ast.Attribute]:
    """Each reference in a module's source to a method that launches or
    connects to Chromium, whether it's called there or passed on."""
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and (
            node.attr in ON_ANY_OBJECT
            or (node.attr in ON_CHROMIUM and is_chromium(node.value))
        )
    ]


def refusals(root: Path) -> list[str]:
    """One refusal for each Chromium launch or connection in the packages'
    source under `root`, outside the sandbox module."""
    return [
        f"{path.relative_to(root)}:{start.lineno}: {ast.unparse(start)} launches "
        f"or connects to Chromium outside the sandbox check: {HOW_TO_FIX}"
        for path in sorted(root.glob("packages/*/src/**/*.py"))
        if path != root / SANDBOX
        for start in chromium_starts(path.read_text())
    ]


def locations(refused: list[str]) -> list[str]:
    """Each refusal's `path:line`."""
    return sorted(refusal.split(": ", 1)[0] for refusal in refused)


def test_packages_launch_chromium_only_through_the_sandbox_check() -> None:
    refused = refusals(REPO)

    assert not refused, "\n".join(refused)


# The gate leaves the sandbox module alone, so nothing there may start Chromium
# except launch's own call to Playwright, which the sandbox check follows.
def test_launch_is_the_sandbox_modules_only_chromium_start() -> None:
    module = ast.parse((REPO / SANDBOX).read_text())
    [launch] = [
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "launch"
    ]

    in_module = [ast.unparse(start) for start in chromium_starts(ast.unparse(module))]
    in_launch = [ast.unparse(start) for start in chromium_starts(ast.unparse(launch))]

    assert in_module == in_launch == ["chromium.launch"]


# The files the gate leaves alone, each with a start it must still see there:
# its silence on the real tree means something only if it recognises them.
LEFT_ALONE = {
    "sandbox-module": (SANDBOX, "chromium.launch"),
    "negative-controls": (NEGATIVE_CONTROLS, "playwright.chromium.launch"),
}


@pytest.mark.parametrize(("path", "start"), LEFT_ALONE.values(), ids=list(LEFT_ALONE))
def test_the_gate_recognises_the_launches_it_leaves_alone(
    path: str, start: str
) -> None:
    found = [ast.unparse(node) for node in chromium_starts((REPO / path).read_text())]

    assert start in found, found


def plant(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


@pytest.mark.parametrize(
    "path", [path for path, _ in LEFT_ALONE.values()], ids=list(LEFT_ALONE)
)
def test_the_sandbox_module_and_package_tests_may_launch_directly(
    tmp_path: Path, path: str
) -> None:
    plant(tmp_path, path, (REPO / path).read_text())

    assert refusals(tmp_path) == []


# Every package's source is scanned, at any depth, and only the runner's
# sandbox module is left alone, not every file of that name.
SCANNED = {
    "runner": "packages/runner/src/aqa_runner/rogue.py",
    "nested-in-cli": "packages/cli/src/aqa_cli/commands/run/rogue.py",
    "core": "packages/core/src/aqa_core/rogue.py",
    "another-sandbox-module": "packages/core/src/aqa_core/sandbox.py",
}


@pytest.mark.parametrize("path", SCANNED.values(), ids=list(SCANNED))
def test_every_module_in_the_packages_source_is_scanned(
    tmp_path: Path, path: str
) -> None:
    plant(tmp_path, path, "browser = playwright.chromium.launch()\n")

    assert locations(refusals(tmp_path)) == [f"{path}:1"]


ROGUE = "packages/runner/src/aqa_runner/rogue.py"


def test_each_start_in_a_module_is_refused(tmp_path: Path) -> None:
    plant(
        tmp_path,
        ROGUE,
        "first = playwright.chromium.launch()\nsecond = chromium.connect(endpoint)\n",
    )

    assert locations(refusals(tmp_path)) == [f"{ROGUE}:1", f"{ROGUE}:2"]


# One call to each method in ON_ANY_OBJECT and ON_CHROMIUM.
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
# A method counts whether it's called on the spot or passed on to be called.
SPELLINGS = {
    "playwright-attribute": "browser = playwright.chromium.launch()",
    "self-attribute": "browser = self.chromium.launch()",
    "bare-name": "browser = chromium.launch()",
    "subscript": 'browser = playwright["chromium"].connect(endpoint)',
    "persistent-context-on-any-object": "context = browser_type.launch_persistent_context(profile)",
    "cdp-on-any-object": "browser = browser_type.connect_over_cdp(endpoint)",
    "method-reference": "start = playwright.chromium.launch",
    "partial": "start = functools.partial(playwright.chromium.launch)",
}


@pytest.mark.parametrize("statement", SPELLINGS.values(), ids=list(SPELLINGS))
def test_each_way_of_reaching_chromium_is_seen(statement: str) -> None:
    assert [start.lineno for start in chromium_starts(statement)] == [1]


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
