"""`replays.replay_run(..., repair=...)` (#242; ADR-0024's #242 amendment): a
step whose target drifted goes to the caller's repair, which offers the whole
script to go on with; replay checks it against the committed script, records
it and runs the step again, or records the refusal and ends the run as drift.
Real Chromium against #240's drift pages, with every locator, as heal runs."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from aqa_core.compiled import Click, CompiledScript, Fill, Press, Target
from aqa_core.project import load_config, load_spec
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.evaluation import AssertionResult
from aqa_runner.executor import RunResult
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.locators import Miss
from aqa_runner.redaction import NO_SECRETS, Redacted
from aqa_runner.repairs import Basis, Drift, ModelReported, Offer, StepAt
from aqa_runner.replays import Opened, Replaying, replay_run
from aqa_runner.run_record import RefusedWriteError, RunRecord
from playwright.async_api import async_playwright

from packages.runner.tests import executor_fixtures as fx
from packages.runner.tests.drift_fixtures import (
    DETAILS,
    SHIPPING,
    TOGGLE,
    order_spec,
    page,
    serving,
)
from packages.runner.tests.egress_fixtures import gate
from packages.runner.tests.secret_fixtures import (
    BOUND_AT_START,
    FAKE_VALUE,
    copies_found,
    hermetic_environment,
)
from packages.runner.tests.test_every_locator import around

MOVED = page("moved")
# How the toggle drifts on the moved page: its role locator still finds it,
# its toolbar-scoped css locator finds nothing.
MISSES: tuple[Miss, ...] = ("not unanimous", "no match")
# The toggle bound where the moved page has it, by locators that agree.
MOVED_TOGGLE = {
    **TOGGLE,
    "locators": [
        fx.by_role("button", "Show details"),
        {"css": "button", "scope": {"css": "#order"}},
    ],
}


@dataclass(frozen=True)
class Repairing:
    """A replay with a repair: its result, its record's directory, each drift
    the repair was handed, the committed script with its dump from before the
    run, and each request the app saw."""

    result: RunResult
    record: Path
    handed: list[Drift]
    committed: CompiledScript
    before: dict[str, Any]
    seen: list[tuple[str, str]]


def shipping_spec(root: Path) -> Path:
    return order_spec(root, SHIPPING)


def secret_spec_at(root: Path) -> Path:
    """The order spec, whose account's password is the test secret
    TEST_PASSWORD, which its config binds at the start origin."""
    path = order_spec(root, SHIPPING)
    (root / "config.yaml").write_text(
        f"budgets: {{ resolve_seconds: 1 }}\nsecrets: {{ {BOUND_AT_START} }}\n"
    )
    account = "{ email: reader@example.test, password: { secret: TEST_PASSWORD } }"
    path.write_text(
        path.read_text().replace(
            "preconditions:\n", f"preconditions:\n  account: {account}\n"
        )
    )
    return path


def repairing(
    tmp_path: Path,
    offer: Callable[[Drift], CompiledScript | Offer | Awaitable[CompiledScript]],
    *,
    script: CompiledScript = DETAILS,
    spec_at: Callable[[Path], Path] = shipping_spec,
    html: str = MOVED,
) -> Repairing:
    """`script` replayed with every locator on `html`, the moved page unless
    a test composes another, for the spec `spec_at` writes under a spec root,
    with a repair that notes each drift and offers what `offer` gives for it,
    awaited when it is a look at the page first."""
    path = spec_at(tmp_path / "qa")
    config = load_config(path.parent / "config.yaml")
    spec = load_spec(path, config)
    # Deep, so a repair that edits in place can't reach the shared fixture.
    committed = fx.for_spec(script, spec).model_copy(deep=True)
    before = committed.model_dump(mode="json")
    record = RunRecord.create(path.parent)
    handed: list[Drift] = []

    async def repair(drift: Drift) -> Offer:
        handed.append(drift)
        offered = offer(drift)
        script = await offered if isinstance(offered, Awaitable) else offered
        return script if isinstance(script, Offer) else Offer(script)

    with serving(html) as app:
        run_gate = gate(allowed=(app.origin,))

        @asynccontextmanager
        async def opening() -> AsyncIterator[Opened]:
            async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
                await register_identity_engine(playwright)
                yield Opened(playwright.chromium, proxy, record)

        async def scenario() -> RunResult:
            replaying = Replaying(spec, config, app.origin, run_gate)
            # No replay test may hang the suite.
            async with asyncio.timeout(60):
                return await replay_run(
                    committed, replaying, opening, every_locator=True, repair=repair
                )

        result = asyncio.run(scenario())
    return Repairing(result, record.path, handed, committed, before, app.seen)


def rebound(script: CompiledScript, **toggle: Any) -> CompiledScript:
    """`script` with its toggle bound as the moved page has it, with
    `toggle`'s fields over that binding's."""
    moved = Target.model_validate({**MOVED_TOGGLE, **toggle})
    return script.model_copy(update={"targets": {**script.targets, "toggle": moved}})


def outcomes(run: Repairing) -> list[tuple[object, ...]]:
    return [(step.seq, step.outcome, step.misses) for step in run.result.steps]


def states(run: Repairing) -> list[tuple[object, ...]]:
    return [(line["seq"], line["state"]) for line in fx.read_steps(run.record)]


def test_a_repaired_step_runs_again_and_every_assertion_is_evaluated(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, lambda drift: rebound(drift.script))

    assert [(drift.at.key, drift.misses) for drift in run.handed] == [("1", MISSES)]
    assert outcomes(run) == [(0, "completed", ()), (1, "completed", ())]
    assert run.result.assertions == (
        AssertionResult("a1", "pass", locator_index=0),
        AssertionResult("a2", "pass", locator_index=0),
    )
    assert run.result.outcome == "passed"
    assert states(run) == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
        (1, "completed"),
    ]


def document(run: Repairing, key: int | str) -> object:
    return json.loads((run.record / f"repairs/{key}.json").read_text())


def repairs(run: Repairing) -> list[tuple[object, ...]]:
    return [
        (repaired.at.key, repaired.misses, repaired.problems, repaired.script)
        for repaired in run.result.repairs
    ]


def test_a_taken_repairs_account_is_kept_and_recorded_beside_its_script(
    tmp_path: Path,
) -> None:
    def offer(drift: Drift) -> Offer:
        said = drift.session.redactor.redact
        reported = ModelReported(0.9, said("it moved to the order"))
        return Offer(rebound(drift.script), Basis(said('button "Show"'), reported))

    run = repairing(tmp_path, offer)

    [repaired] = run.result.repairs
    assert repaired.basis == Basis(
        NO_SECRETS.redact('button "Show"'),
        ModelReported(0.9, NO_SECRETS.redact("it moved to the order")),
    )
    patched = rebound(run.committed)
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [],
        "script": patched.model_dump(mode="json", exclude_none=True),
        "basis": {
            "said": 'button "Show"',
            "model_reported": {
                "confidence": 0.9,
                "rationale": "it moved to the order",
            },
            "stop": None,
        },
    }


def test_a_repair_that_gives_up_says_why_and_any_run_stop_it_hit(
    tmp_path: Path,
) -> None:
    def offer(drift: Drift) -> Offer:
        said = drift.session.redactor.redact("the model gave no response")
        return Offer(drift.script, Basis(said, stop="no_response"))

    run = repairing(tmp_path, offer)

    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert repairs(run) == [("1", MISSES, (), None)]
    assert run.result.repairs[0].basis == Basis(
        NO_SECRETS.redact("the model gave no response"), stop="no_response"
    )
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [],
        "script": None,
        "basis": {
            "said": "the model gave no response",
            "model_reported": None,
            "stop": "no_response",
        },
    }


def test_a_refused_repairs_account_is_kept_and_recorded(tmp_path: Path) -> None:
    def offer(drift: Drift) -> Offer:
        said = drift.session.redactor.redact("a bound element")
        return Offer(rebound(drift.script, semantic="another meaning"), Basis(said))

    run = repairing(tmp_path, offer)

    [repaired] = run.result.repairs
    assert repaired.problems == (
        "/targets/toggle/semantic: a heal never changes a target's meaning",
    )
    assert repaired.basis == Basis(NO_SECRETS.redact("a bound element"))
    assert cast(dict[str, Any], document(run, 1))["basis"] == {
        "said": "a bound element",
        "model_reported": None,
        "stop": None,
    }


def test_an_account_that_spells_a_value_is_not_kept_with_its_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record refused what the repair said, so its refusal keeps none of it."""
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)

    def offer(drift: Drift) -> Offer:
        # Not through its scan: a repair that breaks the rule it is held to.
        spelled = cast(Redacted, FAKE_VALUE)
        return Offer(rebound(drift.script), Basis(spelled))

    run = repairing(tmp_path, offer, spec_at=secret_spec_at)

    [repaired] = run.result.repairs
    assert repaired.problems == (
        "repairs/1.json: a bound value's spelling is in it, so it isn't written",
    )
    assert repaired.basis is None
    assert copies_found(FAKE_VALUE, files=[run.record / "repairs/1.json"]) == []


def test_a_repair_reads_whether_the_run_was_interrupted(tmp_path: Path) -> None:
    seen: list[bool] = []

    def offer(drift: Drift) -> CompiledScript:
        seen.append(drift.interrupted())
        return drift.script

    repairing(tmp_path, offer)

    assert seen == [False]


def test_the_patched_script_is_recorded_and_the_committed_one_is_unchanged(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, lambda drift: rebound(drift.script))

    patched = rebound(run.committed)
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [],
        "script": patched.model_dump(mode="json", exclude_none=True),
    }
    assert repairs(run) == [("1", MISSES, (), patched)]
    assert run.committed.model_dump(mode="json") == run.before


def unlocated(script: CompiledScript) -> CompiledScript:
    """`script` with its toggle found by no locator, which only the loader
    refuses: `model_copy` checks nothing."""
    toggle = script.targets["toggle"].model_copy(update={"locators": ()})
    return script.model_copy(update={"targets": {**script.targets, "toggle": toggle}})


def pressing_two_keys(script: CompiledScript) -> CompiledScript:
    """`script`, rebound, with a replay-safe `press` of two keys inserted
    first, which only replay's admission refuses."""
    press = Press(seq=2, action="press", key="a+b", side_effect=False)
    moved = rebound(script)
    return moved.model_copy(update={"steps": (press, *moved.steps)})


@pytest.mark.parametrize(
    ("offer", "problem"),
    [
        pytest.param(
            lambda drift: rebound(drift.script, semantic="the order's details"),
            "/targets/toggle/semantic: a heal never changes a target's meaning",
            id="validator",
        ),
        pytest.param(
            lambda drift: unlocated(drift.script),
            "repairs/1.json: targets.toggle.locators: must list at least 1 item",
            id="loader",
        ),
        pytest.param(
            lambda drift: pressing_two_keys(drift.script),
            "steps[0] (seq 2): press takes one key, with only modifiers held "
            "before it: 'a+b'",
            id="admission",
        ),
    ],
)
def test_a_refused_repair_ends_the_run_as_drift_with_its_problems_recorded(
    tmp_path: Path, offer: Callable[[Drift], CompiledScript], problem: str
) -> None:
    run = repairing(tmp_path, offer)

    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert run.result.assertions == (
        AssertionResult("a1", "not_evaluated", stopped_at=1),
        AssertionResult("a2", "not_evaluated", stopped_at=1),
    )
    assert run.result.outcome == "failed"
    assert states(run) == [(0, "intent"), (0, "completed")]
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [problem],
        "script": None,
    }
    assert repairs(run) == [("1", MISSES, (problem,), None)]


def adding_a_target(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, with a target added in place: a
    frozen model's `targets` is still a plain dict."""
    drift.script.targets["extra"] = drift.script.targets["toggle"]
    return drift.script


def test_a_repair_that_edits_the_script_it_was_handed_is_checked_against_the_committed_one(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, adding_a_target)

    problem = "/targets/extra: a heal adds a target only for a click it inserts"
    assert repairs(run) == [("1", MISSES, (problem,), None)]
    assert run.committed.model_dump(mode="json") == run.before


def rewriting_the_step_in_place(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, holding the step it handed
    over with its fields written in place: a frozen model's storage is
    still writable."""
    assert isinstance(drift.at, StepAt)
    vars(drift.at.step).update(
        target="summary", side_effect=True, side_effect_basis="network: POST /order"
    )
    return rebound(drift.script).model_copy(update={"steps": (drift.at.step,)})


def test_a_repair_that_rewrites_the_step_it_was_handed_in_place_is_refused(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, rewriting_the_step_in_place)

    problems = (
        "/steps/0/side_effect: a heal rewrites no existing step",
        "/steps/0/side_effect_basis: a heal rewrites no existing step",
        "/steps/0/target: a heal rewrites no existing step",
    )
    assert repairs(run) == [("1", MISSES, problems, None)]
    assert run.committed.model_dump(mode="json") == run.before


def retiming_the_session(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with the browser settings
    the live session holds, their time zone written in place."""
    vars(drift.session.settings)["timezone"] = "Asia/Tokyo"
    return rebound(drift.script).model_copy(update={"browser": drift.session.settings})


def test_a_repair_is_checked_against_the_script_as_replay_was_given_it(
    tmp_path: Path,
) -> None:
    # The session holds the settings the run opened with, the committed
    # script's own: what the repair writes there reaches no check.
    run = repairing(tmp_path, retiming_the_session)

    problem = "/browser: a heal changes nothing but target locators and inserted steps"
    assert repairs(run) == [("1", MISSES, (problem,), None)]


def test_a_step_that_drifts_again_after_its_repair_stops_the_run(
    tmp_path: Path,
) -> None:
    # A rebinding whose css locator still finds nothing on the moved page,
    # so the step drifts again, as it did.
    still_missing = [
        fx.by_role("button", "Show details"),
        {"css": "button.details", "scope": {"css": "#order"}},
    ]
    run = repairing(
        tmp_path, lambda drift: rebound(drift.script, locators=still_missing)
    )

    assert [drift.at.key for drift in run.handed] == ["1"]
    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert run.result.assertions == (
        AssertionResult("a1", "not_evaluated", stopped_at=1),
        AssertionResult("a2", "not_evaluated", stopped_at=1),
    )


def test_a_repair_that_offers_the_script_it_was_handed_gives_up(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, lambda drift: drift.script)

    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert repairs(run) == [("1", MISSES, (), None)]
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [],
        "script": None,
    }


# DETAILS, then a click on the footer's contact button, found by a class the
# button doesn't have, so it drifts too.
CONTACT = {"semantic": "the contact button", "locators": [{"css": "button.contact"}]}
TWO_CLICKS = DETAILS.model_copy(
    update={
        "targets": {**DETAILS.targets, "contact": Target.model_validate(CONTACT)},
        "steps": (
            *DETAILS.steps,
            Click(seq=2, action="click", target="contact", side_effect=False),
        ),
    }
)


def rebinding_each(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, with the drifted step's target bound
    as the moved page has it."""
    if drift.at.key == "1":
        return rebound(drift.script)
    footer = {**CONTACT, "locators": [{"css": "button", "scope": {"css": "footer"}}]}
    targets = {**drift.script.targets, "contact": Target.model_validate(footer)}
    return drift.script.model_copy(update={"targets": targets})


def test_a_later_drift_gets_its_own_repair_handed_the_script_in_use(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, rebinding_each, script=TWO_CLICKS)

    assert [(drift.at.key, drift.misses) for drift in run.handed] == [
        ("1", MISSES),
        ("2", ("no match",)),
    ]
    assert run.handed[1].script.targets["toggle"] == Target.model_validate(MOVED_TOGGLE)
    assert outcomes(run) == [
        (0, "completed", ()),
        (1, "completed", ()),
        (2, "completed", ()),
    ]
    assert [(r.at.key, r.problems) for r in run.result.repairs] == [
        ("1", ()),
        ("2", ()),
    ]
    assert run.result.outcome == "passed"


def rewriting_an_inserted_step_that_ran(drift: Drift) -> CompiledScript:
    """For the toggle, the script `drift` handed over, rebound, with a Tab
    pressed first; for the contact button, rebound too, with that press,
    which ran, made an Enter."""
    rebinding = rebinding_each(drift)
    key = "Tab" if drift.at.key == "1" else "Enter"
    press = Press(seq=3, action="press", key=key, side_effect=False)
    kept = rebinding.steps if drift.at.key == "1" else rebinding.steps[1:]
    return rebinding.model_copy(update={"steps": (press, *kept)})


def test_a_later_repair_that_rewrites_an_inserted_step_that_ran_is_refused(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, rewriting_an_inserted_step_that_ran, script=TWO_CLICKS)

    problem = "/steps/0: a repair rewrites no step that already ran"
    assert [(r.at.key, r.problems) for r in run.result.repairs] == [
        ("1", ()),
        ("2", (problem,)),
    ]
    assert outcomes(run) == [
        (0, "completed", ()),
        (3, "completed", ()),
        (1, "completed", ()),
        (2, "drifted", ("no match",)),
    ]


def seen_on(target: str, check_id: str) -> dict[str, object]:
    """A `visible_unoccluded` check of `target`, with the id `check_id`."""
    return {
        "id": check_id,
        "expect_index": 0,
        "check": "visible_unoccluded",
        "target": target,
        "min_size_px": [1, 1],
        "in_viewport": True,
    }


# The order page, whose toggle renames itself once it is clicked.
RENAMES_ITSELF = page("order").replace(
    "document.querySelector('#details').hidden = false",
    "this.textContent = 'Hide details'",
)
CLICK_TOGGLE = {"seq": 1, "action": "click", "target": "toggle", "side_effect": False}
# The toggle clicked, then seen under the name it no longer has.
CLICKED_THEN_SEEN = fx.compiled(
    [CLICK_TOGGLE],
    targets={"toggle": TOGGLE},
    assertions=[seen_on("toggle", "a1")],
)
# The toggle clicked twice: the second click looks for that name too.
CLICKED_TWICE = fx.compiled(
    [CLICK_TOGGLE, {**CLICK_TOGGLE, "seq": 2}], targets={"toggle": TOGGLE}
)
RENAMED: tuple[Miss, ...] = ("no match", "not unanimous")
# A select, then a password field, each renamed by the step that acts through
# it, and seen under the name it no longer has.
RENAMED_BY_ITS_SELECT = """<select aria-label="Size"
    onchange="this.setAttribute('aria-label', 'Chosen')">
    <option>S</option><option>M</option>
</select>"""
SELECTED_THEN_SEEN = fx.compiled(
    [
        {
            "seq": 1,
            "action": "select",
            "target": "size",
            "option": "M",
            "side_effect": False,
        }
    ],
    targets={
        "size": {
            "semantic": "the order's size",
            "locators": [fx.by_role("combobox", "Size"), {"css": "select"}],
        }
    },
    assertions=[seen_on("size", "a1")],
)
RENAMED_BY_ITS_FILL = """<input type="password" aria-label="Password"
    oninput="this.setAttribute('aria-label', 'Filled')">"""
FILLED_THEN_SEEN = fx.compiled(
    [
        {
            "seq": 1,
            "action": "fill_secret",
            "target": "field",
            "secret": "TEST_PASSWORD",
            "side_effect": False,
        }
    ],
    targets={
        "field": {
            "semantic": "the account's password field",
            "locators": [{"label": "Password"}, {"css": "input"}],
        }
    },
    assertions=[seen_on("field", "a1")],
)


@dataclass(frozen=True)
class UsedThenDrifted:
    """A script with a step that acts through the target `name`, which then
    drifts on `html`, and the record `key`, which names that drift's `subject`."""

    script: CompiledScript
    html: str
    name: str
    key: str
    subject: dict[str, object]
    spec_at: Callable[[Path], Path] = shipping_spec


@pytest.mark.parametrize(
    "used",
    [
        pytest.param(
            UsedThenDrifted(
                CLICKED_THEN_SEEN,
                RENAMES_ITSELF,
                "toggle",
                "assertion-0",
                {"assertion": "a1"},
            ),
            id="assertion",
        ),
        pytest.param(
            UsedThenDrifted(CLICKED_TWICE, RENAMES_ITSELF, "toggle", "2", {"seq": 2}),
            id="step",
        ),
        pytest.param(
            UsedThenDrifted(
                SELECTED_THEN_SEEN,
                RENAMED_BY_ITS_SELECT,
                "size",
                "assertion-0",
                {"assertion": "a1"},
            ),
            id="after_a_select",
        ),
        pytest.param(
            UsedThenDrifted(
                FILLED_THEN_SEEN,
                RENAMED_BY_ITS_FILL,
                "field",
                "assertion-0",
                {"assertion": "a1"},
                secret_spec_at,
            ),
            id="after_a_fill_secret",
        ),
    ],
)
def test_a_drift_on_a_target_a_step_already_used_goes_to_no_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, used: UsedThenDrifted
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)

    run = repairing(
        tmp_path,
        lambda drift: rebound(drift.script),
        script=used.script,
        spec_at=used.spec_at,
        html=used.html,
    )

    problem = (
        f"/targets/{used.name}: a step that already ran acted through this target, "
        "so it goes to no repair"
    )
    assert run.handed == []
    assert repairs(run) == [(used.key, RENAMED, (problem,), None)]
    assert document(run, used.key) == {
        **used.subject,
        "misses": ["no match", "not unanimous"],
        "problems": [problem],
        "script": None,
    }


# The toggle clicked, then the footer's contact button, which drifts, seen.
CLICKED_THEN_CONTACT_SEEN = fx.compiled(
    [CLICK_TOGGLE],
    targets={"toggle": TOGGLE, "contact": CONTACT},
    assertions=[seen_on("contact", "a1")],
)


def rebinding_the_used_toggle_too(drift: Drift) -> CompiledScript:
    """For the toggle, the script `drift` handed over, rebound; for the
    contact button, rebound with the toggle, which then has run, bound again."""
    if isinstance(drift.at, StepAt) and drift.at.step.target == "toggle":
        return rebound(drift.script)
    again = rebinding_each(drift)
    locators = [
        fx.by_role("button", "Show details"),
        {"css": "button", "scope": {"css": "main"}},
    ]
    return rebound(again, locators=locators)


@pytest.mark.parametrize(
    ("script", "key"),
    [
        pytest.param(CLICKED_THEN_CONTACT_SEEN, "assertion-0", id="assertion"),
        pytest.param(TWO_CLICKS, "2", id="step"),
    ],
)
def test_a_repair_that_rebinds_another_target_a_step_already_used_is_refused(
    tmp_path: Path, script: CompiledScript, key: str
) -> None:
    run = repairing(tmp_path, rebinding_the_used_toggle_too, script=script)

    problem = "/targets/toggle: a repair rebinds no target a step that already ran acted through"
    assert repairs(run)[-1] == (key, ("no match",), (problem,), None)
    assert [step.outcome for step in run.result.steps][:2] == ["completed", "completed"]
    assert run.result.outcome == "failed"


# The toggle bound where the moved page has it and clicked, then filled,
# which a button refuses.
CLICKED_THEN_FILLED = DETAILS.model_copy(
    update={
        "targets": {**DETAILS.targets, "toggle": Target.model_validate(MOVED_TOGGLE)},
        "steps": (
            *DETAILS.steps,
            Fill(seq=2, action="fill", target="toggle", value="x", side_effect=False),
        ),
    }
)


def test_a_step_that_completes_or_fails_never_goes_to_the_repair(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, lambda drift: drift.script, script=CLICKED_THEN_FILLED)

    assert run.handed == []
    assert [(step.seq, step.outcome) for step in run.result.steps] == [
        (0, "completed"),
        (1, "completed"),
        (2, "failed"),
    ]
    assert run.result.repairs == ()
    assert not (run.record / "repairs").exists()


# A fetch to a host the run doesn't allow, awaited until the proxy refuses it.
FETCH_UNDECLARED = "() => fetch('http://undeclared.example.test:8080/x').catch(() => 0)"


def test_no_repair_is_asked_for_once_an_egress_block_interrupted_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The block lands during the toggle's first lookup, before it drifts.
    fired = around(monkeypatch, "role=button", (1, FETCH_UNDECLARED, "() => 0"))

    run = repairing(tmp_path, lambda drift: rebound(drift.script))

    assert fired == [1]
    assert run.handed == []
    assert outcomes(run) == [(0, "completed", ()), (1, "drifted", MISSES)]
    assert (run.result.outcome, run.result.error_code) == ("errored", "egress_blocked")


# A Tab pressed (seq 1), then the toggle clicked (seq 2).
TAB_FIRST = DETAILS.model_copy(
    update={
        "steps": (
            Press(seq=1, action="press", key="Tab", side_effect=False),
            Click(seq=2, action="click", target="toggle", side_effect=False),
        )
    }
)


def inserting_before_the_tab(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with a replay-safe `press`
    inserted before seq 1, which already ran."""
    press = Press(seq=3, action="press", key="Tab", side_effect=False)
    moved = rebound(drift.script)
    return moved.model_copy(update={"steps": (press, *moved.steps)})


def test_a_repair_that_inserts_a_step_before_one_that_ran_is_refused(
    tmp_path: Path,
) -> None:
    run = repairing(tmp_path, inserting_before_the_tab, script=TAB_FIRST)

    problem = "/steps: a repair inserts no step before a step that already ran"
    assert repairs(run) == [("2", MISSES, (problem,), None)]
    assert outcomes(run) == [
        (0, "completed", ()),
        (1, "completed", ()),
        (2, "drifted", MISSES),
    ]


def held(tmp_path: Path, value: str, run: Repairing) -> list[str]:
    """Where a copy of `value` is: any file under `tmp_path` (the spec root
    with the run's record), or the run's result."""
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    return copies_found(value, files=files, texts=[repr(run.result)])


# A value a JSON pointer token escapes (RFC 6901): `~` as `~0`, `/` as `~1`.
SLASHED = "fake/session~token-for-242"


def pointer(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


def naming_a_target(name: str) -> Callable[[Drift], CompiledScript]:
    """A repair that offers the script it was handed with the toggle's
    binding added under `name`."""

    def offer(drift: Drift) -> CompiledScript:
        targets = {**drift.script.targets, name: drift.script.targets["toggle"]}
        return drift.script.model_copy(update={"targets": targets})

    return offer


@pytest.mark.parametrize(
    ("value", "offer"),
    [
        pytest.param(
            FAKE_VALUE,
            lambda drift: rebound(
                drift.script,
                locators=[
                    fx.by_role("button", FAKE_VALUE),
                    {"css": "button", "scope": {"css": "#order"}},
                ],
            ),
            id="locator",
        ),
        # Which a validator's problem would name as a pointer token.
        pytest.param(SLASHED, naming_a_target(SLASHED), id="target"),
    ],
)
def test_a_repaired_script_that_spells_a_bound_value_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    offer: Callable[[Drift], CompiledScript],
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)

    run = repairing(tmp_path, offer, spec_at=secret_spec_at)

    problem = "repairs/1.json: a bound value's spelling is in it, so it isn't written"
    assert repairs(run) == [("1", MISSES, (problem,), None)]
    assert document(run, 1) == {
        "seq": 1,
        "misses": ["not unanimous", "no match"],
        "problems": [problem],
        "script": None,
    }
    assert held(tmp_path, value, run) == []
    assert held(tmp_path, pointer(value), run) == []


def misfilled(drift: Drift) -> CompiledScript:
    """The script `drift` handed over, rebound, with the toggle's meaning a
    list holding the bound value: `model_copy` checks no field's type."""
    moved = rebound(drift.script)
    toggle = moved.targets["toggle"].model_copy(update={"semantic": [FAKE_VALUE]})
    return moved.model_copy(update={"targets": {**moved.targets, "toggle": toggle}})


def test_a_repaired_script_holding_a_value_its_field_doesnt_allow_is_refused_unquoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every warning is an error in this suite, so a serializer's warning,
    # which quotes the value, would reach this test as an exception.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)

    run = repairing(tmp_path, misfilled, spec_at=secret_spec_at)

    problem = (
        "repairs/1.json: a field holds a value its type doesn't allow, so it isn't read"
    )
    assert repairs(run) == [("1", MISSES, (problem,), None)]
    assert held(tmp_path, FAKE_VALUE, run) == []


def test_a_refusals_problems_are_scanned_before_they_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The value is an added target's name as a pointer token spells it, so
    # the validator's problem spells it, and the script offered doesn't.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", pointer(SLASHED))

    run = repairing(tmp_path, naming_a_target(SLASHED), spec_at=secret_spec_at)

    problem = "/targets/[SECRET:TEST_PASSWORD]: a heal adds a target only for a click it inserts"
    assert repairs(run) == [("1", MISSES, (problem,), None)]
    assert held(tmp_path, pointer(SLASHED), run) == []


def test_a_repair_whose_record_would_spell_a_bound_value_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A common word, which a run refuses anywhere else in its environment
    # (#215). The toggle's first miss, `not unanimous`, spells it, so neither
    # the repair's record nor its refusal's can be written: replay raises.
    hermetic_environment(monkeypatch)
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "unanimous")

    with pytest.raises(RefusedWriteError) as raised:
        repairing(tmp_path, lambda drift: rebound(drift.script), spec_at=secret_spec_at)

    assert str(raised.value) == (
        "repairs/1.json: a bound value's spelling is in it, so it isn't written"
    )
    assert list(tmp_path.rglob("repairs")) == []
