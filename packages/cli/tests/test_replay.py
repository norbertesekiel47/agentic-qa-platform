"""`aqa replay <spec>...` (API.md §7; ADR-0024's #238 and #239 amendments):
each spec's committed compiled script replayed in strict mode, with no model
and no heal, one line per spec, each run's `replay.json`, and the exit code
rolled up across specs. Every spec is admitted before any reset is sent and
before Playwright's driver starts. Real Chromium against the executor's
fixture app; `packages/runner/tests/test_replay_result.py` holds the verdict
mapping, the precedence and the document's fields."""

import errno
import json
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn

import anthropic
import pytest
import typer.testing
from aqa_cli import replay as replay_module
from aqa_cli.main import app as aqa
from aqa_core.compiled import CompiledScript
from aqa_core.project import load_project
from aqa_runner import executor, text_search
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.loopback_server import LoopbackServer
from aqa_runner.model_router import ModelRouter
from aqa_runner.publication import published_text
from aqa_runner.run_record import RefusedWriteError, RunRecord
from aqa_runner.sandbox import HOST_FIXES, OTHER_FIX
from langchain_anthropic import ChatAnthropic
from playwright._impl._browser_type import BrowserType
from playwright.async_api import Error as PlaywrightError

from packages.cli.tests.test_explore_runs import (
    ACCOUNT,
    REFLECTS,
    SECRETS,
    record_in,
    shop,
)
from packages.runner.tests.egress_fixtures import unused_port
from packages.runner.tests.executor_fixtures import (
    FORM_TARGETS,
    PAGES,
    SHOP_TARGETS,
    App,
    compiled,
    serving_app,
    shop_script,
)
from packages.runner.tests.secret_fixtures import FAKE_VALUE, copies_found

# How long a run took, as its line gives it.
SECONDS = r"\d+\.\ds"
# A reset hook the fixture app answers with 204.
RESET = "  reset: { http: POST /write/reset }\n"
# Two checks that backtrack for exponential time on the catastrophic page's
# forty a's and a b, then one that passes there.
TIMING_OUT = compiled(
    [],
    targets={"line": {"semantic": "the line of a's", "locators": [{"css": "p"}]}},
    assertions=[
        {"id": "a1", "expect_index": 0, "check": "text_visible", "pattern": "(a+)+$"},
        {
            "id": "a2",
            "expect_index": 0,
            "check": "text_in_target",
            "target": "line",
            "pattern": "(a+)+$",
        },
        {
            "id": "a3",
            "expect_index": 0,
            "check": "url_matches",
            "pattern": "catastrophic",
        },
    ],
)
# What a failed launch said, which the diagnostic must not repeat.
ADVICE = "fake raw launch advice 4u49"
NO_DRIVER = (
    "unreachable shop: the browser driver couldn't start, so nothing was replayed\n"
)
# Two fake test secrets' values, and the second's binding and reference.
FIRST = "fake-first-value"
SECOND = "fake-second-value"
OTHER_SECRETS = "secrets: { OTHER_PASSWORD: { origins: [start], field: password } }\n"
OTHER_ACCOUNT = (
    "  account: { email: reader@example.test, password: { secret: OTHER_PASSWORD } }\n"
)
LOGGED = (
    "DEBUG turns on pw:protocol: Playwright would log every protocol message, a "
    "test secret's value among them; unset it to run with test secrets"
)


@pytest.fixture
def app() -> Iterator[App]:
    with serving_app() as served:
        yield served


def committed(spec: Path, script: CompiledScript) -> Path:
    """`script` committed as the compiled script of the spec at `spec`, its
    project's only one, compiled from the spec as it is; the spec's path."""
    root = spec.parent
    [loaded] = load_project(root).specs.values()
    spec_id = loaded.frontmatter.id
    target = root / ".compiled" / f"{spec_id}.json"
    target.parent.mkdir(exist_ok=True)
    current = {"spec_id": spec_id, "spec_hash": loaded.spec_hash}
    target.write_text(published_text(script.model_copy(update=current)))
    return spec


def renamed(spec: Path, spec_id: str) -> Path:
    """The shop spec at `spec` as the spec `spec_id`, in its own file; its
    path."""
    path = spec.with_name(f"{spec_id}.spec.md")
    path.write_text(spec.read_text().replace("id: shop\n", f"id: {spec_id}\n"))
    spec.unlink()
    return path


def replay(
    monkeypatch: pytest.MonkeyPatch,
    args: Sequence[str],
    *,
    browser: bool = True,
    driver: bool = True,
) -> typer.testing.Result:
    """`aqa replay <args>` in this process. Without `browser`, the test fails
    if Playwright starts or attaches to a browser; without `driver`, if it
    starts its driver at all."""
    used: list[str] = []

    def refuse(name: str) -> Callable[..., NoReturn]:
        def refused(*_: object, **__: object) -> NoReturn:
            used.append(name)
            raise AssertionError(f"{name} was started")

        return refused

    if not browser:
        for name in ("launch", "launch_persistent_context", "connect"):
            monkeypatch.setattr(BrowserType, name, refuse(name))
    if not driver:
        monkeypatch.setattr(replay_module, "async_playwright", refuse("the driver"))
    result = typer.testing.CliRunner().invoke(aqa, ["replay", *args])
    assert used == []
    return result


def on(app: App, *specs: Path) -> list[str]:
    return [*map(str, specs), "--url", app.origin]


def document(root: Path) -> dict[str, Any]:
    """The run's `replay.json`."""
    written: dict[str, Any] = json.loads((record_in(root) / "replay.json").read_text())
    return written


def test_strict_replay_builds_no_model_client(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    built: list[str] = []

    class ModelClientBuiltError(Exception):
        pass

    def refuse(name: str) -> Callable[..., None]:
        def constructor(*_: object, **__: object) -> None:
            built.append(name)
            raise ModelClientBuiltError(name)

        return constructor

    clients: list[type] = [
        ModelRouter,
        AnthropicClient,
        ChatAnthropic,
        anthropic.Anthropic,
        anthropic.AsyncAnthropic,
    ]
    for client in clients:
        monkeypatch.setattr(client, "__init__", refuse(client.__name__))
    # The control: building any of them now is caught.
    for client in clients:
        with pytest.raises(ModelClientBuiltError):
            client()
    built.clear()
    spec = committed(shop(tmp_path / "qa"), shop_script("shop"))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 0, result.output
    assert built == []


def test_a_script_that_passes_exits_0_with_one_line(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = committed(shop(tmp_path / "qa"), shop_script("shop"))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 0, result.output
    assert re.fullmatch(rf"passed shop: {SECONDS}, \$0\n", result.stdout)
    assert result.stderr == ""


def test_a_resolved_false_assertion_is_expectation_violated_and_exits_1(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root, page="/page/shop-wrong"), shop_script("shop-wrong"))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert re.fullmatch(
        rf"expectation_violated shop: {SECONDS}, \$0, run record: {record_in(root)}\n"
        "  a2: expectation_violated\n",
        result.stderr,
    )
    # Every check is evaluated, those after the false one included.
    verdicts = document(root)["verdicts"]
    assert [(v["assertion"], v["verdict"]) for v in verdicts if "assertion" in v] == [
        ("a1", "pass"),
        ("a2", "expectation_violated"),
        ("a3", "pass"),
        ("a4", "pass"),
    ]


def test_a_violated_invariant_is_expectation_violated_and_exits_1(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = committed(shop(tmp_path / "qa", page="/page/console-error"), compiled([]))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 1, result.output
    assert result.stderr.endswith(
        "\n  invariant console_errors: expectation_violated\n"
    )


def test_a_check_that_times_out_is_inconclusive_and_exits_3(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than the 2 s rule, which test_text_search measures.
    monkeypatch.setattr(text_search, "SEARCH_SECONDS", 1)
    root = tmp_path / "qa"
    spec = committed(shop(root, page="/page/catastrophic"), TIMING_OUT)

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 3, result.output
    assert re.fullmatch(
        rf"inconclusive shop: {SECONDS}, \$0, run record: {record_in(root)}\n"
        "  a1: inconclusive, check_timed_out\n  a2: inconclusive, check_timed_out\n",
        result.stderr,
    )


def test_a_target_only_a_fallback_finds_drifts_and_is_inconclusive(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    click = {"seq": 1, "action": "click", "target": "save", "side_effect": True}
    click |= {"side_effect_basis": "network: POST /write/save"}
    script = compiled([click], targets={"save": FORM_TARGETS["save"]})
    # The miss is found sooner than the default 10 s.
    quick = "budgets: { resolve_seconds: 1 }\n"
    spec = committed(shop(tmp_path / "qa", page="/page/form", config=quick), script)

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 3, result.output
    assert result.stderr.endswith(
        "\n  step 1: inconclusive, drift: strict replay doesn't heal "
        "(no match, not unanimous)\n"
    )
    # No side effect acts through a fallback (#25).
    assert "/write/save" not in app.paths()


def test_an_egress_block_exits_6_with_no_verdict(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    page = "/page/fetches?to=http%3A%2F%2Fundeclared.example.test%2Fx"
    spec = committed(shop(root, page=page), compiled([]))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 6, result.output
    record = record_in(root)
    assert re.match(
        rf"egress_blocked shop: {SECONDS}, \$0, run record: {record}\n", result.stderr
    )
    assert result.stderr.endswith(
        f"\n  the egress record is {record / 'egress.json'}\n"
    )
    assert (record / "egress.json").is_file()
    assert document(root)["verdicts"] == []


def test_an_unresolved_side_effect_intent_exits_4_non_resumable(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fill of the save button, which takes no text: its intent stays
    # unresolved.
    fill = {"seq": 1, "action": "fill", "target": "save", "value": "x"}
    fill |= {"side_effect": True, "side_effect_basis": "network: POST /write/save"}
    root = tmp_path / "qa"
    spec = committed(
        shop(root), compiled([fill], targets={"save": SHOP_TARGETS["save"]})
    )

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 4, result.output
    first = result.stderr.splitlines()[0]
    assert first.endswith(f", run record: {record_in(root)}, non-resumable")
    assert document(root)["non_resumable"] == [1]


def test_a_withheld_line_still_says_non_resumable(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The run record's path spells the value, and so does its marker,
    # `[SECRET:TEST_PASSWORD]`, so no scan can take it out of the line.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "test")
    fill = {"seq": 1, "action": "fill", "target": "save", "value": "x"}
    fill |= {"side_effect": True, "side_effect_basis": "network: POST /write/save"}
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS)
    committed(spec, compiled([fill], targets={"save": SHOP_TARGETS["save"]}))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 4, result.output
    assert result.stderr == (
        "non_resumable shop: the rest is withheld, since it would spell a bound "
        "value\nnon-resumable\n"
    )


def test_the_run_document_holds_each_verdict_with_its_evidence(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root, page="/page/shop-wrong"), shop_script("shop-wrong"))

    replay(monkeypatch, on(app, spec))

    verdicts = document(root)["verdicts"]
    assert [v["step"] for v in verdicts if "step" in v] == [0, 1, 2, 3, 4, 5, 6]
    record = record_in(root)
    assert [
        v["evidence"] for v in verdicts if not (record / v["evidence"]["path"]).is_dir()
    ] == []


def test_a_spec_with_no_compiled_script_exits_5_before_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")

    result = replay(monkeypatch, [str(spec)], driver=False)

    assert result.exit_code == 5, result.output
    assert result.stderr == "spec_error shop: no compiled script: explore it first\n"


def test_an_invalid_spec_exits_5_before_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa", extra="colour: red\n")

    result = replay(monkeypatch, [str(spec)], driver=False)

    assert result.exit_code == 5, result.output
    assert result.stderr.startswith(f"spec_error {spec}: nothing was replayed\n")
    assert "colour: unknown key" in result.stderr


@pytest.mark.parametrize("when", ["found", "loaded"])
def test_a_compiled_script_that_cant_be_read_exits_15_before_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    spec = committed(shop(tmp_path / "qa"), shop_script("shop"))
    if when == "loaded":
        # Found readable, then unreadable when it is loaded.
        def unreadable(*_: object) -> NoReturn:
            raise PermissionError(errno.EACCES, "fake unreadable script")

        monkeypatch.setattr(replay_module, "load_compiled", unreadable)
    else:
        (spec.parent / ".compiled" / "shop.json").chmod(0)

    result = replay(monkeypatch, [str(spec)], driver=False)

    assert result.exit_code == 15, result.output
    assert result.stderr == (
        "record_error shop: the compiled script could not be read, so nothing "
        "was replayed\n"
    )


def test_a_linked_compiled_script_exits_5_before_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))
    # A repository can commit the compiled script as a link pointing anywhere.
    target = root / ".compiled" / "shop.json"
    elsewhere = target.rename(tmp_path / "elsewhere.json")
    target.symlink_to(elsewhere)

    result = replay(monkeypatch, [str(spec)], driver=False)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        "spec_error shop: nothing was replayed\n"
        f"  {target}: a link, and explore writes only in {root}\n"
    )


def unloadable(spec: Path) -> str:
    target = spec.parent / ".compiled" / "shop.json"
    target.parent.mkdir()
    target.write_text("{}\n")
    return (
        "spec_error shop: the compiled script doesn't load, so nothing was "
        f"replayed\n  {target}: "
    )


def stale(spec: Path) -> str:
    committed(spec, shop_script("shop").model_copy(update={"spec_hash": "x"}))
    target = spec.parent / ".compiled" / "shop.json"
    stored = target.read_text().replace(
        load_project(spec.parent).specs["shop"].spec_hash, "sha256:" + "0" * 64
    )
    target.write_text(stored)
    return (
        "spec_error shop: replay refuses the compiled script, so nothing was "
        "replayed\n  compiled from another version of the spec: explore it again\n"
    )


def two_keys(spec: Path) -> str:
    press = {"seq": 1, "action": "press", "key": "a+b", "side_effect": False}
    committed(spec, compiled([press]))
    return (
        "spec_error shop: replay refuses the compiled script, so nothing was "
        "replayed\n  steps[0] (seq 1): press takes one key, with only modifiers "
        "held before it: 'a+b'\n"
    )


@pytest.mark.parametrize("script", [unloadable, stale, two_keys])
def test_a_script_replay_cant_admit_exits_5_before_the_reset(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: Callable[[Path], str],
) -> None:
    spec = shop(tmp_path / "qa", preconditions=RESET)
    said = script(spec)

    result = replay(monkeypatch, on(app, spec), driver=False)

    assert result.exit_code == 5, result.output
    assert result.stderr.startswith(said)
    assert app.paths() == []


def test_every_spec_is_admitted_before_any_run(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = committed(shop(tmp_path / "a", preconditions=RESET), shop_script("shop"))
    refused = shop(tmp_path / "b", preconditions=RESET)
    stale(refused)

    result = replay(monkeypatch, on(app, good, refused))

    assert result.exit_code == 5, result.output
    assert app.paths() == []


def test_the_exit_code_rolls_up_across_specs(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(text_search, "SEARCH_SECONDS", 1)
    # Neither the first run's code, the last's nor the highest is the
    # roll-up's.
    specs = (
        committed(shop(tmp_path / "a", page="/page/catastrophic"), TIMING_OUT),
        committed(
            shop(tmp_path / "b", page="/page/shop-wrong"), shop_script("shop-wrong")
        ),
        committed(shop(tmp_path / "c"), shop_script("shop")),
    )

    result = replay(monkeypatch, on(app, *specs))

    assert result.exit_code == 1, result.output
    lines = [line for line in result.output.splitlines() if not line.startswith(" ")]
    assert [line.split(" ")[0] for line in lines] == [
        "inconclusive",
        "expectation_violated",
        "passed",
    ]


@pytest.mark.parametrize(
    ("environment", "said"),
    [
        ({}, "AQA_SECRET_TEST_PASSWORD is not set"),
        (
            {"AQA_SECRET_TEST_PASSWORD": FAKE_VALUE, "DEBUG": "pw:protocol"},
            LOGGED,
        ),
    ],
    ids=["unset", "protocol-logging"],
)
def test_an_unusable_test_secret_exits_12_before_the_driver_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    said: str,
) -> None:
    monkeypatch.delenv("AQA_SECRET_TEST_PASSWORD", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS)
    committed(spec, shop_script("shop"))

    result = replay(monkeypatch, [str(spec)], driver=False)

    assert result.exit_code == 12, result.output
    assert result.stderr.startswith(
        f"secret_unusable shop: nothing was replayed\n  {said}"
    )


def test_a_refusal_after_binding_prints_none_of_the_specs_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FIRST)
    # Replay refuses a start path that spells the bound value once it is
    # bound, and the spec's id spells it too.
    page = f"/page/{FIRST}"
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS, page=page)
    spec = committed(renamed(spec, FIRST), compiled([]))

    result = replay(monkeypatch, [str(spec)], driver=False)

    assert result.exit_code == 5, result.output
    assert result.stderr == (
        "spec_error [SECRET:TEST_PASSWORD]: replay refuses the compiled script, so "
        "nothing was replayed\n  preconditions.start_url: a bound value's spelling "
        "is in it, so the replay doesn't start\n"
    )


def test_a_missing_secret_prints_none_of_the_values_bound_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FIRST)
    monkeypatch.delenv("AQA_SECRET_TEST_EMAIL", raising=False)
    # The password binds before the email is found missing, and the spec's
    # id and path spell its value.
    account = (
        "  account: { email: { secret: TEST_EMAIL }, "
        "password: { secret: TEST_PASSWORD } }\n"
    )
    secrets = (
        "secrets: { TEST_PASSWORD: { origins: [start], field: password }, "
        "TEST_EMAIL: { origins: [start], field: { role: textbox, name: Email } } }\n"
    )
    spec = shop(tmp_path / "qa", preconditions=account, config=secrets)
    spec = committed(renamed(spec, FIRST), compiled([]))

    result = replay(monkeypatch, [str(spec)], driver=False)

    assert result.exit_code == 12, result.output
    assert result.stderr == (
        "secret_unusable [SECRET:TEST_PASSWORD]: nothing was replayed\n  "
        "AQA_SECRET_TEST_EMAIL is not set: set it to the value of test secret "
        f"TEST_EMAIL, which {tmp_path / 'qa'}/[SECRET:TEST_PASSWORD].spec.md "
        "references at preconditions.account.email\n"
    )


def later(root: Path, second: str) -> Path:
    """The second spec, at `root`, for `second`: one that doesn't resolve,
    one replay refuses once its secret is bound, whose id spells both
    values, or one that runs and fails."""
    if second == "invalid":
        return shop(root, extra="colour: red\n")
    page = f"/page/{SECOND}" if second == "refused" else "/page/shop-wrong"
    spec = shop(root, preconditions=OTHER_ACCOUNT, config=OTHER_SECRETS, page=page)
    if second == "refused":
        spec = renamed(spec, f"{FIRST}-{SECOND}")
    return committed(spec, shop_script("shop-wrong"))


@pytest.mark.parametrize(
    ("second", "code"), [("invalid", 5), ("refused", 5), ("run", 1)]
)
def test_a_later_specs_stop_and_every_line_scan_with_every_bound_secret(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second: str,
    code: int,
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FIRST)
    monkeypatch.setenv("AQA_SECRET_OTHER_PASSWORD", SECOND)
    # Each spec is under a directory named for the other's value, so its
    # path and its run record's spell that value.
    first = shop(
        tmp_path / SECOND,
        preconditions=ACCOUNT,
        config=SECRETS,
        page="/page/shop-wrong",
    )
    specs = (
        committed(first, shop_script("shop-wrong")),
        later(tmp_path / FIRST, second),
    )

    result = replay(monkeypatch, on(app, *specs))

    assert result.exit_code == code, result.output
    texts = [result.stdout, result.stderr]
    assert copies_found(FIRST, texts=texts) + copies_found(SECOND, texts=texts) == []


def test_each_runs_record_scans_with_every_value_the_invocation_bound(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FIRST)
    monkeypatch.setenv("AQA_SECRET_OTHER_PASSWORD", SECOND)
    # The first spec's page shows the value only the second spec binds.
    monkeypatch.setitem(PAGES, "shows", f"<p>{SECOND}</p>")
    root = tmp_path / "a"
    first = shop(root, preconditions=ACCOUNT, config=SECRETS, page="/page/shows")
    second = shop(tmp_path / "b", preconditions=OTHER_ACCOUNT, config=OTHER_SECRETS)
    specs = (committed(first, compiled([])), committed(second, shop_script("shop")))

    result = replay(monkeypatch, on(app, *specs))

    assert result.exit_code == 0, result.output
    record = record_in(root)
    files = [path for path in record.rglob("*") if path.is_file()]
    assert copies_found(SECOND, files=files) == []
    snapshot = (record / "evidence" / "0" / "a11y_snapshot.yaml").read_text()
    assert "[SECRET:OTHER_PASSWORD]" in snapshot


def test_a_failed_reset_hook_exits_14_and_opens_no_browser(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    preconditions = "  reset: { http: POST /status/500 }\n"
    spec = committed(shop(root, preconditions=preconditions), shop_script("shop"))

    result = replay(monkeypatch, on(app, spec), browser=False)

    assert result.exit_code == 14, result.output
    assert re.fullmatch(
        rf"reset_unsafe shop: {SECONDS}, \$0, run record: {record_in(root)}\n"
        "  the reset hook answered 500\n",
        result.stderr,
    )


def test_an_unreachable_start_origin_exits_13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))
    closed = f"http://127.0.0.1:{unused_port()}"

    result = replay(monkeypatch, [str(spec), "--url", closed])

    assert result.exit_code == 13, result.output
    status, cause = result.stderr.splitlines()[:2]
    assert re.fullmatch(
        rf"unreachable shop: {SECONDS}, \$0, run record: {record_in(root)}", status
    )
    assert cause == "  an allowed host or the egress proxy couldn't be reached"


def test_a_driver_that_cannot_start_exits_13_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "/nonexistent/fake-4u49/node")
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))

    result = replay(monkeypatch, [str(spec)])

    assert (result.exit_code, result.stdout, result.stderr) == (
        13,
        "",
        NO_DRIVER,
    )
    assert not (root / ".aqa").exists()


def test_an_egress_listener_that_cannot_open_exits_13(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(_: LoopbackServer) -> LoopbackServer:
        raise OSError(errno.EMFILE, "fake-4u49 listener failure")

    monkeypatch.setattr(LoopbackServer, "__aenter__", refuse)
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))

    result = replay(monkeypatch, on(app, spec), browser=False)

    assert result.exit_code == 13, result.output
    assert re.fullmatch(
        rf"unreachable shop: {SECONDS}, \$0, run record: {record_in(root)}\n"
        "  the egress listener couldn't start\n",
        result.stderr,
    )


def refusing(name: str, *, failing: str | None) -> Callable[..., Any]:
    """`RunRecord`'s method `name`, raising an OSError for the file
    `failing`, or for every call when None."""
    method = getattr(RunRecord, name)

    def refused(record: RunRecord, first: Any, *args: Any, **kwargs: Any) -> Any:
        if failing in (None, first):
            raise OSError(errno.EIO, "fake write failure")
        return method(record, first, *args, **kwargs)

    return refused


@pytest.mark.parametrize(
    ("refused", "said"),
    [
        (
            ("step_completed", None),
            "the run record took no more lines, so the replay stopped",
        ),
        (
            ("write", "replay.json"),
            "replay.json could not be written to the run record",
        ),
    ],
    ids=["journal", "document"],
)
def test_a_record_that_takes_no_more_exits_15(
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refused: tuple[str, str | None],
    said: str,
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))
    method, failing = refused
    monkeypatch.setattr(RunRecord, method, refusing(method, failing=failing))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 15, result.output
    assert re.fullmatch(
        rf"record_error shop: {SECONDS}, \$0, run record: {record_in(root)}\n"
        f"  {said}\n",
        result.stderr,
    )


def test_a_refusal_that_reaches_the_replay_exits_15(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # None reaches it today: a refused journal line fails its step, and
    # refused evidence and egress records are dropped.
    async def refused(*_: object) -> None:
        raise RefusedWriteError("fake refused write")

    monkeypatch.setattr(executor, "capture_evidence", refused)
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 15, result.output
    assert re.fullmatch(
        rf"record_error shop: {SECONDS}, \$0, run record: {record_in(root)}\n"
        "  the run record took no more lines, so the replay stopped\n",
        result.stderr,
    )


def test_an_egress_block_whose_record_cant_be_written_still_exits_6(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    page = "/page/fetches?to=http%3A%2F%2Fundeclared.example.test%2Fx"
    spec = committed(shop(root, page=page), compiled([]))
    monkeypatch.setattr(RunRecord, "write", refusing("write", failing="egress.json"))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 6, result.output
    record = record_in(root)
    assert re.fullmatch(
        rf"egress_blocked shop: {SECONDS}, \$0, run record: {record}\n"
        "  the run record took no more lines, so the replay stopped\n"
        f"  the egress record couldn't be saved to {record / 'egress.json'}\n",
        result.stderr,
    )
    assert document(root)["error_code"] == "egress_blocked"


def test_a_document_that_spells_a_bound_value_isnt_written(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The spec's id is the bound value, and only the document holds it.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "shop")
    root = tmp_path / "qa"
    spec = shop(root, preconditions=ACCOUNT, config=SECRETS, page="/page/form")
    committed(spec, compiled([]))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 15, result.output
    record = record_in(root)
    assert re.fullmatch(
        rf"record_error \[SECRET:TEST_PASSWORD\]: {SECONDS}, \$0, run record: "
        rf"{record}\n  replay.json could not be written to the run record\n",
        result.stderr,
    )
    assert not (record / "replay.json").exists()


def test_a_launch_that_fails_exits_10_naming_the_host_fix(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*_: object, **__: object) -> None:
        raise PlaywrightError(ADVICE)

    monkeypatch.setattr(BrowserType, "launch", failing)
    spec = committed(shop(tmp_path / "qa"), shop_script("shop"))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 10, result.output
    assert HOST_FIXES.get(sys.platform, OTHER_FIX) in result.stderr
    assert ADVICE not in result.output


def test_a_selector_engine_left_unregistered_exits_10(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def skipped(_: object) -> None:
        pass

    monkeypatch.setattr(replay_module, "register_identity_engine", skipped)
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 10, result.output
    assert re.fullmatch(
        rf"no_sandbox shop: {SECONDS}, \$0, run record: {record_in(root)}\n"
        "  the binding engine isn't registered: call register_identity_engine "
        "before the browser session opens\n",
        result.stderr,
    )


def test_a_spec_root_that_cant_hold_a_run_record_exits_15_before_the_reset(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root, preconditions=RESET), shop_script("shop"))
    (root / ".aqa").write_text("not a directory\n")

    result = replay(monkeypatch, on(app, spec), browser=False)

    assert result.exit_code == 15, result.output
    assert re.fullmatch(
        rf"record_error shop: {SECONDS}, \$0\n"
        "  the run record could not be created; nothing was replayed\n",
        result.stderr,
    )
    assert app.paths() == []


def test_json_prints_each_runs_document_and_nothing_else(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root), shop_script("shop"))

    result = replay(monkeypatch, [*on(app, spec), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [document(root)]
    assert result.stderr == ""


def test_json_gives_a_run_whose_record_cant_be_made_the_same_fields(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrecorded = committed(shop(tmp_path / "a"), shop_script("shop"))
    (tmp_path / "a" / ".aqa").write_text("not a directory\n")
    recorded = committed(shop(tmp_path / "b"), shop_script("shop"))

    result = replay(monkeypatch, [*on(app, unrecorded, recorded), "--json"])

    assert result.exit_code == 15, result.output
    first, second = json.loads(result.stdout)
    assert first == {
        "schema_version": 1,
        "run_id": None,
        "spec_id": "shop",
        "mode": "strict",
        "outcome": "record_error",
        "exit_code": 15,
        "withheld": False,
        "error_code": None,
        "reasons": ["the run record could not be created; nothing was replayed"],
        "non_resumable": [],
        "infrastructure_events": {"count": 0, "events": []},
        "verdicts": [],
    }
    assert list(first) == list(second)


# Bound values a text scan of JSON would break or withhold whole: JSON's
# literals, and a value its marker, `[SECRET:TEST_PASSWORD]`, spells.
@pytest.mark.parametrize("value", ["null", "true", "pass"])
def test_json_prints_valid_json_that_spells_no_bound_value(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", value)
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS)
    committed(spec, compiled([]))

    result = replay(monkeypatch, [*on(app, spec), "--json"])

    assert (result.exit_code, result.stdout) == (15, "[]\n")
    assert result.stderr == (
        "the runs' documents are withheld, since they would spell a bound value\n"
    )


def test_json_prints_an_empty_array_for_a_stop_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")

    result = replay(monkeypatch, [str(spec), "--json"], driver=False)

    assert (result.exit_code, result.stdout) == (5, "[]\n")
    assert result.stderr == "spec_error shop: no compiled script: explore it first\n"


@pytest.mark.parametrize("page", ["shop", "shop-wrong"])
def test_quiet_prints_only_runs_that_didnt_pass(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    root = tmp_path / "qa"
    spec = committed(shop(root, page=f"/page/{page}"), shop_script(page))

    result = replay(monkeypatch, [*on(app, spec), "--quiet"])

    if page == "shop":
        assert (result.exit_code, result.output) == (0, "")
    else:
        assert result.exit_code == 1
        assert re.fullmatch(
            rf"expectation_violated shop: {SECONDS}, \$0, run record: "
            rf"{record_in(root)}\n  a2: expectation_violated\n",
            result.stderr,
        )


def test_json_with_quiet_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = committed(shop(tmp_path / "qa"), shop_script("shop"))

    result = replay(monkeypatch, [str(spec), "--json", "--quiet"], driver=False)

    assert result.exit_code == 2, result.output
    assert "--json" in result.stderr


def test_a_bound_value_is_in_no_line_or_document(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", FAKE_VALUE)
    monkeypatch.setitem(PAGES, "reflects", REFLECTS)
    fill = {"seq": 1, "action": "fill_secret", "target": "password"}
    fill |= {"secret": "TEST_PASSWORD", "side_effect": False}
    script = compiled(
        [fill],
        targets={
            "password": {
                "semantic": "the sign-in password",
                "locators": [{"css": "input[type=password]"}],
            },
            "said": {"semantic": "what was typed", "locators": [{"css": "#said"}]},
        },
        assertions=[
            {
                "id": "a1",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "said",
                "pattern": "nothing typed",
            }
        ],
    )
    root = tmp_path / "qa"
    spec = shop(root, preconditions=ACCOUNT, config=SECRETS, page="/page/reflects")
    committed(spec, script)

    result = replay(monkeypatch, on(app, spec))

    assert result.exit_code == 1, result.output
    record = record_in(root)
    files = [path for path in record.rglob("*") if path.is_file()]
    texts = [result.stdout, result.stderr]
    assert copies_found(FAKE_VALUE, files=files, texts=texts) == []
    assert document(root)["withheld"] is True
