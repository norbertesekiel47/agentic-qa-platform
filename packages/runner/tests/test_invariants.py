"""The four invariants (#47; DATA_MODEL §6; ADR-0024, Settling, and its #47
amendment): what the browser session's page did that keeps a run from
passing besides its assertions, observed from before the first navigation
to the end of the attempt, and which of them the spec counts. The browser
tests launch real Chromium on the OS that runs them: Linux in CI, macOS
locally."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from urllib.parse import quote

import pytest
from aqa_core.spec import InvariantName, Invariants
from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.document_origins import Records
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.invariants import INVARIANTS, invariant_results
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.executor_fixtures import App, serving_app

type Seen = Mapping[InvariantName, Records[str]]


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def observe(
    app: App,
    path: str,
    *,
    origins: tuple[str, ...] = (),
    then: Callable[[BrowserSession], Awaitable[None]] | None = None,
) -> Seen:
    """What the session's invariant observers saw while its page loaded
    `path` on the app and settled, then did `then`: the run allows the app's
    origin and `origins`."""
    run_gate = gate(allowed=(app.origin, *origins), private=origins)

    async def scenario() -> Seen:
        # No invariant test may hang the suite.
        async with (
            asyncio.timeout(60),
            async_playwright() as playwright,
            EgressProxy(run_gate) as proxy,
            open_browser_session(playwright.chromium, egress=proxy) as session,
        ):
            await session.settle(await session.navigate(app.origin + path))
            if then is not None:
                await then(session)
            return session.invariants.seen

    return asyncio.run(scenario())


def violated(seen: Seen) -> dict[str, list[str]]:
    """Each invariant a spec that inherits them all would count as violated,
    with what it saw, sorted."""
    return {
        result.name: sorted(result.seen)
        for result in invariant_results(seen, Invariants())
        if result.outcome == "violated"
    }


@pytest.mark.parametrize(
    ("page", "fired"),
    [
        ("console-error", {"console_errors": ["console-trigger"]}),
        # An uncaught exception and an unhandled rejection, never console errors.
        ("exception", {"js_exceptions": ["exception-trigger", "rejection-trigger"]}),
        # A dedicated worker's: Playwright 1.63 reports its responses on the
        # page, and no console entry for them.
        ("worker-5xx", {"http_5xx": ["500 {origin}/status/500"]}),
        # A 200 response that isn't an image fails with no console entry.
        ("broken-image", {"broken_images": ["{origin}/image/not-an-image"]}),
        ("broken-data-image", {"broken_images": ["data:image/png;base64,AAAA"]}),
    ],
)
def test_each_invariant_fires_on_its_own_trigger_and_no_other(
    app: App, page: str, fired: dict[str, list[str]]
) -> None:
    seen = observe(app, f"/page/{page}")

    assert violated(seen) == {
        name: [entry.format(origin=app.origin) for entry in entries]
        for name, entries in fired.items()
    }


@pytest.mark.parametrize(
    ("page", "fired", "status"),
    [
        (
            "page-5xx",
            {"http_5xx": ["500 {origin}/status/500"]},
            "500 (Internal Server Error)",
        ),
        (
            "missing-image",
            {"broken_images": ["{origin}/status/404"]},
            "404 (Not Found)",
        ),
    ],
)
def test_chromiums_own_entry_for_a_failed_load_is_also_a_console_error(
    app: App, page: str, fired: dict[str, list[str]], status: str
) -> None:
    # DATA_MODEL §6: console_errors counts the browser's own entries, and
    # the benchmark's conduit-bug-003 names console_errors with http_5xx.
    seen = observe(app, f"/page/{page}")

    assert violated(seen) == {
        **{
            name: [entry.format(origin=app.origin) for entry in entries]
            for name, entries in fired.items()
        },
        "console_errors": [
            f"Failed to load resource: the server responded with a status of {status}"
        ],
    }


def test_an_error_during_the_first_navigation_and_one_after_a_reload_are_both_seen(
    app: App,
) -> None:
    async def reload(session: BrowserSession) -> None:
        await session.settle(await session.reload())

    seen = observe(app, "/page/every-trigger", then=reload)

    assert violated(seen) == {
        "console_errors": ["console-navigate", "console-reload"],
        "js_exceptions": ["thrown-navigate", "thrown-reload"],
        "http_5xx": [f"500 {app.origin}/status/500"] * 2,
        "broken_images": [f"{app.origin}/image/not-an-image"] * 2,
    }


def test_nothing_a_page_does_stops_a_broken_image_being_seen(app: App) -> None:
    # The page deletes and replaces Playwright's binding and a global named
    # like ours, breaks what a page-world reporter would use, lies about the
    # image's URL, and stops the event in a capture listener of its own.
    seen = observe(app, "/page/tampering")

    assert violated(seen)["broken_images"] == [f"{app.origin}/image/not-an-image"]


def test_an_error_event_the_page_makes_up_is_no_broken_image(app: App) -> None:
    seen = observe(app, "/page/synthetic-error")

    assert violated(seen) == {}


def test_only_documents_on_allowed_origins_report_broken_images(app: App) -> None:
    with serving_app() as other:
        # A frame on another allowed origin, and a data: frame, on none.
        seen = observe(
            app,
            f"/page/framed?to={quote(other.origin + '/page/broken-image', safe='')}",
            origins=(other.origin,),
        )

    assert violated(seen) == {"broken_images": [f"{other.origin}/image/not-an-image"]}


def test_a_refused_requests_console_error_and_broken_image_dont_count(app: App) -> None:
    # Neither host is declared, so both are refused: by routing, and by the
    # proxy at a redirect hop. An expected-blocked host is refused the same
    # way, so its symptoms are left out the same way (ADR-0026's #47
    # amendment). The page's own console error still counts.
    seen = observe(app, "/page/refused-symptoms")

    assert violated(seen) == {"console_errors": ["unrelated"]}


def test_a_script_that_throws_because_a_refused_script_never_loaded_counts(
    app: App,
) -> None:
    seen = observe(app, "/page/refused-script")

    assert violated(seen) == {"js_exceptions": ["analytics is not defined"]}


def test_chromiums_entry_for_a_load_no_network_refused_still_counts(app: App) -> None:
    # A revoked blob: Chromium's own entry, at a URL no request of the run's
    # could be refused at.
    seen = observe(app, "/page/revoked-blob")

    assert violated(seen) == {
        "console_errors": ["Failed to load resource: net::ERR_FILE_NOT_FOUND"]
    }


def test_a_pages_console_error_that_names_a_refused_url_still_counts(
    app: App,
) -> None:
    # Only Chromium's own entry for a refused load is left out: it carries no
    # arguments, and the page's console call does.
    seen = observe(app, "/page/forged-source")

    assert violated(seen) == {"console_errors": ["forged"]}


def test_what_an_invariant_keeps_is_bounded(app: App) -> None:
    # The page writes what an invariant sees: it keeps the first 100, each
    # cut to 200 characters, and counts them all.
    seen = observe(app, "/page/many-console-errors")

    [console] = [
        result
        for result in invariant_results(seen, Invariants())
        if result.name == "console_errors"
    ]
    assert console.total == 150
    assert console.seen == tuple(
        f"{i}:" + "x" * (199 - len(str(i))) for i in range(100)
    )


def test_a_disabled_invariant_doesnt_count() -> None:
    seen = {name: Records[str]() for name in INVARIANTS}
    seen["console_errors"].add("console-trigger")
    seen["js_exceptions"].add("exception-trigger")

    results = invariant_results(
        seen, Invariants.model_validate({"disable": ["console_errors"]})
    )

    # A disabled invariant still shows what it saw.
    assert [(r.name, r.outcome, r.seen, r.total) for r in results] == [
        ("console_errors", "disabled", ("console-trigger",), 1),
        ("js_exceptions", "violated", ("exception-trigger",), 1),
        ("http_5xx", "held", (), 0),
        ("broken_images", "held", (), 0),
    ]


def test_a_spec_that_inherits_no_invariant_counts_none() -> None:
    seen = {name: Records[str]() for name in INVARIANTS}
    for name in INVARIANTS:
        seen[name].add(name)

    results = invariant_results(seen, Invariants.model_validate({"inherit": False}))

    assert [(r.name, r.outcome) for r in results] == [
        (name, "disabled") for name in INVARIANTS
    ]
