"""The navigator's action tools at the dispatch boundary (#53 P13; ADR-0024's
#53 P13 amendment): a failure after a step's intent ends the attempt and nothing
is dispatched again; an intent or completion the record can't write, or a
cancellation, leaves what the journal says; and observing takes the page
again while it changes, within one budget. Real Chromium against the
executor's fixture app, on the OS that runs them."""

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aqa_runner.document_origins import DocumentChangedError
from aqa_runner.navigator import Click, Fill, Navigate
from aqa_runner.navigator_tools import ActionResult, Attempt
from aqa_runner.reasons import UnansweredError, reason
from aqa_runner.redaction import NO_SECRETS, Redacted
from aqa_runner.run_record import BrokenJournalError, PendingIntent
from playwright.async_api import Page

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.executor_fixtures import App, read_steps, serving_app
from packages.runner.tests.navigator_fixtures import (
    SAVE,
    bound,
    fail_fsync,
    on_app,
    ref,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE

ENDED = (
    "click did not run: this attempt has ended, since step {} was dispatched and "
    "its completion isn't on disk"
)
UNWRITTEN = "steps.jsonl: a bound value's spelling is in it, so it isn't written"
CHANGED = (
    "the page changed while the session looked at it, so what it saw was "
    "discarded: take a new snapshot, or look the target up again"
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def click_save(attempt: Attempt) -> Click:
    return Click(ref=ref(attempt, "button", "Save"), meaning=SAVE)


def posts(app: App, path: str) -> int:
    return app.paths().count(path)


def written(app: App) -> list[tuple[int, str]]:
    """Each line of the attempt's journal: its step and state."""
    assert app.record is not None
    return [(line["seq"], line["state"]) for line in read_steps(app.record)]


@pytest.mark.parametrize("secret_bound", [False, True], ids=["unbound", "bound"])
def test_a_post_intent_failure_ends_the_attempt_and_dispatches_nothing_again(
    app: App, tmp_path: Path, secret_bound: bool
) -> None:
    async def scenario(attempt: Attempt) -> tuple[ActionResult, ActionResult]:
        # A button takes no text: the session's fill raises after the intent.
        failed = await attempt.act(
            Fill(ref=ref(attempt, "button", "Save"), meaning=SAVE, text="Ada")
        )
        assert attempt.ended is True
        return failed, await attempt.act(click_save(attempt))

    failed, after = on_app(
        app,
        tmp_path,
        scenario,
        secrets=[bound(app.origin, FAKE_VALUE)] if secret_bound else [],
    )

    reason_ = (
        "Error: the rest is withheld, since the page was handed a test secret"
        if secret_bound
        else "Error: fill: the element didn't take the value: it takes no text, or "
        "its page changed the value"
    )
    assert failed.text == (
        f"step 1 failed after it was dispatched, so this attempt has ended: {reason_}"
    )
    assert failed.step is not None
    assert failed.step.outcome == "failed"
    assert (after.step, after.text) == (None, ENDED.format(1))
    assert posts(app, "/write/save") == 0
    assert written(app) == [(1, "intent")]


@pytest.mark.parametrize("at", [0, 1], ids=["journal", "directory"])
def test_an_intent_not_forced_to_disk_dispatches_nothing_and_raises(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at: int
) -> None:
    async def scenario(attempt: Attempt) -> None:
        click = click_save(attempt)
        fail_fsync(monkeypatch, at)
        with pytest.raises(OSError, match="fake fsync failure"):
            await attempt.act(click)
        assert (attempt.steps, attempt.record.unresolved()) == ({}, ())
        # A journal a write to failed takes nothing more.
        with pytest.raises(BrokenJournalError):
            await attempt.act(click)

    on_app(app, tmp_path, scenario)

    assert posts(app, "/write/save") == 0
    # The intent's line reached the file, but not the disk for sure, so the
    # record holds no step dispatched.
    assert written(app) == [(1, "intent")]


def test_a_completion_the_scan_refuses_leaves_its_step_pending_and_ends_the_attempt(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> tuple[ActionResult, ActionResult]:
        failed = await attempt.act(click_save(attempt))
        assert (attempt.steps, attempt.record.unresolved()) == (
            {},
            (PendingIntent(1, True),),
        )
        return failed, await attempt.act(click_save(attempt))

    # Only a completion line says "completed".
    failed, after = on_app(
        app, tmp_path, scenario, secrets=[bound(app.origin, "completed")]
    )

    assert failed.text == (
        f"step 1 failed after it was dispatched, so this attempt has ended: {UNWRITTEN}"
    )
    assert after.text == ENDED.format(1)
    assert posts(app, "/write/save") == 1
    assert written(app) == [(1, "intent")]


def test_a_completion_not_forced_to_disk_leaves_its_step_pending_and_raises(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(attempt: Attempt) -> ActionResult:
        first = await attempt.act(Navigate(path="/page/form"))
        assert first.text == "step 1 ran, and the page settled"
        await attempt.observe()
        click = click_save(attempt)
        # The step-2 intent's fsync passes; its completion's fails.
        fail_fsync(monkeypatch, at=1)
        with pytest.raises(OSError, match="fake fsync failure"):
            await attempt.act(click)
        assert list(attempt.steps) == [1]
        assert attempt.record.unresolved() == (PendingIntent(2, True),)
        return await attempt.act(click)

    after = on_app(app, tmp_path, scenario)

    assert after.text == ENDED.format(2)
    assert posts(app, "/write/save") == 1
    # The completion's line reached the file, but not the disk for sure, so
    # the record keeps step 2 unresolved.
    assert written(app) == [
        (1, "intent"),
        (1, "completed"),
        (2, "intent"),
        (2, "completed"),
    ]


def test_a_cancelled_action_leaves_its_step_pending_and_ends_the_attempt(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> ActionResult:
        pay = Click(ref=ref(attempt, "button", "Pay"), meaning="the pay button")
        task = asyncio.create_task(attempt.act(pay))
        # The app holds the payment, so the step is settling when it is
        # cancelled.
        await until(lambda: "/held/pay" in app.paths())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (attempt.steps, attempt.record.unresolved()) == (
            {},
            (PendingIntent(1, True),),
        )
        return await attempt.act(pay)

    after = on_app(app, tmp_path, scenario, page="/page/pay")

    assert after.text == ENDED.format(1)
    assert posts(app, "/held/pay") == 1


def test_a_snapshot_the_page_changes_under_is_taken_again(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[str] = []
    aria_snapshot = Page.aria_snapshot

    async def changing(self: Page, *args: Any, **options: Any) -> str:
        if not taken:
            # A navigation in the page's document while it is looked at.
            async with self.expect_event("framenavigated"):
                await self.evaluate("location.hash = 'moved'")
        taken.append(await aria_snapshot(self, *args, **options))
        return taken[-1]

    async def scenario(attempt: Attempt) -> tuple[Redacted, Redacted | None]:
        monkeypatch.setattr(Page, "aria_snapshot", changing)
        return await attempt.observe(), attempt.observation

    observed, kept = on_app(app, tmp_path, scenario)

    assert len(taken) == 2
    assert observed == kept
    assert re.search(r'button "Save" \[ref=e\d+\]', observed)


async def snapshots(
    attempt: Attempt,
    monkeypatch: pytest.MonkeyPatch,
    look: Callable[[], Awaitable[Redacted]],
    budget: float,
) -> tuple[BaseException, int]:
    """What observing raises when each snapshot is `look`, within `budget`,
    and how many snapshots it started."""
    started: list[float] = []

    async def counted() -> Redacted:
        started.append(asyncio.get_running_loop().time())
        return await look()

    monkeypatch.setattr(attempt.session, "snapshot", counted)
    with pytest.raises((DocumentChangedError, UnansweredError)) as raised:
        await replace(attempt, budget=budget).observe()
    return raised.value, len(started)


def test_four_changed_snapshots_end_the_turn_with_a_document_change(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def changed() -> Redacted:
        raise DocumentChangedError

    async def scenario(attempt: Attempt) -> tuple[BaseException, int]:
        return await snapshots(attempt, monkeypatch, changed, 10)

    raised, started = on_app(app, tmp_path, scenario)

    assert (type(raised), started) == (DocumentChangedError, 4)
    assert isinstance(raised, DocumentChangedError)
    assert reason(raised, NO_SECRETS) == CHANGED


def test_a_snapshot_that_never_returns_ends_at_the_budget(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never() -> Redacted:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def scenario(attempt: Attempt) -> tuple[BaseException, int]:
        return await snapshots(attempt, monkeypatch, never, 0.5)

    raised, started = on_app(app, tmp_path, scenario)

    assert (type(raised), started) == (UnansweredError, 1)
    assert isinstance(raised, UnansweredError)
    assert reason(raised, NO_SECRETS) == "the page didn't answer within 0.5 s"


def test_no_snapshot_starts_once_the_budget_is_spent(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow_change() -> Redacted:
        await asyncio.sleep(0.4)
        raise DocumentChangedError

    async def scenario(attempt: Attempt) -> tuple[BaseException, int]:
        # Two looks and their waits take 1 s: a third would start after
        # the budget.
        return await snapshots(attempt, monkeypatch, slow_change, 0.95)

    raised, started = on_app(app, tmp_path, scenario)

    assert (type(raised), started) == (DocumentChangedError, 2)
