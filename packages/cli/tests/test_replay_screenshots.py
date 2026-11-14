"""`aqa replay`'s screenshots as evidence (#251; ADR-0033's #251 amendment): a
capture that is withheld, by a DevTools call that fails or an OCR engine that
can't load its model, saves nothing and says why in its verdict's
`evidence.screenshot`, and changes no verdict, reason, outcome or exit code.
Real Chromium against the executor's fixture app."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aqa_runner import ocr
from playwright.async_api import CDPSession, Error

from packages.cli.tests.test_explore_runs import ACCOUNT, SECRETS, record_in, shop
from packages.cli.tests.test_replay import committed, document, on, replay
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    App,
    compiled,
    serving_app,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE

# A click whose target only a fallback finds, so with every locator it drifts.
CLICK = {
    "seq": 1,
    "action": "click",
    "target": "save",
    "side_effect": True,
    "side_effect_basis": "network: POST /write/save",
}
DRIFTS = compiled([CLICK], targets={"save": FORM_TARGETS["save"]})
# The miss is found sooner than the default 10 s.
QUICK = "budgets: { resolve_seconds: 1 }\n"
SAVED = {"path": "evidence/1/screenshot.png", "withheld": None}


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def pngs(record: Path) -> list[str]:
    """Each PNG file under `record`, by its path there."""
    return sorted(path.relative_to(record).as_posix() for path in record.rglob("*.png"))


def unscreenshotted(written: dict[str, Any]) -> dict[str, Any]:
    """`written`'s outcome, exit code, reasons and verdicts, each verdict's
    evidence without its screenshot."""
    verdicts = [
        verdict | {"evidence": {"path": verdict["evidence"]["path"]}}
        for verdict in written["verdicts"]
    ]
    kept = ("outcome", "exit_code", "reasons")
    return {key: written[key] for key in kept} | {"verdicts": verdicts}


def test_a_withheld_screenshot_saves_nothing_and_changes_nothing_else(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = tmp_path / "taken" / "qa"
    spec = committed(shop(taken, page="/page/form", config=QUICK), DRIFTS)
    replayed = replay(monkeypatch, on(app, spec))
    send = CDPSession.send

    async def refusing(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureScreenshot":
            raise Error("Protocol error (Page.captureScreenshot): refused")
        return await send(devtools, method, params)

    monkeypatch.setattr(CDPSession, "send", refusing)
    refused = tmp_path / "refused" / "qa"
    spec = committed(shop(refused, page="/page/form", config=QUICK), DRIFTS)

    withheld = replay(monkeypatch, on(app, spec))

    assert (replayed.exit_code, withheld.exit_code) == (3, 3), withheld.output
    assert (pngs(record_in(taken)), pngs(record_in(refused))) == (
        ["evidence/1/screenshot.png"],
        [],
    )
    [_, step] = document(refused)["verdicts"]
    assert step["evidence"] == {
        "path": "evidence/1",
        "screenshot": {
            "path": None,
            "withheld": "a DevTools call or the render failed",
        },
    }
    assert unscreenshotted(document(refused)) == unscreenshotted(document(taken))


# The bound value is never filled, so the capture is the live page's, which a
# run that binds a secret OCR-checks.
@pytest.mark.parametrize("models", ["present", "one missing"])
def test_an_ocr_engine_that_fails_withholds_the_screenshot_and_keeps_the_exit_code(
    app: App,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    models: str,
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    if models == "one missing":
        linked = tmp_path_factory.mktemp("models")
        for name in (ocr.DETECT, ocr.CLASSIFY):
            (linked / name).symlink_to(ocr.MODELS / name)
        monkeypatch.setattr(ocr, "MODELS", linked)
    root = tmp_path / "qa"
    config = SECRETS + QUICK
    spec = shop(root, preconditions=ACCOUNT, config=config, page="/page/form")
    committed(spec, DRIFTS)

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 3, result.output
    [_, step] = document(root)["verdicts"]
    withheld = {"path": None, "withheld": "the image's OCR engine failed"}
    assert step["evidence"]["screenshot"] == (
        SAVED if models == "present" else withheld
    )
    assert pngs(record_in(root)) == (
        ["evidence/1/screenshot.png"] if models == "present" else []
    )
    # No model was downloaded into the spec root.
    assert list(tmp_path.rglob("*.onnx")) == []
