"""Per-case toggle checks for the benchmark pilot (ADR-0023).

Runs inside the checks image (checks.Dockerfile) on the app's Compose network;
bench/harness/toggle.py starts it. Each check starts from a reset app, drives
it, and reports whether its case's planted change is there ("planted") or not
("clean"). A check recognises both states exactly: anything else is an error,
never "clean". These checks only prove that a flag switches its change. They
are not the benchmark's oracle, and the system under test never runs them.

Usage (inside the image):
  python3 toggle_checks.py --list
  python3 toggle_checks.py CASE_ID [CASE_ID ...]
Environment: BENCH_APP_URL (default http://frontend) and BENCH_FIXTURE_PASSWORD,
the seeded accounts' public fixture password.
Output: one JSON object per line, {"case", "state", "detail"} or {"case", "error"}.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from playwright.sync_api import Browser, Error, Page, Playwright, sync_playwright

CLEAN = "clean"
PLANTED = "planted"
BASE = os.environ.get("BENCH_APP_URL", "http://frontend")
# Hit test at the element's center, as DATA_MODEL §7's visible_unoccluded does.
UNOCCLUDED = """el => {
  el.scrollIntoView({block: 'center'});
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return !!hit && (hit === el || el.contains(hit));
}"""


class UnexpectedStateError(Exception):
    pass


@dataclass(frozen=True)
class Observation:
    state: str
    detail: str


def classify(observed: object, clean: object, planted: object) -> Observation:
    """Which state ``observed`` is; raises if it is neither exactly."""
    if observed == clean:
        return Observation(CLEAN, repr(observed))
    if observed == planted:
        return Observation(PLANTED, repr(observed))
    message = f"observed {observed!r}; clean is {clean!r}, planted is {planted!r}"
    raise UnexpectedStateError(message)


def text(page: Page, selector: str) -> str:
    locator = page.locator(selector).first
    locator.wait_for()
    return " ".join(locator.inner_text().split())


class Session:
    """A sandboxed browser plus the app's API, for one run of the checks."""

    def __init__(self, playwright: Playwright, browser: Browser, password: str) -> None:
        self.api = playwright.request.new_context(base_url=BASE)
        self.browser = browser
        self.password = password

    def reset(self) -> None:
        response = self.api.post("/test-api/reset?fixture=seed")
        if response.status != 204:
            raise UnexpectedStateError(f"reset returned {response.status}")

    def token(self, email: str) -> str:
        body = {"user": {"email": email, "password": self.password}}
        response = self.api.post("/api/users/login", data=body)
        token: str = response.json()["user"]["token"]
        return token

    def probe(self, path: str) -> int:
        count: int = self.api.get(path).json()["count"]
        return count

    @contextmanager
    def page(self, email: str | None = None) -> Iterator[Page]:
        """A fresh context and page, signed in as ``email`` when given."""
        context = self.browser.new_context(
            base_url=BASE, viewport={"width": 1280, "height": 800}, timezone_id="UTC"
        )
        if email is not None:
            token = json.dumps(self.token(email))
            context.add_init_script(
                f"window.localStorage.setItem('jwtToken', {token});"
            )
        try:
            yield context.new_page()
        finally:
            context.close()


Check = Callable[[Session], Observation]
CHECKS: dict[str, Check] = {}


def check(case_id: str) -> Callable[[Check], Check]:
    def register(function: Check) -> Check:
        CHECKS[case_id] = function
        return function

    return register


@check("conduit-bug-001")
def article_date(session: Session) -> Observation:
    with session.page() as page:
        page.goto("/article/testing-without-flakes")
        date = text(page, ".banner .date")
        return classify(date, "January 4, 2026", "January 3, 2026")


@check("conduit-bug-002")
def article_page_errors(session: Session) -> Observation:
    with session.page() as page:
        errors: list[str] = []

        def record(error: Error) -> None:
            errors.append(error.name or "Error")

        page.on("pageerror", record)
        page.goto("/article/testing-without-flakes")
        page.locator(".card-text").first.wait_for()
        page.wait_for_load_state("networkidle")
        return classify(sorted(set(errors)), [], ["TypeError"])


@check("conduit-bug-003")
def favorite_status(session: Session) -> Observation:
    token = session.token("reader@conduit.test")
    response = session.api.post(
        "/api/articles/flaky-tests-are-bugs/favorite",
        headers={"authorization": f"Token {token}"},
    )
    return classify(response.status, 200, 500)


@check("conduit-bug-004")
def articles_saved_per_publish(session: Session) -> Observation:
    with session.page("jake@conduit.test") as page:
        page.goto("/editor")
        page.get_by_placeholder("Article Title").fill("Toggle check")
        page.get_by_placeholder("What's this article about?").fill("A toggle check.")
        page.get_by_placeholder("Write your article (in markdown)").fill("Body.")
        page.get_by_role("button", name="Publish Article").click()
        page.wait_for_url(re.compile(r"/article/"))
        # A duplicate create request is still in flight when the page navigates.
        page.wait_for_load_state("networkidle")
        created = session.probe("/test-api/articles/count?author=jake") - 5
        return classify(created, 1, 2)


@check("conduit-bug-005")
def header_links_covered(session: Session) -> Observation:
    with session.page("reader@conduit.test") as page:
        page.goto("/")
        nav = page.locator("nav.navbar")
        # Substring names: an icon-font glyph starts each accessible name (qa/REVIEW.md).
        links = [
            nav.get_by_role("link", name=n)
            for n in ("New Article", "Settings", "reader")
        ]
        covered = [not link.evaluate(UNOCCLUDED) for link in links]
        return classify(covered, [False, False, False], [True, True, True])


@check("conduit-benign-001")
def comment_button_label(session: Session) -> Observation:
    with session.page("reader@conduit.test") as page:
        page.goto("/article/welcome-to-conduit")
        label = text(page, "form.comment-form button[type=submit]")
        return classify(label, "Post Comment", "Add Comment")


@check("conduit-benign-002")
def favorites_count_placement(session: Session) -> Observation:
    with session.page() as page:
        page.goto("/article/flaky-tests-are-bugs")
        button = text(page, ".banner app-favorite-button button")
        beside = page.locator(".banner .favorites-count")
        label = " ".join(beside.inner_text().split()) if beside.count() else None
        return classify(
            (button, label),
            ("Favorite Article (0)", None),
            ("Favorite Article", "0 favorites"),
        )


def main(argv: list[str]) -> int:
    if argv == ["--list"]:
        print(json.dumps(sorted(CHECKS)))
        return 0
    password = os.environ["BENCH_FIXTURE_PASSWORD"]
    with sync_playwright() as playwright:
        # The sandbox stays on (AGENTS.md §6): a launch that can't sandbox fails
        # instead of silently running without it.
        browser = playwright.chromium.launch(chromium_sandbox=True)
        session = Session(playwright, browser, password)
        for case_id in argv:
            result: dict[str, str] = {"case": case_id}
            try:
                if case_id not in CHECKS:
                    raise UnexpectedStateError("no check for this case")
                session.reset()
                observation = CHECKS[case_id](session)
                result |= {"state": observation.state, "detail": observation.detail}
            except (UnexpectedStateError, Error) as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"[:500]
            print(json.dumps(result), flush=True)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
