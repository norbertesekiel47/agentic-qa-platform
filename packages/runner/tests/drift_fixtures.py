"""Fixtures the drift pages' tests share (#240): an order page whose toggle
shows its details, five drifts of it, each one change (the toggle moved,
renamed, removed or drawn in a canvas (#252), or a modal over the page), the
scripts recorded against the order page, and the scripted repair that
dismisses the modal (#325). Later tickets compose a page from these rather
than edit one. Imported by its path, as pytest names the runner's test
modules (TESTING.md §1, Shared egress fixtures)."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch

from aqa_core.compiled import Click, CompiledScript, Step, Target
from aqa_runner.repairs import Drift, StepAt

from packages.runner.tests.executor_fixtures import (
    PAGES,
    App,
    by_role,
    compiled,
    serving_app,
)

DRIFT = Path(__file__).resolve().parents[3] / "tests/fixtures/pages/drift"
# A bare button also matches the footer's, so the css locator is scoped by
# the toolbar, as the generator scopes one.
TOGGLE = {
    "semantic": "the button that shows the order's details",
    "locators": [
        by_role("button", "Show details"),
        {"css": "button", "scope": {"css": "#toolbar"}},
    ],
}
SUMMARY = {
    "semantic": "the order's shipping summary",
    "locators": [{"css": "#summary"}],
}
# What each script's spec expects: its one expectation.
SHIPPING: Final = "The order says when it ships"
IN_VIEW: Final = "The details button is in view"
# The toggle clicked, then the summary read and seen.
DETAILS = compiled(
    [{"seq": 1, "action": "click", "target": "toggle", "side_effect": False}],
    targets={"toggle": TOGGLE, "summary": SUMMARY},
    assertions=[
        {
            "id": "a1",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "summary",
            "text": "ships on Monday",
        },
        {
            "id": "a2",
            "expect_index": 0,
            "check": "visible_unoccluded",
            "target": "summary",
            "min_size_px": [1, 1],
            "in_viewport": True,
        },
    ],
)
# The toggle seen, with no step.
BUTTON = compiled(
    [],
    targets={"toggle": TOGGLE},
    assertions=[
        {
            "id": "a1",
            "expect_index": 0,
            "check": "visible_unoccluded",
            "target": "toggle",
            "min_size_px": [1, 1],
            "in_viewport": True,
        }
    ],
)

# The toggle's contrast, which its own background, a button's, gives it.
CONTRASTED = compiled(
    [],
    targets={"toggle": TOGGLE},
    assertions=[
        {
            "id": "a1",
            "expect_index": 0,
            "check": "contrast_min",
            "target": "toggle",
            "min_ratio": 4.5,
        }
    ],
)


# The modal's dismiss button, which no committed target names: the target a
# repair adds for the click it inserts (#325).
NO_THANKS: Final[dict[str, Any]] = {
    "semantic": "the newsletter dialog's dismiss button",
    "locators": [
        by_role("button", "No thanks"),
        {"css": "button", "scope": {"css": "#overlay"}},
    ],
}
DISMISS = Click(seq=2, action="click", target="no_thanks", side_effect=False)
# The patch from `DETAILS` to `dismissed(DETAILS)`.
DISMISSED_PATCH: Final[list[dict[str, Any]]] = [
    {"op": "add", "path": "/targets/no_thanks", "value": NO_THANKS},
    {
        "op": "add",
        "path": "/steps/0",
        "value": {
            "seq": 2,
            "side_effect": False,
            "satisfies": [],
            "action": "click",
            "target": "no_thanks",
        },
    },
]


def dismissed(
    script: CompiledScript, before: int = 1, inserted: Step = DISMISS
) -> CompiledScript:
    """`script` with `no_thanks` added as its last target and `inserted`
    right before its step `before`."""
    seqs = [step.seq for step in script.steps]
    at = seqs.index(before)
    return script.model_copy(
        update={
            "targets": {
                **script.targets,
                "no_thanks": Target.model_validate(NO_THANKS),
            },
            "steps": (*script.steps[:at], inserted, *script.steps[at:]),
        }
    )


def dismissing(inserted: Step = DISMISS) -> Callable[[Drift], CompiledScript]:
    """A repair for the modal page: the script it was handed, `dismissed` with
    `inserted` right before the drifted step."""

    def offer(drift: Drift) -> CompiledScript:
        assert isinstance(drift.at, StepAt)
        return dismissed(drift.script, drift.at.step.seq, inserted)

    return offer


def page(name: str) -> str:
    """The drift page `name`: `order`, or a drift of it."""
    return (DRIFT / f"{name}.html").read_text()


@contextmanager
def serving(html: str) -> Iterator[App]:
    """The fixture app, serving `html` at `/page/order` until the block ends."""
    with patch.dict(PAGES, {"order": html}), serving_app() as app:
        yield app


def order_spec(root: Path, expectation: str) -> Path:
    """The order spec under the spec root `root`, with `expectation` its
    only one; its config finds a miss in 1 s, not the default 10. The
    spec's path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("budgets: { resolve_seconds: 1 }\n")
    path = root / "order.spec.md"
    path.write_text(
        "---\nid: order\ngoal: A customer finds out when their order ships.\n"
        "preconditions:\n  start_url: '/page/order'\n"
        f"expect:\n  - {expectation}\n---\n"
    )
    return path
