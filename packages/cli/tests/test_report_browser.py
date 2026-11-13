"""What a browser does with the page `aqa report` writes (UX_SPEC §3.5 and §7;
#249): it loads offline, a hostile record stays text, axe-core finds no WCAG 2.2
AA violation, and the timeline and tabs work by keyboard at 390 and 1440 px. Real
Chromium, launched as every run's is (`aqa_runner.sandbox.launch`), against the
reports of real runs on #240's drift pages."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import distribution
from pathlib import Path
from typing import Any, Final, Literal

import pytest
from aqa_cli.report_page import STYLE
from aqa_runner.sandbox import launch
from playwright.async_api import Browser, Dialog, Page, async_playwright

from packages.cli.tests.report_fixtures import (
    Ran,
    copied,
    having,
    healed,
    replayed,
    reported,
    tagged,
    tree,
)
from packages.runner.tests.drift_fixtures import (
    BUTTON,
    DETAILS,
    IN_VIEW,
    SHIPPING,
    page,
)

# WCAG 2.2 AA, as axe-core tags its rules (UX_SPEC §7).
TAGS: Final = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
SIZES: Final = {390: 844, 1440: 900}
type Scheme = Literal["dark", "light"]
SCHEMES: Final[tuple[Scheme, ...]] = ("dark", "light")
TABS: Final = ("a11y", "network", "console", "reasoning")
# What a hostile record puts in each text the report shows.
HOSTILE: Final = (
    "</script><script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '" onmouseover="x',
    "javascript:alert(1)",
)
# The axe-core axe-playwright-python bundles, read without importing the module,
# which ships no types for mypy --strict (TECH_STACK §1).
AXE: Final = Path(
    str(
        distribution("axe-playwright-python").locate_file(
            "axe_playwright_python/axe.min.js"
        )
    )
).read_text()


def report_file(ran: Ran, to: Path) -> Path:
    """`aqa report` of a copy of `ran`'s record under `to`; the page's path."""
    copy = copied(ran, to)
    with pytest.MonkeyPatch.context() as patch:
        result = reported(copy, patch)
    assert result.exit_code == 0, result.output
    return copy.record / "report.html"


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The report of each outcome the report pins, from a real run."""
    runs = {
        "passed": lambda root: replayed(root, page("order"), DETAILS, SHIPPING),
        "inconclusive": lambda root: replayed(root, page("moved"), DETAILS, SHIPPING),
        "expectation_violated": lambda root: replayed(
            root, page("modal"), BUTTON, IN_VIEW
        ),
        "drift_consistent": healed,
    }
    made = {}
    for outcome, run in runs.items():
        ran = run(tmp_path_factory.mktemp(outcome) / "qa")
        made[outcome] = report_file(ran, tmp_path_factory.mktemp(f"{outcome}-copy"))
    return made


@pytest.fixture(scope="module")
def drift_run(tmp_path_factory: pytest.TempPathFactory) -> Ran:
    return healed(tmp_path_factory.mktemp("drift") / "qa")


@asynccontextmanager
async def opened(
    *,
    width: int = 1440,
    scheme: Scheme = "dark",
) -> AsyncIterator[Page]:
    """A new page of a sandboxed Chromium, `width` wide in `scheme`."""
    async with async_playwright() as playwright:
        browser: Browser = await launch(playwright.chromium)
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": SIZES[width]},
                color_scheme=scheme,
            )
            yield await context.new_page()
        finally:
            await browser.close()


def test_the_page_loads_nothing_but_itself(reports: dict[str, Path]) -> None:
    report = reports["drift_consistent"]
    asked: list[str] = []

    async def scenario() -> None:
        async with opened() as viewer:
            viewer.on("request", lambda request: asked.append(request.url))
            await viewer.goto(report.as_uri())
            await viewer.wait_for_load_state("networkidle")

    asyncio.run(scenario())

    assert asked == [report.as_uri()]


def attributes(html: str) -> list[tuple[str, str | None]]:
    every = tree(html).all(lambda _: True)
    return [pair for element in every for pair in element.attrs.items()]


@pytest.mark.parametrize(
    "outcome", ["passed", "inconclusive", "expectation_violated", "drift_consistent"]
)
def test_the_page_holds_one_script_one_style_and_no_way_out(
    reports: dict[str, Path], outcome: str
) -> None:
    html = reports[outcome].read_text()
    page_tree = tree(html)

    [policy] = page_tree.all(having("http-equiv", "Content-Security-Policy"))
    assert "default-src 'none'" in (policy.attrs["content"] or "")
    assert "base-uri 'none'" in (policy.attrs["content"] or "")
    assert "form-action 'none'" in (policy.attrs["content"] or "")
    assert len(page_tree.all(tagged("script"))) == 1
    assert len(page_tree.all(tagged("style"))) == 1
    for tag in ("link", "img", "iframe", "object", "form", "embed"):
        assert page_tree.all(tagged(tag)) == []
    names = [name for name, _ in attributes(html)]
    assert "src" not in names
    assert "style" not in names
    assert [name for name in names if name.startswith("on")] == []
    assert all(
        "http:" not in (value or "") and "https:" not in (value or "")
        for _, value in attributes(html)
    )
    [style] = page_tree.all(tagged("style"))
    assert "url(" not in style.raw_text
    assert "@import" not in style.raw_text


def hostile(ran: Ran) -> None:
    """`ran`'s record, with hostile text in every text the report shows."""
    said = " ".join(HOSTILE)
    run_json = ran.record / "run.json"
    kept = json.loads(run_json.read_text())
    kept["spec_id"] = said
    kept["script"]["spec_id"] = said
    kept["goal"] = said
    kept["expectations"] = [said]
    toggle = kept["script"]["targets"]["toggle"]
    toggle["semantic"] = said
    run_json.write_text(json.dumps(kept))
    repair = ran.record / "repairs" / "1.json"
    offered = json.loads(repair.read_text())
    offered["script"]["targets"]["toggle"]["locators"][0]["name"] = said
    repair.write_text(json.dumps(offered))
    evidence = ran.record / "evidence" / "1"
    with (evidence / "a11y_snapshot.yaml").open("a") as snapshot:
        snapshot.write(f"- paragraph: {said}\n")
    (evidence / "console_log.json").write_text(
        json.dumps({"entries": [{"type": "log", "text": said}], "total": 1})
    )
    (evidence / "network_log.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"method": "GET", "url": "javascript:alert(1)", "status": 200}
                ],
                "total": 1,
            }
        )
    )

    def edited(document: dict[str, Any]) -> None:
        document["spec_id"] = said
        document["reasons"] = [said]
        step = document["verdicts"][1]
        step["verdict"] = "expectation_violated"
        step["reason"] = said
        step["finding"] = {"category": "visual", "title": said, "repro_steps": [0]}
        step["heal"]["bound"] = said
        step["heal"]["model_reported"]["rationale"] = f"{said} \u202e reversed"

    ran.edit_replay_json(edited)


def test_hostile_text_stays_text(drift_run: Ran, tmp_path: Path) -> None:
    copy = copied(drift_run, tmp_path)
    hostile(copy)
    with pytest.MonkeyPatch.context() as patch:
        result = reported(copy, patch)
    assert result.exit_code == 0, result.output
    report = copy.record / "report.html"
    dialogs: list[str] = []
    found: dict[str, Any] = {}

    async def scenario() -> None:
        async with opened() as viewer:

            async def dismissed(dialog: Dialog) -> None:
                dialogs.append(dialog.message)
                await dialog.dismiss()

            viewer.on("dialog", dismissed)
            await viewer.goto(report.as_uri())
            for tab in TABS:
                await viewer.click(f"#tab-{tab}")
                await viewer.mouse.move(5, 5)
            found["counts"] = await viewer.evaluate(
                """() => Object.fromEntries(
                    ["script", "img", "iframe", "object", "form"].map(
                        (tag) => [tag, document.getElementsByTagName(tag).length]))"""
            )
            found["text"] = await viewer.evaluate("() => document.body.textContent")
            await viewer.hover("text=Step 1")

    asyncio.run(scenario())

    assert dialogs == []
    assert found["counts"] == {
        "script": 1,
        "img": 0,
        "iframe": 0,
        "object": 0,
        "form": 0,
    }
    text: str = found["text"]
    said = " ".join(HOSTILE)
    # The goal, an expectation (in the header and for each assertion), the
    # snapshot, a console text, a reason (in the timeline and the panel), a
    # finding title, a rationale, bound (in a tab and the panel), spec_id, and a
    # target's meaning, each as the text it is.
    assert text.count(said) == 14
    # The locator as the compiled file spells it in JSON, and a network URL.
    assert json.dumps(said)[1:-1] in text
    assert "GET javascript:alert(1) · status 200" in " ".join(text.split())
    assert "\\u202e reversed" in text
    assert "\u202e" not in text


async def axe_violations(viewer: Page) -> list[dict[str, Any]]:
    """What axe-core finds on the page as it stands, with `TAGS`' rules."""
    await viewer.evaluate(AXE)
    found: dict[str, Any] = await viewer.evaluate(
        "options => axe.run(document, options)",
        {"runOnly": {"type": "tag", "values": TAGS}, "resultTypes": ["violations"]},
    )
    return [
        {"id": v["id"], "impact": v["impact"], "at": [n["target"] for n in v["nodes"]]}
        for v in found["violations"]
    ]


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("width", SIZES)
@pytest.mark.parametrize(
    "outcome", ["passed", "inconclusive", "expectation_violated", "drift_consistent"]
)
def test_axe_finds_no_wcag_22_aa_violation(
    reports: dict[str, Path], outcome: str, width: int, scheme: Scheme
) -> None:
    report = reports[outcome]
    found: dict[str, list[dict[str, Any]]] = {}

    async def scenario() -> None:
        async with opened(width=width, scheme=scheme) as viewer:
            await viewer.goto(report.as_uri())
            found["opened"] = await axe_violations(viewer)
            for tab in TABS:
                await viewer.click(f"#tab-{tab}")
                found[tab] = await axe_violations(viewer)

    asyncio.run(scenario())

    assert found == {"opened": [], **{tab: [] for tab in TABS}}


async def selected(viewer: Page) -> dict[str, Any]:
    """Which step and tab are selected, which step's evidence each panel shows,
    and what is focused, with its outline."""
    state: dict[str, Any] = await viewer.evaluate(
        """() => ({
            step: [...document.querySelectorAll('[role="option"]')].findIndex(
                (o) => o.getAttribute("aria-selected") === "true"),
            tab: document.querySelector('[role="tab"][aria-selected="true"]').id,
            shown: [...document.querySelectorAll("[data-step]")]
                .filter((b) => !b.hidden).map((b) => b.dataset.step),
            focused: document.activeElement.id,
            outline: getComputedStyle(document.activeElement).outlineStyle,
            panel: [...document.querySelectorAll('[role="tabpanel"]')]
                .filter((p) => !p.hidden).map((p) => p.id),
        })"""
    )
    return state


def test_the_timeline_and_tabs_work_by_keyboard(reports: dict[str, Path]) -> None:
    report = reports["drift_consistent"]
    seen: list[dict[str, Any]] = []
    named: list[int] = []

    async def scenario() -> None:
        async with opened() as viewer:
            await viewer.goto(report.as_uri())
            named.append(await viewer.get_by_role("listbox", name="Steps").count())
            await viewer.keyboard.press("Tab")
            seen.append(await selected(viewer))
            for key in (
                "ArrowLeft",
                "ArrowRight",
                "Tab",
                "ArrowRight",
                "ArrowRight",
                "ArrowLeft",
            ):
                await viewer.keyboard.press(key)
                seen.append(await selected(viewer))

    asyncio.run(scenario())

    assert named == [1]
    # The run's drifted step is selected as the page opens, and Tab reaches it.
    # What shows of it: the label naming it, and its block in each of the four
    # panels.
    assert [(s["step"], s["tab"], s["shown"], s["focused"]) for s in seen] == [
        (1, "tab-a11y", ["1"] * 5, "step-1"),
        (0, "tab-a11y", ["0"] * 5, "step-0"),
        (1, "tab-a11y", ["1"] * 5, "step-1"),
        (1, "tab-a11y", ["1"] * 5, "tab-a11y"),
        (1, "tab-network", ["1"] * 5, "tab-network"),
        (1, "tab-console", ["1"] * 5, "tab-console"),
        (1, "tab-network", ["1"] * 5, "tab-network"),
    ]
    assert [s["panel"] for s in seen[-3:]] == [
        ["panel-network"],
        ["panel-console"],
        ["panel-network"],
    ]
    assert all(s["outline"] != "none" for s in seen)


async def boxes(viewer: Page) -> dict[str, Any]:
    found: dict[str, Any] = await viewer.evaluate(
        """() => ({
            steps: [...document.querySelectorAll('[role="option"]')].map((o) => {
                const box = o.getBoundingClientRect();
                return { top: box.top, left: box.left };
            }),
            tabs: [...document.querySelectorAll('[role="tab"]')].map(
                (tab) => tab.getBoundingClientRect().top),
            width: document.documentElement.scrollWidth,
        })"""
    )
    return found


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_timeline_stacks_at_390_and_runs_across_at_1440(
    reports: dict[str, Path], scheme: Scheme
) -> None:
    report = reports["drift_consistent"]
    laid: dict[int, dict[str, Any]] = {}

    async def scenario() -> None:
        for width in SIZES:
            async with opened(width=width, scheme=scheme) as viewer:
                await viewer.goto(report.as_uri())
                laid[width] = await boxes(viewer)
                for tab in TABS:
                    await viewer.click(f"#tab-{tab}")
                    assert (await boxes(viewer))["width"] <= width

    asyncio.run(scenario())

    narrow, wide = laid[390]["steps"], laid[1440]["steps"]
    assert len(narrow) == len(wide) == 2
    assert narrow[0]["top"] < narrow[1]["top"]
    assert narrow[0]["left"] == narrow[1]["left"]
    assert laid[390]["width"] <= 390
    assert wide[0]["top"] == wide[1]["top"]
    assert wide[0]["left"] < wide[1]["left"]
    # The four tabs sit in one row above their panel at either width.
    assert [len(set(laid[width]["tabs"])) for width in SIZES] == [1, 1]
    assert len(laid[390]["tabs"]) == 4


def test_the_style_has_no_motion() -> None:
    assert "transition" not in STYLE
    assert "animation" not in STYLE
