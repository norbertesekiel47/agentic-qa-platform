"""Human-supplied, base-hashed locator replacements, validated without writes."""

import json
import re
from pathlib import Path
from typing import Literal

from aqa_core.compiled import CompiledScript, Locator
from aqa_core.config import ProjectConfig
from aqa_core.heal_patch import heal_patch_problems
from aqa_core.project import parse_compiled
from aqa_core.schema import StrictModel
from aqa_core.spec import canonical_hash
from manifest import _reject_duplicates


class Replacement(StrictModel):
    op: Literal["replace"]
    path: str
    value: tuple[Locator, ...]


class Rebinding(StrictModel):
    base_hash: str
    operations: tuple[Replacement, ...]


def apply_rebinding(
    original: CompiledScript, text: str, config: ProjectConfig, *, source: Path
) -> CompiledScript:
    """Return a strictly validated candidate; never mutate the original or disk.
    Invalid patch input, or a change `aqa_core.heal_patch` refuses, raises
    ValueError; invalid compiled input raises SpecError.
    `source` is only the diagnostic name for the reconstructed candidate."""
    json.loads(text, object_pairs_hook=_reject_duplicates)
    patch = Rebinding.model_validate_json(text)
    data = original.model_dump(mode="json")
    if patch.base_hash != canonical_hash(data):
        raise ValueError("rebinding base hash is stale")
    replaced: set[str] = set()
    for operation in patch.operations:
        match = re.fullmatch(r"/targets/((?:[^/~]|~[01])+)/locators", operation.path)
        target = match[1].replace("~1", "/").replace("~0", "~") if match else None
        if target not in original.targets or target in replaced:
            raise ValueError("replace a known target's locators exactly once")
        replaced.add(target)
        data["targets"][target]["locators"] = [
            v.model_dump(mode="json") for v in operation.value
        ]
    candidate = parse_compiled(json.dumps(data), config, source=source)
    if problems := heal_patch_problems(original, candidate):
        raise ValueError("; ".join(problems))
    return candidate
