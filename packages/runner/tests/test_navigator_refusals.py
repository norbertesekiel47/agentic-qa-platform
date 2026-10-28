"""What the navigator's action tools refuse (#53 P13; ADR-0024's #53 P13
amendment): an attempt that can't keep its secrets, and every call refused
before its intent, which is a tool error the attempt survives, in fixed
text through `aqa_runner.reasons` with the session's redactor. Real
Chromium against the executor's fixture app, on the OS that runs them."""

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest
from aqa_core.schema import Contract
from aqa_runner.locator_generation import GeneratedUse, Seen, TargetUses
from aqa_runner.navigator import (
    Click,
    Fill,
    FillSecret,
    Navigate,
    Press,
    Reload,
    Select,
)
from aqa_runner.navigator_tools import Action, ActionResult, Attempt
from aqa_runner.redaction import Redactor
from playwright.async_api import Page

from packages.runner.tests.document_fixtures import (
    Sites,
    run_gate,
    serving_sites,
    until,
)
from packages.runner.tests.executor_fixtures import PAGES, App, read_steps, serving_app
from packages.runner.tests.navigator_fixtures import (
    NAME,
    SAVE,
    SIGN_IN,
    SIZE,
    bound,
    explored,
    journal,
    on_app,
    ref,
)
from packages.runner.tests.secret_fixtures import (
    FAKE_VALUE,
    OTHER_FAKE_VALUE,
    copies_found,
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


# What an attempt refuses to be made with, and why: each a run's admission
# should refuse first.
OTHER_REDACTOR = "an attempt's record must be scanned by its session's redactor"
UNCOVERED = "an attempt's session redactor must cover every secret it can fill"
CONTRACT = (
    "a subject contract spells a bound value, so the run's admission must refuse "
    "its config"
)
MARKER = (
    "a secret's marker spells a bound value, so no scan can remove it; the run's "
    "admission must refuse it"
)


def exactly(text: str) -> str:
    return f"^{re.escape(text)}$"


def test_an_attempt_that_cant_keep_its_secrets_is_refused_at_construction(
    app: App, tmp_path: Path
) -> None:
    token = bound(app.origin, OTHER_FAKE_VALUE, "API_TOKEN")
    meaning = "the banner's save button"

    async def scenario(attempt: Attempt) -> list[str]:
        before = app.paths()
        for change, refusal in (
            # The same secret, scanned by another redactor.
            ({"record": attempt.record.redacting(Redactor([token]))}, OTHER_REDACTOR),
            ({"secrets": {"TEST_PASSWORD": bound(app.origin, FAKE_VALUE)}}, UNCOVERED),
            (
                {
                    "contracts": {
                        meaning: Contract(region=f"div.{OTHER_FAKE_VALUE}", part="a")
                    }
                },
                CONTRACT,
            ),
            (
                {
                    "contracts": {
                        meaning: Contract(
                            region="div.banner", part=f"a.{OTHER_FAKE_VALUE}"
                        )
                    }
                },
                CONTRACT,
            ),
        ):
            with pytest.raises(ValueError, match=exactly(refusal)):
                replace(attempt, **change)
        clean = Contract(region="div.banner", part="a.save")
        assert replace(attempt, contracts={meaning: clean}).contracts == {
            meaning: clean
        }
        assert app.paths() == before
        return before

    assert on_app(app, tmp_path, scenario, secrets=[token]) == ["/page/form"]
    assert app.record is not None
    assert read_steps(app.record) == []


@pytest.mark.parametrize(
    "secrets",
    [
        [("FAKE_NAME", "FAKE_NAME")],
        [("OTHER_FAKE", "FAKE"), (FAKE_VALUE, "OTHER_FAKE")],
        [("secret", "FAKE")],
        [("password", "TEST_PASSWORD")],
    ],
    ids=["its-own-name", "another-secrets-name", "secret", "password"],
)
def test_a_secret_set_whose_markers_spell_a_value_is_refused_before_the_page_opens(
    app: App, tmp_path: Path, secrets: list[tuple[str, str]]
) -> None:
    async def unreached(_: Attempt) -> None:
        raise AssertionError("the attempt was made")

    with pytest.raises(ValueError, match=exactly(MARKER)):
        on_app(
            app,
            tmp_path,
            unreached,
            secrets=[bound(app.origin, value, name) for value, name in secrets],
        )

    assert app.paths() == []
    assert app.record is not None
    assert read_steps(app.record) == []


# Bound for the argument checks: a value with characters each encoding
# changes, one written as a secret name, and a backslash and an n, which
# JSON writes for a line feed.
NAMED_VALUE = "FAKE_NAMED_VALUE"
ESCAPED = "fake\\nvalue"
HOLDS = "holds a test secret's value"


def test_an_argument_spelling_a_bound_value_is_refused_before_its_intent(
    app: App, tmp_path: Path
) -> None:
    secrets = [
        bound(app.origin, FAKE_VALUE),
        bound(app.origin, NAMED_VALUE, "API_TOKEN"),
        bound(app.origin, ESCAPED, "OTHER_TOKEN"),
    ]

    async def scenario(attempt: Attempt) -> list[ActionResult]:
        save, name = ref(attempt, "button", "Save"), ref(attempt, "textbox", "Name")
        size = ref(attempt, "combobox", "Size")
        calls: list[Action] = [
            Navigate(path=f"/page/form?q={quote(FAKE_VALUE)}"),
            Click(ref=FAKE_VALUE, meaning=SAVE),
            Click(ref=save, meaning=f"the {FAKE_VALUE} button"),
            Fill(ref=FAKE_VALUE, meaning=NAME, text="Ada"),
            Fill(ref=name, meaning=f"the {FAKE_VALUE} field", text="Ada"),
            Fill(ref=name, meaning=NAME, text=FAKE_VALUE),
            FillSecret(ref=FAKE_VALUE, meaning=SIGN_IN, name="TEST_PASSWORD"),
            FillSecret(ref=name, meaning=f"the {FAKE_VALUE} field", name="API_TOKEN"),
            FillSecret(ref=name, meaning=SIGN_IN, name=NAMED_VALUE),
            Select(ref=FAKE_VALUE, meaning=SIZE, option="M"),
            Select(ref=size, meaning=f"the {FAKE_VALUE} list", option="M"),
            Select(ref=size, meaning=SIZE, option=FAKE_VALUE),
            Press(key=FAKE_VALUE),
            # A line feed, which the intent's JSON would write as \n.
            Fill(ref=name, meaning=NAME, text="fake\nvalue"),
            Click(ref=save, meaning="the fake\nvalue button"),
        ]
        results = [await attempt.act(call) for call in calls]
        assert attempt.ended is False
        return results

    results = on_app(app, tmp_path, scenario, secrets=secrets)

    assert [result.text for result in results] == [
        f"navigate did not run: its path {HOLDS}",
        f"click did not run: its ref {HOLDS}",
        f"click did not run: its meaning {HOLDS}",
        f"fill did not run: its ref {HOLDS}",
        f"fill did not run: its meaning {HOLDS}",
        f"fill did not run: its text {HOLDS}; fill a secret with fill_secret",
        f"fill_secret did not run: its ref {HOLDS}",
        f"fill_secret did not run: its meaning {HOLDS}",
        f"fill_secret did not run: its name {HOLDS}",
        f"select did not run: its ref {HOLDS}",
        f"select did not run: its meaning {HOLDS}",
        f"select did not run: its option {HOLDS}",
        f"press did not run: its key {HOLDS}",
        f"fill did not run: its text {HOLDS}; fill a secret with fill_secret",
        f"click did not run: its meaning {HOLDS}",
    ]
    assert [result.step for result in results] == [None] * 15
    assert app.paths() == ["/page/form"]
    assert app.record is not None
    assert read_steps(app.record) == []


def test_an_argument_the_tool_cant_take_is_refused_before_its_intent(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[ActionResult]:
        size = ref(attempt, "combobox", "Size")
        calls: list[Action] = [
            Press(key="Tab+a"),
            Select(ref=size, meaning=SIZE, option=""),
            FillSecret(
                ref=ref(attempt, "textbox", "Name"),
                meaning=SIGN_IN,
                name="OTHER_SECRET",
            ),
        ]
        return [await attempt.act(call) for call in calls]

    results = on_app(app, tmp_path, scenario, secrets=[bound(app.origin, FAKE_VALUE)])

    assert [(result.step, result.text) for result in results] == [
        (
            None,
            (
                "press did not run: it takes one key, with only modifiers held before "
                "it, such as Enter or Shift+a"
            ),
        ),
        (
            None,
            "select did not run: its option is empty; choose an option by its label",
        ),
        (
            None,
            (
                "fill_secret did not run: no test secret named OTHER_SECRET is bound "
                "for this run"
            ),
        ),
    ]
    assert app.paths() == ["/page/form"]
    assert app.record is not None
    assert read_steps(app.record) == []


# A form whose field, list and button each have a two-word name.
LABELLED = """<label>Display name <input oninput="fetch('/did/fill')"></label>
    <label>Shirt size <select onchange="fetch('/did/select')">
        <option>S</option><option>M</option></select></label>
    <button onclick="fetch('/write/save', {method: 'POST'})">Save draft</button>"""
NAMES_LABEL = (
    "did not run: its meaning holds the element's label; say what the element is "
    "for and where it sits instead"
)


def test_a_meaning_holding_a_multi_word_name_is_refused_before_its_intent(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(PAGES, "labelled", LABELLED)

    async def scenario(attempt: Attempt) -> list[ActionResult]:
        calls: list[Action] = [
            Click(
                ref=ref(attempt, "button", "Save draft"),
                meaning="the form's save draft button",
            ),
            Fill(
                ref=ref(attempt, "textbox", "Display name"),
                meaning="the form's DISPLAY  NAME field",
                text="Ada",
            ),
            Select(
                ref=ref(attempt, "combobox", "Shirt size"),
                meaning="the form's shirt\nsize list",
                option="M",
            ),
        ]
        return [await attempt.act(call) for call in calls]

    results = on_app(app, tmp_path, scenario, page="/page/labelled")

    assert [(result.step, result.text) for result in results] == [
        (None, f"{tool} {NAMES_LABEL}") for tool in ("click", "fill", "select")
    ]
    assert app.paths() == ["/page/labelled"]
    assert app.record is not None
    assert read_steps(app.record) == []


def test_a_meaning_holding_a_one_word_name_runs(app: App, tmp_path: Path) -> None:
    async def scenario(attempt: Attempt) -> ActionResult:
        save = ref(attempt, "button", "Save")
        return await attempt.act(Click(ref=save, meaning="the form's Save button"))

    result = on_app(app, tmp_path, scenario)

    assert result.text == "step 1 ran, and the page settled"
    assert app.paths() == ["/page/form", "/write/save"]


@pytest.mark.parametrize(
    ("extra", "ran"),
    [
        ('steps:\n  - Press "save  DRAFT"\n', True),
        ('  - The "Save draft" button is enabled\n', True),
        # The spec quotes more, or less, than the name, so it doesn't quote
        # the name.
        ('steps:\n  - Press "Save draft now"\n', False),
        ('steps:\n  - Press "Save"\n', False),
        ('steps:\n  - Press ""\n', False),
    ],
    ids=[
        "in-a-step",
        "in-an-expectation",
        "inside-a-longer-quote",
        "a-shorter-quote",
        "an-empty-quote",
    ],
)
def test_a_meaning_holding_a_name_the_spec_quotes_runs(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str, ran: bool
) -> None:
    monkeypatch.setitem(PAGES, "labelled", LABELLED)

    async def scenario(attempt: Attempt) -> ActionResult:
        button = ref(attempt, "button", "Save draft")
        return await attempt.act(Click(ref=button, meaning="the Save draft button"))

    result = on_app(app, tmp_path, scenario, page="/page/labelled", extra=extra)

    if ran:
        assert result.text == "step 1 ran, and the page settled"
        assert app.paths() == ["/page/labelled", "/write/save"]
    else:
        assert result.text == f"click {NAMES_LABEL}"
        assert app.paths() == ["/page/labelled"]


def test_a_fill_secret_whose_meaning_holds_a_multi_word_name_runs(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0025's #234 amendment refuses labels in click, fill and select.
    monkeypatch.setitem(
        PAGES, "account", '<label>Account password <input type="password"></label>'
    )

    async def scenario(attempt: Attempt) -> ActionResult:
        field = ref(attempt, "textbox", "Account password")
        return await attempt.act(
            FillSecret(
                ref=field, meaning="the Account password field", name="TEST_PASSWORD"
            )
        )

    result = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/account",
        secrets=[bound(app.origin, FAKE_VALUE)],
    )

    assert result.text == "step 1 ran, and the page settled"


@pytest.mark.parametrize(
    "meaning",
    ["the autosave drafts button", "the save-draft button"],
    ids=["inside-longer-words", "hyphenated"],
)
def test_a_meaning_holding_a_name_only_as_part_of_other_words_runs(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meaning: str
) -> None:
    monkeypatch.setitem(PAGES, "labelled", LABELLED)

    async def scenario(attempt: Attempt) -> ActionResult:
        button = ref(attempt, "button", "Save draft")
        return await attempt.act(Click(ref=button, meaning=meaning))

    result = on_app(app, tmp_path, scenario, page="/page/labelled")

    assert result.text == "step 1 ran, and the page settled"
    assert app.paths() == ["/page/labelled", "/write/save"]


@pytest.mark.parametrize("secret_bound", [False, True], ids=["unbound", "bound"])
def test_a_tool_on_a_page_off_the_allowed_origins_returns_a_policy_error(
    app: App, tmp_path: Path, secret_bound: bool
) -> None:
    async def scenario(attempt: Attempt) -> tuple[list[str], list[str], ActionResult]:
        page = attempt.session.page
        # The page leaves for a host the run doesn't allow, which lands on
        # Chromium's error page, without any step.
        await until(lambda: not page.url.startswith(app.origin))
        await page.wait_for_load_state()
        calls: list[Action] = [
            Click(ref="e1", meaning=SAVE),
            Fill(ref="e1", meaning=NAME, text="Ada"),
            Select(ref="e1", meaning=SIZE, option="M"),
            Reload(),
            Press(key="Enter"),
        ]
        if secret_bound:
            calls.append(FillSecret(ref="e1", meaning=SIGN_IN, name="TEST_PASSWORD"))
        texts = [str((await attempt.act(call)).text) for call in calls]
        lines = [str(line) for line in journal(attempt)]
        return texts, lines, await attempt.act(Navigate(path="/page/shop"))

    texts, lines, back = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/leaves",
        secrets=[bound(app.origin, FAKE_VALUE)] if secret_bound else [],
    )

    refusal = (
        "the page is on an origin the run doesn't allow (withheld: the run has a "
        "test secret)"
        if secret_bound
        else "the page is on no origin a run could allow: nothing there is observed "
        "or acted on; navigate to an allowed origin, or restart"
    )
    tools = ["click", "fill", "select", "reload", "press"]
    tools += ["fill_secret"] if secret_bound else []
    assert texts == [f"{tool} did not run: {refusal}" for tool in tools]
    assert lines == []
    # The page's egress block stopped the run, so navigating doesn't run either.
    assert back.text == (
        "navigate did not run: the run stopped: the page reached a host the run "
        "doesn't allow, or an allowed host couldn't be reached"
    )


def test_a_page_that_changes_while_the_target_is_generated_is_a_tool_error(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add = TargetUses.add

    async def moved(self: TargetUses, page: Page, used: Seen, use: GeneratedUse) -> int:
        index = await add(self, page, used, use)
        async with page.expect_event("framenavigated"):
            await page.evaluate("location.hash = 'moved'")
        return index

    monkeypatch.setattr(TargetUses, "add", moved)

    async def scenario(attempt: Attempt) -> ActionResult:
        return await attempt.act(
            Click(ref=ref(attempt, "button", "Save"), meaning=SAVE)
        )

    result = on_app(app, tmp_path, scenario)

    assert result.text == (
        "click did not run: the page changed while the session looked at it, so what "
        "it saw was discarded: take a new snapshot, or look the target up again"
    )
    assert result.step is None
    assert app.paths() == ["/page/form"]
    assert app.record is not None
    assert read_steps(app.record) == []


def test_a_pre_intent_refusal_is_a_tool_error_the_attempt_survives(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> tuple[str, ActionResult, ActionResult]:
        stale = ref(attempt, "button", "Save")
        await attempt.observe()
        refused = await attempt.act(Click(ref=stale, meaning=SAVE))
        done = await attempt.act(
            Click(ref=ref(attempt, "button", "Save"), meaning=SAVE)
        )
        return stale, refused, done

    stale, refused, done = on_app(app, tmp_path, scenario)

    assert refused.text == (
        f"click did not run: '{stale}' is from an older snapshot: a ref acts only on "
        "the snapshot that gave it, so take a new snapshot"
    )
    assert refused.step is None
    assert done.text == "step 1 ran, and the page settled"
    assert app.paths() == ["/page/form", "/write/save"]


def test_an_action_whose_serialized_intent_spells_a_bound_value_is_refused_before_its_intent_and_dispatch(
    app: App, tmp_path: Path
) -> None:
    # Spelled only by the intent line's own JSON around the fill's text.
    wrapped = bound(app.origin, '"value": "Ada"')

    async def scenario(attempt: Attempt) -> ActionResult:
        name = ref(attempt, "textbox", "Name")
        result = await attempt.act(Fill(ref=name, meaning=NAME, text="Ada"))
        assert attempt.ended is False
        return result

    result = on_app(app, tmp_path, scenario, secrets=[wrapped])

    assert result.text == (
        "fill did not run: steps.jsonl: a bound value's spelling is in it, so it "
        "isn't written"
    )
    assert result.step is None
    assert app.paths() == ["/page/form"]
    assert app.record is not None
    assert read_steps(app.record) == []


# A button a page names with a custom tag, which a cover keeps from the
# pointer, so no locator finds it for an action.
COVERED = """<x-fake-tag-for-53 role="button" tabindex="0">Save</x-fake-tag-for-53>
    <div style="position: fixed; inset: 0; background: white"></div>"""


@pytest.mark.parametrize("secret_bound", [False, True], ids=["unbound", "bound"])
def test_a_locator_error_naming_a_page_chosen_tag_is_fixed_text_in_a_bound_run(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_bound: bool
) -> None:
    monkeypatch.setitem(PAGES, "covered", COVERED)

    async def scenario(attempt: Attempt) -> ActionResult:
        button = ref(attempt, "button", "Save")
        return await attempt.act(Click(ref=button, meaning="the covered save button"))

    result = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/covered",
        # A secret the page never sees.
        secrets=[bound(app.origin, FAKE_VALUE)] if secret_bound else [],
    )

    assert result.text == (
        "click did not run: no locator could be generated for this element "
        "(withheld: the run has a test secret)"
        if secret_bound
        else "click did not run: the covered save button: no locator of the grammar "
        "finds the x-fake-tag-for-53 element alone for an action"
    )
    assert result.step is None


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    with serving_sites(monkeypatch) as served:
        yield served


def test_every_bound_value_refusal_is_fixed_text(sites: Sites, tmp_path: Path) -> None:
    secrets = [
        bound(sites.app, FAKE_VALUE),
        # Spelled only by an intent line's own JSON.
        bound(sites.app, '"side_effect": true', "OTHER_TOKEN"),
    ]

    async def fill_secret(attempt: Attempt, url: str, label: str) -> str:
        await attempt.session.navigate(url)
        await attempt.observe()
        call = FillSecret(
            ref=ref(attempt, "textbox", label), meaning=SIGN_IN, name="TEST_PASSWORD"
        )
        return str((await attempt.act(call)).text)

    async def scenario(attempt: Attempt) -> tuple[list[str], list[str]]:
        texts = [
            await fill_secret(attempt, f"{sites.other}/signin", "Password"),
            await fill_secret(attempt, f"{sites.app}/ugc", "Password"),
            await fill_secret(attempt, f"{sites.app}/signin", "Email"),
            await fill_secret(attempt, f"{sites.app}/signin", "Password"),
            # Last: a check on the page after this one can meet one of its
            # frames detached (a follow-up for the session).
            await fill_secret(attempt, f"{sites.app}/framed", "Password"),
        ]
        assert attempt.ended is False
        return texts, [str(line) for line in journal(attempt)]

    texts, lines = explored(
        tmp_path,
        scenario,
        sites.app,
        page="/signin",
        egress=run_gate(sites),
        secrets=secrets,
    )

    refused = "fill_secret did not run: fill_secret refused TEST_PASSWORD"
    assert texts == [
        f"{refused}: the page is on none of its destinations",
        f"{refused}: the page's document isn't on its URL's origin",
        f'{refused}: the field isn\'t an <input type="password">',
        (
            "fill_secret did not run: steps.jsonl: a bound value's spelling is in "
            "it, so it isn't written"
        ),
        # A field in a frame has no locator on the page, so generation
        # refuses it before its binding is checked.
        (
            "fill_secret did not run: no locator could be generated for this "
            "element (withheld: the run has a test secret)"
        ),
    ]
    assert copies_found(FAKE_VALUE, texts=texts) == []
    assert lines == []


def test_a_tool_error_from_a_page_that_throws_the_secret_back_holds_no_copy(
    app: App, tmp_path: Path
) -> None:
    async def scenario(attempt: Attempt) -> list[str]:
        password = ref(attempt, "textbox", "Password")
        filled = await attempt.act(
            FillSecret(ref=password, meaning=SIGN_IN, name="TEST_PASSWORD")
        )
        await attempt.observe()
        # Once filled, the field throws its value back from the checks a
        # later action on it makes.
        retyped = await attempt.act(
            Fill(ref=ref(attempt, "textbox", "Password"), meaning=SIGN_IN, text="Ada")
        )
        return [str(filled.text), str(retyped.text)]

    texts = on_app(
        app,
        tmp_path,
        scenario,
        page="/page/signin-rethrows",
        secrets=[bound(app.origin, FAKE_VALUE)],
    )

    assert texts == [
        "step 1 ran, and the page settled",
        (
            "step 2 failed after it was dispatched, so this attempt has ended: "
            "Error: ElementHandle.evaluate: the rest is withheld, since the page "
            "was handed a test secret"
        ),
    ]
    assert copies_found(FAKE_VALUE, texts=texts) == []
