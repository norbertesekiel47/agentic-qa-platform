"""Fixtures the navigator action tools' tests share (#53 P13): an attempt
of a run against a fixture site, in real Chromium, with the run's record
part and the session's redactor, as explore will open one. Imported by its
path, as pytest names the runner's test modules (TESTING.md §1, Shared
egress fixtures)."""

import asyncio
import errno
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict, Unpack

import pytest
from aqa_core.config import ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import SecretDestination
from aqa_core.schema import Contract
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import open_browser_session
from aqa_runner.egress import EgressGate
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import NAVIGATION_SECONDS
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.navigator_tools import Attempt
from aqa_runner.redaction import Redactor
from aqa_runner.run_record import RunRecord
from playwright.async_api import async_playwright
from pydantic import SecretStr

from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import App, a_spec, read_steps

# The meanings the form page's elements are acted on under.
SAVE = "the form's save button"
NAME = "the form's name field"
SIZE = "the form's size list"
SIGN_IN = "the sign-in form's password field"
STATUS = "the form's save status"

# The plan the attempts explore for: one check reads the save status's text.
PLAN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": "the form",
                "claim": "is saved",
                "checks": [
                    {
                        "check": "text_in_target",
                        "target_meaning": STATUS,
                        "text": "saved",
                    }
                ],
            }
        ],
        "requires": [],
    }
)


def bound(origin: str, value: str, name: str = "TEST_PASSWORD") -> BoundSecret:
    """A fake test secret, `value`, bound to password fields on `origin`."""
    return BoundSecret(name, SecretStr(value), SecretDestination((origin,), "password"))


class Run(TypedDict, total=False):
    """How an attempt's run is set up, past its start origin: the page it
    starts on (the form by default), its egress gate (one allowing only the
    start origin by default), the secrets it binds, its plan and contracts,
    its `resolve_seconds`, and its project config (an empty one by
    default)."""

    page: str
    egress: EgressGate
    secrets: Sequence[BoundSecret]
    plan: CoveragePlan
    contracts: Mapping[str, Contract | None]
    budget: float
    config: ProjectConfig


def explored[T](
    tmp_path: Path,
    scenario: Callable[[Attempt], Awaitable[T]],
    start: str,
    *,
    app: App | None = None,
    **run: Unpack[Run],
) -> T:
    """`scenario`'s result for the attempt of a run whose start origin is
    `start`, set up as `run` says, for a spec that starts on its page: the
    session registers the identity engine first and scans with the run's
    redactor, its record's attempt part scans with the same, its egress
    blocks and infrastructure events interrupt it, as replay's do, and, once
    the attempt is made, the page is opened and observed once. `app`, the
    executor's fixture app, notes the part's journal as each request
    arrives."""
    secrets = run.get("secrets", ())
    budget = run.get("budget", 5)
    page = run.get("page", "/page/form")
    config = run.get("config", ProjectConfig())
    run_gate = run.get("egress") or gate(allowed=(start,))
    expected = config.egress.expected_blocked
    redactor = Redactor(secrets)
    record = RunRecord.create(tmp_path).redacting(redactor).part("attempt-1")
    if app is not None:
        app.record = record.path

    async def attempted() -> T:
        async with async_playwright() as playwright:
            await register_identity_engine(playwright)
            async with (
                EgressProxy(run_gate) as proxy,
                open_browser_session(
                    playwright.chromium, egress=proxy, redactor=redactor
                ) as session,
                # No navigator test may hang the suite.
                asyncio.timeout(90),
            ):
                session.limit_waits(
                    action_seconds=budget, navigation_seconds=NAVIGATION_SECONDS
                )
                attempt = Attempt(
                    session,
                    record,
                    start,
                    run.get("plan", PLAN),
                    run.get("contracts", {}),
                    {secret.name: secret for secret in secrets},
                    budget,
                    a_spec(tmp_path, config, start_url=page),
                    config,
                    run_gate,
                    lambda: (
                        bool(run_gate.infrastructure_events)
                        or proxy.egress_blocks(expected).blocked
                    ),
                )
                await session.navigate(f"{start}{page}")
                await attempt.observe()
                return await scenario(attempt)

    return asyncio.run(attempted())


def on_app[T](
    app: App,
    tmp_path: Path,
    scenario: Callable[[Attempt], Awaitable[T]],
    **run: Unpack[Run],
) -> T:
    """`scenario`'s result for an attempt against the executor's fixture app,
    which notes the attempt's journal as each request arrives."""
    return explored(tmp_path, scenario, app.origin, **run, app=app)


def ref(attempt: Attempt, role: str, name: str) -> str:
    """The ref the attempt's last observation gives the first element with
    `role` and `name`, whatever states it shows, such as [active]."""
    assert attempt.observation is not None
    found = re.search(
        rf'- {role} "{re.escape(name)}"(?: \[[^\]]*\])*? \[ref=(e\d+)\]',
        attempt.observation,
    )
    assert found is not None, attempt.observation
    return found[1]


def journal(attempt: Attempt) -> list[dict[str, Any]]:
    """The attempt's steps journal, each line without its time."""
    return read_steps(attempt.record.path)


def fail_fsync(monkeypatch: pytest.MonkeyPatch, at: int = 0) -> list[int]:
    """Make the `at`-th fsync from now raise EIO; return the calls seen."""
    calls: list[int] = []
    fsync = os.fsync

    def failing(fd: int) -> None:
        calls.append(fd)
        if len(calls) == at + 1:
            raise OSError(errno.EIO, "fake fsync failure")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", failing)
    return calls
