"""The pieces of explore's attempts (#56): what makes two confirmations'
scripts identical, normalized structural identity (`replayed_as`): when and
by which models a script was compiled, whether it was confirmed and its
steps' flags don't count, everything else does, a target's meaning
included; the restart rule; and the single `--confirm-repeat`."""

from aqa_runner.explore_attempts import UNRESET, Attempts, replayed_as

from packages.runner.tests.executor_fixtures import shop_script


def test_identical_leaves_out_provenance_confirmation_and_flags() -> None:
    script = shop_script("shop")
    later = script.model_copy(
        update={
            "compiled_at": script.compiled_at.replace(year=2030),
            "confirmed": True,
            "steps": tuple(
                step.model_copy(
                    update={
                        "side_effect": True,
                        "side_effect_basis": "fake: a write in its window",
                    }
                )
                for step in script.steps
            ),
        }
    )
    shorter = script.model_copy(update={"steps": script.steps[:1]})

    assert replayed_as(later) == replayed_as(script) != replayed_as(shorter)


def test_a_changed_meaning_is_not_identical() -> None:
    script = shop_script("shop")
    name, target = next(iter(script.targets.items()))
    renamed = script.model_copy(
        update={
            "targets": {
                **script.targets,
                name: target.model_copy(
                    update={"semantic": f"{target.semantic}, again"}
                ),
            }
        }
    )

    assert renamed.steps == script.steps
    assert replayed_as(renamed) != replayed_as(script)


def test_a_restart_needs_a_reset_hook_and_an_attempt_left() -> None:
    unreset = Attempts(3, resets=False)
    reset = Attempts(3, resets=True)
    last = Attempts(1, resets=True)

    assert [each.restart() for each in (unreset, reset, last)] == [
        f"restart did not run: {UNRESET}",
        "this attempt has ended, and a new one starts",
        "restart did not run: this is the run's last attempt",
    ]
    assert [each.restarted for each in (unreset, reset, last)] == [False, True, False]
    reset.began()
    assert (reset.made, reset.restarted) == (2, False)


def test_confirm_repeat_lets_one_confirmation_repeat_side_effects() -> None:
    attempts = Attempts(3, resets=False, repeat=True)

    assert [attempts.authorizes_repeat(), attempts.authorizes_repeat()] == [True, False]
    assert Attempts(3, resets=False).authorizes_repeat() is False
