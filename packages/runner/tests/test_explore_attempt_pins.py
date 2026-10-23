"""A `probe_equals` check that didn't pass keeps its option across attempts
(#56, after #213; relay 142): a failed confirmation's non-passing probe
check pins the next attempt by check ID and probe path, the next attempt
names the option by its fresh ref, and a path its probe no longer offers
unlocks nothing. Real Chromium against the executor's fixture app, with a
scripted model."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from aqa_core.config import ProjectConfig

from packages.runner.tests.executor_fixtures import App, serving_app
from packages.runner.tests.explore_graph_fixtures import Scripted, planned
from packages.runner.tests.test_explore_attempts import before_resets
from packages.runner.tests.test_explore_graph import written
from packages.runner.tests.test_explore_probes import (
    COUNT,
    COUNTED,
    FINISH,
    IDLE,
    RESET,
    blocks,
    check,
    probed,
)

# o1 for the count, which isn't 2, and o2 for a decoy key that holds 2.
DECOY = b'{"count": 3, "other": 2}'
TURNS = ProjectConfig.model_validate({"budgets": {"actions_per_attempt": 3}})
# What the next attempt reads of a confirmation whose count read 3.
FAILED = (
    "step 0, the start URL: completed\n"
    "a1: failed\n"
    "the page after step 0, without its refs:\n"
    "- generic [active]:\n"
    "  - generic:\n"
    "    - text: Name\n"
    '    - textbox "Name"\n'
    "  - generic:\n"
    "    - text: Size\n"
    '    - combobox "Size":\n'
    '      - option "S" [selected]\n'
    '      - option "M"\n'
    '  - button "Save"\n'
    "  - paragraph: Not saved"
)


def keeps(ref: str) -> str:
    return (
        f"assert_check did not run: a1 didn't pass on {ref}, so it keeps {ref} for "
        "the rest of this attempt"
    )


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def serving(app: App, body: bytes) -> Callable[[], object]:
    """Before a reset: the count probe answers `body` from then on."""
    return lambda: app.probe_bodies.__setitem__("count", [body])


def test_a_failed_confirmations_probe_check_pins_the_next_attempt(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.probe_bodies["count"] = [b'{"count": 2, "other": 2}']
    before_resets(monkeypatch, {1: serving(app, DECOY)})
    client = Scripted(planned(COUNTED), check("o1"), FINISH, check("o2"), IDLE, IDLE)

    run = probed(app, tmp_path, client, RESET + COUNT, config=TURNS)

    assert blocks(client, "confirmation")[2:] == [FAILED] * 3
    assert blocks(client, "results")[3] == keeps("o1")
    assert (run.outcome, run.exit_code, written(run)["reasons"]) == (
        "gave_up",
        3,
        [
            "the attempt took all 3 of its turns",
            "a1: never bound",
            "confirmation a1: failed",
        ],
    )


def test_a_pinned_path_the_probe_no_longer_offers_binds_no_other(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.probe_bodies["count"] = [b'{"count": 2, "other": 2}']
    before_resets(
        monkeypatch, {1: serving(app, DECOY), 2: serving(app, b'{"other": 2}')}
    )
    client = Scripted(planned(COUNTED), check("o1"), FINISH, check("o1"), IDLE, IDLE)

    probed(app, tmp_path, client, RESET + COUNT, config=TURNS)

    assert blocks(client, "results")[3] == (
        "assert_check did not run: a1 didn't pass on an option this attempt's "
        "probe no longer offers, so it binds no other"
    )
