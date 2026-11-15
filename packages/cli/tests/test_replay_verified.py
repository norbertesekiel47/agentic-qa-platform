"""`aqa replay --mode verified` (API.md §7; ADR-0024's #256 amendment): every
check strict replay runs, and each `model_verify` check judged by the verifier
role on the page's masked screenshot, on a scripted model; and strict
admission's refusal of a `visual: model` spec everywhere else. Real Chromium
against the executor's fixture app. `packages/runner/tests/test_verifier.py`
holds the request and the verdict schema, and `test_verified_cassette.py` the
real adapter."""

import asyncio
import base64
import json
import re
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import anthropic
import langsmith
import pytest
import typer.testing
from aqa_cli import replay as replay_module
from aqa_cli.main import app as aqa
from aqa_core.compiled import ModelVerify
from aqa_core.config import ProjectConfig
from aqa_core.project import load_project
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.browser_session import BrowserSession
from aqa_runner.evaluation import NO_VERIFIER, AssertionResult, Verified
from aqa_runner.model_router import ModelRouter
from aqa_runner.model_turns import Spending
from aqa_runner.run_record import RunRecord
from aqa_runner.verification import Verifying
from langchain_anthropic import ChatAnthropic
from playwright.async_api import CDPSession, ElementHandle, Error

from packages.cli.tests.test_explore_runs import shop, unlaunched
from packages.cli.tests.test_replay import committed
from packages.runner.tests.executor_fixtures import (
    App,
    compiled,
    serving_app,
    shop_script,
)
from packages.runner.tests.explore_graph_fixtures import (
    NO_RESPONSE,
    REFUSED,
    Scripted,
)
from packages.runner.tests.test_evaluate import evaluated
from packages.runner.tests.verified_fixtures import (
    BADGE,
    CHECKED,
    DRIFTED,
    EXPECTATION,
    MODEL_VERIFY,
    TWICE,
    badge_spec,
    evidence,
    page,
    replying,
    serving,
)

KEY = "fake-key-for-aqa-replay-verified-tests"
FOLDER = "verified/assertion-0"
SHOT = f"{FOLDER}/screenshot.png"
SECONDS = r"\d+\.\ds"
# The badge's verdict once the verifier said nothing it decides.
INTERRUPTED = "the run was interrupted, so the verifier wasn't called"
SILENCED = "an earlier verifier call got no response, so the verifier wasn't called"
SPENT = "the verified budget ran out, so the verifier wasn't called"
NO_REF = (
    "the target's element has no ref in the page's snapshot, so the verifier "
    "wasn't called"
)
WITHHELD = "the screenshot was withheld, so the verifier wasn't called"
MISCITED = (
    "the verifier's reply didn't cite exactly the screenshot and the target's "
    "element ref, so it decides nothing"
)
UNJUDGED = "the verifier couldn't judge it from the screenshot"
# A count no ref is given: an inline span, which the snapshot folds into the
# button's line (LAB_NOTES, 2026-10-02).
FOLDED = '<button id="like">Like <span id="count">3</span></button>'
COUNT = compiled(
    [],
    targets={"badge": {**BADGE, "locators": [{"css": "#count"}]}},
    assertions=[{"id": "a1", **MODEL_VERIFY}],
)


@pytest.fixture(autouse=True)
def provider_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)


def invoke(args: Sequence[str]) -> typer.testing.Result:
    return typer.testing.CliRunner().invoke(aqa, list(args))


def verified(
    monkeypatch: pytest.MonkeyPatch,
    app: App,
    spec: Path,
    client: Scripted,
    *more: str,
) -> typer.testing.Result:
    """`aqa replay --mode verified` of `spec` on `app`, its model `client`."""
    monkeypatch.setattr(replay_module, "AnthropicClient", client)
    return invoke(
        ["replay", "--mode", "verified", str(spec), "--url", app.origin, *more]
    )


def record_in(spec: Path) -> Path:
    [record] = (spec.parent / ".aqa" / "runs").iterdir()
    return record


def document(spec: Path) -> dict[str, Any]:
    written: dict[str, Any] = json.loads((record_in(spec) / "replay.json").read_text())
    return written


def verdict(spec: Path, check: str = "a1") -> dict[str, Any]:
    [found] = [v for v in document(spec)["verdicts"] if v.get("assertion") == check]
    assert isinstance(found, dict)
    return found


def refusing_constructors(
    monkeypatch: pytest.MonkeyPatch, clients: list[type]
) -> list[str]:
    """Each constructor of `clients` refused from now on, as `test_replay.py`'s
    guard refuses them; the names of those a test then built."""
    built: list[str] = []

    class ModelClientBuiltError(Exception):
        pass

    def refuse(name: str) -> Callable[..., None]:
        def constructor(*_: object, **__: object) -> None:
            built.append(name)
            raise ModelClientBuiltError(name)

        return constructor

    for client in clients:
        monkeypatch.setattr(client, "__init__", refuse(client.__name__))
    # The control: building any of them now is caught.
    for client in clients:
        with pytest.raises(ModelClientBuiltError):
            client()
    built.clear()
    return built


@pytest.fixture
def app() -> Iterator[App]:
    with serving(page("green")) as served:
        yield served


# Strict admission, everywhere but `aqa replay --mode verified`.


@pytest.mark.parametrize("command", ["replay", "run", "heal"])
def test_strict_admission_refuses_a_visual_model_spec_before_anything_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    built = refusing_constructors(
        monkeypatch,
        [
            ModelRouter,
            AnthropicClient,
            ChatAnthropic,
            anthropic.Anthropic,
            anthropic.AsyncAnthropic,
        ],
    )
    spec = badge_spec(tmp_path / "qa")

    result = invoke([command, str(spec), "--url", "http://127.0.0.1:9"])

    assert result.exit_code == 5, result.output
    assert "expect[0]" in result.stderr
    assert "--mode verified" in result.stderr
    assert not (tmp_path / "qa" / ".aqa" / "runs").exists()
    assert built == []


@pytest.mark.parametrize(
    "mode", [[], ["--mode", "verified"]], ids=["strict", "verified"]
)
def test_a_model_verify_on_an_expectation_not_marked_visual_model_is_a_spec_error(
    tmp_path: Path, mode: list[str]
) -> None:
    spec = badge_spec(tmp_path / "qa", expectation=EXPECTATION)

    result = invoke(["replay", *mode, str(spec), "--url", "http://127.0.0.1:9"])

    assert result.exit_code == 5, result.output
    assert "assertions[0] (a1)" in result.stderr
    assert not (tmp_path / "qa" / ".aqa" / "runs").exists()


def test_a_visual_model_expectation_no_model_verify_establishes_is_a_spec_error(
    tmp_path: Path,
) -> None:
    seen = compiled(
        [],
        targets={"badge": BADGE},
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "visible_unoccluded",
                "target": "badge",
                "min_size_px": [1, 1],
                "in_viewport": True,
            }
        ],
    )
    spec = badge_spec(tmp_path / "qa", seen)

    result = invoke(
        ["replay", "--mode", "verified", str(spec), "--url", "http://127.0.0.1:9"]
    )

    assert result.exit_code == 5, result.output
    assert "expect[0]" in result.stderr
    assert "model_verify" in result.stderr
    assert not (tmp_path / "qa" / ".aqa" / "runs").exists()


def test_a_verified_replay_needs_the_provider_key_before_any_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    spec = badge_spec(tmp_path / "qa")

    result = invoke(
        ["replay", "--mode", "verified", str(spec), "--url", "http://127.0.0.1:9"]
    )

    assert result.exit_code == 5, result.output
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert not (tmp_path / "qa" / ".aqa" / "runs").exists()


def test_explore_refuses_the_verified_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--plan-only` takes no `--budget` (exit 2), so this explores.
    spec = shop(tmp_path / "qa")
    args = [str(spec), "--url", "http://127.0.0.1:4100"]

    result = unlaunched(monkeypatch, [*args, "--budget", "verified_model_usd=1"])

    assert result.exit_code == 5, result.output
    assert (
        "--budget: give NAME=VALUE, NAME one of attempts, actions_per_attempt, "
        "model_usd, minutes, resolve_seconds\n"
    ) in result.stderr
    assert "verified_model_usd" not in result.stderr


def test_a_mode_replay_doesnt_have_is_a_usage_error(tmp_path: Path) -> None:
    spec = badge_spec(tmp_path / "qa")

    result = invoke(["replay", "--mode", "lax", str(spec)])

    assert result.exit_code == 2, result.output


def test_a_verified_replay_with_no_model_verify_builds_no_client_and_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = refusing_constructors(
        monkeypatch,
        [AnthropicClient, ChatAnthropic, anthropic.Anthropic, anthropic.AsyncAnthropic],
    )
    spec = committed(shop(tmp_path / "qa"), shop_script("shop"))

    with serving_app() as shop_app:
        result = invoke(
            ["replay", "--mode", "verified", str(spec), "--url", shop_app.origin]
        )

    assert result.exit_code == 0, result.output
    assert re.fullmatch(rf"passed shop: {SECONDS}, verified \$0\n", result.stdout)
    assert built == []
    written = document(spec)
    assert (written["mode"], written["verified_cost_usd"]) == ("verified", "0")
    assert all(each["verifier"] is None for each in written["verdicts"])


# The verifier's verdicts, on a scripted model.


def test_a_pass_sends_the_saved_screenshot_and_cites_its_folder(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted(replying("pass"))
    spec = badge_spec(tmp_path / "qa")

    result = verified(monkeypatch, app, spec, client)

    assert result.exit_code == 0, result.output
    record = record_in(spec)
    [request] = client.requests
    image = request[-1].content[-1]
    assert isinstance(image, dict)
    assert base64.b64decode(image["base64"]) == (record / SHOT).read_bytes()
    [path, ref] = evidence(request)
    assert path == SHOT
    assert f"[ref={ref}]" in (record / FOLDER / "a11y_snapshot.yaml").read_text()
    found = verdict(spec)
    assert (found["verdict"], found["reason"]) == ("pass", None)
    assert found["evidence"] == {
        "path": FOLDER,
        "screenshot": {"path": SHOT, "withheld": None},
        "before_screenshot": None,
    }
    assert found["verifier"] == {
        "verdict": "pass",
        "evidence_refs": [SHOT, ref],
        "model_reported": {
            "confidence": 0.9,
            "rationale": "the badge is drawn in one colour",
        },
    }
    [cost] = [
        json.loads(line) for line in (record / "costs.jsonl").read_text().splitlines()
    ]
    assert (cost["role"], cost["mode"]) == ("verifier", "verified")
    assert document(spec)["verified_cost_usd"] == cost["cost_usd"]
    models = json.loads((record / "run.json").read_text())["models"]
    assert list(models) == ["verifier"]
    assert re.fullmatch(
        rf"passed badge: {SECONDS}, verified \${cost['cost_usd']}\n", result.stdout
    )


@pytest.mark.parametrize(
    ("flag", "printed"), [("--json", "["), ("--quiet", "")], ids=["json", "quiet"]
)
def test_json_and_quiet_work_as_in_strict_mode(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, printed: str
) -> None:
    spec = badge_spec(tmp_path / "qa")

    result = verified(monkeypatch, app, spec, Scripted(replying("pass")), flag)

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(printed)
    if flag == "--json":
        [written] = json.loads(result.stdout)
        assert written == document(spec)
    else:
        assert result.stdout == ""


def test_a_withheld_screenshot_is_inconclusive_and_calls_no_model(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    send = CDPSession.send

    async def refusing(
        devtools: CDPSession, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "Page.captureScreenshot":
            raise Error("Protocol error (Page.captureScreenshot): refused")
        return await send(devtools, method, params)

    monkeypatch.setattr(CDPSession, "send", refusing)
    client = Scripted()
    spec = badge_spec(tmp_path / "qa")

    result = verified(monkeypatch, app, spec, client)

    assert result.exit_code == 3, result.output
    assert client.requests == []
    assert not (record_in(spec) / SHOT).exists()
    found = verdict(spec)
    assert (found["verdict"], found["reason"]) == ("inconclusive", WITHHELD)
    assert found["evidence"]["path"] == FOLDER
    assert found["evidence"]["screenshot"] == {
        "path": None,
        "withheld": "a DevTools call or the render failed",
    }
    assert found["verifier"] is None


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (replying("pass", [SHOT, "e999"]), MISCITED, [SHOT, "e999"]),
            id="other_refs",
        ),
        pytest.param((replying("inconclusive"), UNJUDGED, None), id="inconclusive"),
        pytest.param(
            (REFUSED, "the verifier's reply was refusal, so it decides nothing", None),
            id="refusal",
        ),
    ],
)
def test_a_reply_that_decides_nothing_is_inconclusive(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[Any, str, list[str] | None],
) -> None:
    reply, reason, said = case
    client = Scripted(reply)
    spec = badge_spec(tmp_path / "qa")

    result = verified(monkeypatch, app, spec, client)

    assert result.exit_code == 3, result.output
    assert len(client.requests) == 1
    found = verdict(spec)
    assert (found["verdict"], found["reason"]) == ("inconclusive", reason)
    assert found["evidence"]["path"] == FOLDER
    if said is not None:
        assert found["verifier"]["evidence_refs"] == said


def test_a_target_that_drifted_is_inconclusive_with_no_folder_and_no_call(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted()
    spec = badge_spec(tmp_path / "qa", DRIFTED)

    result = verified(monkeypatch, app, spec, client)

    assert result.exit_code == 3, result.output
    assert client.requests == []
    assert not (record_in(spec) / "verified").exists()
    found = verdict(spec)
    assert found["verdict"] == "inconclusive"
    assert found["reason"].startswith("drift: strict replay doesn't heal (")
    assert found["evidence"]["path"] == "evidence/0"
    assert found["verifier"] is None


def test_an_element_the_snapshot_gives_no_ref_is_inconclusive_with_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted()
    spec = badge_spec(tmp_path / "qa", COUNT)

    with serving(FOLDED) as folded:
        result = verified(monkeypatch, folded, spec, client)

    assert result.exit_code == 3, result.output
    assert client.requests == []
    found = verdict(spec)
    assert (found["verdict"], found["reason"]) == ("inconclusive", NO_REF)
    assert (record_in(spec) / FOLDER / "a11y_snapshot.yaml").is_file()


def test_the_budget_stops_the_next_call_and_the_run_cant_pass(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One scripted response costs $0.003, more than the whole budget.
    config = "budgets: { resolve_seconds: 2, verified_model_usd: 0.001 }\n"
    client = Scripted(replying("pass"))
    spec = badge_spec(tmp_path / "qa", TWICE, config=config)

    result = verified(monkeypatch, app, spec, client)

    assert result.exit_code == 3, result.output
    assert len(client.requests) == 1
    assert verdict(spec, "a1")["verdict"] == "pass"
    assert (verdict(spec, "a2")["verdict"], verdict(spec, "a2")["reason"]) == (
        "inconclusive",
        SPENT,
    )
    assert not (record_in(spec) / "verified/assertion-1").exists()


def test_no_response_ends_the_run_no_response_and_makes_no_later_call(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted(NO_RESPONSE)
    spec = badge_spec(tmp_path / "qa", TWICE)

    result = verified(monkeypatch, app, spec, client)

    assert result.exit_code == 11, result.output
    assert len(client.requests) == 1
    written = document(spec)
    assert written["outcome"] == "no_response"
    assert "the model gave no response (APIConnectionError)" in written["reasons"]
    assert (verdict(spec, "a1")["verdict"], verdict(spec, "a1")["reason"]) == (
        "inconclusive",
        "the model gave no response (APIConnectionError)",
    )
    assert (verdict(spec, "a2")["verdict"], verdict(spec, "a2")["reason"]) == (
        "inconclusive",
        SILENCED,
    )


def test_an_interrupted_run_looks_at_nothing_and_calls_no_verifier(
    tmp_path: Path,
) -> None:
    # Before any look: the session and element are never touched.
    root = tmp_path / "qa"
    badge_spec(root)
    client = Scripted()
    record = RunRecord.create(root)
    verifying = Verifying(
        ModelRouter.from_config(ProjectConfig(), client),
        Spending(record, Decimal(1)),
        load_project(root).specs["badge"],
        CHECKED,
        lambda: True,
        1,
    )
    check = CHECKED.assertions[0]
    assert isinstance(check, ModelVerify)

    said = asyncio.run(
        verifying.verify(cast(BrowserSession, None), check, cast(ElementHandle, None))
    )

    assert said == Verified("inconclusive", INTERRUPTED)
    assert client.requests == []
    assert verifying.stops == []


def test_a_model_verify_reached_with_no_verifier_is_never_a_pass(
    tmp_path: Path,
) -> None:
    with serving_app() as form_app:
        result, _ = evaluated(
            form_app, tmp_path, {"check": "model_verify", "target": "save"}
        )

    assert result == AssertionResult(
        "a1", "not_evaluated", verified=Verified("inconclusive", NO_VERIFIER)
    )


def test_aqa_report_refuses_a_verified_runs_record(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = badge_spec(tmp_path / "qa")
    replayed = verified(monkeypatch, app, spec, Scripted(replying("pass")))
    assert replayed.exit_code == 0, replayed.output
    assert "run viewer:" not in replayed.output
    record = record_in(spec)
    monkeypatch.chdir(tmp_path / "qa")

    result = invoke(["report", record.name])

    assert result.exit_code == 5, result.output
    assert result.stderr.startswith(
        f"spec_error {record.name}: a verified run's record, which aqa report "
        "doesn't read yet"
    )
    assert not (record / "report.html").exists()
