"""What `aqa heal apply <proposal_id>` refuses (API.md §7; ADR-0024's #247 amendment):
a proposal file is untrusted input, and applying never changes assertions, meanings or
side-effect steps, even from a hand-edited file. Some of these edit a file under its
id, which the id check refuses; the rest carry the id their content has, so only the
proposal's shape, the loader, the validator or replay's admission can refuse them."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aqa_core.heal_patch import blob_sha, proposal_id

from packages.cli.tests.heal_apply_fixtures import (
    A_STEP,
    KEPT_SIDE_EFFECT,
    REBIND,
    SAVING,
    apply,
    committed,
    files,
    recorded,
)
from packages.runner.tests.drift_fixtures import (
    DISMISSED_PATCH,
    GOVERNED,
    NO_THANKS,
    SHIPPING,
    SUBJECT_CONFIG,
    SUMMARY,
    UNCONTRACTED,
)

# What a refusal says after the first line, by why.
EDITED = "the proposal doesn't match its id, so it was edited after its run"
NOT_ONE = "the proposal isn't one aqa run writes"
OUTSIDE = "a heal changes nothing but target locators and inserted steps"
A_LOCATOR = [{"css": "#moved"}]
NESTED_TOO_DEEP: object = []
for _ in range(400):
    NESTED_TOO_DEEP = [NESTED_TOO_DEEP]


def replacing(path: str, value: object) -> list[dict[str, Any]]:
    return [{"op": "replace", "path": path, "value": value}]


def inserting(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"op": "add", "path": "/steps/0", "value": step}]


ADDS_NO_THANKS = {"op": "add", "path": "/targets/no_thanks", "value": NO_THANKS}


@pytest.mark.parametrize(
    "patch",
    [
        # One the validator allows: only the id can tell it from what was reviewed.
        pytest.param(
            replacing("/targets/toggle/locators", [{"css": "#decoy"}]),
            id="a-decoy-locator",
        ),
        pytest.param(replacing("/assertions/0/text", "anything"), id="an-assertion"),
    ],
)
def test_a_patch_edited_after_its_run_is_refused_by_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: list[dict[str, Any]]
) -> None:
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(root, REBIND, edit=lambda held: held.update(patch=patch))
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {proposal}: nothing was applied\n  {EDITED}\n"
    )
    assert result.stdout == ""
    assert files(tmp_path) == before


def test_a_base_edited_to_match_a_changed_script_is_refused_by_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    target = committed(root)
    typed = proposal_id("order", blob_sha(target.read_bytes()), REBIND)
    target.write_text(target.read_text() + "\n")
    recorded(root, REBIND, edit=lambda held: held.update(id=typed))
    before = files(tmp_path)

    result = apply(root, monkeypatch, typed)

    assert result.exit_code == 5, result.output
    assert EDITED in result.stderr
    assert files(tmp_path) == before


def test_a_rewritten_file_isnt_found_under_its_old_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    target = committed(root)
    typed = proposal_id("order", blob_sha(target.read_bytes()), REBIND)
    recorded(root, replacing("/targets/toggle/locators", [{"css": "#d"}]))
    before = files(tmp_path)

    result = apply(root, monkeypatch, typed)

    assert result.exit_code == 5, result.output
    assert f"  no run record under {root} holds it\n" in result.stderr
    assert files(tmp_path) == before


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        pytest.param(
            lambda held: held.update(compiled="../../evil.json"),
            EDITED,
            id="compiled-out-of-the-root",
        ),
        pytest.param(
            lambda held: held["base"].update(pr_head_sha="abc123"),
            NOT_ONE,
            id="a-pr-head",
        ),
        pytest.param(lambda held: held.update(note="hi"), NOT_ONE, id="an-extra-key"),
    ],
)
def test_edited_fields_outside_the_id_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[dict[str, Any]], object],
    reason: str,
) -> None:
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(root, REBIND, edit=edit)
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {proposal}: nothing was applied\n  {reason}\n"
    )
    assert files(tmp_path) == before


@pytest.mark.parametrize(
    "change",
    [
        # A value and no `from`, so each is refused by its op alone.
        {"op": "remove", "path": "/targets/toggle/locators", "value": A_LOCATOR},
        {"op": "move", "path": "/targets/toggle/locators", "value": A_LOCATOR},
        {"op": "replace", "path": "/targets/toggle/locators"},
        {"op": "replace", "path": "/targets/toggle/locators", "value": [], "from": "/"},
        {"op": "replace", "path": "/x", "value": NESTED_TOO_DEEP},
    ],
    ids=["remove", "move", "no-value", "an-extra-key", "a-value-nested-too-deep"],
)
def test_a_proposal_takes_only_a_heals_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    """Each with the id its content has, so only the proposal's shape can refuse."""
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(root, [change])
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert f"  {NOT_ONE}\n" in result.stderr
    assert files(tmp_path) == before


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        pytest.param(
            replacing("/assertions/0/text", "anything"),
            f"/assertions: {OUTSIDE}",
            id="an-assertions-text",
        ),
        pytest.param(
            replacing("/targets/toggle/semantic", "anything"),
            "/targets/toggle/semantic: a heal never changes a target's meaning",
            id="a-meaning",
        ),
        pytest.param(
            replacing("/steps/1/target", "summary"),
            "/steps/1/target: a heal rewrites no existing step",
            id="a-side-effect-steps-target",
        ),
        pytest.param(
            inserting(
                {"seq": 9, "action": "navigate", "url": "/x", "side_effect": False}
            ),
            "/steps/0/action: a heal inserts no navigation, which could route "
            "around a path that is broken for its users",
            id="an-inserted-navigate",
        ),
        # The validator's arguments swapped would apply this: a step removed one way
        # is a safe step inserted the other.
        pytest.param(
            replacing("/steps", [KEPT_SIDE_EFFECT]),
            "/steps/0: a heal removes no step",
            id="a-step-removed",
        ),
        # Replay's admission would refuse this too, so the words show which came first.
        pytest.param(
            replacing("/spec_hash", "sha256:" + "1" * 64),
            f"/spec_hash: {OUTSIDE}",
            id="the-spec-hash",
        ),
        # A target a heal adds is for a click it inserts, and only that (#325).
        pytest.param(
            [ADDS_NO_THANKS],
            "/targets/no_thanks: a heal adds a target only for a click it inserts",
            id="an-added-target-no-step-uses",
        ),
        pytest.param(
            [
                ADDS_NO_THANKS,
                *inserting(
                    {
                        "seq": 9,
                        "action": "fill",
                        "target": "no_thanks",
                        "value": "x",
                        "side_effect": False,
                    }
                ),
            ],
            "/steps/0/action: a heal acts on a target it adds only by a click",
            id="a-fill-on-an-added-target",
        ),
    ],
)
def test_a_patch_the_validator_refuses_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch: list[dict[str, Any]],
    reason: str,
) -> None:
    """Each with the id its content has, so only the validator can refuse. That the
    validator is called on what was read is what these show: `test_heal_patch.py` owns
    each of its rules."""
    root = tmp_path / "qa"
    committed(root, SAVING)
    proposal = recorded(root, patch)
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {proposal}: nothing was applied\n  {reason}\n"
    )
    assert result.stdout == ""
    assert files(tmp_path) == before


@pytest.mark.parametrize(
    ("patch", "said"),
    [
        pytest.param(
            replacing("/targets/toggle/locators", []),
            "targets.toggle.locators",
            id="no-locators",
        ),
        pytest.param(
            replacing("/targets/toggle/locators", [{"css": "\ud800"}]),
            "unable to parse raw data as a unicode string",
            id="a-lone-surrogate",
        ),
        pytest.param(
            [{"op": "add", "path": "/targets/toggle/unknown", "value": 1}],
            "targets.toggle.unknown",
            id="an-unknown-field",
        ),
        pytest.param(inserting(A_STEP), "steps[1].seq", id="a-seq-already-used"),
    ],
)
def test_a_patch_the_loader_refuses_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch: list[dict[str, Any]],
    said: str,
) -> None:
    root = tmp_path / "qa"
    target = committed(root, SAVING)
    proposal = recorded(root, patch)
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr.startswith(
        f"spec_error {proposal}: nothing was applied\n  {target}: "
    )
    assert said in result.stderr
    assert files(tmp_path) == before


def test_a_path_the_patch_cant_follow_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(root, replacing("/missing/deeper", 1))
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {proposal}: nothing was applied\n"
        "  /missing/deeper: a patch can't follow this path\n"
    )
    assert files(tmp_path) == before


def test_a_spec_edited_since_the_run_is_refused_as_replay_refuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(root, REBIND)
    spec = root / "order.spec.md"
    spec.write_text(spec.read_text().replace(SHIPPING, "The order says what it holds"))
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {proposal}: nothing was applied\n"
        "  compiled from another version of the spec: explore it again\n"
    )
    assert files(tmp_path) == before


def test_subject_contracts_changed_since_the_run_are_refused_as_replay_refuses_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(root, REBIND)
    (root / "config.yaml").write_text(
        "budgets: { resolve_seconds: 1 }\n"
        "subjects:\n"
        "  - { spec: order, expect: 0, region: main, part: p }\n"
    )
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert "compiled under other subject contracts: explore it again" in result.stderr
    assert files(tmp_path) == before


def test_an_inserted_step_replay_wouldnt_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(
        root,
        inserting({"seq": 9, "action": "press", "key": "a+b", "side_effect": False}),
    )
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {proposal}: nothing was applied\n"
        "  steps[0] (seq 9): press takes one key, with only modifiers held before "
        "it: 'a+b'\n"
    )
    assert files(tmp_path) == before


def test_a_refusal_escapes_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    committed(root)
    proposal = recorded(
        root,
        [
            {
                "op": "add",
                "path": "/targets/\x1b[31mred",
                "value": {"semantic": "new", "locators": [{"css": "#n"}]},
            }
        ],
    )

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert "\x1b" not in result.stderr
    assert (
        "  /targets/\\x1b[31mred: a heal adds a target only for a click it inserts\n"
        in result.stderr
    )


def governed_meaning(patch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`patch`, `DISMISSED_PATCH`, with the target it adds given the summary's
    meaning, which the config's subject row governs."""
    added, *rest = patch
    value = {**added["value"], "semantic": SUMMARY["semantic"]}
    return [{**added, "value": value}, *rest]


def test_an_added_target_with_a_governed_meaning_is_refused_as_the_seam_refuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same problem in the same words as `repairs/<key>.json` records at
    the seam (#332): both run replay's admission."""
    root = tmp_path / "qa"
    committed(root, GOVERNED)
    (root / "config.yaml").write_text(SUBJECT_CONFIG)
    proposal = recorded(root, governed_meaning(DISMISSED_PATCH))
    before = files(tmp_path)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        f"spec_error {proposal}: nothing was applied\n  {UNCONTRACTED}\n"
    )
    assert files(tmp_path) == before


def test_an_added_target_with_its_own_meaning_applies_under_the_subject_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    committed(root, GOVERNED)
    (root / "config.yaml").write_text(SUBJECT_CONFIG)
    proposal = recorded(root, DISMISSED_PATCH)

    result = apply(root, monkeypatch, proposal)

    assert result.exit_code == 0, result.output
