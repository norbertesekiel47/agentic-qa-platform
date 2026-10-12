"""The navigator's action tools (#53 P13; ADR-0024's #53 P13 amendment):
each of the seven actions through the browser session, its intent on disk
before the app sees the action, its completion after, and the step kept as
compiling takes it; targets generated under each meaning's subject contract
and kept apart for actions and text checks. Real Chromium against the
executor's fixture app, on the OS that runs them."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import ByCss, ByRole, Target
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.schema import Contract
from aqa_runner import reasons, settling
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.browser_session import BrowserSession
from aqa_runner.locator_generation import (
    BindingRefusedError,
    GeneratedUse,
    Seen,
    TargetUses,
    seen_element,
)
from aqa_runner.navigator import (
    Click,
    Fill,
    FillSecret,
    Navigate,
    Prefix,
    Press,
    Reload,
    Select,
    Turn,
    navigator_request,
)
from aqa_runner.navigator_tools import Action, ActionResult, Attempt
from aqa_runner.run_record import PendingIntent
from playwright.async_api import ElementHandle, Page

from packages.runner.tests.document_fixtures import until
from packages.runner.tests.executor_fixtures import PAGES, App, read_steps, serving_app
from packages.runner.tests.navigator_fixtures import (
    NAME,
    SAVE,
    SIGN_IN,
    SIZE,
    bound,
    journal,
    on_app,
    ref,
)
from packages.runner.tests.secret_fixtures import (
    FAKE_VALUE,
    OTHER_FAKE_VALUE,
    copies_found,
)

# A password field that tells the app when it takes text.
SIGN_IN_PAGE = """<label>Password <input type="password"
    oninput="fetch('/did/fill')"></label>"""


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setitem(PAGES, "sign-in", SIGN_IN_PAGE)
    with serving_app() as served:
        yield served


def arrived(app: App, method: str, path: str) -> list[dict[str, Any]]:
    """The journal as it stood when the last such request reached the app."""
    return [lines for seen, at, lines in app.records if (seen, at) == (method, path)][
        -1
    ]


@dataclass(frozen=True)
class Case:
    """An action: the page it starts on, the call, the request the app sees
    for it, and its intent's action and target."""

    page: str
    call: Callable[[Attempt], Action]
    request: tuple[str, str]
    action: dict[str, str]
    target_used: str | None


@pytest.mark.parametrize(
    "case",
    [
        Case(
            "/page/form",
            lambda _: Navigate(path="/page/shop"),
            ("GET", "/page/shop"),
            {"action": "navigate", "url": "/page/shop"},
            None,
        ),
        Case(
            "/page/form",
            lambda _: Reload(),
            ("GET", "/page/form"),
            {"action": "reload"},
            None,
        ),
        Case(
            "/page/form",
            lambda attempt: Click(ref=ref(attempt, "button", "Save"), meaning=SAVE),
            ("POST", "/write/save"),
            {"action": "click"},
            SAVE,
        ),
        Case(
            "/page/form",
            lambda attempt: Fill(
                ref=ref(attempt, "textbox", "Name"), meaning=NAME, text="Ada"
            ),
            ("GET", "/did/fill"),
            {"action": "fill", "value": "Ada"},
            NAME,
        ),
        Case(
            "/page/sign-in",
            lambda attempt: FillSecret(
                ref=ref(attempt, "textbox", "Password"),
                meaning=SIGN_IN,
                name="TEST_PASSWORD",
            ),
            ("GET", "/did/fill"),
            {"action": "fill_secret", "secret": "TEST_PASSWORD"},
            SIGN_IN,
        ),
        Case(
            "/page/form",
            lambda attempt: Select(
                ref=ref(attempt, "combobox", "Size"), meaning=SIZE, option="M"
            ),
            ("GET", "/did/select"),
            {"action": "select", "option": "M"},
            SIZE,
        ),
        Case(
            "/page/form",
            lambda _: Press(key="Enter"),
            ("GET", "/did/press"),
            {"action": "press", "key": "Enter"},
            None,
        ),
    ],
    ids=["navigate", "reload", "click", "fill", "fill_secret", "select", "press"],
)
def test_each_action_is_on_disk_as_its_intent_before_it_reaches_the_app(
    app: App, tmp_path: Path, case: Case
) -> None:
    async def scenario(
        attempt: Attempt,
    ) -> tuple[ActionResult, list[dict[str, Any]], Any]:
        result = await attempt.act(case.call(attempt))
        return result, journal(attempt), dict(attempt.steps)

    result, lines, steps = on_app(
        app, tmp_path, scenario, page=case.page, secrets=[bound(app.origin, FAKE_VALUE)]
    )

    intent = {
        "seq": 1,
        "state": "intent",
        "action": case.action,
        # Every action of an attempt is a side-effect step until #54.
        "side_effect": True,
        "target_used": case.target_used,
    }
    assert arrived(app, *case.request)[-1] == intent
    assert lines == [
        intent,
        {"seq": 1, "state": "completed", "locator_used": None, "settled": "idle"},
    ]
    assert result.text == "step 1 ran, and the page settled"
    assert result.step is not None
    assert (result.step.seq, result.step.outcome) == (1, "completed")
    assert list(steps) == [1]
    assert steps[1].fields == case.action
    if case.target_used is None:
        assert steps[1].target is None
    else:
        assert isinstance(steps[1].target, Target)
        assert steps[1].target.semantic == case.target_used


def test_a_step_whose_requests_never_finish_reports_that_settling_timed_out(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than settling's 10 s, which test_settling measures.
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)

    async def scenario(
        attempt: Attempt,
    ) -> tuple[ActionResult, list[dict[str, Any]], list[int]]:
        click = Click(ref=ref(attempt, "button", "Wait"), meaning="the wait button")
        result = await attempt.act(click)
        return result, journal(attempt), list(attempt.steps)

    result, lines, steps = on_app(app, tmp_path, scenario, page="/page/wait")

    assert result.text == (
        "step 1 ran, and the page was still busy when settling's wait ran out"
    )
    assert lines[-1] == {
        "seq": 1,
        "state": "completed",
        "locator_used": None,
        "settled": "timeout",
    }
    assert steps == [1]


FIELD_REFUSED = (
    'fill_secret refused TEST_PASSWORD: the field isn\'t an <input type="password">'
)


async def held_value(attempt: Attempt, css: str) -> str:
    return await attempt.session.page.locator(css).input_value()


def test_a_refused_fill_secret_comes_back_as_a_tool_error_naming_the_secret(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> tuple[ActionResult, str]:
        # The binding names password fields; this one is the name field.
        call = FillSecret(
            ref=ref(attempt, "textbox", "Name"), meaning=NAME, name="TEST_PASSWORD"
        )
        result = await attempt.act(call)
        assert attempt.ended is False
        return result, await held_value(attempt, "input[name=name]")

    result, held = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/signin",
        secrets=[bound(app.origin, FAKE_VALUE)],
    )

    assert (result.step, result.text) == (
        None,
        f"fill_secret did not run: {FIELD_REFUSED}",
    )
    assert held == ""
    assert app.record is not None
    assert read_steps(app.record) == []


def test_a_refusal_inside_fill_secret_after_its_intent_ends_the_attempt(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    precheck = BrowserSession.secret_refusal

    async def then_moved(
        self: BrowserSession, element: ElementHandle, secret: BoundSecret
    ) -> None:
        await precheck(self, element, secret)
        # The page moves the field after the precheck passed.
        await element.evaluate("(field) => { field.type = 'text'; }")

    monkeypatch.setattr(BrowserSession, "secret_refusal", then_moved)

    async def scenario(attempt: Attempt) -> tuple[ActionResult, str]:
        call = FillSecret(
            ref=ref(attempt, "textbox", "Password"),
            meaning=SIGN_IN,
            name="TEST_PASSWORD",
        )
        result = await attempt.act(call)
        assert attempt.record.unresolved() == (PendingIntent(1, True),)
        return result, await held_value(attempt, "input[name=password]")

    result, held = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/signin",
        secrets=[bound(app.origin, FAKE_VALUE)],
    )

    assert result.text == (
        "step 1 failed after it was dispatched, so this attempt has ended: "
        f"{FIELD_REFUSED}"
    )
    assert held == ""


# A token field whose page copies what it holds into its button's id and
# test ID, which a stable CSS token and a test ID locator could carry.
COPYING_PAGE = """<label>API token <input type="password" oninput="
        const save = document.querySelector('button');
        save.id = this.value;
        save.dataset.testid = this.value;
    "></label><button>Save</button>"""


def test_a_locator_carrying_a_filled_secret_never_reaches_a_model_request(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "token", COPYING_PAGE)

    async def scenario(attempt: Attempt) -> tuple[Target | None, list[str]]:
        token = FillSecret(
            ref=ref(attempt, "textbox", "API token"),
            meaning="the token form's token field",
            name="TEST_PASSWORD",
        )
        assert (await attempt.act(token)).text == "step 1 ran, and the page settled"
        await attempt.observe()
        clicked = await attempt.act(
            Click(ref=ref(attempt, "button", "Save"), meaning=SAVE)
        )
        assert await attempt.session.page.locator("button").get_attribute("id") == (
            OTHER_FAKE_VALUE
        )
        turn = Turn(
            log=(), notes="", results=(clicked.text,), snapshot=await attempt.observe()
        )
        request = navigator_request(
            Prefix(spec="{}", plan="{}"), turn, redactor=attempt.session.redactor
        )
        return attempt.steps[2].target, [str(message.content) for message in request]

    target, request = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/token",
        secrets=[bound(app.origin, OTHER_FAKE_VALUE)],
    )

    # Role, then structure: the test ID and id kinds carry the value, so
    # they are dropped, and the kinds after them are still tried.
    assert target == Target(
        semantic=SAVE,
        locators=(ByRole(role="button", name="Save"), ByCss(css="button")),
    )
    assert (
        copies_found(OTHER_FAKE_VALUE, texts=[*request, target.model_dump_json()]) == []
    )


TEXT_CHECKED_PLAN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": "the form's save button",
                "claim": "says Save",
                "checks": [
                    {"check": "text_in_target", "target_meaning": SAVE, "text": "Save"}
                ],
            }
        ],
        "requires": [],
    }
)


def test_an_action_under_a_text_checked_meaning_shares_its_target_uses_and_a_later_text_check_gets_a_nameless_target(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> tuple[int, tuple[Target, ...]]:
        save = ref(attempt, "button", "Save")
        assert (await attempt.act(Click(ref=save, meaning=SAVE))).step is not None
        observation = await attempt.observe()
        uses = attempt.uses(SAVE)
        # As `assert_check` (P14) will put the same meaning's target to a
        # text check.
        seen = await seen_element(
            attempt.session, observation, ref(attempt, "button", "Save")
        )
        async with attempt.session.observing_page() as page:
            index = await uses.add(page, seen, "assertion")
        return index, uses.targets

    index, targets = on_app(app, tmp_path, scenario, plan=TEXT_CHECKED_PLAN)

    assert index == 1
    assert targets[0].locators[0] == ByRole(role="button", name="Save")
    assert targets[1] == Target(
        semantic=SAVE, locators=(ByRole(role="button"), ByCss(css="button.save"))
    )


def test_a_paraphrased_action_meaning_cannot_name_a_text_checked_assertion_target(
    app: App, tmp_path: Path
) -> None:
    paraphrase = "the save button on the form"

    async def scenario(attempt: Attempt) -> tuple[Target | None, tuple[Target, ...]]:
        save = ref(attempt, "button", "Save")
        await attempt.act(Click(ref=save, meaning=paraphrase))
        return attempt.steps[1].target, attempt.uses(SAVE).targets

    target, planned = on_app(app, tmp_path, scenario, plan=TEXT_CHECKED_PLAN)

    assert target is not None
    assert target.semantic == paraphrase
    assert planned == ()


# The favorite button rendered twice, as Conduit's article page does: in
# the banner and in the article actions (relay 60).
COPY = """<app-favorite-button><button onclick="fetch('/write/favorite', {method: 'POST'})">
    Unfavorite Article <span class="counter">(1)</span></button></app-favorite-button>"""
ARTICLE = (
    f'<div class="article-page"><div class="banner">{COPY}</div>'
    f'<div class="article-actions">{COPY}</div></div>'
)
FAVORITE = "the banner's favorite button"
BANNER = Contract(region="div.banner", part="app-favorite-button > button")


def favorite_buttons(attempt: Attempt) -> list[str]:
    """The refs of the favorite button's copies, banner first."""
    assert attempt.observation is not None
    return re.findall(
        r'- button "Unfavorite Article \(1\)" \[ref=(e\d+)\]', attempt.observation
    )


@pytest.mark.parametrize(
    ("copy", "meaning", "expected"),
    [
        (
            1,
            FAVORITE,
            (
                "click did not run: this check's subject binds only inside "
                "div.banner, and this element is outside it"
            ),
        ),
        (
            0,
            "a favorite button no check reads",
            (
                "click did not run: this element is repeated on the page, and no "
                "subject contract in the project config covers this use, so no copy "
                "can be bound; act under the target_meaning of a planned check whose "
                "contract covers it"
            ),
        ),
    ],
    ids=["outside-the-contracts-region", "unlisted-meaning"],
)
def test_a_click_on_a_copy_its_meaning_cant_bind_is_refused_by_name_and_dispatches_nothing(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    copy: int,
    meaning: str,
    expected: str,
) -> None:
    monkeypatch.setitem(PAGES, "article", ARTICLE)

    async def scenario(attempt: Attempt) -> tuple[ActionResult, tuple[Target, ...]]:
        button = favorite_buttons(attempt)[copy]
        result = await attempt.act(Click(ref=button, meaning=meaning))
        return result, attempt.uses(meaning).targets

    result, targets = on_app(
        app, tmp_path, scenario, page="/page/article", contracts={FAVORITE: BANNER}
    )

    assert (result.step, result.text) == (None, expected)
    # The verdict refused before any target was started.
    assert targets == ()
    assert app.paths() == ["/page/article"]
    assert app.record is not None
    assert read_steps(app.record) == []


def test_an_unlisted_copy_refused_for_a_check_names_no_action(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "article", ARTICLE)

    async def scenario(attempt: Attempt) -> list[str]:
        uses = attempt.uses("a favorite button no check reads")
        assert attempt.observation is not None
        seen = await seen_element(
            attempt.session, attempt.observation, favorite_buttons(attempt)[0]
        )
        page, redactor = attempt.session.page, attempt.session.redactor
        # As the check tools (P14) will put the meaning to a check.
        with pytest.raises(BindingRefusedError) as checked:
            await uses.add(page, seen, "assertion")
        with pytest.raises(BindingRefusedError) as sighted:
            await uses.see_for_negative_check(page, seen)
        return [reasons.reason(error.value, redactor) for error in (checked, sighted)]

    unlisted = (
        "this element is repeated on the page, and no subject contract in the "
        "project config covers this use, so no copy can be bound"
    )
    assert on_app(app, tmp_path, scenario, page="/page/article") == [unlisted] * 2


def test_a_click_under_a_planned_checks_meaning_inherits_its_contract(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "article", ARTICLE)

    async def scenario(attempt: Attempt) -> tuple[ActionResult, Target | None]:
        banner = favorite_buttons(attempt)[0]
        result = await attempt.act(Click(ref=banner, meaning=FAVORITE))
        return result, attempt.steps[1].target

    result, target = on_app(
        app, tmp_path, scenario, page="/page/article", contracts={FAVORITE: BANNER}
    )

    assert result.text == "step 1 ran, and the page settled"
    assert target is not None
    assert (target.semantic, target.contract) == (FAVORITE, BANNER)
    assert app.paths().count("/write/favorite") == 1


# The save button and a text field named Password, 3000 px down a page that
# tells the app whenever it is scrolled away from its top.
FAR = """<div style="height: 3000px"></div>
    <button onclick="fetch('/write/save', {method: 'POST'})">Save</button>
    <label>Password <input name="password"></label>
    <script>
        addEventListener("scroll", () => { if (scrollY > 0) fetch("/did/scrolled"); });
    </script>"""


def test_an_offscreen_target_is_scrolled_to_before_its_intent_and_acted_on_after_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "far", FAR)
    scrolls_before: list[int] = []

    def scrolls() -> int:
        return app.paths().count("/did/scrolled")

    add = TargetUses.add

    async def until_scrolled(
        self: TargetUses, page: Page, used: Seen, use: GeneratedUse
    ) -> int:
        # The barrier: generation returns, and the tool goes on to its
        # intent, only once the app has seen the scroll it made.
        index = await add(self, page, used, use)
        await until(lambda: scrolls() > scrolls_before[-1])
        return index

    monkeypatch.setattr(TargetUses, "add", until_scrolled)

    async def acted(attempt: Attempt, call: Callable[[], Action]) -> ActionResult:
        await attempt.session.page.evaluate("scrollTo(0, 0)")
        await attempt.observe()
        scrolls_before.append(scrolls())
        return await attempt.act(call())

    async def scenario(attempt: Attempt) -> tuple[list[str], int]:
        def save() -> Click:
            return Click(ref=ref(attempt, "button", "Save"), meaning=SAVE)

        def secret() -> FillSecret:
            password = ref(attempt, "textbox", "Password")
            return FillSecret(ref=password, meaning=SIGN_IN, name="TEST_PASSWORD")

        texts = [
            str((await acted(attempt, call)).text) for call in (save, save, secret)
        ]
        return texts, len(attempt.uses(SAVE).targets)

    texts, save_targets = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/far",
        secrets=[bound(app.origin, FAKE_VALUE)],
    )

    assert texts == [
        "step 1 ran, and the page settled",
        "step 2 ran, and the page settled",
        f"fill_secret did not run: {FIELD_REFUSED}",
    ]
    # The second click joined the first's target.
    assert save_targets == 1
    scrolled = [lines for _, path, lines in app.records if path == "/did/scrolled"]
    saved = [lines for _, path, lines in app.records if path == "/write/save"]
    states = [[(line["seq"], line["state"]) for line in lines] for lines in scrolled]
    first, second, third = (states[at] for at in scrolls_before)
    # Each scroll came before its step's intent; each action after it.
    assert first == []
    assert second == [(1, "intent"), (1, "completed")]
    assert third == [(1, "intent"), (1, "completed"), (2, "intent"), (2, "completed")]
    assert [lines[-1]["seq"] for lines in saved] == [1, 2]
    assert [lines[-1]["state"] for lines in saved] == ["intent", "intent"]
    assert app.record is not None
    assert [line["seq"] for line in read_steps(app.record)] == [1, 1, 2, 2]
