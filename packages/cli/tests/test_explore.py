"""`aqa explore <spec> --plan-only` (API.md §7; ADR-0024; #41): the spec, the
project config, the spec root and the start origin resolved before any model
call, one plan call on the navigator role, the plan written to the run record,
and an exit code for every outcome. No browser is launched."""

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import langsmith
import pytest
import typer.testing
from aqa_cli import explore as explore_module
from aqa_cli.main import app
from aqa_core.coverage_plan import CoveragePlan, plan_hash
from aqa_core.model_costs import Usage
from aqa_runner.chat_client import Reply
from aqa_runner.model_router import ModelCallError
from langchain_core.messages import AIMessage
from langsmith.utils import tracing_is_enabled
from playwright._impl._browser_type import BrowserType

from packages.runner.tests.test_model_router import Factory, FakeClient, reply

CONFIG = 'base_url: "http://127.0.0.1:4100"\n'

SPEC = """\
---
id: checkout
goal: A returning user pays with an expired card and is told why it failed.
preconditions:
  start_url: /login
  probes:
    orders_count: "GET /test-api/orders/count"
expect:
  - An error message says the card has expired
  - text: The Pay button is visible and not covered
    visual: deterministic
---

Notes for people.
"""

# A plan for SPEC that covers both expectations.
PLAN = CoveragePlan.model_validate(
    {
        "expectations": [
            {
                "expect_index": 0,
                "subject": "the payment error message",
                "claim": "says the card has expired",
                "checks": [{"check": "text_visible", "text": "card has expired"}],
            },
            {
                "expect_index": 1,
                "subject": "the payment step's submit button",
                "claim": "visible and not covered",
                "checks": [
                    {
                        "check": "visible_unoccluded",
                        "target_meaning": "the payment step's submit button",
                    }
                ],
            },
        ],
        "requires": [],
    }
)

SONNET = "claude-sonnet-5-5"


def project(root: Path, *, config: str = CONFIG, spec: str = SPEC) -> Path:
    """A spec root at `root` holding `config` and the spec `spec`; the spec's
    path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(config)
    path = root / "checkout.spec.md"
    path.write_text(spec)
    return path


def plan_with(entries: list[dict[str, Any]]) -> CoveragePlan:
    return CoveragePlan.model_validate({"expectations": entries, "requires": []})


def unsupported(
    needs: str | None, reason: str = "it is about how it looks"
) -> CoveragePlan:
    """PLAN, with expectation 1 unsupported."""
    first = PLAN.expectations[0].model_dump(exclude_none=True)
    second = {
        "expect_index": 1,
        "subject": "the payment step's submit button",
        "claim": "keeps its brand colour",
        "unsupported": {"reason": reason, "needs": needs},
    }
    return plan_with([first, second])


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way Playwright starts or attaches to a browser, recorded: the
    test fails if any is used."""
    used: list[str] = []
    for name in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):

        def refuse(*_: object, _name: str = name, **__: object) -> None:
            used.append(_name)
            raise AssertionError(f"a browser was started with {_name}")

        monkeypatch.setattr(BrowserType, name, refuse)
    return used


@pytest.fixture(autouse=True)
def tracing_left_as_found() -> Iterator[None]:
    """A router switches LangSmith's tracing off for the whole process; the
    next test starts from the unset state."""
    yield
    langsmith.configure(enabled=None, client=None)


@pytest.fixture
def run(
    monkeypatch: pytest.MonkeyPatch, launches: list[str]
) -> Callable[..., tuple[typer.testing.Result, Factory]]:
    """`run(args, **clients)` runs `aqa explore` in this process, with each
    model scripted by its client, and a fake provider key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")

    def invoke(
        args: list[str], **clients: FakeClient
    ) -> tuple[typer.testing.Result, Factory]:
        factory = Factory(**clients)
        monkeypatch.setattr(explore_module, "AnthropicClient", factory)
        result = typer.testing.CliRunner().invoke(app, ["explore", *args])
        assert launches == []
        return result, factory

    return invoke


Run = Callable[..., tuple[typer.testing.Result, Factory]]


def record_of(root: Path) -> dict[str, Any]:
    """The one plan.json under `root`'s run records."""
    [written] = (root / ".aqa" / "runs").glob("*/plan.json")
    document = json.loads(written.read_text())
    assert isinstance(document, dict)
    return document


def test_plan_only_writes_the_plan_and_its_hash_and_exits_0_without_a_browser(
    tmp_path: Path, run: Run
) -> None:
    spec = project(tmp_path / "qa")

    result, factory = run(
        [str(spec), "--plan-only"], **{SONNET: FakeClient(reply(parsed=PLAN))}
    )

    assert result.exit_code == 0, result.output
    assert factory.built == [("anthropic", SONNET, None)]
    record = record_of(tmp_path / "qa")
    assert record["outcome"] == "planned"
    assert CoveragePlan.model_validate(record["plan"]) == PLAN
    assert record["plan_hash"] == plan_hash(PLAN)
    assert record["spec_id"] == "checkout"
    assert record["spec_hash"].startswith("sha256:")
    [call] = record["calls"]
    assert (call["role"], call["mode"], call["status"]) == (
        "navigator",
        "explore",
        "ok",
    )
    assert plan_hash(PLAN) in result.stdout
    assert str(tmp_path / "qa" / ".aqa" / "runs") in result.stdout


def test_the_plan_is_written_to_a_run_record_that_ignores_itself(
    tmp_path: Path, run: Run
) -> None:
    spec = project(tmp_path / "qa")

    run([str(spec), "--plan-only"], **{SONNET: FakeClient(reply(parsed=PLAN))})

    assert (tmp_path / "qa" / ".aqa" / ".gitignore").read_text() == "*\n"
    [run_dir] = (tmp_path / "qa" / ".aqa" / "runs").iterdir()
    assert record_of(tmp_path / "qa")["run_id"] == run_dir.name


def test_explore_finds_the_spec_root_above_the_spec(tmp_path: Path, run: Run) -> None:
    root = tmp_path / "qa"
    project(root)
    nested = root / "payments" / "checkout.spec.md"
    nested.parent.mkdir()
    (root / "checkout.spec.md").rename(nested)

    result, _ = run(
        [str(nested), "--plan-only"], **{SONNET: FakeClient(reply(parsed=PLAN))}
    )

    assert result.exit_code == 0, result.output
    assert record_of(root)["spec_id"] == "checkout"


def test_an_expectation_the_plan_cannot_cover_exits_5_and_is_named(
    tmp_path: Path, run: Run
) -> None:
    spec = project(tmp_path / "qa")
    plan = unsupported(None)

    result, _ = run(
        [str(spec), "--plan-only"], **{SONNET: FakeClient(reply(parsed=plan))}
    )

    assert result.exit_code == 5, result.output
    assert 'expect[1] "The Pay button is visible and not covered"' in result.stderr
    assert "no M1 check can establish it" in result.stderr
    assert "it is about how it looks" in result.stderr
    assert record_of(tmp_path / "qa")["outcome"] == "spec_error"


@pytest.mark.parametrize("needs", ["pixel_diff", "contrast_min"])
def test_an_expectation_that_needs_an_m2_check_exits_5_and_names_it(
    tmp_path: Path, run: Run, needs: str
) -> None:
    spec = project(tmp_path / "qa")

    result, _ = run(
        [str(spec), "--plan-only"],
        **{SONNET: FakeClient(reply(parsed=unsupported(needs)))},
    )

    assert result.exit_code == 5, result.output
    assert f"it needs {needs}" in result.stderr


def test_a_model_written_reason_is_printed_with_its_control_characters_visible(
    tmp_path: Path, run: Run
) -> None:
    spec = project(tmp_path / "qa")
    plan = unsupported(None, reason="first line\nsecond \x1b[31mred\x1b[0m")

    result, _ = run(
        [str(spec), "--plan-only"], **{SONNET: FakeClient(reply(parsed=plan))}
    )

    assert "first line\\nsecond \\x1b[31mred\\x1b[0m" in result.stderr
    assert "\x1b" not in result.stderr


# Each kind of problem #39's parser reports: the spec as written, the problem
# named in the output.
SPEC_PROBLEMS = {
    "unknown key": (SPEC.replace("goal:", "colour: red\ngoal:"), "colour: unknown key"),
    "repeated key": (
        SPEC.replace("goal:", "id: checkout\ngoal:", 1),
        "duplicate key 'id'",
    ),
    "wrong type": (
        SPEC.replace("expect:\n", "steps: one\nexpect:\n"),
        "steps: must be a list",
    ),
    "missing key": (
        SPEC.replace("goal: A returning", "x-goal: A returning"),
        "goal: missing key",
    ),
    "YAML": (SPEC.replace("id: checkout", "id: [checkout"), "line "),
    "id not the file name": (
        SPEC.replace("id: checkout", "id: payment"),
        "doesn't match the file name",
    ),
    "start_url not a path": (
        SPEC.replace("start_url: /login", "start_url: login"),
        "start_url",
    ),
    "undeclared secret": (
        SPEC.replace(
            "  start_url: /login\n",
            "  start_url: /login\n  account: { password: { secret: NOT_DECLARED } }\n",
        ),
        "secret NOT_DECLARED is not declared",
    ),
    "visual: model": (
        SPEC.replace("visual: deterministic", "visual: model"),
        "visual: model is a spec error",
    ),
    "disable without inherit": (
        SPEC.replace(
            "---\n\nNotes",
            "invariants: { inherit: false, disable: [http_5xx] }\n---\n\nNotes",
        ),
        "disable has no effect",
    ),
}


@pytest.mark.parametrize("problem", sorted(SPEC_PROBLEMS))
def test_each_spec_problem_exits_5_names_it_and_calls_no_model(
    tmp_path: Path, run: Run, problem: str
) -> None:
    text, named = SPEC_PROBLEMS[problem]
    spec = project(tmp_path / "qa", spec=text)

    result, factory = run([str(spec), "--plan-only"])

    assert result.exit_code == 5, result.output
    assert named in result.stderr
    assert factory.built == []
    assert not (tmp_path / "qa" / ".aqa").exists()


def test_a_spec_that_is_not_utf8_exits_5(tmp_path: Path, run: Run) -> None:
    spec = project(tmp_path / "qa")
    spec.write_bytes(SPEC.encode("utf-16"))

    result, factory = run([str(spec), "--plan-only"])

    assert result.exit_code == 5, result.output
    assert "not UTF-8 text" in result.stderr
    assert factory.built == []


def test_a_spec_id_used_twice_in_the_project_exits_5(tmp_path: Path, run: Run) -> None:
    spec = project(tmp_path / "qa")
    (tmp_path / "qa" / "other").mkdir()
    (tmp_path / "qa" / "other" / "checkout.spec.md").write_text(SPEC)

    result, factory = run([str(spec), "--plan-only"])

    assert result.exit_code == 5, result.output
    assert "spec ids are unique in a project" in result.stderr
    assert factory.built == []


def test_a_spec_path_that_is_a_directory_exits_5(tmp_path: Path, run: Run) -> None:
    project(tmp_path / "qa")
    directory = tmp_path / "qa" / "folder.spec.md"
    directory.mkdir()

    result, factory = run([str(directory), "--plan-only"])

    assert result.exit_code == 5, result.output
    assert "a directory, not a file" in result.stderr
    assert factory.built == []


@pytest.mark.parametrize(
    ("config", "args", "named"),
    [
        ("colour: red\n", [], "colour: unknown key"),
        (
            "roles: { navigator: { model: no-such-model } }\n",
            ["--url", "http://127.0.0.1:4100"],
            "roles.navigator",
        ),
        ("", [], "no start origin"),
        (CONFIG, ["--url", "http://127.0.0.1:4100/shop"], "--url"),
    ],
)
def test_a_config_error_exits_5_before_any_model_call(
    tmp_path: Path, run: Run, config: str, args: list[str], named: str
) -> None:
    spec = project(tmp_path / "qa", config=config)

    result, factory = run([str(spec), "--plan-only", *args])

    assert result.exit_code == 5, result.output
    assert named in result.stderr
    assert factory.built == []
    assert not (tmp_path / "qa" / ".aqa").exists()


def test_a_spec_with_no_project_config_above_it_exits_5(
    tmp_path: Path, run: Run
) -> None:
    spec = tmp_path / "loose" / "checkout.spec.md"
    spec.parent.mkdir()
    spec.write_text(SPEC)

    result, factory = run([str(spec), "--plan-only"])

    assert result.exit_code == 5, result.output
    assert "no config.yaml" in result.stderr
    assert factory.built == []


@pytest.mark.parametrize("name", ["notes.md", "missing.spec.md"])
def test_a_path_that_is_not_a_spec_of_the_project_exits_5(
    tmp_path: Path, run: Run, name: str
) -> None:
    project(tmp_path / "qa")
    path = tmp_path / "qa" / name
    if name == "notes.md":
        path.write_text(SPEC)

    result, factory = run([str(path), "--plan-only"])

    assert result.exit_code == 5, result.output
    assert str(path) in result.stderr
    assert factory.built == []


def test_no_provider_key_exits_5_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run: Run
) -> None:
    spec = project(tmp_path / "qa")
    factory_run = run
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    result, factory = factory_run([str(spec), "--plan-only"])

    assert result.exit_code == 5, result.output
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert factory.built == []


def test_explore_without_plan_only_is_refused_before_any_model_call(
    tmp_path: Path, run: Run
) -> None:
    spec = project(tmp_path / "qa")

    result, factory = run([str(spec)])

    assert result.exit_code == 2, result.output
    assert "--plan-only" in result.stderr
    assert factory.built == []


def test_a_refused_plan_exits_3(tmp_path: Path, run: Run) -> None:
    spec = project(tmp_path / "qa")

    result, _ = run(
        [str(spec), "--plan-only"], **{SONNET: FakeClient(reply(refused=True))}
    )

    assert result.exit_code == 3, result.output
    assert "refused" in result.stderr
    record = record_of(tmp_path / "qa")
    assert (record["outcome"], record["plan"]) == ("gave_up", None)
    assert [call["status"] for call in record["calls"]] == ["refusal"]


def test_a_plan_that_does_not_parse_exits_3(tmp_path: Path, run: Run) -> None:
    spec = project(tmp_path / "qa")

    result, _ = run(
        [str(spec), "--plan-only"], **{SONNET: FakeClient(reply(parsed=None))}
    )

    assert result.exit_code == 3, result.output
    assert "didn't parse" in result.stderr
    assert [call["status"] for call in record_of(tmp_path / "qa")["calls"]] == [
        "invalid"
    ]


def test_a_plan_cut_off_at_the_output_bound_exits_3_and_says_so(
    tmp_path: Path, run: Run
) -> None:
    spec = project(tmp_path / "qa")
    cut_off = Reply(
        message=AIMessage(
            content='{"expectations": [',
            response_metadata={"stop_reason": "max_tokens"},
        ),
        usage=Usage(input_tokens=2000, cached_input_tokens=0, output_tokens=4096),
        refused=False,
        parsed=None,
    )

    result, _ = run([str(spec), "--plan-only"], **{SONNET: FakeClient(cut_off)})

    assert result.exit_code == 3, result.output
    assert "cut off at the 4096-token bound" in result.stderr


def test_a_plan_that_does_not_fit_its_spec_exits_3(tmp_path: Path, run: Run) -> None:
    spec = project(tmp_path / "qa")
    short = plan_with([PLAN.expectations[0].model_dump(exclude_none=True)])

    result, _ = run(
        [str(spec), "--plan-only"], **{SONNET: FakeClient(reply(parsed=short))}
    )

    assert result.exit_code == 3, result.output
    assert "2 expectations" in result.stderr
    assert record_of(tmp_path / "qa")["plan"] is not None


def test_a_provider_that_gives_no_response_exits_11(tmp_path: Path, run: Run) -> None:
    spec = project(tmp_path / "qa")
    timeout = anthropic.APITimeoutError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )

    result, _ = run([str(spec), "--plan-only"], **{SONNET: FakeClient(timeout)})

    assert result.exit_code == 11, result.output
    assert "no response" in result.stderr
    # Nothing was billed, so nothing is recorded.
    assert not (tmp_path / "qa" / ".aqa").exists()


def test_a_fallback_that_gives_no_response_exits_11_and_keeps_the_refusal(
    tmp_path: Path, run: Run
) -> None:
    config = CONFIG + "roles: { navigator: { fallback: claude-opus-5-5 } }\n"
    spec = project(tmp_path / "qa", config=config)
    timeout = anthropic.APITimeoutError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )

    result, _ = run(
        [str(spec), "--plan-only"],
        **{
            SONNET: FakeClient(reply(refused=True)),
            "claude-opus-5-5": FakeClient(timeout),
        },
    )

    assert result.exit_code == 11, result.output
    record = record_of(tmp_path / "qa")
    assert [call["status"] for call in record["calls"]] == ["refusal"]


def test_a_fallback_that_fails_otherwise_is_not_called_no_response(
    tmp_path: Path, run: Run
) -> None:
    # Only a provider's failure is "no response" (exit 11); anything else is a
    # fault of ours, raised as it is, with the billed refusal still recorded.
    config = CONFIG + "roles: { navigator: { fallback: claude-opus-5-5 } }\n"
    spec = project(tmp_path / "qa", config=config)

    result, _ = run(
        [str(spec), "--plan-only"],
        **{
            SONNET: FakeClient(reply(refused=True)),
            "claude-opus-5-5": FakeClient(ValueError("a fault of ours")),
        },
    )

    assert isinstance(result.exception, ModelCallError)
    assert result.exit_code not in {0, 3, 5, 11}
    assert [call["status"] for call in record_of(tmp_path / "qa")["calls"]] == [
        "refusal"
    ]


def test_explore_switches_ambient_tracing_off_before_its_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run: Run
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    spec = project(tmp_path / "qa")
    seen: list[object] = []

    class Watching(FakeClient):
        async def call(self, *args: Any) -> Reply:
            seen.append(tracing_is_enabled())
            return await super().call(*args)

    run([str(spec), "--plan-only"], **{SONNET: Watching(reply(parsed=PLAN))})

    assert seen == [False]


def test_the_console_script_exits_5_on_a_spec_error(aqa: str, tmp_path: Path) -> None:
    spec = project(tmp_path / "qa", spec=SPEC.replace("goal:", "colour: red\ngoal:"))
    environment = {**os.environ, "ANTHROPIC_API_KEY": "fake-key-for-tests"}

    result = subprocess.run(
        [aqa, "explore", str(spec), "--plan-only"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )

    assert result.returncode == 5, result.stderr
    assert "colour: unknown key" in result.stderr
