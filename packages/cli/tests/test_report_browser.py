"""What a browser does with the page `aqa report` writes (UX_SPEC §3.5 and §7;
#249, #313): it loads nothing but itself and the screenshots it cites in the
record, refuses an image from anywhere else, a hostile record stays text, axe-core
finds no WCAG 2.2 AA violation, and the timeline, the tabs and the swipe work by
keyboard at 390 and 1440 px. Real Chromium, launched as every run's is
(`aqa_runner.sandbox.launch`), against the reports of real runs on #240's drift
pages."""

import asyncio
import json
import re
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
    unhealed,
)
from packages.runner.tests.drift_fixtures import (
    BUTTON,
    DETAILS,
    IN_VIEW,
    SHIPPING,
    page,
)
from packages.runner.tests.executor_fixtures import serving_app

# WCAG 2.2 AA, as axe-core tags its rules (UX_SPEC §7).
TAGS: Final = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
SIZES: Final = {390: 844, 1440: 900}
type Scheme = Literal["dark", "light"]
SCHEMES: Final[tuple[Scheme, ...]] = ("dark", "light")
TABS: Final = ("screenshot", "a11y", "network", "console", "reasoning")
# The paths a screenshot may have in a record (DATA_MODEL §7).
SCREENSHOT: Final = re.compile(r"(evidence/|heals/(assertion-)?)[0-9]+/screenshot\.png")
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
    """The report of each outcome the report pins, and of a run whose heal gave
    up, from a real run."""
    runs = {
        "passed": lambda root: replayed(root, page("order"), DETAILS, SHIPPING),
        "inconclusive": lambda root: replayed(root, page("moved"), DETAILS, SHIPPING),
        "expectation_violated": lambda root: replayed(
            root, page("modal"), BUTTON, IN_VIEW
        ),
        "drift_consistent": healed,
        # A run whose heal gave up, so a step has both screenshots (#313).
        "gave_up": unhealed,
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


def cited_screenshots(report: Path) -> list[str]:
    """The `file:` URI of each screenshot the page at `report` shows, each a
    screenshot path of the record's, a regular file inside it reached through
    no link."""
    record = report.parent
    shown = [
        image.attrs["src"] or ""
        for image in tree(report.read_text()).all(tagged("img"))
    ]
    for src in shown:
        assert SCREENSHOT.fullmatch(src)
        parts = src.split("/")
        assert not any(
            record.joinpath(*parts[: n + 1]).is_symlink() for n in range(len(parts))
        )
        assert (record / src).resolve().is_relative_to(record.resolve())
        assert (record / src).is_file()
    return [(record / src).as_uri() for src in shown]


def test_the_page_loads_nothing_but_itself(reports: dict[str, Path]) -> None:
    report = reports["drift_consistent"]
    asked: list[str] = []

    async def scenario() -> None:
        async with opened() as viewer:
            viewer.on("request", lambda request: asked.append(request.url))
            await viewer.goto(report.as_uri())
            await viewer.wait_for_load_state("networkidle")

    asyncio.run(scenario())

    # The page, then the screenshots it cites, which a hidden panel loads too.
    shots = cited_screenshots(report)
    assert asked[0] == report.as_uri()
    assert set(asked[1:]) <= set(shots)
    assert set(asked[1:]) & set(shots)


def attributes(html: str) -> list[tuple[str, str | None]]:
    every = tree(html).all(lambda _: True)
    return [pair for element in every for pair in element.attrs.items()]


# The screenshots each report shows, in the order its page cites them.
SHOWN: Final = {
    "passed": [],
    "inconclusive": ["evidence/1/screenshot.png"],
    "expectation_violated": [],
    "drift_consistent": ["heals/1/screenshot.png"],
    "gave_up": ["heals/1/screenshot.png", "evidence/1/screenshot.png"],
}


@pytest.mark.parametrize(
    "outcome",
    ["passed", "inconclusive", "expectation_violated", "drift_consistent", "gave_up"],
)
def test_the_page_holds_one_script_one_style_and_no_way_out(
    reports: dict[str, Path], outcome: str
) -> None:
    html = reports[outcome].read_text()
    page_tree = tree(html)

    [policy] = page_tree.all(having("http-equiv", "Content-Security-Policy"))
    directives = [each.strip() for each in (policy.attrs["content"] or "").split(";")]
    assert directives[:4] == [
        "default-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "img-src 'self'",
    ]
    assert re.fullmatch(r"style-src 'sha256-[A-Za-z0-9+/]{43}='", directives[4])
    assert re.fullmatch(r"script-src 'sha256-[A-Za-z0-9+/]{43}='", directives[5])
    assert len(directives) == 6
    assert len(page_tree.all(tagged("script"))) == 1
    assert len(page_tree.all(tagged("style"))) == 1
    for tag in ("link", "iframe", "object", "form", "embed"):
        assert page_tree.all(tagged(tag)) == []
    # Images only of the record's own screenshots, each a file in it.
    shown = [image.attrs["src"] for image in page_tree.all(tagged("img"))]
    assert shown == SHOWN[outcome]
    assert cited_screenshots(reports[outcome]) == [
        (reports[outcome].parent / src).as_uri() for src in SHOWN[outcome]
    ]
    assert "data:" not in html
    names = [name for name, _ in attributes(html)]
    sourced = [each.tag for each in page_tree.all(having("src"))]
    assert sourced == ["img"] * len(shown)
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
        # The step's screenshot withheld for a hostile reason, and the heal's at
        # a path that is a script.
        step["evidence"]["screenshot"] = {"path": None, "withheld": said}
        step["heal"]["screenshot"] = {"path": "javascript:alert(1)", "withheld": None}

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
    # finding title, a rationale, bound (in a tab and the panel), spec_id, a
    # target's meaning (in the timeline and the Screenshot tab) and a
    # screenshot's withheld reason, each as the text it is.
    assert text.count(said) == 16
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
    "outcome",
    ["passed", "inconclusive", "expectation_violated", "drift_consistent", "gave_up"],
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
    # What shows of it: the label naming it, and its block in each of the five
    # panels.
    assert [(s["step"], s["tab"], s["shown"], s["focused"]) for s in seen] == [
        (1, "tab-a11y", ["1"] * 6, "step-1"),
        (0, "tab-a11y", ["0"] * 6, "step-0"),
        (1, "tab-a11y", ["1"] * 6, "step-1"),
        (1, "tab-a11y", ["1"] * 6, "tab-a11y"),
        (1, "tab-network", ["1"] * 6, "tab-network"),
        (1, "tab-console", ["1"] * 6, "tab-console"),
        (1, "tab-network", ["1"] * 6, "tab-network"),
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
            // Whether each image, swipe and frame shown lies across its panel.
            inside: [...document.querySelectorAll("img, input, .frame")]
                .filter((e) => e.getClientRects().length > 0)
                .map((e) => {
                    const box = e.getBoundingClientRect();
                    const panel = e.closest('[role="tabpanel"]').getBoundingClientRect();
                    return box.left >= panel.left && box.right <= panel.right;
                }),
            // Whether the focused tab lies inside the tab strip's scroll box, to
            // the pixel, which a scrolled edge can miss by a fraction.
            seen: (() => {
                const tab = document.activeElement.getBoundingClientRect();
                const strip = document.querySelector('[role="tablist"]')
                    .getBoundingClientRect();
                return Math.round(tab.left) >= Math.round(strip.left)
                    && Math.round(tab.right) <= Math.round(strip.right);
            })(),
        })"""
    )
    return found


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_timeline_stacks_at_390_and_runs_across_at_1440(
    reports: dict[str, Path], scheme: Scheme
) -> None:
    report = reports["drift_consistent"]
    laid: dict[int, dict[str, Any]] = {}
    # The gave-up report, whose Screenshot tab has a swipe, as each tab shows.
    swiped: dict[int, list[dict[str, Any]]] = {}
    focused: list[bool] = []

    async def scenario() -> None:
        for width in SIZES:
            async with opened(width=width, scheme=scheme) as viewer:
                await viewer.goto(report.as_uri())
                laid[width] = await boxes(viewer)
                for tab in TABS:
                    await viewer.click(f"#tab-{tab}")
                    assert (await boxes(viewer))["width"] <= width
                # A tab focused from the keyboard is scrolled into the strip.
                await viewer.focus("#tab-a11y")
                for key in ("End", "Home"):
                    await viewer.keyboard.press(key)
                    focused.append((await boxes(viewer))["seen"])
            async with opened(width=width, scheme=scheme) as viewer:
                await viewer.goto(reports["gave_up"].as_uri())
                swiped[width] = []
                for tab in TABS:
                    await viewer.click(f"#tab-{tab}")
                    swiped[width].append(await boxes(viewer))

    asyncio.run(scenario())

    for width, shown in swiped.items():
        assert [each["width"] <= width for each in shown] == [True] * len(TABS)
        assert [len(set(each["tabs"])) for each in shown] == [1] * len(TABS)
        # The Screenshot tab's frame, its two images and its swipe.
        assert shown[0]["inside"] == [True] * 4
    assert focused == [True] * 4

    narrow, wide = laid[390]["steps"], laid[1440]["steps"]
    assert len(narrow) == len(wide) == 2
    assert narrow[0]["top"] < narrow[1]["top"]
    assert narrow[0]["left"] == narrow[1]["left"]
    assert laid[390]["width"] <= 390
    assert wide[0]["top"] == wide[1]["top"]
    assert wide[0]["left"] < wide[1]["left"]
    # The five tabs sit in one row above their panel at either width.
    assert [len(set(laid[width]["tabs"])) for width in SIZES] == [1, 1]
    assert len(laid[390]["tabs"]) == 5


def test_the_style_has_no_motion() -> None:
    assert "transition" not in STYLE
    assert "animation" not in STYLE


def test_the_screenshots_load_under_the_policy(reports: dict[str, Path]) -> None:
    loaded: list[dict[str, Any]] = []

    async def scenario() -> None:
        async with opened() as viewer:
            await viewer.goto(reports["gave_up"].as_uri())
            await viewer.click("#tab-screenshot")
            await viewer.wait_for_load_state("networkidle")
            loaded.extend(
                await viewer.evaluate(
                    """() => [...document.images].map((image) => ({
                        src: image.getAttribute("src"),
                        complete: image.complete,
                        width: image.naturalWidth,
                    }))"""
                )
            )

    asyncio.run(scenario())

    assert [(each["src"], each["complete"]) for each in loaded] == [
        ("heals/1/screenshot.png", True),
        ("evidence/1/screenshot.png", True),
    ]
    assert all(each["width"] > 0 for each in loaded)


# A 1x1 PNG, as an image a page could embed.
PIXEL: Final = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
)


def test_an_image_from_anywhere_else_is_refused(reports: dict[str, Path]) -> None:
    found: dict[str, Any] = {}

    async def scenario(origin: str) -> None:
        async with opened() as viewer:
            await viewer.goto(reports["gave_up"].as_uri())
            found["refused"] = await viewer.evaluate(
                """async (sources) => {
                    const refused = [];
                    document.addEventListener("securitypolicyviolation", (event) => {
                        refused.push(event.effectiveDirective);
                    });
                    const images = sources.map((source) => {
                        const image = document.createElement("img");
                        image.alt = "";
                        document.body.append(image);
                        const ended = new Promise((settled) => {
                            image.addEventListener("load", settled);
                            image.addEventListener("error", settled);
                        });
                        image.src = source;
                        return ended.then(() => image.naturalWidth);
                    });
                    const widths = await Promise.all(images);
                    while (refused.length < sources.length) {
                        await new Promise((later) => setTimeout(later, 50));
                    }
                    return { widths, refused };
                }""",
                [f"{origin}/image/ok", PIXEL],
            )

    with serving_app() as app:
        asyncio.run(scenario(app.origin))
        asked = app.paths()

    assert found["refused"] == {"widths": [0, 0], "refused": ["img-src", "img-src"]}
    # The page never asked the server, whatever Playwright's request event says.
    assert asked == []


async def swiped(viewer: Page) -> dict[str, Any]:
    """The swipe's value, what is focused and its outline, and which image is at
    the frame's left and right quarters, the frame scrolled into view."""
    state: dict[str, Any] = await viewer.evaluate(
        """() => {
            const frame = document.querySelector("#panel-screenshot .frame");
            frame.scrollIntoView({ block: "center" });
            const box = frame.getBoundingClientRect();
            const y = box.top + box.height / 2;
            const at = (x) => document.elementFromPoint(x, y).getAttribute("src");
            return {
                value: document.querySelector("#swipe-1").value,
                focused: document.activeElement.id,
                outline: getComputedStyle(document.activeElement).outlineStyle,
                left: at(box.left + box.width / 4),
                right: at(box.left + (box.width * 3) / 4),
                split: frame.style.getPropertyValue("--split"),
            };
        }"""
    )
    return state


HEALS: Final = "heals/1/screenshot.png"
LEFT_IT: Final = "evidence/1/screenshot.png"


def test_the_swipe_works_by_keyboard(reports: dict[str, Path]) -> None:
    seen: list[dict[str, Any]] = []

    async def scenario() -> None:
        async with opened() as viewer:
            await viewer.goto(reports["gave_up"].as_uri())
            # The drifted step, then Accessibility, then the Screenshot tab left
            # of it, then its panel and the swipe in it.
            for key in ("Tab", "Tab", "ArrowLeft", "Tab", "Tab"):
                await viewer.keyboard.press(key)
            seen.append(await swiped(viewer))
            for key in ("ArrowRight", "End", "Home"):
                await viewer.keyboard.press(key)
                seen.append(await swiped(viewer))

    asyncio.run(scenario())

    assert [
        (each["value"], each["split"], each["left"], each["right"]) for each in seen
    ] == [
        ("50", "50%", HEALS, LEFT_IT),
        ("51", "51%", HEALS, LEFT_IT),
        ("100", "100%", HEALS, HEALS),
        ("0", "0%", LEFT_IT, LEFT_IT),
    ]
    assert [each["focused"] for each in seen] == ["swipe-1"] * 4
    assert all(each["outline"] != "none" for each in seen)


def test_the_split_holds_still_without_input(reports: dict[str, Path]) -> None:
    seen: list[dict[str, Any]] = []

    async def scenario() -> None:
        async with opened() as viewer:
            await viewer.goto(reports["gave_up"].as_uri())
            await viewer.click("#tab-screenshot")
            seen.append(await swiped(viewer))
            await viewer.wait_for_timeout(1500)
            seen.append(await swiped(viewer))

    asyncio.run(scenario())

    first, then = seen
    assert first == then
    assert (first["value"], first["split"]) == ("50", "50%")
