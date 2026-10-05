"""Fixtures the result redaction tests share (#50 A2): a browser session on
the executor's fixture app that scans with a run's redactor, fake bound test
secrets, and a walk over everything a replay result holds. Imported by its
path, as pytest names the runner's test modules (TESTING.md §1, Shared
egress fixtures)."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import fields, is_dataclass

from aqa_core.project import SecretDestination
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.redaction import Redactor
from playwright.async_api import async_playwright
from pydantic import SecretStr

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.executor_fixtures import App

# What a result may hold: plain data only, never a live Playwright object.
PLAIN = (str, int, float, bool, type(None))


def fake_secret(name: str, value: str, origin: str) -> BoundSecret:
    """A fake test secret, `value`, bound to password fields on `origin`."""
    return BoundSecret(name, SecretStr(value), SecretDestination((origin,), "password"))


@asynccontextmanager
async def browsing(app: App, redactor: Redactor) -> AsyncIterator[BrowserSession]:
    """A session of a run whose start origin is the fixture app, scanning
    what it records with `redactor`."""
    async with (
        async_playwright() as playwright,
        egress_proxy(app.origin) as egress,
        open_browser_session(
            playwright.chromium, egress=egress, redactor=redactor
        ) as session,
    ):
        yield session


def leaves(value: object) -> list[object]:
    """Everything reachable from `value` through dataclass fields,
    sequences, sets and mappings that is none of those itself."""
    if is_dataclass(value) and not isinstance(value, type):
        return [
            leaf for item in fields(value) for leaf in leaves(getattr(value, item.name))
        ]
    if isinstance(value, Mapping):
        return [
            leaf for pair in value.items() for item in pair for leaf in leaves(item)
        ]
    if isinstance(value, (tuple, list, set, frozenset)):
        return [leaf for item in value for leaf in leaves(item)]
    return [value]
