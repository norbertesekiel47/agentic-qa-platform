"""`aqa replay` on the drift fixture pages (#240; ADR-0024's #238 amendment,
ADR-0025's #25 amendment): each script recorded against the clean order
page passes there, and each drift of it replays to its pinned outcome. A
binding the locators don't all agree on is drift, `inconclusive`, never
`expectation_violated`. Real Chromium against the executor's fixture app,
serving one page at `/page/order`."""

import re
from pathlib import Path

import pytest
from aqa_core.compiled import CompiledScript

from packages.cli.tests.test_replay import SECONDS, committed, on, replay
from packages.runner.tests.drift_fixtures import (
    BUTTON,
    DETAILS,
    IN_VIEW,
    SHIPPING,
    order_spec,
    page,
    serving,
)


@pytest.mark.parametrize(
    ("script", "expectation"),
    [
        pytest.param(DETAILS, SHIPPING, id="details"),
        pytest.param(BUTTON, IN_VIEW, id="button"),
    ],
)
def test_each_recorded_script_passes_on_the_clean_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: CompiledScript,
    expectation: str,
) -> None:
    spec = committed(order_spec(tmp_path / "qa", expectation), script)

    with serving(page("order")) as app:
        result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 0, result.output
    assert re.fullmatch(rf"passed order: {SECONDS}, \$0\n", result.stdout)
    assert result.stderr == ""
