"""`aqa run <spec>...` (API.md §7; ADR-0024's #244 amendment): each spec's
committed script replayed with every locator, a drift healed on a scripted model
into a proposal a person applies, and the exit code rolled up across specs. Real
Chromium against #240's drift pages. `test_heal*.py` hold the heal's rules and
`test_replay_result_heals.py` the verdicts'; these hold the command."""

import difflib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import langsmith
import pytest
import typer.testing
from aqa_cli import common
from aqa_cli import run as run_module
from aqa_cli.main import app as aqa
from aqa_core.compiled import CompiledScript
from aqa_core.heal_patch import heal_patch_problems
from aqa_core.model_roles import resolve_roles
from aqa_core.price_map import vendored
from aqa_core.project import load_project, parse_compiled
from aqa_runner.publication import published_text
from aqa_runner.strict_replay import Replayed
from playwright._impl._browser_type import BrowserType

from packages.runner.tests.drift_fixtures import (
    BUTTON,
    DETAILS,
    IN_VIEW,
    SHIPPING,
    SUMMARY,
    TOGGLE,
    order_spec,
    page,
    serving,
)
from packages.runner.tests.executor_fixtures import App, compiled, for_spec
from packages.runner.tests.explore_graph_fixtures import (
    NO_RESPONSE,
    Move,
    Scripted,
    calling,
)
from packages.runner.tests.heal_fixtures import NO_POINT, NO_TOOL, rebinding
from packages.runner.tests.secret_fixtures import (
    BOUND_AT_START,
    OTHER_FAKE_VALUE,
    copies_found,
)

MOVED = page("moved")
RENAMED = page("renamed")
ORDER = page("order")
KEY = "fake-key-for-aqa-run-tests"
GIT = shutil.which("git") or "git"
CONFIG = "budgets: { resolve_seconds: 2 }\n"
TOGGLE_MEANING = "the button that shows the order's details"
# The toggle's drift, then the summary's, each its own repair.
THE_SUMMARY_DRIFTS_TOO = compiled(
    [{"seq": 1, "action": "click", "target": "toggle", "side_effect": False}],
    targets={
        "toggle": TOGGLE,
        "summary": {**SUMMARY, "locators": [{"css": "#summary"}, {"css": "#gone"}]},
    },
    assertions=DETAILS.model_dump(mode="json", exclude_none=True)["assertions"],
)


@dataclass(frozen=True)
class Drifted:
    """A drift page, what the healer picks on it, how the run says it bound that,
    and the locators the generator writes for it."""

    html: str
    pick: Move
    bound: str
    locators: list[dict[str, Any]]


BOTH = [
    pytest.param(
        Drifted(
            MOVED,
            rebinding("button", "Show details"),
            'button "Show details"',
            [
                {"role": "button", "name": "Show details"},
                {"css": "button", "scope": {"css": "#order"}},
            ],
        ),
        id="moved",
    ),
    pytest.param(
        Drifted(
            RENAMED,
            rebinding("button", "View details"),
            'button "View details"',
            [
                {"role": "button", "name": "View details"},
                {"css": "button", "scope": {"css": "#toolbar"}},
            ],
        ),
        id="renamed",
    ),
]


@pytest.fixture(autouse=True)
def provider_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)


def order_at(
    root: Path,
    script: CompiledScript = DETAILS,
    expectation: str = SHIPPING,
    config: str = CONFIG,
) -> Path:
    """The order spec under `root`, with `script` compiled from it and committed
    as published. The spec's path."""
    path = order_spec(root, expectation)
    (root / "config.yaml").write_text(config)
    compiled = root / ".compiled"
    compiled.mkdir(exist_ok=True)
    spec = load_project(root).specs["order"]
    (compiled / "order.json").write_text(published_text(for_spec(script, spec)))
    return path


def compiled_of(spec: Path) -> Path:
    return spec.parent / ".compiled" / "order.json"


def run(
    monkeypatch: pytest.MonkeyPatch,
    args: Sequence[str],
    client: Scripted | None = None,
    *,
    browser: bool = True,
) -> typer.testing.Result:
    """`aqa run <args>` in this process, its model `client`. Without `browser`,
    the test fails if Playwright's driver starts."""
    monkeypatch.setattr(run_module, "AnthropicClient", client or Scripted())
    used: list[str] = []

    def refuse(name: str) -> Callable[..., NoReturn]:
        def refused(*_: object, **__: object) -> NoReturn:
            used.append(name)
            raise AssertionError(f"{name} was started")

        return refused

    if not browser:
        monkeypatch.setattr(BrowserType, "launch", refuse("launch"))
        monkeypatch.setattr(common, "async_playwright", refuse("the driver"))
    result = typer.testing.CliRunner().invoke(aqa, ["run", *args])
    assert used == []
    return result


def replay_json(spec: Path) -> dict[str, Any]:
    [record] = (spec.parent / ".aqa" / "runs").iterdir()
    loaded: dict[str, Any] = json.loads((record / "replay.json").read_text())
    return loaded


def record_of(spec: Path) -> Path:
    [record] = (spec.parent / ".aqa" / "runs").iterdir()
    return record


def printed_diff(result: typer.testing.Result) -> list[str]:
    """The unified diff a run printed of its proposal, as its lines were."""
    lines = [line.removeprefix("  ") for line in result.stderr.splitlines()]
    return lines[lines.index("--- a/.compiled/order.json") :]


def cited_paths_all_exist(spec: Path) -> None:
    record = record_of(spec)
    for verdict in replay_json(spec)["verdicts"]:
        if (heal := verdict["heal"]) is not None:
            for path in (heal["repair"], heal["record"]):
                assert path is None or (record / path).exists(), path


def assert_healed_and_applies(
    spec: Path,
    app: App,
    result: typer.testing.Result,
    case: Drifted,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run proposed `case`'s locators, said so, left the committed file alone,
    and `aqa heal apply` changes only those locators, by the diff the run printed,
    into a script the validator takes that replays clean with every locator and no
    model."""
    before = compiled_of(spec).read_text()
    assert result.exit_code == 7, result.output
    document = replay_json(spec)
    assert (document["mode"], document["outcome"], document["exit_code"]) == (
        "run",
        "drift_consistent",
        7,
    )
    proposal = document["heal_proposal"]
    assert proposal["patch"] == [
        {"op": "replace", "path": "/targets/toggle/locators", "value": case.locators}
    ]
    assert proposal["compiled"] == ".compiled/order.json"
    assert result.stdout == ""
    # The run viewer's command comes before the proposal, which ends the block.
    assert (
        f"  run viewer: aqa report {record_of(spec).name}\n"
        f"  heal proposal {proposal['id']}:\n"
    ) in result.stderr
    assert f"  toggle ({TOGGLE_MEANING}): bound {case.bound}\n" in result.stderr
    assert "  --- a/.compiled/order.json\n" in result.stderr
    assert "  +++ b/.compiled/order.json\n" in result.stderr
    assert compiled_of(spec).read_text() == before
    cited_paths_all_exist(spec)
    monkeypatch.chdir(spec.parent)
    applied = typer.testing.CliRunner().invoke(aqa, ["heal", "apply", proposal["id"]])
    assert applied.exit_code == 0, applied.output
    after = compiled_of(spec).read_text()
    expected = json.loads(before)
    expected["targets"]["toggle"]["locators"] = case.locators
    assert json.loads(after) == expected
    assert printed_diff(result) == list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            "a/.compiled/order.json",
            "b/.compiled/order.json",
            lineterm="",
        )
    )
    config = load_project(spec.parent).config
    assert (
        heal_patch_problems(
            parse_compiled(before, config, source=compiled_of(spec)),
            parse_compiled(after, config, source=compiled_of(spec)),
        )
        == []
    )
    replayed = typer.testing.CliRunner().invoke(
        aqa, ["replay", str(spec), "--url", app.origin]
    )
    assert replayed.exit_code == 0, replayed.output


@pytest.mark.parametrize("case", BOTH)
def test_a_drifted_step_heals_into_a_proposal_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Drifted
) -> None:
    spec = order_at(tmp_path / "qa")

    with serving(case.html) as app:
        result = run(monkeypatch, [str(spec), "--url", app.origin], Scripted(case.pick))
        assert_healed_and_applies(spec, app, result, case, monkeypatch)


@pytest.mark.parametrize("case", BOTH)
def test_a_drifted_assertion_target_heals_into_a_proposal_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Drifted
) -> None:
    spec = order_at(tmp_path / "qa", BUTTON, IN_VIEW)

    with serving(case.html) as app:
        result = run(monkeypatch, [str(spec), "--url", app.origin], Scripted(case.pick))
        assert (record_of(spec) / "repairs/assertion-0.json").exists()
        assert_healed_and_applies(spec, app, result, case, monkeypatch)


def test_a_run_line_says_the_outcome_time_cost_and_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")

    with serving(MOVED) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(rebinding("button", "Show details")),
        )

    first = result.stderr.splitlines()[0]
    assert first.startswith("drift_consistent order: ")
    # One scripted response costs $0.003 at the healer's prices.
    assert f", $0.003, run record: {record_of(spec)}" in first


def test_a_run_that_heals_keeps_its_run_json_with_the_healers_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")

    with serving(MOVED) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(rebinding("button", "Show details")),
        )

    assert result.exit_code == 7, result.output
    record = record_of(spec)
    kept = json.loads((record / "run.json").read_text())
    assert (kept["schema_version"], kept["run_id"], kept["spec_id"]) == (
        1,
        record.name,
        "order",
    )
    assert kept["goal"] == "A customer finds out when their order ships."
    assert kept["expectations"] == [SHIPPING]
    healer = resolve_roles(load_project(spec.parent).config, vendored())["healer"]
    assert kept["models"] == {
        "healer": {
            "provider": "anthropic",
            "model": healer.model.name,
            "effort": healer.effort,
            "fallback": None,
        }
    }
    committed = compiled_of(spec).read_text()
    assert CompiledScript.model_validate_json(
        json.dumps(kept["script"])
    ) == CompiledScript.model_validate_json(committed)
    assert json.dumps(kept["script"], indent=2, ensure_ascii=False) + "\n" == committed
    assert result.stderr.startswith(
        f"drift_consistent order: {kept['duration_seconds']:.1f}s, $0.003, "
    )


def test_a_run_with_no_drift_passes_and_builds_no_model_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")
    client = Scripted()

    with serving(ORDER) as app:
        result = run(monkeypatch, [str(spec), "--url", app.origin], client)

    assert result.exit_code == 0, result.output
    assert (client.models, client.requests) == ([], [])
    assert result.stderr == ""
    first, hint = result.stdout.splitlines()
    assert first.startswith("passed order: ")
    assert first.endswith(", $0")
    assert hint == f"  run viewer: aqa report {record_of(spec).name}"
    document = replay_json(spec)
    assert (document["mode"], document["heal_proposal"]) == ("run", None)
    assert all(verdict["heal"] is None for verdict in document["verdicts"])


def test_run_without_the_providers_key_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    spec = order_at(tmp_path / "qa")

    result = run(monkeypatch, [str(spec), "--url", "http://127.0.0.1:9"], browser=False)

    assert result.exit_code == 5
    assert "ANTHROPIC_API_KEY: not set" in result.stderr
    assert not (spec.parent / ".aqa").exists()


def test_run_needs_at_least_one_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(monkeypatch, [], browser=False)

    assert result.exit_code == 2


@pytest.mark.parametrize("flag", ["--json", "--quiet", "--budget"])
def test_run_takes_only_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    spec = order_at(tmp_path / "qa")

    result = run(monkeypatch, [str(spec), flag], browser=False)

    assert result.exit_code == 2


def test_run_falls_back_to_the_configs_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(ORDER) as app:
        config = f"base_url: '{app.origin}'\n{CONFIG}"
        spec = order_at(tmp_path / "qa", config=config)
        result = run(monkeypatch, [str(spec)], Scripted())

    assert result.exit_code == 0, result.output


def test_a_spec_with_no_compiled_script_exits_5_before_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")
    compiled_of(spec).unlink()

    result = run(monkeypatch, [str(spec), "--url", "http://127.0.0.1:9"], browser=False)

    assert result.exit_code == 5
    assert "no compiled script: explore it first" in result.stderr


def test_a_heal_out_of_budget_is_inconclusive_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = "budgets: { resolve_seconds: 2, heal_turns: 1 }\n"
    spec = order_at(tmp_path / "qa", config=config)

    with serving(MOVED) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(calling("nonsense", {}), calling("nonsense", {})),
        )

    assert result.exit_code == 3, result.output
    document = replay_json(spec)
    assert document["outcome"] == "inconclusive"
    assert document["heal_proposal"] is None
    assert "the turn budget (1) ran out" in result.stderr
    cited_paths_all_exist(spec)


def test_heal_model_usd_below_one_response_stops_the_heal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = "budgets: { resolve_seconds: 2, heal_model_usd: 0.001 }\n"
    spec = order_at(tmp_path / "qa", config=config)

    with serving(MOVED) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(rebinding("button", "Show details")),
        )

    assert result.exit_code == 3, result.output
    assert replay_json(spec)["heal_proposal"] is None
    assert "the spend budget ran out on this turn, so its call didn't run" in (
        result.stderr
    )


def test_a_run_whose_record_cant_be_made_exits_15_and_says_it_was_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`aqa run` prints no document yet, so what it holds is read where it ends."""
    spec = order_at(tmp_path / "qa")
    (spec.parent / ".aqa").write_text("not a directory\n")
    ended: list[Replayed] = []

    def keeping(*args: Any, **kwargs: Any) -> list[Replayed]:
        ended.extend(common.replayed_runs(*args, **kwargs))
        return ended

    monkeypatch.setattr(run_module, "replayed_runs", keeping)

    with serving(ORDER) as app:
        result = run(monkeypatch, [str(spec), "--url", app.origin])

    assert result.exit_code == 15, result.output
    assert re.fullmatch(
        r"record_error order: \d+\.\ds, \$0\n"
        "  the run record could not be created; nothing was replayed\n",
        result.stderr,
    )
    assert app.seen == []
    assert ended[0].document == {
        "schema_version": 1,
        "run_id": None,
        "spec_id": "order",
        "mode": "run",
        "outcome": "record_error",
        "exit_code": 15,
        "withheld": False,
        "error_code": None,
        "reasons": ["the run record could not be created; nothing was replayed"],
        "non_resumable": [],
        "infrastructure_events": {"count": 0, "events": []},
        "verdicts": [],
        "heal_proposal": None,
    }


def test_two_repairs_on_two_targets_each_say_what_they_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa", THE_SUMMARY_DRIFTS_TOO)

    with serving(MOVED) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(
                rebinding("button", "Show details"),
                rebinding("paragraph", "Order 42 ships on Monday"),
            ),
        )

    assert result.exit_code == 7, result.output
    assert (
        f'  toggle ({TOGGLE_MEANING}): bound button "Show details"\n' in result.stderr
    )
    assert (
        "  summary (the order's shipping summary): bound paragraph\n" in result.stderr
    )


def test_a_model_that_gives_no_response_exits_11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")

    with serving(MOVED) as app:
        result = run(
            monkeypatch, [str(spec), "--url", app.origin], Scripted(NO_RESPONSE)
        )

    assert result.exit_code == 11, result.output
    assert replay_json(spec)["outcome"] == "no_response"
    assert "the model gave no response (APIConnectionError)" in result.stderr


def test_a_repaired_check_that_fails_exits_1_with_no_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")

    with serving(MOVED.replace("Monday", "Friday")) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(rebinding("button", "Show details")),
        )

    assert result.exit_code == 1, result.output
    assert replay_json(spec)["heal_proposal"] is None
    assert "heal proposal" not in result.stderr


def test_the_removed_page_with_no_pick_exits_3_with_no_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")

    with serving(page("removed")) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(NO_TOOL, NO_POINT),
        )

    assert result.exit_code == 3, result.output
    assert result.stderr.startswith("inconclusive order: ")
    assert replay_json(spec)["heal_proposal"] is None
    assert "heal proposal" not in result.stderr


def test_each_spec_has_a_budget_of_its_own_and_the_exit_code_rolls_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = "budgets: { resolve_seconds: 2, heal_turns: 1 }\n"
    first = order_at(tmp_path / "one", config=config)
    second = order_at(tmp_path / "two", config=config)
    show = rebinding("button", "Show details")

    with serving(MOVED) as app:
        result = run(
            monkeypatch,
            [str(first), str(second), "--url", app.origin],
            Scripted(show, show),
        )

    assert result.exit_code == 7, result.output
    assert result.stderr.count("drift_consistent order: ") == 2


def test_the_proposal_cites_the_blob_git_gives_the_file_as_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = order_at(tmp_path / "qa")
    compiled = compiled_of(spec)
    compiled.write_bytes(b"\xef\xbb\xbf" + compiled.read_bytes())
    git = subprocess.run(
        [GIT, "hash-object", "--no-filters", str(compiled)],
        capture_output=True,
        text=True,
        check=True,
    )

    with serving(MOVED) as app:
        result = run(
            monkeypatch,
            [str(spec), "--url", app.origin],
            Scripted(rebinding("button", "Show details")),
        )

    assert result.exit_code == 7, result.output
    assert replay_json(spec)["heal_proposal"]["base"] == {
        "blob_sha": git.stdout.strip(),
        "pr_head_sha": None,
    }


def test_a_bound_value_on_the_page_reaches_no_output_or_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", OTHER_FAKE_VALUE)
    config = f"{CONFIG}secrets: {{ {BOUND_AT_START} }}\n"
    root = tmp_path / "qa"
    path = order_spec(root, SHIPPING)
    account = "{ email: reader@example.test, password: { secret: TEST_PASSWORD } }"
    path.write_text(
        path.read_text().replace(
            "preconditions:\n", f"preconditions:\n  account: {account}\n"
        )
    )
    (root / "config.yaml").write_text(config)
    (root / ".compiled").mkdir()
    spec = load_project(root).specs["order"]
    (root / ".compiled/order.json").write_text(published_text(for_spec(DETAILS, spec)))
    html = MOVED.replace(
        "<h1>Order 42</h1>", f"<h1>Order 42</h1><p>{OTHER_FAKE_VALUE}</p>"
    )
    client = Scripted(rebinding("button", "Show details"))

    with serving(html) as app:
        result = run(monkeypatch, [str(path), "--url", app.origin], client)

    assert result.exit_code == 7, result.output
    files = [each for each in (root / ".aqa").rglob("*") if each.is_file()]
    assert record_of(path) / "run.json" in files
    sent = [request[-1].text for request in client.requests]
    assert (
        copies_found(OTHER_FAKE_VALUE, files=files, texts=[result.output, *sent]) == []
    )
