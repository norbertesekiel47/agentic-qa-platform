"""The awaiting set, proven both ways (#327; ADR-0024's #327 amendment):
`AWAITING_RERECORD` names exactly the recorder's cases whose cassette opens
with a plan request that is no longer the one the adapter sends today, and a
name in it whose cassette replays again fails its test, so no name outlives
its recording. Today's request is the body the real adapter sends a local
stand-in for the provider, with a fake key, for the spec the recorder builds.
No test here reaches a provider."""

import asyncio
import copy
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from aqa_core.project import load_config
from aqa_runner.anthropic_client import AnthropicClient, ProviderError
from aqa_runner.coverage_plan import make_plan
from aqa_runner.model_router import ModelRouter

from packages.runner.tests.conftest import (
    AWAITED,
    AWAITING_RERECORD,
    CASSETTES,
    Recording,
    prompt_hash_matches,
)
from packages.runner.tests.m2_plan_fixtures import CASE as M2_CASE
from packages.runner.tests.m2_plan_fixtures import m2_spec
from packages.runner.tests.provider_stand_in import Start, answer, planned, standing_in
from packages.runner.tests.record_cassettes import (
    CASES,
    EXPLORE,
    INJECTED,
    case_spec,
    plan_case,
    record,
)
from packages.runner.tests.test_coverage_plan import (
    PLAN,
    checkout,
    plan_through_the_adapter,
)

pytestmark = pytest.mark.usefixtures("reset_tracing")

Cassette = Callable[..., AbstractContextManager[Recording]]

# The recorder's cases whose cassette opens with a plan request: the plan
# cases, the fixture explore and the injection explores, which replay its
# plan. The navigator's instructions and tools are unchanged (#327), so only
# that first request is compared.
OPENS_WITH_A_PLAN = tuple(
    name for name in CASES if plan_case(name) or name == EXPLORE or name in INJECTED
)
# An answer the plan call keeps as it is: no plan, so no retry.
NO_PLAN = answer(0, [{"type": "text", "text": "not a plan"}], "end_turn")


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Start]:
    with standing_in(monkeypatch) as start:
        yield start


def todays_request(name: str, root: Path, provider: Start) -> bytes:
    """The plan request the real adapter sends today for the case `name`,
    for the spec the recorder builds for it under `root`."""
    spec = case_spec(name, root)
    config = load_config(spec.path.parent / "config.yaml")
    stand_in = provider([NO_PLAN])
    asyncio.run(make_plan(ModelRouter.from_config(config, AnthropicClient), spec))
    return stand_in.sent[0]


def replays(name: str, library: Path, provider: Start, root: Path) -> bool:
    """Whether `name`'s cassette in `library` opens with today's plan
    request, by the replay's own matcher, the prompt hash. An absent
    cassette doesn't."""
    path = library / f"{name}.yaml"
    if not path.is_file():
        return False
    first = yaml.safe_load(path.read_text())["interactions"][0]["request"]["body"]
    sent = todays_request(name, root, provider)
    return prompt_hash_matches(SimpleNamespace(body=sent), SimpleNamespace(body=first))


def test_the_awaiting_set_is_exactly_the_cases_whose_plan_request_changed(
    provider: Start, tmp_path: Path
) -> None:
    differing = {
        name
        for name in OPENS_WITH_A_PLAN
        if not replays(name, CASSETTES, provider, tmp_path / name)
    }

    assert differing == AWAITING_RERECORD
    assert CASES.keys() >= AWAITING_RERECORD


def recorded_for_today(provider: Start, tmp_path: Path) -> Path:
    """A library holding `coverage_plan` recorded against the stand-in for
    today's request, as the recorder records it."""
    library = tmp_path / "library"
    library.mkdir()
    provider([planned(0, PLAN.model_dump(mode="json", exclude_none=True))])
    record("coverage_plan", tmp_path / "round" / "001", library=library)
    return library


def test_a_cassette_recorded_for_todays_request_matches_and_replays_again(
    provider: Start, cassette: Cassette, tmp_path: Path
) -> None:
    library = recorded_for_today(provider, tmp_path)

    assert replays("coverage_plan", library, provider, tmp_path / "today")
    with (
        pytest.raises(
            AssertionError,
            match=(
                r"^cassettes/coverage_plan\.yaml replays again: remove it from "
                r"AWAITING_RERECORD$"
            ),
        ),
        cassette("coverage_plan", replay_only=True, library=library),
    ):
        plan_through_the_adapter(checkout(tmp_path / "replayed" / "qa"))


def with_an_unplayed_answer(library: Path) -> Path:
    """`library`'s `coverage_plan`, with a second interaction whose request
    no replay sends."""
    path = library / "coverage_plan.yaml"
    cassette: dict[str, Any] = yaml.safe_load(path.read_text())
    extra = copy.deepcopy(cassette["interactions"][0])
    body = json.loads(extra["request"]["body"])
    body["system"] = "a request no replay sends"
    extra["request"]["body"] = json.dumps(body)
    cassette["interactions"].append(extra)
    path.write_text(yaml.safe_dump(cassette))
    return library


@pytest.mark.parametrize("how", ["no match", "no file", "a response unplayed"])
def test_an_awaiting_cassette_that_does_not_replay_is_expected_to_fail(
    provider: Start, cassette: Cassette, tmp_path: Path, how: str
) -> None:
    if how == "no match":
        name, library = "coverage_plan", CASSETTES
    elif how == "no file":
        name, library = M2_CASE, CASSETTES
    else:
        name = "coverage_plan"
        library = with_an_unplayed_answer(recorded_for_today(provider, tmp_path))
    root = tmp_path / "replayed" / "qa"
    spec = m2_spec(root) if name == M2_CASE else checkout(root)

    with (
        pytest.raises(AWAITED, match=rf"^cassettes/{name}\.yaml awaits "),
        cassette(name, replay_only=True, library=library),
    ):
        plan_through_the_adapter(spec)


def played_then_caught(tmp_path: Path) -> None:
    """Replay today's `coverage_plan` request, then send a request with no
    match and catch its refusal, as explore catches a provider's error."""
    plan_through_the_adapter(checkout(tmp_path / "replayed" / "qa"))
    with pytest.raises(ProviderError):
        plan_through_the_adapter(m2_spec(tmp_path / "other" / "qa"))


def test_an_awaiting_cassette_whose_refused_request_was_caught_is_expected_to_fail(
    provider: Start, cassette: Cassette, tmp_path: Path
) -> None:
    library = recorded_for_today(provider, tmp_path)

    with (
        pytest.raises(AWAITED, match=r"^cassettes/coverage_plan\.yaml awaits "),
        cassette("coverage_plan", replay_only=True, library=library),
    ):
        played_then_caught(tmp_path)
