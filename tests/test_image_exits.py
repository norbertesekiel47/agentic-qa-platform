"""Only `aqa_runner.screenshots.take_screenshot` makes an image in the
packages' source (`packages/*/src`), and it OCR-checks the PNG before a
`Screenshot` holds it (#250, ADR-0035). Any other image capture there, as
far as its spelling shows, fails here. With the `Screenshot` no other code
can make and `RunRecord.write_screenshot`, which writes only one
(`packages/runner/tests/test_screenshots.py`), no code path saves or sends
an image that hasn't passed the check."""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

SCREENSHOTS = "packages/runner/src/aqa_runner/screenshots.py"

HOW_TO_FIX = (
    "take an image only through aqa_runner.screenshots.take_screenshot, which "
    "OCR-checks it before a Screenshot holds it (ADR-0035)"
)


# The DevTools methods that return an image of the page or an archive of
# its images (https://chromedevtools.github.io/devtools-protocol/tot/Page/,
# /HeadlessExperimental/).
DEVTOOLS = {
    "Page.captureScreenshot",
    "Page.captureSnapshot",
    "Page.startScreencast",
    "Page.printToPDF",
    "HeadlessExperimental.beginFrame",
}
# Playwright 1.63's: a page's, locator's or element's `screenshot`, a page's
# `pdf`, and the methods of a context's `tracing` that start a trace, which
# can hold screenshots. A context's `record_video_dir` records video.
ON_ANY_OBJECT = {"screenshot", "pdf"}
ON_TRACING = {"start", "start_chunk"}

# The captures the screenshots module makes, each in its function: the live
# page's and its archive in `_captured`, the render's in `_render`.
CHECKED = {
    ("_captured", "Page.captureScreenshot"),
    ("_captured", "Page.captureSnapshot"),
    ("_render", "screenshot"),
}


def captures(tree: ast.AST) -> list[tuple[int, str]]:
    """Each image capture in `tree`: its line and what it calls."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        match node:
            case ast.Attribute(attr=name) if name in ON_ANY_OBJECT:
                found.append((node.lineno, name))
            case ast.Attribute(attr=name, value=ast.Attribute(attr="tracing")) if (
                name in ON_TRACING
            ):
                found.append((node.lineno, f"tracing.{name}"))
            case ast.keyword(arg="record_video_dir"):
                found.append((node.lineno, "record_video_dir"))
            case ast.Constant(value=str(text)) if text in DEVTOOLS:
                found.append((node.lineno, text))
    return found


def checked(tree: ast.Module) -> set[tuple[int, str]]:
    """The screenshots module's captures that are in their functions."""
    return {
        (line, name)
        for function in tree.body
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        for line, name in captures(function)
        if (function.name, name) in CHECKED
    }


def refusals(root: Path) -> list[str]:
    """One refusal for each image capture in the packages' source under
    `root`, outside the capture functions of the screenshots module."""
    refused: list[str] = []
    for path in sorted(root.glob("packages/*/src/**/*.py")):
        tree = ast.parse(path.read_text())
        allowed = checked(tree) if path == root / SCREENSHOTS else set()
        refused.extend(
            f"{path.relative_to(root)}:{line}: {name} makes an image outside "
            f"the checked capture: {HOW_TO_FIX}"
            for line, name in captures(tree)
            if (line, name) not in allowed
        )
    return refused


def references(source: str, name: str) -> list[str]:
    """The module's top-level functions and classes that read `name`."""
    return [
        definition.name
        for definition in ast.parse(source).body
        if isinstance(definition, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and any(
            isinstance(node, ast.Name) and node.id == name
            for node in ast.walk(definition)
        )
    ]


def plant(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_packages_make_images_only_in_the_checked_capture() -> None:
    refused = refusals(REPO)

    assert not refused, "\n".join(refused)


# The live page's capture, which `take_screenshot` alone calls, and the
# render's, which only it reaches; the key that makes a Screenshot, which
# only `take_screenshot` hands over, after the OCR check.
def test_only_take_screenshot_reaches_a_capture_or_makes_a_screenshot() -> None:
    source = (REPO / SCREENSHOTS).read_text()

    assert references(source, "_captured") == ["take_screenshot"]
    assert references(source, "_render") == ["_captured"]
    assert references(source, "_CAPTURED") == ["Screenshot", "take_screenshot"]


# The gate's silence on the real tree means something only if it sees the
# captures it leaves alone.
def test_the_gate_sees_the_captures_it_leaves_alone() -> None:
    found = captures(ast.parse((REPO / SCREENSHOTS).read_text()))

    assert sorted(name for _, name in found) == [
        "Page.captureScreenshot",
        "Page.captureSnapshot",
        "screenshot",
    ]


# One of each spelling the gate refuses: Playwright 1.63's image captures
# (https://playwright.dev/python/docs/api/class-page#page-screenshot, and
# `pdf`, a context's `record_video_dir`, tracing's screenshots), and the
# DevTools methods that return an image or an archive of the page's images
# (https://chromedevtools.github.io/devtools-protocol/tot/Page/).
SPELLINGS = {
    "page-screenshot": "image = await page.screenshot()",
    "locator-screenshot": "image = await page.locator('form').screenshot()",
    "pdf": "document = await page.pdf()",
    "video": "context = await browser.new_context(record_video_dir='videos')",
    "tracing": "await context.tracing.start(screenshots=True)",
    "tracing-chunk": "await context.tracing.start_chunk()",
    "devtools-screenshot": "shot = await devtools.send('Page.captureScreenshot')",
    "devtools-archive": "archive = await devtools.send('Page.captureSnapshot')",
    "devtools-screencast": "await devtools.send('Page.startScreencast')",
    "devtools-pdf": "await devtools.send('Page.printToPDF')",
    "devtools-frame": "await devtools.send('HeadlessExperimental.beginFrame')",
}


@pytest.mark.parametrize("statement", SPELLINGS.values(), ids=list(SPELLINGS))
def test_each_way_of_making_an_image_is_seen(statement: str) -> None:
    assert [line for line, _ in captures(ast.parse(statement))] == [1]


OTHER_CALLS = {
    "layout": "metrics = await devtools.send('Page.getLayoutMetrics')",
    "dom-snapshot": "dom = await devtools.send('DOMSnapshot.captureSnapshot')",
    "screenshots-module": "from aqa_runner import screenshots",
    "taking": "shot = await screenshots.take_screenshot(session)",
    "tracing-module": "from aqa_runner.tracing import ignore_ambient_tracing",
    "task-start": "worker.start()",
}


@pytest.mark.parametrize("statement", OTHER_CALLS.values(), ids=list(OTHER_CALLS))
def test_other_calls_pass(statement: str) -> None:
    assert captures(ast.parse(statement)) == []


# Every package's source is scanned, at any depth, and only the runner's
# screenshots module is left alone, not every file of that name.
SCANNED = {
    "runner": "packages/runner/src/aqa_runner/rogue.py",
    "nested-in-cli": "packages/cli/src/aqa_cli/commands/run/rogue.py",
    "core": "packages/core/src/aqa_core/rogue.py",
    "another-screenshots-module": "packages/core/src/aqa_core/screenshots.py",
}


@pytest.mark.parametrize("path", SCANNED.values(), ids=list(SCANNED))
def test_every_module_in_the_packages_source_is_scanned(
    tmp_path: Path, path: str
) -> None:
    plant(tmp_path, path, "image = await page.screenshot()\n")

    refused = refusals(tmp_path)

    assert [refusal.split(": ", 1)[0] for refusal in refused] == [f"{path}:1"]
    assert "take_screenshot" in refused[0]


def test_the_screenshots_module_passes_as_it_is(tmp_path: Path) -> None:
    plant(tmp_path, SCREENSHOTS, (REPO / SCREENSHOTS).read_text())

    assert refusals(tmp_path) == []


# In the screenshots module itself, a capture anywhere but the two capture
# functions is refused, and so is a capture function's call from elsewhere.
def test_a_capture_elsewhere_in_the_screenshots_module_is_refused(
    tmp_path: Path,
) -> None:
    source = (REPO / SCREENSHOTS).read_text()
    rogue = "\n\nasync def _peek(page):\n    return await page.screenshot()\n"
    plant(tmp_path, SCREENSHOTS, source + rogue)
    line = source.count("\n") + 4

    assert [refusal.split(": ", 1)[0] for refusal in refusals(tmp_path)] == [
        f"{SCREENSHOTS}:{line}"
    ]


def test_another_caller_of_a_capture_is_seen() -> None:
    source = (REPO / SCREENSHOTS).read_text()
    rogue = "\n\nasync def _unchecked(session):\n    return await _captured(session)\n"

    assert references(source + rogue, "_captured") == ["take_screenshot", "_unchecked"]
