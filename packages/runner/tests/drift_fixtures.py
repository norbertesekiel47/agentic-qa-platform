"""Fixtures the drift pages' tests share (#240): an order page whose toggle
shows its details, four drifts of it, each one change (the toggle moved,
renamed or removed, or a modal over the page), and the scripts recorded
against the order page. Later tickets compose a page from these rather
than edit one. Imported by its path, as pytest names the runner's test
modules (TESTING.md §1, Shared egress fixtures)."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from unittest.mock import patch

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
