"""`aqa explore <spec>` (API.md §7; ADR-0024's #53 P17 amendment): the spec
explored end to end through `aqa_runner.explore`, one line saying how it
ended, each budget `--budget` lays over the config's, and a compiled script
that is up to date kept with no key, no model call and no browser. Real
Chromium against the executor's fixture app with a scripted model, or no
browser at all; `test_explore_output.py` holds what printing withholds and
`test_explore_driver.py` the browser driver's start and stop."""

import asyncio
import errno
import json
import os
import re
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn

import langsmith
import pytest
import typer.testing
from aqa_cli import explore as explore_module
from aqa_cli.main import app as aqa
from aqa_core.config import Budgets, ProjectConfig
from aqa_core.coverage_plan import CoveragePlan
from aqa_core.project import load_compiled, load_project
from aqa_runner import explore as runner_explore
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import PolicyEvent, PolicyEventError
from aqa_runner.egress import EgressGate
from aqa_runner.explore import Explored
from aqa_runner.publication import published_text
from aqa_runner.run_record import RunRecord
from aqa_runner.sandbox import HOST_FIXES, OTHER_FIX
from langchain_core.messages import BaseMessage
from playwright._impl._browser_type import BrowserType
from playwright.async_api import Error as PlaywrightError

from packages.runner.tests.egress_fixtures import unused_port
from packages.runner.tests.executor_fixtures import PAGES, App, compiled, serving_app
from packages.runner.tests.explore_graph_fixtures import (
    CLICK,
    FILL,
    NO_RESPONSE,
    REFUSED,
    Move,
    Scripted,
    planned,
    ref,
    tool,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE, copies_found
from packages.runner.tests.test_explore_confirm import BEACON, resets
from packages.runner.tests.test_explore_graph import (
    ASSERT,
    ASSERT_NOW,
    FINISH,
    deadlines,
    paying_until_expired,
)
from packages.runner.tests.test_explore_side_effects import (
    CHECK,
    PAGES_HERE,
    REPEATED,
    agreed,
)
from packages.runner.tests.test_navigator_side_effects import ON_A_PAGE

KEY = "fake-key-for-tests"
# The start origin a spec root's config gives, where nothing listens.
BASE_URL = "http://127.0.0.1:4100"
# The confirmation's reset hook, which the fixture app answers with 204.
RESET = "  reset: { http: POST /write/reset }\n"
ACCOUNT = (
    "  account: { email: reader@example.test, password: { secret: TEST_PASSWORD } }\n"
)
SECRETS = "secrets: { TEST_PASSWORD: { origins: [start], field: password } }\n"
# The path the scripted attempt explores and compiles: fill, save, check.
PATH = (FILL, CLICK, ASSERT, FINISH)
# How long a run took, as its line gives it.
SECONDS = r"\d+\.\ds"
UNCONFIRMED = (
    "warning: {} is unconfirmed: its path has side-effect steps, or loading its "
    "start URL may change the app, and the spec has no reset hook, so no replay "
    "confirmed it (AGENTS.md §6). Declare a reset hook, and explore it again\n"
)
AWAY = tool("navigate", lambda _: {"path": "/page/refused-script"})
SENTINEL = "fake-…-4u49"


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)


def shop(
    root: Path,
    *,
    preconditions: str = "",
    config: str = "",
    page: str = "/page/shop",
    extra: str = "",
) -> Path:
    """The shop spec under the spec root `root`, whose `config.yaml` holds
    BASE_URL and `config`; the spec's path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(f"base_url: {json.dumps(BASE_URL)}\n{config}")
    path = root / "shop.spec.md"
    path.write_text(
        "---\nid: shop\ngoal: A shopper saves their name.\n"
        f"preconditions:\n  start_url: '{page}'\n{preconditions}"
        f"expect:\n  - The shop says the name was saved\n{extra}---\n"
    )
    return path


def explore(
    monkeypatch: pytest.MonkeyPatch,
    args: Sequence[str],
    client: Scripted | None = None,
    *,
    browser: bool = True,
    driver: bool = True,
) -> typer.testing.Result:
    """`aqa explore <args>` in this process, with `client` as every model.
    Without `browser`, the test fails if Playwright starts or attaches to a
    browser; without `driver`, if it starts its driver at all."""
    used: list[str] = []

    def refuse(name: str) -> Callable[..., NoReturn]:
        def refused(*_: object, **__: object) -> NoReturn:
            used.append(name)
            raise AssertionError(f"{name} was started")

        return refused

    if not browser:
        for name in (
            "launch",
            "launch_persistent_context",
            "connect",
            "connect_over_cdp",
        ):
            monkeypatch.setattr(BrowserType, name, refuse(name))
    if not driver:
        monkeypatch.setattr(explore_module, "async_playwright", refuse("the driver"))
    monkeypatch.setattr(explore_module, "AnthropicClient", client or Scripted())
    result = typer.testing.CliRunner().invoke(aqa, ["explore", *args])
    assert used == []
    return result


def unlaunched(
    monkeypatch: pytest.MonkeyPatch, args: Sequence[str], client: Scripted | None = None
) -> typer.testing.Result:
    """`explore`, with no browser started."""
    return explore(monkeypatch, args, client, browser=False)


def unstarted(
    monkeypatch: pytest.MonkeyPatch, args: Sequence[str], client: Scripted | None = None
) -> typer.testing.Result:
    """`explore`, with neither Playwright's driver nor a browser started."""
    return explore(monkeypatch, args, client, browser=False, driver=False)


def on(app: App, spec: Path, *args: str) -> list[str]:
    return [str(spec), "--url", app.origin, *args]


def record_in(root: Path) -> Path:
    """The one run record under the spec root `root`."""
    [record] = (root / ".aqa" / "runs").iterdir()
    return record


def document(root: Path) -> dict[str, Any]:
    """The run's `explore.json`."""
    written: dict[str, Any] = json.loads((record_in(root) / "explore.json").read_text())
    return written


def kept(spec: Path, **update: Any) -> Path:
    """An up-to-date compiled script for the spec at `spec`, published as
    explore publishes one, changed by `update`; its path."""
    root = spec.parent
    loaded = load_project(root).specs["shop"]
    steps = [
        {"seq": 1, "action": "navigate", "url": "/page/shop", "side_effect": False}
    ]
    script = compiled(steps, targets=update.pop("targets", None)).model_copy(
        update={"spec_id": "shop", "spec_hash": loaded.spec_hash, **update}
    )
    target = root / ".compiled" / "shop.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(published_text(script))
    return target


def test_explore_without_plan_only_explores_the_spec(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    client = Scripted(planned(), *PATH)

    result = explore(monkeypatch, on(app, shop(root, preconditions=RESET)), client)

    assert result.exit_code == 0, result.output
    target = root / ".compiled" / "shop.json"
    assert re.fullmatch(
        rf"compiled shop: {SECONDS}, \$0\.015, {target}\n", result.stdout
    )
    assert result.stderr == ""
    script = load_compiled(target, ProjectConfig())
    assert script.confirmed is True
    assert [each.model_dump(exclude_none=True) for each in script.assertions] == [
        {
            "id": "a1",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "t3",
            "text": "Saved Ada",
        }
    ]
    assert len(client.requests) == 5
    confirmation = document(root)["confirmation"]
    assert (confirmation["outcome"], confirmation["error_code"]) == ("passed", None)
    assert [step["locator_index"] for step in confirmation["steps"]] == [None, 0, 0]


def test_a_planned_check_that_reads_false_gives_up_with_exit_3_naming_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    args = on(app, shop(root), "--budget", "actions_per_attempt=2")
    client = Scripted(planned(), ASSERT_NOW, ASSERT_NOW)

    result = explore(monkeypatch, args, client)

    assert result.exit_code == 3, result.output
    assert result.stdout == ""
    assert re.fullmatch(
        rf"gave_up shop: {SECONDS}, \$0\.009, run record: {record_in(root)}\n"
        "  the attempt took all 2 of its turns\n  a1: failed\n",
        result.stderr,
    )


def test_the_action_budget_gives_up_with_exit_3(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = on(app, shop(tmp_path / "qa"), "--budget", "actions_per_attempt=1")

    result = explore(monkeypatch, args, Scripted(planned(), FILL))

    assert result.exit_code == 3, result.output
    assert "\n  the attempt took all 1 of its turns\n" in result.stderr


def test_the_spend_budget_gives_up_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    args = [str(shop(root)), "--url", "http://127.0.0.1:4100"]

    result = unlaunched(
        monkeypatch, [*args, "--budget", "model_usd=0.003"], Scripted(planned())
    )

    assert result.exit_code == 3, result.output
    assert result.stderr.endswith("\n  the spend budget ran out with the plan\n")
    assert len((record_in(root) / "costs.jsonl").read_text().splitlines()) == 1


NONDEFAULT = (
    "budgets: { attempts: 4, actions_per_attempt: 30, model_usd: 2, minutes: 20, "
    "resolve_seconds: 8 }\n"
)


def budgets_given(
    monkeypatch: pytest.MonkeyPatch, spec: Path, overrides: Sequence[str]
) -> Budgets:
    """The budgets `aqa_runner.explore.explore` was given for a run of the
    spec at `spec` with `overrides`, whose plan the model refuses."""
    given: list[Budgets] = []

    async def through(
        spec: Any, config: ProjectConfig, *args: Any, **kwargs: Any
    ) -> Explored:
        given.append(config.budgets)
        return await runner_explore.explore(spec, config, *args, **kwargs)

    monkeypatch.setattr(explore_module, "explore_spec", through)
    budget = [arg for override in overrides for arg in ("--budget", override)]
    args = [str(spec), "--url", "http://127.0.0.1:4100", *budget]
    result = unlaunched(monkeypatch, args, Scripted(REFUSED))
    assert result.exit_code == 3, result.output
    [budgets] = given
    return budgets


def test_every_budget_name_reaches_the_runner_over_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa", config=NONDEFAULT)
    overrides = [
        "attempts=2",
        "actions_per_attempt=3",
        "model_usd=0.5",
        "minutes=7",
        "resolve_seconds=4",
    ]

    assert budgets_given(monkeypatch, spec, overrides) == Budgets(
        attempts=2, actions_per_attempt=3, model_usd=0.5, minutes=7, resolve_seconds=4
    )


def test_a_budget_not_given_keeps_the_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa", config=NONDEFAULT)

    assert budgets_given(monkeypatch, spec, ["minutes=7"]) == Budgets(
        attempts=4, actions_per_attempt=30, model_usd=2, minutes=7, resolve_seconds=8
    )


def test_a_navigator_refusal_with_no_fallback_gives_up_with_exit_3(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = explore(
        monkeypatch, on(app, shop(tmp_path / "qa")), Scripted(planned(), REFUSED)
    )

    assert result.exit_code == 3, result.output
    assert "\n  the model refused its turn\n" in result.stderr


def test_an_undeclared_host_while_navigating_exits_6_and_records_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"

    result = explore(monkeypatch, on(app, shop(root)), Scripted(planned(), AWAY, FILL))

    assert result.exit_code == 6, result.output
    record = record_in(root)
    assert result.stderr.endswith(
        f"\n  the egress record is {record / 'egress.json'}\n"
    )
    refused = json.loads((record / "egress.json").read_text())["refused"]
    assert refused == [{"host": "analytics.example.test", "port": 80}]


def test_an_undeclared_host_during_confirmation_exits_6_and_records_it(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    resets(monkeypatch, lambda: monkeypatch.setitem(PAGES, "shop", BEACON))

    result = explore(
        monkeypatch,
        on(app, shop(root, preconditions=RESET)),
        Scripted(planned(), *PATH),
    )

    assert result.exit_code == 6, result.output
    record = record_in(root)
    assert result.stderr.endswith(
        f"\n  the egress record is {record / 'egress.json'}\n"
    )
    assert not (root / ".compiled").exists()


def test_an_unreachable_start_origin_exits_13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    closed = f"http://127.0.0.1:{unused_port()}"

    result = explore(
        monkeypatch, [str(shop(root)), "--url", closed], Scripted(planned())
    )

    assert result.exit_code == 13, result.output
    # No action's outcome is unknown, so the line ends at the record.
    assert re.fullmatch(
        rf"unreachable shop: {SECONDS}, \$0\.003, run record: {record_in(root)}",
        result.stderr.splitlines()[0],
    )


def test_an_invalid_spec_exits_5_before_any_model_call_or_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted()
    spec = shop(tmp_path / "qa", extra="colour: red\n")

    result = unstarted(monkeypatch, [str(spec)], client)

    assert result.exit_code == 5, result.output
    assert result.stderr.startswith(f"spec_error {spec}: nothing was explored\n")
    assert "colour: unknown key" in result.stderr
    assert client.models == []


def test_a_subject_contract_naming_an_unknown_expectation_exits_5_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted()
    config = "subjects: [{ spec: shop, expect: 3, region: main, part: p }]\n"
    spec = shop(tmp_path / "qa", config=config)

    result = unstarted(
        monkeypatch, [str(spec), "--url", "http://127.0.0.1:4100"], client
    )

    assert result.exit_code == 5, result.output
    assert result.stderr.startswith(f"spec_error {spec}: nothing was explored\n")
    assert client.models == []


def test_a_plan_the_compiler_cannot_bind_is_exit_5_before_the_proxy_and_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = "  probes: { count: GET /probe/count }\n"
    plan = CoveragePlan.model_validate(
        {
            "expectations": [
                {
                    "expect_index": 0,
                    "subject": "the shop's saved count",
                    "claim": "one more shop was saved",
                    "checks": [{"check": "probe_equals_baseline", "probe": "count"}],
                }
            ],
            "requires": [],
        }
    )
    spec = shop(tmp_path / "qa", preconditions=probes)

    result = explore(
        monkeypatch,
        [str(spec), "--url", "http://127.0.0.1:4100"],
        Scripted(planned(plan)),
    )

    assert result.exit_code == 5, result.output
    assert "\n  the plan has checks exploring can't keep\n" in result.stderr


def fill_button(messages: Sequence[BaseMessage]) -> Any:
    """A fill of the save button, which takes no text: its intent stays
    unresolved."""
    meaning = "the shop's save button"
    return tool(
        "fill",
        lambda m: {"ref": ref(m, "button", "Save"), "meaning": meaning, "text": "x"},
    )(messages)


def test_an_unresolved_side_effect_intent_exits_4_and_says_non_resumable(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Scripted(planned(), fill_button)

    result = explore(monkeypatch, on(app, shop(tmp_path / "qa")), client)

    assert result.exit_code == 4, result.output
    assert result.stderr.splitlines()[0].endswith(", non-resumable")


def test_a_block_with_an_unresolved_side_effect_intent_exits_6_and_says_non_resumable(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    limits = deadlines(monkeypatch)
    gates: list[EgressGate] = []

    class Kept(EgressGate):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            gates.append(self)

    monkeypatch.setattr(runner_explore, "EgressGate", Kept)

    def pay(messages: Sequence[BaseMessage]) -> Any:
        move = paying_until_expired(app, limits, gates[0])
        assert callable(move)
        return move(messages)

    spec = shop(root, page="/page/pay")
    result = explore(monkeypatch, on(app, spec), Scripted(planned(), pay))

    assert result.exit_code == 6, result.output
    first = result.stderr.splitlines()[0]
    assert first.endswith(f"run record: {record_in(root)}, non-resumable")


# What a failed launch said, which the diagnostic must not repeat: Playwright's
# own launch errors carry advice to turn the sandbox off.
ADVICE = "fake raw launch advice 4u49"


@pytest.mark.parametrize(
    "kind", [PlaywrightError, KeyError, ValueError, OSError, ProcessLookupError]
)
def test_each_launch_failure_exits_10_naming_the_host_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: type[Exception]
) -> None:
    async def failing(*_: object, **__: object) -> None:
        raise kind(ADVICE)

    monkeypatch.setattr(BrowserType, "launch", failing)
    spec = shop(tmp_path / "qa")

    result = explore(
        monkeypatch, [str(spec), "--url", "http://127.0.0.1:4100"], Scripted(planned())
    )

    assert result.exit_code == 10, result.output
    assert HOST_FIXES.get(sys.platform, OTHER_FIX) in result.stderr
    assert f"({kind.__name__})" in result.stderr
    assert "Traceback" not in result.output
    assert ADVICE not in result.output


def test_no_response_while_planning_exits_11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")

    result = explore(
        monkeypatch,
        [str(spec), "--url", "http://127.0.0.1:4100"],
        Scripted(NO_RESPONSE),
    )

    assert result.exit_code == 11, result.output
    assert "\n  the model gave no response (APIConnectionError)\n" in result.stderr


def test_a_missing_secret_value_exits_12_before_any_model_call_or_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AQA_SECRET_TEST_PASSWORD", raising=False)
    client = Scripted()
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS)

    result = unstarted(
        monkeypatch, [str(spec), "--url", "http://127.0.0.1:4100"], client
    )

    assert result.exit_code == 12, result.output
    assert result.stderr.startswith(
        "secret_unusable shop: nothing was explored\n  AQA_SECRET_TEST_PASSWORD is not set"
    )
    assert client.models == []


def test_a_failed_reset_hook_exits_14_before_the_browser(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preconditions = "  reset: { http: POST /status/500 }\n"

    result = explore(
        monkeypatch,
        on(app, shop(tmp_path / "qa", preconditions=preconditions)),
        Scripted(planned()),
    )

    assert result.exit_code == 14, result.output
    assert "\n  the reset hook answered 500\n" in result.stderr


def test_a_spec_root_that_cannot_hold_a_run_record_exits_15_before_any_model_call_or_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = shop(root)
    (root / ".aqa").write_text("not a directory\n")
    client = Scripted(planned())

    result = unlaunched(
        monkeypatch, [str(spec), "--url", "http://127.0.0.1:4100"], client
    )

    assert result.exit_code == 15, result.output
    assert re.fullmatch(
        rf"record_error shop: {SECONDS}, \$0\n"
        "  the run record could not be created; nothing was planned\n",
        result.stderr,
    )
    assert client.requests == []


def test_a_side_effect_path_without_a_reset_hook_is_written_unconfirmed_and_warns(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"

    result = explore(monkeypatch, on(app, shop(root)), Scripted(planned(), *PATH))

    assert result.exit_code == 0, result.output
    target = root / ".compiled" / "shop.json"
    assert re.fullmatch(
        rf"compiled shop: {SECONDS}, \$0\.015, {target}\n", result.stdout
    )
    assert result.stderr == UNCONFIRMED.format(target)
    assert load_compiled(target, ProjectConfig()).confirmed is False


def test_a_confirmation_that_saw_a_side_effect_with_no_reset_hook_warns(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The start's load writes only when loaded again, as the confirmation does.
    monkeypatch.setitem(PAGES, "again-start", PAGES_HERE["again-start"])
    app.probes["visits"] = []
    root = tmp_path / "qa"
    spec = shop(root, page="/page/again-start")

    result = explore(
        monkeypatch, on(app, spec), Scripted(planned(ON_A_PAGE), CHECK, agreed([]))
    )

    assert result.exit_code == 0, result.output
    target = root / ".compiled" / "shop.json"
    assert re.fullmatch(
        rf"compiled shop: {SECONDS}, \$0\.009, {target}\n", result.stdout
    )
    assert result.stderr == f"  {REPEATED}\n"
    assert load_compiled(target, ProjectConfig()).confirmed is True


def test_force_explores_an_up_to_date_script_again(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa", preconditions=RESET)
    target = kept(spec)
    before = load_compiled(target, ProjectConfig()).compiled_at
    client = Scripted(planned(), *PATH)

    result = explore(monkeypatch, on(app, spec, "--force"), client)

    assert result.exit_code == 0, result.output
    assert load_compiled(target, ProjectConfig()).compiled_at != before
    assert len(client.requests) == 5


def test_a_stale_script_is_explored_again(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa", preconditions=RESET)
    target = kept(spec, spec_hash="sha256:" + "0" * 64)

    result = explore(monkeypatch, on(app, spec), Scripted(planned(), *PATH))

    assert result.exit_code == 0, result.output
    spec_hash = load_project(tmp_path / "qa").specs["shop"].spec_hash
    assert load_compiled(target, ProjectConfig()).spec_hash == spec_hash


# A sign-in page that shows the password typed into it, and the shop's
# unsaved status, which the plan's check reads false.
REFLECTS = (
    '<label>Password <input type="password" '
    "oninput=\"document.querySelector('#said').textContent = this.value\"></label>"
    '<p id="said">nothing typed</p><p id="status">Not saved</p>'
)
FILL_SECRET = tool(
    "fill_secret",
    lambda m: {
        "ref": ref(m, "textbox", "Password"),
        "meaning": "the sign-in password",
        "name": "TEST_PASSWORD",
    },
)


def test_explore_diagnostics_hold_no_secret_copy(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setitem(PAGES, "reflects", REFLECTS)
    client = Scripted(planned(), FILL_SECRET, ASSERT_NOW, REFUSED)
    spec = shop(
        tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS, page="/page/reflects"
    )

    result = explore(monkeypatch, on(app, spec), client)

    assert result.exit_code == 3, result.output
    assert result.stderr.endswith("\n  the model refused its turn\n  a1: failed\n")
    assert copies_found(FAKE_VALUE, texts=[result.stdout, result.stderr]) == []
    # The page did show it: the next request held its marker.
    assert "[SECRET:TEST_PASSWORD]" in client.requests[2][-1].text


EVIL = "http://evil.example.test:31337"


@pytest.mark.parametrize("binds", [True, False], ids=["binds", "binds-none"])
def test_a_policy_refusal_in_a_run_binding_a_secret_prints_and_saves_no_origin_or_port(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binds: bool
) -> None:
    async def refused(*_: object, **__: object) -> None:
        raise PolicyEventError(PolicyEvent("document", f"{EVIL}/x?q=1", EVIL))

    monkeypatch.setattr(BrowserSession, "navigate", refused)
    root = tmp_path / "qa"
    if binds:
        monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
        spec = shop(root, preconditions=ACCOUNT, config=SECRETS)
    else:
        spec = shop(root)

    result = explore(monkeypatch, on(app, spec), Scripted(planned()))

    assert result.exit_code == 3, result.output
    saved = (record_in(root) / "explore.json").read_text()
    if binds:
        assert (
            "\n  the start URL didn't load: the page is on an origin the run doesn't "
            "allow (withheld: the run has a test secret)\n"
        ) in result.stderr
        for text in (result.stdout, result.stderr, saved):
            assert "evil.example.test" not in text
            assert "31337" not in text
    else:
        assert (
            f"the page is on {EVIL}, which isn't one of the run's allowed origins"
            in result.stderr
        )
    assert not (record_in(root) / "egress.json").exists()


def directory_sync_failing(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """`.compiled`'s directory fsync, after the install, raises EIO."""
    fsync = os.fsync

    def failing(handle: int) -> None:
        if directory.exists() and os.fstat(handle).st_ino == directory.stat().st_ino:
            raise OSError(errno.EIO, "fake directory fsync failure")
        fsync(handle)

    monkeypatch.setattr(os, "fsync", failing)


@pytest.mark.parametrize("installed", [True, False], ids=["after-install", "before"])
def test_a_publication_failing_after_install_exits_15_naming_the_published_script(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    root = tmp_path / "qa"
    target = root / ".compiled" / "shop.json"
    spec = shop(root, preconditions=RESET)
    if installed:
        directory_sync_failing(monkeypatch, target.parent)
    else:
        target.parent.mkdir(parents=True)
        target.parent.chmod(0o555)
    try:
        result = explore(monkeypatch, on(app, spec), Scripted(planned(), *PATH))
    finally:
        target.parent.chmod(0o755)

    assert result.exit_code == 15, result.output
    record = record_in(root)
    if installed:
        assert re.fullmatch(
            rf"record_error shop: {SECONDS}, \$0\.015, {target}, run record: {record}\n"
            "  the compiled script was published, but the file system failed after "
            "it, so it may not survive a crash\n",
            result.stderr,
        )
        assert load_compiled(target, ProjectConfig()).confirmed is True
    else:
        assert re.fullmatch(
            rf"record_error shop: {SECONDS}, \$0\.015, run record: {record}\n"
            "  the compiled script couldn't be written\n",
            result.stderr,
        )
        assert not target.exists()


def interrupting(monkeypatch: pytest.MonkeyPatch, kind: type[BaseException]) -> Move:
    """A turn whose request is interrupted by `kind`: Ctrl-C, delivered as a
    terminal delivers it, a SIGINT, which `asyncio.run` turns into a
    cancellation of the command's task; or that task cancelled by its
    caller."""
    commands: list[asyncio.Task[Any]] = []

    async def through(*args: Any, **kwargs: Any) -> Explored:
        task = asyncio.current_task()
        assert task is not None
        commands.append(task)
        return await runner_explore.explore(*args, **kwargs)

    monkeypatch.setattr(explore_module, "explore_spec", through)

    async def interrupted(_messages: Sequence[BaseMessage]) -> Any:
        if kind is KeyboardInterrupt:
            os.kill(os.getpid(), signal.SIGINT)
        else:
            commands[0].cancel()
        await asyncio.sleep(5)
        pytest.fail("the turn was never interrupted")

    return interrupted


@pytest.mark.parametrize("kind", [KeyboardInterrupt, asyncio.CancelledError])
def test_an_interrupted_explore_keeps_its_costs_and_turns_and_prints_no_summary(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: type[BaseException]
) -> None:
    root = tmp_path / "qa"
    client = Scripted(planned(), FILL, interrupting(monkeypatch, kind))
    args = on(app, shop(root))

    if kind is KeyboardInterrupt:
        result = explore(monkeypatch, args, client)
        assert result.exit_code == 130, result.output
        output = result.output
    else:
        with pytest.raises(asyncio.CancelledError):
            explore(monkeypatch, args, client)
        output = ""

    record = record_in(root)
    assert len((record / "costs.jsonl").read_text().splitlines()) == 2
    assert json.loads((record / "turns" / "1.json").read_text())["outcome"] == "ok"
    assert not (record / "turns" / "2.json").exists()
    assert not (record / "explore.json").exists()
    assert "shop:" not in output


def test_a_block_whose_egress_record_cannot_be_saved_keeps_6_and_non_resumable(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    write = RunRecord.write

    def unwritten(record: RunRecord, name: str, *args: Any, **kwargs: Any) -> None:
        if name == "egress.json":
            raise OSError(errno.ENOSPC, "fake full disk")
        write(record, name, *args, **kwargs)

    monkeypatch.setattr(RunRecord, "write", unwritten)
    limits = deadlines(monkeypatch)
    gates: list[EgressGate] = []

    class Kept(EgressGate):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            gates.append(self)

    monkeypatch.setattr(runner_explore, "EgressGate", Kept)

    def pay(messages: Sequence[BaseMessage]) -> Any:
        move = paying_until_expired(app, limits, gates[0])
        assert callable(move)
        return move(messages)

    result = explore(
        monkeypatch, on(app, shop(root, page="/page/pay")), Scripted(planned(), pay)
    )

    assert result.exit_code == 6, result.output
    record = record_in(root)
    lines = result.stderr.splitlines()
    assert lines[0].endswith(f"run record: {record}, non-resumable")
    assert "  the run record took no more lines, so the attempt stopped" in lines
    assert (
        lines[-1]
        == f"  the egress record couldn't be saved to {record / 'egress.json'}"
    )
