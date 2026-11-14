"""Screenshots of the run viewer for its pull requests (#249, #313; UX_SPEC §9):
the `drift_consistent` report of a fixture run on #240's moved page, and the
Screenshot tab of a run there whose heal gave up, at 390 and 1440 px wide, in the
dark and the light theme. Never a real app's record: run records hold page text
(TESTING §5). From the repository root:

    uv run python -m packages.cli.tests.report_screenshots docs/media/report
"""

import asyncio
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

import typer.testing
from aqa_cli.main import app as aqa
from aqa_runner.sandbox import launch
from playwright.async_api import async_playwright

from packages.cli.tests.report_fixtures import Ran, healed, unhealed

# Each width and the height of its viewport, as the browser tests size them.
SIZES: Final = {390: 844, 1440: 900}
SCHEMES: Final[tuple[Literal["dark", "light"], ...]] = ("dark", "light")


async def shoot(report: Path, out: Path, name: str, tab: str | None) -> list[Path]:
    """Full-page screenshots of the report at `report`, one per size and
    scheme, in `out` as `<name>-<width>-<scheme>.png`, with the tab `tab` open
    when it names one, in a Chromium launched as every run's is."""
    shots = []
    async with async_playwright() as playwright:
        browser = await launch(playwright.chromium)
        try:
            for width, height in SIZES.items():
                for scheme in SCHEMES:
                    context = await browser.new_context(
                        viewport={"width": width, "height": height},
                        color_scheme=scheme,
                    )
                    page = await context.new_page()
                    await page.goto(report.as_uri())
                    if tab is not None:
                        await page.click(f"#tab-{tab}")
                    shot = out / f"{name}-{width}-{scheme}.png"
                    await page.screenshot(path=shot, full_page=True)
                    await context.close()
                    shots.append(shot)
        finally:
            await browser.close()
    return shots


# Each shot's name, the fixture run it shows, and the tab it opens, if any: the
# Screenshot tab opens on the drifted step, the first that didn't pass.
SHOTS: Final[tuple[tuple[str, Callable[[Path], Ran], str | None], ...]] = (
    ("drift-consistent", healed, None),
    ("screenshot-tab", unhealed, "screenshot"),
)


def main(out: Path) -> list[Path]:
    """Run the fixtures, report them, and write their screenshots to `out`."""
    out = out.absolute()
    out.mkdir(parents=True, exist_ok=True)
    started = Path.cwd()
    shots = []
    with tempfile.TemporaryDirectory() as scratch:
        for name, run, tab in SHOTS:
            ran = run(Path(scratch) / name / "qa")
            os.chdir(ran.root)
            try:
                result = typer.testing.CliRunner().invoke(
                    aqa, ["report", ran.record.name]
                )
            finally:
                os.chdir(started)
            if result.exit_code != 0:
                raise SystemExit(result.output)
            report = ran.record / "report.html"
            shots.extend(asyncio.run(shoot(report, out, name, tab)))
    return shots


if __name__ == "__main__":
    main(Path(sys.argv[1]))
