"""The navigator's check tools (#53 P14; ADR-0024's #53 P14 amendment):
`assert_check` binds a planned check to the element its ref names, through
its meaning's targets and subject contract, and evaluates it on the page now;
`note_for_absence` notes a not_visible check's element while it shows; and
`finish` judges every bound check on the page as the confirmation will, then
compiles the kept path. Real Chromium against the executor's fixture app."""

import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import (
    ByCss,
    ByLabel,
    ByRole,
    ByTestId,
    CompiledScript,
    Target,
    TextInTarget,
)
from aqa_core.compiler import ExploredPath, Provenance, assertion_for, compile_script
from aqa_core.config import ProjectConfig, SubjectContract
from aqa_core.coverage_plan import CoveragePlan, planned_checks
from aqa_core.schema import Contract
from aqa_runner import locator_generation, navigator_tools
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.evaluation import (
    AssertionOutcome,
    AssertionResult,
    Evaluation,
    evaluate,
)
from aqa_runner.navigator import AssertCheck, Click, Fill, Finish, NoteForAbsence
from aqa_runner.navigator_tools import Attempt, Finished
from aqa_runner.run_record import PendingIntent
from playwright.async_api import Error
from pydantic import ValidationError

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.executor_fixtures import PAGES, App, serving_app
from packages.runner.tests.navigator_fixtures import (
    NAME,
    SAVE,
    STATUS,
    bound,
    on_app,
    ref,
)
from packages.runner.tests.secret_fixtures import OTHER_FAKE_VALUE

VALUE = OTHER_FAKE_VALUE
BY = Provenance(
    models={"navigator": "fake-model"},
    price_map="6f0c3a9d2b8e4f17a5c9d0e3b6a1f4c8d2e7b9a0",
    at=datetime(2026, 10, 12, tzinfo=UTC),
)
COMPILED = "finish compiled the path, and every check passes on this page"
NOTED = (
    "a1's element is noted where it shows: call assert_check for a1 with no ref "
    "once it is gone"
)
ENDED = (
    "this attempt has ended, since step 2 was dispatched and its completion isn't "
    "on disk"
)
VISUAL = "the page's visual button"
LINK = "the header's Sign in link"
FAVORITE = "the banner's favorite button"
COUNT = "the banner's favorites count"
BUTTON_ROW = Contract(region="div.banner", part="app-favorite-button > button")
COUNT_ROW = Contract(region="div.banner", part="span.counter", leaf=True)

# A favorite button, in the banner and again in the article's actions.
COPY = (
    "<app-favorite-button><button>Unfavorite Article "
    '<span class="counter">(1)</span></button></app-favorite-button>'
)
ARTICLE = f'<div class="banner">{COPY}</div><div class="article-actions">{COPY}</div>'
REMOVES = "<button onclick=\"document.querySelector('{}').remove()\">{}</button>"
SIGN_IN = '<nav class="navbar"><a href="/page/form">Sign in</a></nav>'

# The pages these tests explore, past the executor's fixture app's own.
CHECK_PAGES = {
    # The header's link, which one button removes, and the whole header.
    "header": SIGN_IN
    + REMOVES.format("nav a", "Sign in now")
    + REMOVES.format("nav", "Drop the header"),
    # The header's link and the page's own, with the same name.
    "both-links": f'{SIGN_IN}<main><a href="/page/form">Sign in</a></main>'
    + REMOVES.format("nav a", "Sign in now"),
    # The header's link, whose test id and whose header's id spell a bound value.
    "secret-header": (
        f'<nav class="navbar" id="{VALUE}">'
        f'<a href="/page/form" data-testid="{VALUE}">Sign in</a></nav>'
    )
    + REMOVES.format("nav a", "Sign in now"),
    # A link the button removes as it signs in.
    "terminal": f"""{SIGN_IN}<p class="status">Signed out</p>
        <button onclick="document.querySelector('nav a').remove();
            document.querySelector('p.status').textContent = 'Signed in'">
        Sign in now</button>""",
    # Two statuses; a button changes only the second.
    "ready": """<p id="first">Ready</p><p id="second">Ready</p>
        <button onclick="document.querySelector('#second').textContent = 'Gone'">
        Change</button>
        <button onclick="document.querySelector('#first').textContent = 'Gone'">
        Change the first</button>""",
    "article": ARTICLE,
    "article-hiding": ARTICLE
    + REMOVES.format("div.banner app-favorite-button > button", "Hide it"),
    # The banner's favorite button, and a button that removes the banner.
    "regioned": (
        '<div class="banner"><app-favorite-button><button>Unfavorite Article (1)'
        "</button></app-favorite-button></div>"
    )
    + REMOVES.format("div.banner", "Drop the banner"),
    # The banner's button with its count flattened into its text.
    "flattened": (
        '<div class="banner"><app-favorite-button><button>Unfavorite Article bonus '
        '1 (3)</button></app-favorite-button></div><div class="article-actions">'
        f"{COPY}</div>"
    ),
    # A status in the banner, beside the favorite button and its count.
    "decoy": (
        f'<div class="banner"><span class="decoy" role="status">1</span>{COPY}</div>'
        f'<div class="article-actions">{COPY}</div>'
    ),
    # Two Save buttons: only the first one's test id tells them apart.
    "twins": f'<button data-testid="{VALUE}">Save</button><button>Save</button>',
    # The visual page's anchor as a button, which the snapshot gives a ref.
    "visual-button": PAGES["visual"]
    .replace('<a id="visual"', '<button id="visual"')
    .replace("Save</a>", "Save</button>")
    .replace("display:block", "display:block;border:0;padding:0"),
    # A status whose id only the first page has.
    "one": '<p id="status" class="status">Saved</p><a href="/page/two">Next</a>',
    "two": '<p class="status">Saved</p>',
    # A status whose test id spells a bound value.
    "token": (
        f'<p id="status" class="status" data-testid="{VALUE}">Saved Ada in size S</p>'
    ),
}


def plan(*checks: dict[str, object]) -> CoveragePlan:
    """A plan of one expectation, checked by `checks`, in order."""
    return CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": "the form",
                    "claim": "is saved",
                    "checks": list(checks),
                }
            ],
            "requires": [],
        }
    )


SAVED = plan(
    {"check": "text_in_target", "target_meaning": STATUS, "text": "Saved Ada in size S"}
)
GONE = plan({"check": "not_visible", "target_meaning": LINK})
HIDDEN = plan({"check": "not_visible", "target_meaning": FAVORITE})
# The shop's status, the first page's (which has #status), the second's
# (which doesn't), and the header's link, as a negative check finds it.
SHOP_STATUS = Target(
    semantic=STATUS,
    locators=(
        ByTestId(testid="status"),
        ByCss(css="#status"),
        ByRole(role="paragraph"),
        ByCss(css="p"),
    ),
)
FIRST_STATUS = Target(
    semantic=STATUS,
    locators=(ByCss(css="#status"), ByRole(role="paragraph"), ByCss(css="p.status")),
)
SECOND_STATUS = Target(
    semantic=STATUS, locators=(ByRole(role="paragraph"), ByCss(css="p.status"))
)
NAVBAR = ByCss(css="nav.navbar")
HEADER_LINK = Target(
    semantic=LINK,
    locators=(
        ByRole(scope=NAVBAR, role="link", name="Sign in"),
        ByCss(scope=NAVBAR, css='a[href="/page/form"]'),
    ),
)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    for name, page in CHECK_PAGES.items():
        monkeypatch.setitem(PAGES, name, page)
    with serving_app() as served:
        yield served


def secret(app: App, *, binds: bool = True) -> list[BoundSecret]:
    """The run's secrets: the fake value, bound on the app's origin, or none."""
    return [bound(app.origin, VALUE, "API_TOKEN")] if binds else []


def refs(attempt: Attempt, pattern: str) -> list[str]:
    """The refs the attempt's last observation gives the lines `pattern`
    matches up to the ref."""
    found: list[str] = re.findall(
        rf"{pattern} \[ref=(e\d+)\]", attempt.observation or ""
    )
    return found


def paragraph(attempt: Attempt, nth: int = 0) -> str:
    return refs(attempt, "- paragraph")[nth]


def favorite_buttons(attempt: Attempt) -> list[str]:
    """The refs of the banner's favorite button and the article actions'."""
    return refs(attempt, r'button "Unfavorite Article \(1\)"')


def told(finished: Finished) -> tuple[CompiledScript | None, str]:
    """What `finish` returned: its script and its text."""
    return finished.script, finished.text


async def saved(attempt: Attempt) -> None:
    """Fill the form's name with Ada and save it."""
    await attempt.act(
        Fill(ref=ref(attempt, "textbox", "Name"), meaning=NAME, text="Ada")
    )
    await attempt.act(Click(ref=ref(attempt, "button", "Save"), meaning=SAVE))


async def clicked(
    attempt: Attempt, name: str, meaning: str, role: str = "button"
) -> None:
    """Click the page's element called `name`, under `meaning`, then observe
    the page."""
    await attempt.act(Click(ref=ref(attempt, role, name), meaning=meaning))
    await attempt.observe()


# What the pages' buttons that remove the header's link are for.
UNLINKS = "the page's button that removes the header's link"


async def noted_link(attempt: Attempt) -> str:
    return await attempt.note(
        NoteForAbsence(check_id="a1", ref=ref(attempt, "link", "Sign in"))
    )


def test_a_bound_check_is_evaluated_by_the_executor(app: App, tmp_path: Path) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        before = await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        failing = attempt.checks
        await saved(attempt)
        await attempt.observe()
        after = await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        return [before, failing, after, attempt.checks, attempt.uses(STATUS).targets]

    # The right element in the wrong state stays bound.
    assert on_app(app, tmp_path, scenario, page="/page/shop", plan=SAVED) == [
        "a1 is bound, and on this page it is failed",
        {"a1": AssertionResult("a1", "failed", locator_index=0)},
        "a1 is bound, and on this page it is pass",
        {"a1": AssertionResult("a1", "pass", locator_index=0)},
        (SHOP_STATUS,),
    ]


def test_a_call_that_doesnt_fit_its_check_binds_nothing(
    app: App, tmp_path: Path
) -> None:
    calls = [
        AssertCheck(check_id="a9", ref=None),
        AssertCheck(check_id="a1", ref=None),
        AssertCheck(check_id="a2", ref="e1"),
        AssertCheck(check_id="a3", ref="e1"),
        AssertCheck(check_id="a4", ref=None),
        AssertCheck(check_id="a1", ref=VALUE),
    ]

    async def scenario(attempt: Attempt) -> list[object]:
        refused = [await attempt.check(call) for call in calls]
        checks = attempt.checks
        url = await attempt.check(AssertCheck(check_id="a2", ref=None))
        return [*refused, checks, url, attempt.checks["a2"]]

    no = "assert_check did not run:"
    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/shop",
        plan=plan(
            {"check": "text_in_target", "target_meaning": STATUS, "text": "Saved"},
            {"check": "url_matches", "pattern": "/page/shop$"},
            {"check": "not_visible", "target_meaning": LINK},
            {"check": "probe_equals_baseline", "probe": "count"},
        ),
        secrets=secret(app),
    ) == [
        f"{no} the plan has no check a9",
        f"{no} a1 reads an element: call it with the ref of the element it reads",
        f"{no} a2 reads no element: call it with no ref",
        (
            f"{no} a3 is a not_visible check: note its element with note_for_absence "
            "while it shows, then call assert_check with no ref once it is gone"
        ),
        f"{no} a4 is a probe_equals_baseline check, which exploring can't bind yet",
        f"{no} its ref holds a test secret's value",
        {"a1": None, "a2": None, "a3": None, "a4": None},
        "a2 is bound, and on this page it is pass",
        AssertionResult("a2", "pass"),
    ]


@pytest.mark.parametrize(
    ("state", "outcome"), [("", "pass"), ("?state=covered", "failed")]
)
def test_a_visible_unoccluded_check_is_judged_in_the_viewport_with_a_size(
    app: App, tmp_path: Path, state: str, outcome: AssertionOutcome
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        text = await attempt.check(
            AssertCheck(check_id="a1", ref=ref(attempt, "button", "Save"))
        )
        return [text, attempt.checks]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page=f"/page/visual-button{state}",
        plan=plan({"check": "visible_unoccluded", "target_meaning": VISUAL}),
    ) == [
        f"a1 is bound, and on this page it is {outcome}",
        {"a1": AssertionResult("a1", outcome, locator_index=0)},
    ]


def test_a_checks_target_never_keeps_a_locator_spelling_a_bound_value(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        text = await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        return [text, attempt.uses(STATUS).targets]

    assert on_app(
        app, tmp_path, scenario, page="/page/token", plan=SAVED, secrets=secret(app)
    ) == ["a1 is bound, and on this page it is pass", (FIRST_STATUS,)]


@pytest.mark.parametrize(
    ("binds", "reason"),
    [
        (
            False,
            (
                "the page is on no origin a run could allow: nothing there is "
                "observed or acted on; navigate to an allowed origin, or restart"
            ),
        ),
        (
            True,
            (
                "the page is on an origin the run doesn't allow (withheld: the run "
                "has a test secret)"
            ),
        ),
    ],
    ids=["unbound", "bound"],
)
def test_a_check_on_a_page_off_the_allowed_origins_names_no_origin_when_a_secret_is_bound(
    app: App, tmp_path: Path, *, binds: bool, reason: str
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        await until(lambda: attempt.session.page.url.startswith("chrome-error:"))
        url = await attempt.check(AssertCheck(check_id="a1", ref=None))
        element = await attempt.check(AssertCheck(check_id="a2", ref="e1"))
        return [url, element, attempt.checks]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/leaves",
        plan=plan(
            {"check": "url_matches", "pattern": "/page/leaves$"},
            {"check": "visible_unoccluded", "target_meaning": VISUAL},
        ),
        secrets=secret(app, binds=binds),
    ) == [
        f"a1 is bound, and on this page it is not_evaluated: {reason}",
        f"assert_check did not run: {reason}",
        {"a1": AssertionResult("a1", "not_evaluated", error=reason), "a2": None},
    ]


def test_note_for_absence_binds_the_agents_element_where_it_was_seen(
    app: App, tmp_path: Path
) -> None:
    with pytest.raises(ValidationError):
        NoteForAbsence.model_validate(
            {"check_id": "a1", "ref": "e3", "text": "Sign in"}
        )

    async def scenario(attempt: Attempt) -> list[object]:
        link = ref(attempt, "link", "Sign in")
        unknown = await attempt.note(NoteForAbsence(check_id="a9", ref=link))
        wrong = await attempt.note(NoteForAbsence(check_id="a2", ref=link))
        note = await noted_link(attempt)
        await clicked(attempt, "Sign in now", UNLINKS)
        gone = await attempt.check(AssertCheck(check_id="a1", ref=None))
        own = ref(attempt, "link", "Sign in")
        await attempt.check(AssertCheck(check_id="a2", ref=own))
        finished = await attempt.finish(Finish(steps=(1,)), BY)
        targets = attempt.uses(LINK).targets
        return [unknown, wrong, note, gone, attempt.checks, targets, finished.text]

    # The page's own Sign in link still shows: only the header's is gone.
    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/both-links",
        plan=plan(
            {"check": "not_visible", "target_meaning": LINK},
            {
                "check": "text_in_target",
                "target_meaning": "the page's own Sign in link",
                "text": "Sign in",
            },
        ),
    ) == [
        "note_for_absence did not run: the plan has no check a9",
        (
            "note_for_absence did not run: a2 is a text_in_target check, and it "
            "notes only a not_visible check's element"
        ),
        NOTED,
        "a1 is bound, and on this page it is pass",
        {
            # Absence resolves no element, so no locator's index is kept.
            "a1": AssertionResult("a1", "pass"),
            "a2": AssertionResult("a2", "pass", locator_index=0),
        },
        (HEADER_LINK,),
        COMPILED,
    ]


def test_a_contracted_negative_check_binds_only_inside_its_region(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        banner, lower = favorite_buttons(attempt)
        outside = await attempt.note(NoteForAbsence(check_id="a1", ref=lower))
        unseen = await attempt.check(AssertCheck(check_id="a1", ref=None))
        note = await attempt.note(NoteForAbsence(check_id="a1", ref=banner))
        await clicked(
            attempt,
            "Hide it",
            "the page's button that hides the banner's favorite button",
        )
        gone = await attempt.check(AssertCheck(check_id="a1", ref=None))
        return [outside, unseen, note, gone, attempt.uses(FAVORITE).targets]

    scope = ByCss(css="app-favorite-button")
    # The other copy still shows when the banner's is gone.
    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/article-hiding",
        plan=HIDDEN,
        contracts={FAVORITE: BUTTON_ROW},
    ) == [
        (
            "note_for_absence did not run: this check's subject binds only inside "
            "div.banner, and this element is outside it"
        ),
        (
            "assert_check did not run: the banner's favorite button: it wasn't seen "
            "before its negative check"
        ),
        NOTED,
        "a1 is bound, and on this page it is pass",
        (
            Target(
                semantic=FAVORITE,
                locators=(
                    ByRole(scope=scope, role="button", name="Unfavorite Article (1)"),
                    ByCss(scope=scope, css="button"),
                ),
                contract=BUTTON_ROW,
            ),
        ),
    ]


def test_a_negative_checks_sighting_never_tries_or_keeps_a_bound_value(
    app: App, tmp_path: Path, handed: list[str]
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        handed.clear()
        note = await noted_link(attempt)
        await clicked(attempt, "Sign in now", UNLINKS)
        gone = await attempt.check(AssertCheck(check_id="a1", ref=None))
        return [note, gone, attempt.uses(LINK).targets]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/secret-header",
        plan=GONE,
        secrets=secret(app),
    ) == [NOTED, "a1 is bound, and on this page it is pass", (HEADER_LINK,)]
    assert handed
    assert [text for text in handed if VALUE in text] == []
    assert [text for text in handed if re.search(text, VALUE)] == []


async def never_noted(_: Attempt, __: pytest.MonkeyPatch) -> None:
    return None


async def still_shown(attempt: Attempt, _: pytest.MonkeyPatch) -> None:
    await noted_link(attempt)


async def header_removed(attempt: Attempt, _: pytest.MonkeyPatch) -> None:
    await noted_link(attempt)
    await clicked(
        attempt, "Drop the header", "the page's button that removes the header"
    )


async def banner_removed(attempt: Attempt, _: pytest.MonkeyPatch) -> None:
    (banner,) = favorite_buttons(attempt)
    await attempt.note(NoteForAbsence(check_id="a1", ref=banner))
    await clicked(
        attempt, "Drop the banner", "the page's button that removes the banner"
    )


async def look_broken(attempt: Attempt, monkeypatch: pytest.MonkeyPatch) -> None:
    await noted_link(attempt)

    async def breaking(*_: object) -> object:
        raise Error("fake page break")

    monkeypatch.setattr(locator_generation, "resolve", breaking)


@dataclass(frozen=True)
class Producer:
    """A way a negative check is refused: what the attempt does before the
    check, on which page, under which contracts, and the reason it gets."""

    before: Callable[[Attempt, pytest.MonkeyPatch], Awaitable[None]]
    page: str
    contracts: Mapping[str, Contract]
    reason: str


@pytest.mark.parametrize("binds", [False, True], ids=["unbound", "bound"])
@pytest.mark.parametrize(
    "producer",
    [
        Producer(
            never_noted,
            "/page/header",
            {},
            "the header's Sign in link: it wasn't seen before its negative check",
        ),
        Producer(
            still_shown,
            "/page/header",
            {},
            "the header's Sign in link: it is still shown: a locator it was seen by "
            "finds something visible in its scope",
        ),
        Producer(
            header_removed,
            "/page/header",
            {},
            "the header's Sign in link: no scope it was seen under is on the page",
        ),
        Producer(
            banner_removed,
            "/page/regioned",
            {FAVORITE: BUTTON_ROW},
            "the banner's favorite button: its region is not on the page exactly once",
        ),
        Producer(
            look_broken,
            "/page/header",
            {},
            "the header's Sign in link: the page broke the look at its negative check",
        ),
    ],
    ids=[
        "never-noted",
        "still-shown",
        "scope-missing",
        "region-invalid",
        "look-broken",
    ],
)
def test_a_negative_checks_refusal_says_why_in_a_run_that_binds_a_secret(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    producer: Producer,
    binds: bool,
) -> None:
    meaning = FAVORITE if producer.contracts else LINK

    async def scenario(attempt: Attempt) -> list[object]:
        await producer.before(attempt, monkeypatch)
        refused = await attempt.check(AssertCheck(check_id="a1", ref=None))
        return [refused, attempt.checks]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page=producer.page,
        plan=plan({"check": "not_visible", "target_meaning": meaning}),
        contracts=producer.contracts,
        secrets=secret(app, binds=binds),
    ) == [f"assert_check did not run: {producer.reason}", {"a1": None}]


@pytest.mark.parametrize(
    ("binds", "said"),
    [
        (False, "a1 is bound, and on this page it is pass"),
        (
            True,
            (
                "assert_check did not run: no locator could be generated for this "
                "element (withheld: the run has a test secret)"
            ),
        ),
    ],
    ids=["unbound", "bound"],
)
def test_an_ordinary_locator_error_stays_withheld_in_a_run_that_binds_a_secret(
    app: App, tmp_path: Path, *, binds: bool, said: str
) -> None:
    async def scenario(attempt: Attempt) -> str:
        return await attempt.check(
            AssertCheck(check_id="a1", ref=ref(attempt, "button", "Save"))
        )

    # Bound, the test id is dropped, and nothing else finds the button alone.
    assert (
        on_app(
            app,
            tmp_path,
            scenario,
            page="/page/twins",
            plan=plan({"check": "visible_unoccluded", "target_meaning": SAVE}),
            secrets=secret(app, binds=binds),
        )
        == said
    )


def test_finish_judges_every_check_as_the_confirmation_will(
    app: App, tmp_path: Path
) -> None:
    twice = plan(
        {"check": "text_in_target", "target_meaning": STATUS, "text": "Saved"},
        {"check": "text_in_target", "target_meaning": STATUS, "pattern": "^Saved$"},
    )

    async def scenario(attempt: Attempt) -> list[object]:
        await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        await clicked(attempt, "Next", "the page's Next link", "link")
        await attempt.check(AssertCheck(check_id="a2", ref=paragraph(attempt)))
        targets = attempt.uses(STATUS).targets
        refused = told(await attempt.finish(Finish(steps=(1,)), BY))
        judged = attempt.checks["a1"]
        # The first locator misses here; normal mode passes by a later one.
        normally = await evaluate(
            attempt.session,
            assertion_for("a1", 0, planned_checks(twice)["a1"][1], target="a1"),
            Evaluation(
                {"a1": FIRST_STATUS},
                attempt.spec,
                attempt.start,
                attempt.gate,
                attempt.budget,
                attempt.session.redactor,
            ),
        )
        again = await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        finished = await attempt.finish(Finish(steps=(1,)), BY)
        script = finished.script
        return [
            targets,
            refused,
            judged,
            normally,
            again,
            attempt.uses(STATUS).targets,
            finished.text,
            None if script is None else (script.targets, script.assertions),
        ]

    # A target used twice splits where a locator misses (relay 4).
    assert on_app(app, tmp_path, scenario, page="/page/one", plan=twice) == [
        (FIRST_STATUS, SECOND_STATUS),
        (
            None,
            (
                "finish compiled nothing: on this page a1 is binding_unresolved (no "
                "match, not unanimous, not unanimous)"
            ),
        ),
        AssertionResult(
            "a1", "binding_unresolved", ("no match", "not unanimous", "not unanimous")
        ),
        AssertionResult("a1", "pass", locator_index=1),
        "a1 is bound, and on this page it is pass",
        (FIRST_STATUS, SECOND_STATUS),
        COMPILED,
        (
            {
                "t1": Target(
                    semantic="the page's Next link",
                    locators=(
                        ByRole(role="link", name="Next"),
                        ByCss(css='a[href="/page/two"]'),
                    ),
                ),
                "t2": SECOND_STATUS,
            },
            (
                TextInTarget(
                    check="text_in_target",
                    id="a1",
                    expect_index=0,
                    text="Saved",
                    target="t2",
                ),
                TextInTarget(
                    check="text_in_target",
                    id="a2",
                    expect_index=0,
                    pattern="^Saved$",
                    target="t2",
                ),
            ),
        ),
    ]


def test_finish_compiles_the_kept_path_and_every_bound_check(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> Finished:
        await saved(attempt)
        await attempt.observe()
        await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        return await attempt.finish(Finish(steps=(1, 2)), BY)

    script, text = told(on_app(app, tmp_path, scenario, page="/page/shop", plan=SAVED))

    assert text == COMPILED
    assert script is not None
    assert script.confirmed is False
    assert script.targets == {
        "t1": Target(
            semantic=NAME,
            locators=(
                ByRole(role="textbox", name="Name"),
                ByLabel(label="Name"),
                ByCss(css="#name"),
                ByCss(css="input"),
            ),
        ),
        "t2": Target(
            semantic=SAVE,
            locators=(
                ByRole(role="button", name="Save"),
                ByCss(css="#save"),
                ByCss(css="button"),
            ),
        ),
        "t3": SHOP_STATUS,
    }
    assert [
        step.model_dump(include={"seq", "action", "target", "value"})
        for step in script.steps
    ] == [
        {"seq": 1, "action": "fill", "target": "t1", "value": "Ada"},
        {"seq": 2, "action": "click", "target": "t2"},
    ]
    assert script.assertions == (
        TextInTarget(
            check="text_in_target",
            id="a1",
            expect_index=0,
            text="Saved Ada in size S",
            target="t3",
        ),
    )


def test_finish_judges_every_bound_check_not_only_the_first(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        bound_now = [
            await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt, 0))),
            await attempt.check(AssertCheck(check_id="a2", ref=paragraph(attempt, 1))),
        ]
        await attempt.act(
            Click(ref=ref(attempt, "button", "Change"), meaning="the change button")
        )
        refused = told(await attempt.finish(Finish(steps=(1,)), BY))
        checks = attempt.checks
        both = told(await attempt.finish(Finish(steps=(1, 7)), BY))
        await attempt.act(
            Click(ref=ref(attempt, "button", "Change the first"), meaning="the first")
        )
        each = told(await attempt.finish(Finish(steps=(1, 2)), BY))
        return [bound_now, refused, checks, both, each]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/ready",
        plan=plan(
            {"check": "text_in_target", "target_meaning": "the first", "text": "Ready"},
            {
                "check": "text_in_target",
                "target_meaning": "the second",
                "text": "Ready",
            },
        ),
    ) == [
        [
            "a1 is bound, and on this page it is pass",
            "a2 is bound, and on this page it is pass",
        ],
        (None, "finish compiled nothing: on this page a2 is failed"),
        {
            "a1": AssertionResult("a1", "pass", locator_index=0),
            "a2": AssertionResult("a2", "failed", locator_index=0),
        },
        (
            None,
            (
                "finish compiled nothing: on this page a2 is failed; finish names "
                "step 7, which the attempt never took"
            ),
        ),
        # Every check that fails, in plan order.
        (None, "finish compiled nothing: on this page a1 is failed; a2 is failed"),
    ]


def test_finish_refreshes_every_check_even_when_the_path_doesnt_compile(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        unbound = told(await attempt.finish(Finish(steps=()), BY))
        await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        failing = attempt.checks
        await saved(attempt)
        refused = told(await attempt.finish(Finish(steps=(1, 2, 7)), BY))
        return [unbound, failing, refused, attempt.checks]

    assert on_app(app, tmp_path, scenario, page="/page/shop", plan=SAVED) == [
        (None, "finish compiled nothing: a1 (expect[0], text_in_target): never bound"),
        {"a1": AssertionResult("a1", "failed", locator_index=0)},
        (
            None,
            "finish compiled nothing: finish names step 7, which the attempt never took",
        ),
        {"a1": AssertionResult("a1", "pass", locator_index=0)},
    ]


def test_a_contracted_check_binds_its_part_and_finish_keeps_the_contract(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        banner, lower = favorite_buttons(attempt)
        refused = await attempt.check(AssertCheck(check_id="a1", ref=lower))
        bound_now = await attempt.check(AssertCheck(check_id="a1", ref=banner))
        script, text = told(await attempt.finish(Finish(steps=()), BY))
        targets = None if script is None else script.targets
        return [refused, bound_now, attempt.checks, text, targets]

    row = SubjectContract(
        spec="replay",
        expect=0,
        region="div.banner",
        part="app-favorite-button > button",
    )
    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/article",
        plan=plan(
            {
                "check": "text_in_target",
                "target_meaning": FAVORITE,
                "text": "Unfavorite Article (1)",
            }
        ),
        contracts={FAVORITE: BUTTON_ROW},
        config=ProjectConfig(subjects=(row,)),
    ) == [
        (
            "assert_check did not run: this check's subject binds only inside "
            "div.banner, and this element is outside it"
        ),
        "a1 is bound, and on this page it is pass",
        {"a1": AssertionResult("a1", "pass", locator_index=0)},
        COMPILED,
        {
            "t1": Target(
                semantic=FAVORITE,
                locators=(ByRole(role="button"), ByCss(css="button")),
                contract=BUTTON_ROW,
            )
        },
    ]


def test_after_a_terminal_failure_no_tool_does_any_work(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def counting(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def counted(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return function(*args, **kwargs)

        return counted

    for name in ("compile_script", "evaluate_each", "seen_element"):
        monkeypatch.setattr(
            navigator_tools, name, counting(name, getattr(navigator_tools, name))
        )
    for name in ("observing_page", "snapshot", "visible_text", "resolve"):
        monkeypatch.setattr(
            BrowserSession, name, counting(name, getattr(BrowserSession, name))
        )
    button = "the page's sign-in button"

    async def scenario(attempt: Attempt) -> dict[str, object]:
        await attempt.note(
            NoteForAbsence(check_id="a2", ref=ref(attempt, "link", "Sign in"))
        )
        await clicked(
            attempt, "Sign in now", "the page's button that signs the reader in"
        )
        calls.clear()
        await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt)))
        checked = set(calls)
        await attempt.check(AssertCheck(check_id="a2", ref=None))
        calls.clear()
        control = await attempt.finish(Finish(steps=(1,)), BY)
        finished = set(calls)
        ended = await attempt.act(
            Fill(ref=ref(attempt, "button", "Sign in now"), meaning=button, text="x")
        )
        checks = attempt.checks
        targets = [attempt.uses(meaning).targets for meaning in (STATUS, LINK)]
        calls.clear()
        after = [
            await attempt.check(AssertCheck(check_id="a1", ref=paragraph(attempt))),
            await attempt.note(
                NoteForAbsence(check_id="a2", ref=ref(attempt, "button", "Sign in now"))
            ),
            told(await attempt.finish(Finish(steps=(1,)), BY)),
        ]
        done = list(calls)
        async with attempt.session.observing_page() as page:
            # Still the link's sighting: a note of the visible button would fail.
            sighting = await attempt.uses(LINK).add_negative_check(page)
        path = ExploredPath(
            attempt.steps, [1], {"a1": targets[0][0], "a2": targets[1][0]}
        )
        return {
            "control": [checked, control.text, finished],
            "ended": ended.text,
            "after": after,
            "calls": done,
            "kept": [
                attempt.checks == checks,
                [attempt.uses(meaning).targets for meaning in (STATUS, LINK)]
                == targets,
            ],
            "unresolved": attempt.record.unresolved(),
            "sighting": sighting,
            "compiles": compile_script(
                attempt.spec, attempt.config, attempt.plan, path, BY
            ).spec_id,
        }

    assert on_app(
        app,
        tmp_path,
        scenario,
        page="/page/terminal",
        plan=plan(
            {"check": "text_in_target", "target_meaning": STATUS, "text": "Signed in"},
            {"check": "not_visible", "target_meaning": LINK},
        ),
    ) == {
        "control": [
            {
                "seen_element",
                "observing_page",
                "evaluate_each",
                "visible_text",
                "resolve",
            },
            COMPILED,
            {"evaluate_each", "visible_text", "resolve", "compile_script"},
        ],
        "ended": (
            "step 2 failed after it was dispatched, so this attempt has ended: Error: "
            "fill: the element didn't take the value: it takes no text, or its page "
            "changed the value"
        ),
        "after": [
            f"assert_check did not run: {ENDED}",
            f"note_for_absence did not run: {ENDED}",
            (None, f"finish did not run: {ENDED}"),
        ],
        "calls": [],
        "kept": [True, True],
        "unresolved": (PendingIntent(2, True),),
        "sighting": 0,
        "compiles": "replay",
    }


@dataclass(frozen=True)
class Offered:
    """An element offered for the favorites count, and why it is refused."""

    page: str
    ref: Callable[[Attempt], str]
    reason: str


@pytest.mark.parametrize(
    "offered",
    [
        Offered(
            "/page/article",
            lambda attempt: favorite_buttons(attempt)[1],
            "this check's subject binds only inside div.banner, and this element is "
            "outside it",
        ),
        Offered(
            "/page/article",
            lambda attempt: favorite_buttons(attempt)[0],
            "this check's subject is the one element matching span.counter inside "
            "div.banner, and this element is another one (describe_parts lists an "
            "element's parts)",
        ),
        Offered(
            "/page/flattened",
            lambda attempt: ref(attempt, "button", "Unfavorite Article bonus 1 (3)"),
            "this check's subject is the element matching span.counter inside "
            "div.banner, and that region has none",
        ),
        Offered(
            "/page/decoy",
            lambda attempt: refs(attempt, "- status")[0],
            "this check's subject is the one element matching span.counter inside "
            "div.banner, and this element is another one (describe_parts lists an "
            "element's parts)",
        ),
    ],
    ids=["other-copy", "favorite-button", "flattened-button", "decoy-in-banner"],
)
def test_a_contract_refusal_names_its_region_and_part_and_binds_nothing(
    app: App, tmp_path: Path, offered: Offered
) -> None:
    async def scenario(attempt: Attempt) -> list[object]:
        refused = await attempt.check(
            AssertCheck(check_id="a1", ref=offered.ref(attempt))
        )
        return [refused, attempt.checks, attempt.uses(COUNT).targets]

    assert on_app(
        app,
        tmp_path,
        scenario,
        page=offered.page,
        plan=plan({"check": "text_in_target", "target_meaning": COUNT, "text": "(1)"}),
        contracts={COUNT: COUNT_ROW},
    ) == [f"assert_check did not run: {offered.reason}", {"a1": None}, ()]
