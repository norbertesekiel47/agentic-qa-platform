"""What makes two confirmations' scripts identical (#56): normalized
structural identity, `replayed_as`. When and by which models a script was
compiled, whether it was confirmed and its steps' flags don't count;
everything else does, a target's meaning included."""

from aqa_runner.explore_attempts import replayed_as

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
