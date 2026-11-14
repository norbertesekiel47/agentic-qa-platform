"""Screenshots as a replay's evidence (#251; ADR-0033's #251 amendment): with
`RunSetup.screenshots`, which only `strict_replay` sets, a kept step that drifted
or failed saves one masked screenshot in its evidence folder, and what became of
it is in `RunResult.shots`. A completed step, a drifted step a repair took, and
a replay that doesn't ask, as explore's confirmation and the bench pilot don't,
save none. A hostile run that heals keeps no image but the ones
`RunRecord.write_screenshot` wrote. Real Chromium against the executor's fixture
app."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.project import load_config, load_spec
from aqa_runner.admission import admitted
from aqa_runner.browser_session import BrowserSession, launched_browser
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.evidence import Shot
from aqa_runner.executor import RunResult
from aqa_runner.heal import Healing
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.model_router import ModelRouter
from aqa_runner.model_turns import Spending
from aqa_runner.redaction import Redactor
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.run_record import RunRecord
from aqa_runner.screenshots import Screenshot
from aqa_runner.settling import Window
from aqa_runner.strict_replay import Replayed, StrictRun, strict_replay
from playwright.async_api import Error, async_playwright

from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    PAGES,
    App,
    a_spec,
    asking_screenshots,
    compiled,
    for_spec,
    run,
    serving_app,
)
from packages.runner.tests.explore_graph_fixtures import Scripted
from packages.runner.tests.heal_fixtures import BLOB, NO_TOOL
from packages.runner.tests.screenshot_pages import RED, red_pixels
from packages.runner.tests.secret_fixtures import (
    BOUND_AT_START,
    FAKE_VALUE,
    secret_spec,
)
from packages.runner.tests.test_replay_inserted_steps import (
    CLICK_CONTACT,
    POST,
    contact_does,
    inserting,
    replayed,
)

# A fill on the form's name field, and a click on a button no page has.
FILL = {
    "seq": 1,
    "action": "fill",
    "target": "name",
    "value": "Ada",
    "side_effect": False,
}
NEVER = {
    "never": {"semantic": "a button that never comes", "locators": [{"css": "#never"}]}
}
DRIFTS = compiled(
    [{"seq": 1, "action": "click", "target": "never", "side_effect": False}],
    targets=NEVER,
)
# The drift is found sooner than the default 10 s.
QUICK = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def pngs(record: Path) -> list[str]:
    """Each PNG file under `record`, by its path there."""
    return sorted(path.relative_to(record).as_posix() for path in record.rglob("*.png"))


def test_a_step_that_fails_saves_its_screenshot_and_no_other_step_does(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asking_screenshots(monkeypatch)
    # The page's own execCommand throws, so the fill's dispatch fails.
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/throws")

    done = run(app, tmp_path, compiled([FILL], targets=FORM_TARGETS), spec=spec)

    assert [step.outcome for step in done.result.steps] == ["completed", "failed"]
    assert done.result.shots == {1: Shot("evidence/1/screenshot.png")}
    assert pngs(done.record.path) == ["evidence/1/screenshot.png"]


def test_a_start_navigation_that_fails_saves_its_screenshot(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navigate = BrowserSession.navigate

    async def failing(session: BrowserSession, url: str) -> Window:
        await navigate(session, url)
        raise Error("fake failure once the page loaded")

    monkeypatch.setattr(BrowserSession, "navigate", failing)
    asking_screenshots(monkeypatch)

    done = run(app, tmp_path, compiled([]))

    assert [step.outcome for step in done.result.steps] == ["failed"]
    assert done.result.shots == {0: Shot("evidence/0/screenshot.png")}
    assert pngs(done.record.path) == ["evidence/0/screenshot.png"]


def test_an_inserted_step_that_changed_something_saves_its_screenshot(
    tmp_path: Path,
) -> None:
    done = replayed(
        tmp_path, inserting(CLICK_CONTACT), html=contact_does(POST), screenshots=True
    )

    assert [(step.seq, step.outcome) for step in done.result.steps] == [
        (0, "completed"),
        (3, "failed"),
    ]
    # The drifted step the repair took keeps no evidence of its drift.
    assert done.result.shots == {3: Shot("evidence/3/screenshot.png")}
    assert pngs(done.record) == ["evidence/3/screenshot.png"]


def test_a_replay_that_doesnt_ask_saves_no_screenshot(app: App, tmp_path: Path) -> None:
    spec = a_spec(tmp_path, QUICK, start_url="/page/form")
    record = RunRecord.create(tmp_path)
    run_gate = gate(allowed=(app.origin,))

    @asynccontextmanager
    async def opening() -> AsyncIterator[Opened]:
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            await register_identity_engine(playwright)
            yield Opened(playwright.chromium, proxy, record)

    async def scenario() -> RunResult:
        # As explore's confirmation and the bench pilot replay.
        replaying = Replaying(spec, QUICK, app.origin, run_gate)
        async with asyncio.timeout(60):
            return await replay_run(
                for_spec(DRIFTS, spec), replaying, opening, every_locator=True
            )

    result = asyncio.run(scenario())

    assert [step.outcome for step in result.steps] == ["completed", "drifted"]
    assert result.shots == {}
    assert pngs(record.path) == []


# A sign-in page that draws its field and what was typed in pure red, which
# nothing else on it is.
SIGN_IN = f"""<style>input, #seen {{{RED}}}</style>
<label>Password <input type="password" name="password"></label>
<p id="seen"></p><script>
document.querySelector("[name=password]").addEventListener("input", (event) => {{
    document.querySelector("#seen").textContent = event.target.value;
}});
</script>"""
SIGNS_IN_THEN_DRIFTS = compiled(
    [
        {
            "seq": 1,
            "action": "fill_secret",
            "target": "password",
            "secret": "TEST_PASSWORD",
            "side_effect": False,
        },
        {"seq": 2, "action": "click", "target": "never", "side_effect": False},
    ],
    targets={
        "password": {
            "semantic": "the password field",
            "locators": [{"css": "[name=password]"}],
        },
        **NEVER,
    },
)
# The signatures an image file starts with; WebP's and ISO-BMFF's (AVIF,
# HEIC) are at their offsets in `an_image`.
SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
    b"\x00\x00\x01\x00",
    b"%PDF",
    b"<svg",
)
PNG = SIGNATURES[0]
# How base64 spells a PNG's signature.
BASE64_PNG = b"iVBORw0KGgo"


def an_image(data: bytes) -> bool:
    """Whether `data` starts as an image file does."""
    return (
        data.lstrip().startswith(SIGNATURES)
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data[4:8] == b"ftyp"
    )


def test_every_image_a_hostile_run_keeps_is_one_write_screenshot_wrote(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setitem(PAGES, "sign-in", SIGN_IN)
    root = secret_spec(tmp_path, start_url="/page/sign-in").path.parent
    (root / "config.yaml").write_text(
        f"secrets: {{ {BOUND_AT_START} }}\nbudgets: {{ resolve_seconds: 2 }}\n"
    )
    config = load_config(root / "config.yaml")
    spec = load_spec(root / "login.spec.md", config)
    script = for_spec(SIGNS_IN_THEN_DRIFTS, spec)
    _, _, bound, _ = admitted(script, spec, config, app.origin)
    written: list[str] = []
    write = RunRecord.write_screenshot

    def spying(record: RunRecord, name: str, shot: Screenshot) -> Path:
        written.append(name)
        return write(record, name, shot)

    monkeypatch.setattr(RunRecord, "write_screenshot", spying)

    def heal(spending: Spending) -> Healing:
        router = ModelRouter.from_config(config, Scripted(NO_TOOL))
        budgets = config.budgets
        return Healing(router, spending, budgets.heal_turns, budgets.resolve_seconds)

    async def scenario() -> tuple[Replayed, list[int]]:
        async with async_playwright() as playwright:
            await register_identity_engine(playwright)
            run = StrictRun(root, spec, config, app.origin, script, bound, BLOB)
            scan = Redactor(bound.values())
            async with asyncio.timeout(60):
                ended = await strict_replay(run, playwright, scan, heal=heal)
            assert ended.record is not None
            async with launched_browser(playwright.chromium) as browser:
                scratch = await browser.new_page()
                reds = [
                    await red_pixels(scratch, (ended.record / name).read_bytes())
                    for name in written
                ]
        return ended, reds

    ended, reds = asyncio.run(scenario())

    # The fill went in, then the drift's heal gave up.
    verdicts = cast("list[dict[str, object]]", ended.document["verdicts"])
    assert ended.outcome == "inconclusive"
    assert [verdict["verdict"] for verdict in verdicts] == [
        "pass",
        "pass",
        "inconclusive",
    ]
    assert ended.record is not None
    files = {
        path.relative_to(ended.record).as_posix(): path.read_bytes()
        for path in sorted(ended.record.rglob("*"))
        if path.is_file()
    }
    assert sorted(name for name, data in files.items() if an_image(data)) == sorted(
        written
    )
    assert {"evidence/2/screenshot.png", "heals/2/screenshot.png"} <= set(written)
    assert [
        name
        for name, data in files.items()
        if name not in written and (PNG in data or BASE64_PNG in data)
    ] == []
    assert reds == [0] * len(written)
