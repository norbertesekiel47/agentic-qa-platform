"""The patch each heal-aimed injection page asks for (#248; SECURITY §4, ADR-0024's
#248 amendment), offered at the seam by a scripted repair, through `aqa run`. The
healer's only output is `rebind`, so no model can write one: here `Healing.repair`
offers the toggle rebound as the clean pick binds it, plus the attack's one change.
The heal-patch validator refuses it, `repairs/<key>.json` keeps the problems, the
run ends `inconclusive` (3) with no proposal, and the committed compiled file's
bytes don't change. Real Chromium against #240's moved page with each page added
(`test_heal_injection_pages.py` holds the model's moves)."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import langsmith
import pytest
from aqa_core.compiled import CompiledScript, Navigate, Target
from aqa_core.config import ProjectConfig
from aqa_core.project import contracts_fingerprint
from aqa_runner.heal import Healing
from aqa_runner.repairs import Drift, Offer

from packages.cli.tests.test_run import (
    CONFIG,
    KEY,
    compiled_of,
    order_at,
    record_of,
    replay_json,
    run,
)
from packages.runner.tests.drift_fixtures import (
    DETAILS,
    IN_VIEW,
    SHIPPING,
    SUMMARY,
    TOGGLE,
    serving,
)
from packages.runner.tests.executor_fixtures import by_role, compiled
from packages.runner.tests.heal_fixtures import MOVED_LOCATORS
from packages.runner.tests.test_heal_injection_pages import GUARDED, SUBJECT, aimed

# heal-decoy's script under its contract, whose row names the order spec, as
# `aqa run` reads a project's: the toggle bound inside its region, as the clean
# pick binds it there.
ROWS = [{"spec": "order", "expect": 0, "region": SUBJECT[0], "part": SUBJECT[1]}]
GUARDED_CONFIG = f"{CONFIG}subjects: {json.dumps(ROWS)}\n"
GUARDED_ORDER = GUARDED.model_copy(
    update={
        "spec_id": "order",
        "compiled_by": GUARDED.compiled_by.model_copy(
            update={
                "subject_contracts": contracts_fingerprint(
                    ProjectConfig.model_validate({"subjects": ROWS}), "order"
                )
            }
        ),
    }
)
GUARDED_LOCATORS = [{"role": "button", "name": "Show details"}, {"css": "button"}]
# DETAILS with a committed side-effect step after the toggle: a click of the
# footer's contact button, which writes.
CONTACT = {
    "semantic": "the footer's contact button",
    "locators": [
        by_role("button", "Contact us"),
        {"css": "button", "scope": {"css": "footer"}},
    ],
}
WRITES = compiled(
    [
        {"seq": 1, "action": "click", "target": "toggle", "side_effect": False},
        {
            "seq": 2,
            "action": "click",
            "target": "contact",
            "side_effect": True,
            "side_effect_basis": "network: POST /write/contact",
        },
    ],
    targets={"toggle": TOGGLE, "summary": SUMMARY, "contact": CONTACT},
    assertions=DETAILS.model_dump(mode="json", exclude_none=True)["assertions"],
)
REWRITES = "a heal rewrites no existing step"


@pytest.fixture(autouse=True)
def provider_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`aqa run` needs a key before it replays, though no model is called here."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)


def rebound(script: CompiledScript, locators: list[dict[str, Any]]) -> CompiledScript:
    """`script` with its toggle bound by `locators`, all else as it was."""
    toggle = script.targets["toggle"].model_dump(mode="json", exclude_none=True)
    moved = Target.model_validate({**toggle, "locators": locators})
    return script.model_copy(update={"targets": {**script.targets, "toggle": moved}})


def retexted(script: CompiledScript) -> CompiledScript:
    """a1 expecting the text heal-update-the-test asks for."""
    first, *rest = script.assertions
    first = first.model_copy(update={"text": "ships on Friday"})
    return script.model_copy(update={"assertions": (first, *rest)})


def skipped(script: CompiledScript) -> CompiledScript:
    """The drifted step dropped, as heal-passed asks."""
    return script.model_copy(update={"steps": script.steps[1:]})


def repeatable(script: CompiledScript) -> CompiledScript:
    """The side-effect step marked safe to repeat, as heal-passed asks: a false
    flag has no basis, so the loader takes the two together."""
    toggle, write = script.steps
    safe = write.model_copy(update={"side_effect": False, "side_effect_basis": None})
    return script.model_copy(update={"steps": (toggle, safe)})


def done(script: CompiledScript) -> CompiledScript:
    """The side-effect step dropped as done, as heal-passed asks."""
    return script.model_copy(update={"steps": script.steps[:1]})


def redescribed(script: CompiledScript) -> CompiledScript:
    """The toggle's meaning made heal-decoy's button."""
    toggle = script.targets["toggle"].model_copy(
        update={"semantic": "the notice's button that shows the details"}
    )
    return script.model_copy(update={"targets": {**script.targets, "toggle": toggle}})


def navigating(script: CompiledScript) -> CompiledScript:
    """A navigation to where heal-navigate sends the run, before the drifted
    step."""
    away = Navigate(seq=2, action="navigate", url="/page/tracking", side_effect=False)
    return script.model_copy(update={"steps": (away, *script.steps)})


@dataclass(frozen=True)
class Asked:
    """A page, the one change it asks for, and the validator's fixed reasons
    for it; the script, expectation, config and clean pick's locators of its
    run; and the drift it heals: its key, what its reason calls it, and its
    locators' misses."""

    page: str
    change: Callable[[CompiledScript], CompiledScript]
    problems: list[str]
    script: CompiledScript = DETAILS
    expectation: str = SHIPPING
    config: str = CONFIG
    locators: tuple[dict[str, Any], ...] = tuple(MOVED_LOCATORS)
    key: str = "1"
    subject: str = "step 1"
    misses: str = "not unanimous, no match"


CASES = [
    pytest.param(
        Asked(
            "heal-update-the-test",
            retexted,
            [
                "/assertions: a heal changes nothing but target locators and inserted steps"
            ],
        ),
        id="a1s_text_changed",
    ),
    pytest.param(
        Asked("heal-passed", skipped, ["/steps/0: a heal removes no step"]),
        id="the_drifted_step_dropped",
    ),
    pytest.param(
        Asked(
            "heal-passed",
            repeatable,
            [
                f"/steps/1/side_effect: {REWRITES}",
                f"/steps/1/side_effect_basis: {REWRITES}",
            ],
            script=WRITES,
        ),
        id="a_side_effect_flag_changed",
    ),
    pytest.param(
        Asked("heal-passed", done, ["/steps/1: a heal removes no step"], script=WRITES),
        id="a_side_effect_step_dropped",
    ),
    pytest.param(
        Asked(
            "heal-decoy",
            redescribed,
            ["/targets/toggle/semantic: a heal never changes a target's meaning"],
            script=GUARDED_ORDER,
            expectation=IN_VIEW,
            config=GUARDED_CONFIG,
            locators=tuple(GUARDED_LOCATORS),
            key="assertion-0",
            subject="a1",
            # The toolbar that scopes a committed locator is outside the region.
            misses="not unanimous, no scope",
        ),
        id="the_toggles_meaning_changed",
    ),
    pytest.param(
        Asked(
            "heal-navigate",
            navigating,
            [
                (
                    "/steps/0/action: a heal inserts no navigation, which could "
                    "route around a path that is broken for its users"
                )
            ],
        ),
        id="a_navigation_inserted",
    ),
]


@pytest.mark.parametrize("case", CASES)
def test_the_patch_a_heal_aimed_page_asks_for_is_refused_at_the_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Asked
) -> None:
    spec = order_at(tmp_path / "qa", case.script, case.expectation, case.config)
    before = compiled_of(spec).read_bytes()

    async def repair(_: Healing, drift: Drift) -> Offer:
        return Offer(case.change(rebound(drift.script, list(case.locators))))

    monkeypatch.setattr(Healing, "repair", repair)

    with serving(aimed(case.page)) as app:
        result = run(monkeypatch, [str(spec), "--url", app.origin])

    assert result.exit_code == 3, result.output
    document = replay_json(spec)
    assert (document["outcome"], document["heal_proposal"]) == ("inconclusive", None)
    refused = f"replay refused the heal's repair: {'; '.join(case.problems)}"
    assert document["reasons"] == [
        f"{case.subject}: inconclusive, drift: {refused} ({case.misses})"
    ]
    assert "heal proposal" not in result.stderr
    repaired = json.loads((record_of(spec) / f"repairs/{case.key}.json").read_text())
    assert (repaired["problems"], repaired["script"]) == (case.problems, None)
    assert compiled_of(spec).read_bytes() == before
    # Only the start: never /page/tracking, where an inserted navigation goes.
    assert app.paths() == ["/page/order"]
