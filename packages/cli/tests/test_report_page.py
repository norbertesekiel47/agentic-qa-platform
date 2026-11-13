"""What `aqa report` shows of a run (UX_SPEC §3.5; API.md §7; #249), from real
runs on #240's drift pages: one report per outcome it pins, and what a damaged or
hostile record shows instead. A record is untrusted input, so each test edits a
copy of a real one as a person could. `test_report_browser.py` holds what a
browser does with the page."""

import difflib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aqa_core.compiled import CompiledScript
from aqa_core.heal_patch import proposal_id
from aqa_runner.heal_proposal import diff
from aqa_runner.publication import published_text

from packages.cli.tests.report_fixtures import (
    GOAL,
    Element,
    Ran,
    copied,
    having,
    healed,
    replayed,
    replayed_with_a_secret,
    reported,
    tagged,
    tree,
)
from packages.runner.tests.drift_fixtures import (
    BUTTON,
    DETAILS,
    IN_VIEW,
    SHIPPING,
    page,
)

PRIVATE = (
    "Private. This report is part of the private run record under .aqa/runs/ and "
    "may hold page text"
)
NOT_ONE = "The proposal isn't one aqa run writes, so no diff is shown."
# What a link planted in a record leads to: none of it may reach the page.
OUTSIDE = "outside-the-record-4u49"
TOGGLE = '{"role": "button", "name": "Show details"}'
# The navigation every run starts with, as its timeline entry reads.
STEP_0 = (
    'Step 0 ✓ passed Action: {"action": "navigate", "url": "/page/order"} '
    "Target: none Locator: none"
)


@pytest.fixture(scope="module")
def passed(tmp_path_factory: pytest.TempPathFactory) -> Ran:
    return replayed(
        tmp_path_factory.mktemp("passed") / "qa", page("order"), DETAILS, SHIPPING
    )


@pytest.fixture(scope="module")
def inconclusive(tmp_path_factory: pytest.TempPathFactory) -> Ran:
    return replayed(
        tmp_path_factory.mktemp("inconclusive") / "qa", page("moved"), DETAILS, SHIPPING
    )


@pytest.fixture(scope="module")
def violated(tmp_path_factory: pytest.TempPathFactory) -> Ran:
    return replayed(
        tmp_path_factory.mktemp("violated") / "qa", page("modal"), BUTTON, IN_VIEW
    )


@pytest.fixture(scope="module")
def drift(tmp_path_factory: pytest.TempPathFactory) -> Ran:
    return healed(tmp_path_factory.mktemp("drift") / "qa")


@pytest.fixture(scope="module")
def withheld(tmp_path_factory: pytest.TempPathFactory) -> Ran:
    return replayed_with_a_secret(
        tmp_path_factory.mktemp("withheld") / "qa", DETAILS, SHIPPING
    )


def report_of(
    ran: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[Ran], object] | None = None,
) -> tuple[str, Element]:
    """`aqa report` of a copy of `ran`'s record, edited by `edit` first: the page,
    as written and as a tree. The command exits 0 and says where it wrote it."""
    copy = copied(ran, tmp_path)
    if edit is not None:
        edit(copy)
    result = reported(copy, monkeypatch)
    written = copy.record / "report.html"
    assert result.exit_code == 0, result.output
    assert result.stdout == f"report {copy.record.name}: {written}\n"
    assert result.stderr == ""
    html = written.read_text()
    return html, tree(html)


def facts(page_tree: Element) -> dict[str, str]:
    """The header's facts, by name."""
    terms = page_tree.all(tagged("dt"))
    values = page_tree.all(tagged("dd"))
    return {term.text: value.text for term, value in zip(terms, values, strict=True)}


def steps(page_tree: Element) -> list[str]:
    """Each timeline entry's text."""
    return [entry.text for entry in page_tree.all(having("role", "option"))]


def panel(page_tree: Element) -> str:
    return page_tree.one(having("aria-labelledby", "verdict-title")).text


def block(page_tree: Element, tab: str, step: int) -> Element:
    """What tab `tab` shows for the timeline's entry `step`."""
    shown = page_tree.one(having("id", f"panel-{tab}"))
    return shown.one(having("data-step", str(step)))


def assert_header(
    page_tree: Element, ran: Ran, chip: str, mode: str, models: str
) -> None:
    assert page_tree.one(tagged("h1")).text == GOAL
    found = facts(page_tree)
    assert found["Verdict"] == chip
    assert found["Mode"].startswith(f"{mode} ")
    assert found["Duration"] == ran.seconds
    assert found["Cost"] == ran.cost
    assert found["Model config"] == models
    assert PRIVATE in page_tree.text


def test_a_passed_replay_shows_what_passed(
    passed: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html, shown = report_of(passed, tmp_path, monkeypatch)

    assert_header(
        shown, passed, "✓ passed", "strict", "none: strict replay calls no model"
    )
    assert steps(shown) == [
        STEP_0,
        (
            'Step 1 ✓ passed Action: {"action": "click", "target": "toggle"} '
            "Target: toggle, the button that shows the order's details "
            f"Locator: {TOGGLE}"
        ),
    ]
    assert panel(shown) == (
        "Why aqa decided this Classification: ✓ passed What passed "
        f"a1: {SHIPPING} a2: {SHIPPING} console_errors: held js_exceptions: held "
        "http_5xx: held broken_images: held"
    )
    assert "intentional" not in html.lower()


def test_an_inconclusive_replay_shows_its_drift_and_misses(
    inconclusive: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html, shown = report_of(inconclusive, tmp_path, monkeypatch)

    assert_header(
        shown,
        inconclusive,
        "? inconclusive",
        "strict",
        "none: strict replay calls no model",
    )
    reason = "drift: strict replay doesn't heal (not unanimous, no match)"
    assert steps(shown) == [
        STEP_0,
        (
            'Step 1 ? inconclusive Action: {"action": "click", "target": "toggle"} '
            "Target: toggle, the button that shows the order's details Locator: none "
            f"{reason} Misses: not unanimous, no match"
        ),
    ]
    assert (
        f"Verdicts that didn't pass Step 1 ? inconclusive {reason} Misses: not "
        "unanimous, no match Evidence: evidence/1/a11y_snapshot.yaml "
        "evidence/1/console_log.json evidence/1/network_log.json"
    ) in panel(shown)
    assert "intentional" not in html.lower()


def test_an_expectation_violated_replay_shows_its_finding_and_evidence(
    violated: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html, shown = report_of(violated, tmp_path, monkeypatch)

    assert_header(
        shown,
        violated,
        "✗ expectation violated",
        "strict",
        "none: strict replay calls no model",
    )
    assert steps(shown) == [
        STEP_0,
    ]
    assert panel(shown) == (
        "Why aqa decided this Classification: ✗ expectation violated "
        "Verdicts that didn't pass Assertion a1 ✗ expectation violated "
        f"Finding: visual {IN_VIEW} Evidence: evidence/0/a11y_snapshot.yaml "
        "evidence/0/console_log.json evidence/0/network_log.json"
    )
    assert "intentional" not in html.lower()


def locator_lines(text: str) -> range:
    """The lines of the published compiled file `text` that hold the toggle's
    locators, their brackets aside."""
    lines = text.splitlines()
    start = lines.index('      "locators": [', lines.index('    "toggle": {'))
    end = lines.index("      ]", start)
    return range(start + 1, end)


def test_a_run_that_healed_shows_its_classification_binding_and_diff(
    drift: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html, shown = report_of(drift, tmp_path, monkeypatch)

    assert_header(
        shown,
        drift,
        "⇄ heal proposed: UI drift, expectations hold",
        "run",
        "healer: anthropic claude-sonnet-5-5, effort the provider's default, "
        "fallback none",
    )
    assert steps(shown) == [
        STEP_0,
        (
            'Step 1 ⇄ UI drift, expectations hold Action: {"action": "click", '
            '"target": "toggle"} Target: toggle, the button that shows the order\'s '
            f"details Locator: {TOGGLE}"
        ),
    ]
    proposal = drift.replay_json()["heal_proposal"]
    said = panel(shown)
    assert "Classification: ⇄ UI drift, expectations hold" in said
    assert (
        f"Assertions that passed after the repair a1: {SHIPPING} a2: {SHIPPING}" in said
    )
    assert (
        'What the healer bound Step 1: bound to button "Show details", the page\'s '
        "own role and name; confidence 0.9 (model-reported, uncalibrated)"
    ) in said
    assert f"Heal proposal {proposal['id']}" in said
    shown_diff = shown.one(having("class", "diff")).raw_text.split("\n")
    assert shown_diff == drift.diff
    assert "intentional" not in html.lower()


def test_the_heal_diff_touches_only_the_rebound_locators(
    drift: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, shown = report_of(drift, tmp_path, monkeypatch)

    kept = json.loads((drift.record / "run.json").read_text())
    recorded = CompiledScript.model_validate_json(json.dumps(kept["script"]))
    patched = kept["script"]
    [change] = drift.replay_json()["heal_proposal"]["patch"]
    patched["targets"]["toggle"]["locators"] = change["value"]
    after = CompiledScript.model_validate_json(json.dumps(patched))
    before_text, after_text = published_text(recorded), published_text(after)
    shown_diff = shown.one(having("class", "diff")).raw_text.split("\n")
    assert shown_diff == diff("order", recorded, after)
    changed = difflib.SequenceMatcher(
        None, before_text.splitlines(), after_text.splitlines()
    ).get_opcodes()
    for tag, i1, i2, j1, j2 in changed:
        if tag != "equal":
            assert set(range(i1, i2)) <= set(locator_lines(before_text))
            assert set(range(j1, j2)) <= set(locator_lines(after_text))


def with_patch(
    patch: list[dict[str, Any]], *, same_id: bool = False
) -> Callable[[Ran], object]:
    """An edit that gives the record's proposal `patch`, with the id of its new
    content unless `same_id`, so only the validator can refuse it."""

    def edit(ran: Ran) -> None:
        def changed(document: dict[str, Any]) -> None:
            held = document["heal_proposal"]
            held["patch"] = patch
            if not same_id:
                held["id"] = proposal_id("order", held["base"]["blob_sha"], patch)

        ran.edit_replay_json(changed)

    return edit


def another_id(ran: Ran) -> None:
    ran.edit_replay_json(lambda d: d["heal_proposal"].update(id="ah_000000000000"))


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            with_patch([{"op": "replace", "path": "/assertions/0/text", "value": "x"}]),
            id="an-assertion",
        ),
        pytest.param(
            with_patch(
                [{"op": "replace", "path": "/targets/toggle/semantic", "value": "x"}]
            ),
            id="a-meaning",
        ),
        pytest.param(
            with_patch(
                [
                    {
                        "op": "add",
                        "path": "/assertions/-",
                        "value": {
                            "id": "a9",
                            "expect_index": 0,
                            "check": "text_visible",
                            "text": "anything",
                        },
                    }
                ]
            ),
            id="an-added-assertion",
        ),
        pytest.param(
            with_patch(
                [
                    {
                        "op": "replace",
                        "path": "/targets/toggle/locators",
                        "value": [{"css": "#decoy"}],
                    }
                ],
                same_id=True,
            ),
            id="edited-under-its-id",
        ),
        pytest.param(another_id, id="another-id"),
        pytest.param(
            lambda ran: ran.edit_replay_json(
                lambda d: d["heal_proposal"].update(compiled="../../x.json")
            ),
            id="another-file",
        ),
        pytest.param(
            lambda ran: ran.edit_replay_json(
                lambda d: d.update(heal_proposal="ah_000000000000")
            ),
            id="not-a-proposal",
        ),
    ],
)
def test_a_proposal_aqa_run_doesnt_write_gets_no_diff(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[Ran], object],
) -> None:
    _, shown = report_of(drift, tmp_path, monkeypatch, edit)

    assert shown.all(having("class", "diff")) == []
    assert NOT_ONE in panel(shown)


def assert_links_stay_in(record: Path, page_tree: Element) -> None:
    hrefs = [e.attrs["href"] or "" for e in page_tree.all(having("href"))]
    assert hrefs
    for href in hrefs:
        if href.startswith("#"):
            continue
        assert not href.startswith("/")
        assert ":" not in href
        target = record / href
        assert target.resolve().is_relative_to(record.resolve())
        assert target.is_file()
        parts = Path(href).parts
        assert not any(
            (record.joinpath(*parts[: n + 1])).is_symlink() for n in range(len(parts))
        )


@pytest.mark.parametrize("outcome", ["passed", "inconclusive", "violated", "drift"])
def test_every_link_is_a_file_inside_the_record(
    outcome: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: Ran = request.getfixturevalue(outcome)
    copy = copied(ran, tmp_path)
    result = reported(copy, monkeypatch)

    assert result.exit_code == 0, result.output
    shown = tree((copy.record / "report.html").read_text())
    if outcome != "passed":
        assert_links_stay_in(copy.record, shown)
    else:
        assert shown.all(having("href")) == []


def planted(ran: Ran, name: str) -> str:
    """A link at `name` in `ran`'s record, to a file outside it; its name."""
    outside = ran.root.parent / "outside.txt"
    outside.write_text(f"{OUTSIDE}\n")
    (ran.record / name).symlink_to(outside)
    return name


@pytest.mark.parametrize(
    "path",
    ["javascript:alert(1)", "/etc/passwd", "../../x", "evidence/1/../../x", "link"],
)
@pytest.mark.parametrize("field", ["evidence", "repair"])
def test_a_path_outside_the_record_gets_no_link(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    field: str,
) -> None:
    def edit(ran: Ran) -> None:
        given = path
        if path == "link" and field == "evidence":
            (ran.record / "evidence" / "9").symlink_to(ran.record / "evidence" / "1")
            given = "evidence/9"
        elif path == "link":
            given = planted(ran, "repairs/7.json")

        def changed(document: dict[str, Any]) -> None:
            [step] = [v for v in document["verdicts"] if v.get("step") == 1]
            if field == "evidence":
                step["evidence"]["path"] = given
            else:
                step["heal"]["repair"] = given

        ran.edit_replay_json(changed)

    html, shown = report_of(drift, tmp_path, monkeypatch, edit)

    [step] = shown.one(having("aria-labelledby", "verdict-title")).all(tagged("li"))[:1]
    assert "evidence not found" in step.text
    hrefs = [e.attrs["href"] for e in shown.all(having("href"))]
    assert path not in hrefs
    if field == "evidence":
        assert not any((h or "").startswith("evidence/1/") for h in hrefs)
    else:
        assert "repairs/1.json" not in hrefs
    assert OUTSIDE not in html
    assert_links_stay_in(tmp_path / "qa" / ".aqa" / "runs" / drift.record.name, shown)


def deleted(name: str) -> Callable[[Ran], object]:
    return lambda ran: (ran.record / name).unlink()


def garbage(name: str) -> Callable[[Ran], object]:
    return lambda ran: (ran.record / name).write_bytes(b"\xff\xfe\x00{not json")


def linked(name: str) -> Callable[[Ran], object]:
    def edit(ran: Ran) -> None:
        (ran.record / name).unlink()
        planted(ran, name)

    return edit


def ways(name: str) -> list[Any]:
    return [
        pytest.param(deleted(name), id=f"{name}-deleted"),
        pytest.param(garbage(name), id=f"{name}-garbage"),
        pytest.param(linked(name), id=f"{name}-linked"),
    ]


@pytest.mark.parametrize("edit", ways("run.json"))
def test_without_a_usable_run_json_its_fields_are_not_recorded(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[Ran], object],
) -> None:
    html, shown = report_of(drift, tmp_path, monkeypatch, edit)

    assert shown.one(tagged("h1")).text == "Goal: not recorded"
    found = facts(shown)
    assert (found["Duration"], found["Model config"]) == (
        "not recorded",
        "not recorded",
    )
    assert found["Cost"] == drift.cost
    assert "Expectations not recorded" in shown.text
    assert steps(shown)[1].endswith("Target: toggle Locator: not recorded")
    assert "The heal diff: not recorded." in panel(shown)
    assert "a1: not recorded" in panel(shown)
    assert OUTSIDE not in html


@pytest.mark.parametrize("edit", ways("steps.jsonl"))
def test_without_a_usable_journal_actions_come_from_run_json(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[Ran], object],
) -> None:
    html, shown = report_of(drift, tmp_path, monkeypatch, edit)

    first, second = steps(shown)
    assert first == "Step 0 ✓ passed Action: not recorded Target: none Locator: none"
    assert second.startswith(
        'Step 1 ⇄ UI drift, expectations hold Action: {"action": "click", '
        '"target": "toggle"} Target: toggle'
    )
    assert OUTSIDE not in html


def costing(cost_usd: str) -> Callable[[Ran], object]:
    """An edit that gives the record's one cost line `cost_usd`, which a
    `CostRecord` still reads."""

    def edit(ran: Ran) -> None:
        [line] = (ran.record / "costs.jsonl").read_text().splitlines()
        call = json.loads(line) | {"cost_usd": cost_usd}
        (ran.record / "costs.jsonl").write_text(json.dumps(call) + "\n")

    return edit


@pytest.mark.parametrize(
    ("edit", "cost"),
    [
        (deleted("costs.jsonl"), "$0"),
        (garbage("costs.jsonl"), "couldn't be read"),
        (linked("costs.jsonl"), "couldn't be read"),
        (
            lambda ran: (ran.record / "costs.jsonl").write_text("{}\n"),
            "couldn't be read",
        ),
        # A finite Decimal, which no sum in the default context can hold.
        (costing("1e999999999"), "couldn't be read"),
    ],
    ids=["deleted", "garbage", "linked", "not-a-cost-record", "past-any-sum"],
)
def test_a_cost_record_that_cant_be_read_says_so(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[Ran], object],
    cost: str,
) -> None:
    html, shown = report_of(drift, tmp_path, monkeypatch, edit)

    assert facts(shown)["Cost"] == cost
    assert OUTSIDE not in html


@dataclass(frozen=True)
class Shown:
    """An evidence file, its tab, and what the tab shows without it and with
    one that can't be read."""

    name: str
    tab: str
    absent: str
    broken: str


@pytest.mark.parametrize(
    "shown",
    [
        Shown(
            "evidence/1/a11y_snapshot.yaml",
            "a11y",
            "Not recorded for this step.",
            "The snapshot couldn't be read.",
        ),
        Shown(
            "evidence/1/console_log.json",
            "console",
            "No console messages.",
            "The console log couldn't be read.",
        ),
        Shown(
            "evidence/1/network_log.json",
            "network",
            "No requests.",
            "The network log couldn't be read.",
        ),
    ],
    ids=["snapshot", "console", "network"],
)
@pytest.mark.parametrize("way", ["deleted", "garbage", "linked"])
def test_evidence_that_cant_be_read_gives_its_tab_a_fixed_state(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shown: Shown,
    way: str,
) -> None:
    edit = {"deleted": deleted, "garbage": garbage, "linked": linked}[way](shown.name)

    html, page_tree = report_of(drift, tmp_path, monkeypatch, edit)

    expected = shown.absent if way == "deleted" else shown.broken
    assert block(page_tree, shown.tab, 1).text == expected
    assert OUTSIDE not in html


@pytest.mark.parametrize(
    ("way", "said"),
    [
        ("deleted", ("not recorded", "evidence not found")),
        ("garbage", ("couldn't be read", "repairs/1.json")),
        ("linked", ("couldn't be read", "evidence not found")),
    ],
)
def test_a_repair_that_cant_be_read_shows_no_locator(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    way: str,
    said: tuple[str, str],
) -> None:
    edit = {"deleted": deleted, "garbage": garbage, "linked": linked}[way]
    locator, link = said

    html, shown = report_of(drift, tmp_path, monkeypatch, edit("repairs/1.json"))

    assert steps(shown)[1].endswith(f"Locator: {locator}")
    assert link in panel(shown)
    assert OUTSIDE not in html


def test_an_outcome_outside_the_four_is_errored_with_its_reasons(
    passed: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(ran: Ran) -> None:
        ran.edit_replay_json(
            lambda d: d.update(
                outcome="egress_blocked",
                exit_code=6,
                error_code="egress_blocked",
                reasons=["the page asked for a host it may not reach", 7],
                verdicts=[],
            )
        )

    _, shown = report_of(passed, tmp_path, monkeypatch, blocked)

    assert facts(shown)["Verdict"] == "⊘ errored egress_blocked"
    assert panel(shown) == (
        "Why aqa decided this Classification: ⊘ errored egress_blocked "
        "The run ended egress_blocked. the page asked for a host it may not reach "
        "a reason that couldn't be read"
    )
    assert "No step has a verdict in this record." in shown.text


def test_malformed_verdicts_get_a_fixed_state_and_the_rest_render(
    drift: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(ran: Ran) -> None:
        def changed(document: dict[str, Any]) -> None:
            step = document["verdicts"][1]
            step |= {
                "verdict": "won",
                "reason": 7,
                "misses": "no match",
                "locator_index": -1,
                "heal": {"repair": None, "model_reported": "high"},
            }
            document["verdicts"].insert(0, "not a verdict")
            document["verdicts"].append({"step": 2, "locator_index": 99})

        ran.edit_replay_json(changed)

    _, shown = report_of(drift, tmp_path, monkeypatch, broken)

    entries = steps(shown)
    assert len(entries) == 3
    assert entries[1].startswith("Step 1 ⊘ verdict couldn't be read")
    assert entries[1].endswith("Locator: none")
    assert entries[2] == (
        "Step 2 ⊘ verdict couldn't be read Action: not recorded Target: none "
        "Locator: none"
    )
    assert "An entry that couldn't be read" in panel(shown)
    assert block(shown, "reasoning", 1).text == (
        "The heal bound no element for this step. Confidence: no confidence "
        "reported No rationale reported."
    )


def test_each_tab_shows_the_selected_steps_evidence(
    drift: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def louder(ran: Ran) -> None:
        network = ran.record / "evidence" / "0" / "network_log.json"
        log = json.loads(network.read_text())
        log["entries"].append(
            {"method": "POST", "url": "http://127.0.0.1:9/save", "status": 503}
        )
        log["entries"].append("not an entry")
        log["total"] = 5
        network.write_text(json.dumps(log))
        console = ran.record / "evidence" / "0" / "console_log.json"
        console.write_text(
            json.dumps(
                {
                    "entries": [{"type": "error", "text": "the save failed"}, 7],
                    "total": 2,
                }
            )
        )

    _, shown = report_of(drift, tmp_path, monkeypatch, louder)

    assert 'button "Show details"' in block(shown, "a11y", 1).text
    assert block(shown, "a11y", 1).one(tagged("pre")).attrs["tabindex"] == "0"
    requests = block(shown, "network", 0).text
    assert "Showing the first 3 of 5 requests" in requests
    assert "GET http://127.0.0.1:" in requests
    assert "/page/order · status 200" in requests
    assert "POST http://127.0.0.1:9/save · status 503 ! server error (5xx)" in requests
    assert "This entry couldn't be read." in requests
    assert block(shown, "network", 1).text == "No requests."
    assert block(shown, "console", 0).text == (
        "The page's console messages during this step, in its own words error the "
        "save failed This entry couldn't be read."
    )
    assert block(shown, "console", 1).text == "No console messages."
    assert block(shown, "reasoning", 0).text == "No model ran for this step."
    assert block(shown, "reasoning", 1).text == (
        'Bound to button "Show details", the page\'s own role and name. '
        "Confidence: 0.9 (model-reported, uncalibrated) The model's rationale, in "
        "its own words (model-reported, uncalibrated) it fits"
    )


@pytest.mark.parametrize(
    "confidence",
    [10**400, "0.9", True, None],
    ids=["past-a-float", "text", "a-boolean", "null"],
)
def test_a_confidence_that_isnt_a_number_is_not_reported(
    drift: Ran,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confidence: object,
) -> None:
    def edit(ran: Ran) -> None:
        ran.edit_replay_json(
            lambda d: d["verdicts"][1]["heal"]["model_reported"].update(
                confidence=confidence
            )
        )

    _, shown = report_of(drift, tmp_path, monkeypatch, edit)

    assert "Confidence: no confidence reported" in block(shown, "reasoning", 1).text
    assert "confidence no confidence reported" in panel(shown)


def test_a_strict_replay_says_no_model_ran(
    passed: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, shown = report_of(passed, tmp_path, monkeypatch)

    assert block(shown, "reasoning", 0).text == "Strict replay calls no model."


def test_a_snapshot_left_empty_is_not_recorded(
    drift: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def emptied(ran: Ran) -> None:
        (ran.record / "evidence" / "1" / "a11y_snapshot.yaml").write_text("")
        (ran.record / "evidence" / "1" / "network_log.json").write_text("")

    _, shown = report_of(drift, tmp_path, monkeypatch, emptied)

    assert block(shown, "a11y", 1).text == "Not recorded for this step."
    assert block(shown, "network", 1).text == "No requests."


def test_a_run_binding_a_test_secret_shows_its_evidence_withheld(
    withheld: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert withheld.replay_json()["withheld"] is True

    html, shown = report_of(withheld, tmp_path, monkeypatch)

    requests = block(shown, "network", 0).text
    assert requests.startswith(
        "The URLs and methods were withheld, because the run bound a test secret."
    )
    assert "Status 200" in requests
    assert "/page/order" not in requests
    assert block(shown, "console", 0).text.startswith(
        "The console text was withheld, because the run bound a test secret."
    )
    assert "fake-api-token-for-49" not in html
