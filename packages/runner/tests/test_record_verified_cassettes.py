"""The recorder's verified cases (#339; ADR-0024's #339 amendment): each badge page's
`aqa replay --mode verified` recorded in real Chromium from a local stand-in for the
provider, with a fake key, within the round's own bound, and promoted only when its
one verifier reply parses, cites exactly the screenshot, then the badge's ref, and
its run ends as its page should, which the rule says in fixed words when it doesn't.
Role, mode and allowance come from the case's row in the recorder's table. No test
here reaches a provider."""

from collections.abc import Sequence
from typing import Any

import pytest
from aqa_core.config import ModelRoleName, ProjectConfig
from aqa_core.model_costs import CostRecord, Mode, Status, Usage, cost_record
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import vendored

from packages.runner.tests.test_verified_cassette import SHOT
from packages.runner.tests.verified_fixtures import (
    CASES,
    VerifiedCase,
    verified_disallowed,
)

pytestmark = pytest.mark.usefixtures("reset_tracing")

PASSING, VIOLATING = CASES
# The two references a request gives, in its order.
GIVEN = [SHOT, "e2"]


def line(
    status: Status = "ok", role: ModelRoleName = "verifier", mode: Mode = "verified"
) -> CostRecord:
    """A cost line as a run writes it, `role`'s in `mode`."""
    roles = resolve_roles(ProjectConfig.model_validate({}), vendored())
    return cost_record(
        role=role,
        mode=mode,
        model=roles[role].model,
        usage=Usage(input_tokens=1612, cached_input_tokens=0, output_tokens=61),
        latency_ms=1,
        status=status,
    )


def badge(
    verdict: str = "pass",
    refs: Sequence[str] | None = tuple(GIVEN),
    *,
    finding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A `replay.json` whose a1 got `verdict`, the verifier citing `refs`, or not
    parsing when None, and holds `finding`."""
    said = None if refs is None else {"verdict": verdict, "evidence_refs": list(refs)}
    return {
        "verdicts": [
            {
                "assertion": "a1",
                "verdict": verdict,
                "finding": finding,
                "verifier": said,
            }
        ]
    }


VISUAL = {"category": "visual", "title": "The status badge is green"}


@pytest.mark.parametrize(
    ("case", "outcome", "document", "costs", "words"),
    [
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=None),
            (),
            "it didn't make exactly one verifier call",
            id="no_call",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(),
            (line(), line()),
            "it didn't make exactly one verifier call",
            id="a_second_call",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(),
            (line(role="healer", mode="heal"),),
            "it didn't make exactly one verifier call",
            id="another_roles_call",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=None),
            (line("invalid"),),
            "the verifier's reply didn't parse as its verdict",
            id="an_invalid_reply",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=None),
            (line(),),
            "the verifier's reply didn't parse as its verdict",
            id="no_verifier_on_the_verdict",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=["verified/approved/screenshot.png", "e404"]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="other_references",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=[SHOT]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="fewer_references",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive", refs=[*GIVEN, "e405"]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="more_references",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(refs=[*GIVEN, "e2"]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="a_duplicated_reference",
        ),
        pytest.param(
            PASSING,
            "passed",
            badge(refs=GIVEN[::-1]),
            (line(),),
            "the verifier's reply didn't cite exactly the screenshot, then the badge's ref",
            id="the_two_reversed",
        ),
        pytest.param(
            PASSING,
            "expectation_violated",
            badge("expectation_violated", finding=VISUAL),
            (line(),),
            "it ended expectation_violated, which this case doesn't allow",
            id="green_violated",
        ),
        pytest.param(
            PASSING,
            "inconclusive",
            badge("inconclusive"),
            (line(),),
            "it ended inconclusive, which this case doesn't allow",
            id="green_inconclusive",
        ),
        pytest.param(
            VIOLATING,
            "passed",
            badge(),
            (line(),),
            "it ended passed, which this case doesn't allow",
            id="red_passed",
        ),
        pytest.param(
            VIOLATING,
            "expectation_violated",
            badge("expectation_violated"),
            (line(),),
            "it ended expectation_violated with no visual finding",
            id="red_with_no_finding",
        ),
        pytest.param(
            VIOLATING,
            "expectation_violated",
            badge("expectation_violated", finding={**VISUAL, "category": "functional"}),
            (line(),),
            "it ended expectation_violated with no visual finding",
            id="red_with_another_finding",
        ),
    ],
)
def test_the_rule_says_in_fixed_words_why_a_run_is_never_promoted(
    case: VerifiedCase,
    outcome: str,
    document: dict[str, Any],
    costs: tuple[CostRecord, ...],
    words: str,
) -> None:
    assert verified_disallowed(case, outcome, document, costs, GIVEN) == words


@pytest.mark.parametrize(
    ("case", "outcome", "document"),
    [
        pytest.param(PASSING, "passed", badge(), id="green_passed"),
        pytest.param(
            VIOLATING,
            "expectation_violated",
            badge("expectation_violated", finding=VISUAL),
            id="red_violated_with_a_visual_finding",
        ),
    ],
)
def test_the_rule_allows_its_two_endings(
    case: VerifiedCase, outcome: str, document: dict[str, Any]
) -> None:
    assert verified_disallowed(case, outcome, document, (line(),), GIVEN) is None
