"""What `aqa explore <spec>` prints (API.md §7; ADR-0026's #53 P17
amendment): every line goes through one boundary that scans the whole text
with the run's test secrets once they are bound, escapes it and scans it
again, and writes none of a text that still spells a bound value: fixed
fallback lines instead, each escaped before its check, with the run's
`non-resumable` fact kept, or nothing. Before a run, the budgets'
diagnostics and a compiled script kept up to date print with nothing bound.
The runner's explore and the browser driver are stand-ins here unless a test
says otherwise; no browser, no provider."""

import dataclasses
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import langsmith
import pytest
import typer.testing
from aqa_cli import explore as explore_module
from aqa_cli.main import app as aqa
from aqa_core.config import Budgets
from aqa_core.project import SpecError, load_project
from aqa_runner import explore as runner_explore
from aqa_runner.bound_secrets import bound_secrets
from aqa_runner.explore import Ended, Explored
from aqa_runner.redaction import Redactor
from playwright.async_api import Error as PlaywrightError

from packages.cli.tests.test_explore_runs import (
    ACCOUNT,
    KEY,
    SECRETS,
    UNCONFIRMED,
    explore,
    kept,
    shop,
    unlaunched,
    unstarted,
)
from packages.runner.tests.explore_graph_fixtures import REFUSED, Scripted
from packages.runner.tests.secret_fixtures import copies_found
from packages.runner.tests.test_explore_preflight import planned

START = "http://127.0.0.1:4100"
SENTINEL = "fake-os-detail-sentinel-4u49"
# What is written instead of a line that would still spell a bound value.
WITHHELD = "{} {}: the rest is withheld, since it would spell a bound value\n"
# Everything `_say` escapes, less the line break it joins lines with.
RAW_CONTROL = re.compile(
    r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
# Where each secret may go: the start origin's password field, or its
# Email textbox.
FIELDS = {
    "password": "{ origins: [start], field: password }",
    "email": "{ origins: [start], field: { role: textbox, name: Email } }",
}
INTENT = [{"part": "attempt-1", "seq": 1}]
WITHHELD_EGRESS = {
    "error_code": "egress_blocked",
    "refused_count": 1,
    "overflowed": False,
    "hosts_and_ports_withheld": True,
}
# A value its own markers spell again with each scan, one `x` fewer each
# time, though no marker alone spells it.
SELF_REPRODUCING = "[SECRET:TEST_PASSWORD]x"


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    yield
    # A router switches LangSmith's tracing off for the whole process.
    langsmith.configure(enabled=None, client=None)


class Runner:
    """`aqa_runner.explore.explore`'s stand-in: it returns `explored`, or
    raises it, and keeps each call's config."""

    def __init__(self, explored: Explored | BaseException) -> None:
        self.explored = explored
        self.calls: list[Any] = []

    async def __call__(
        self, _spec: Any, config: Any, *_args: Any, **_kwargs: Any
    ) -> Explored:
        self.calls.append(config)
        if isinstance(self.explored, BaseException):
            raise self.explored
        return self.explored


class Driver:
    """`async_playwright()`'s stand-in: `start()` returns this driver, or
    raises `starts`; `stop()` counts each stop, then raises `stops`."""

    def __init__(
        self, starts: BaseException | None = None, stops: BaseException | None = None
    ) -> None:
        self.starts = starts
        self.stops = stops
        self.stopped = 0

    def __call__(self) -> Driver:
        return self

    async def start(self) -> Driver:
        if self.starts is not None:
            raise self.starts
        return self

    async def stop(self) -> None:
        self.stopped += 1
        if self.stops is not None:
            raise self.stops


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    spec: Path,
    explored: Explored | BaseException,
    driver: Driver | None = None,
    *args: str,
) -> tuple[typer.testing.Result, Runner]:
    """`aqa explore <spec>` from START, with the runner's explore a stand-in
    giving `explored` and, when given, the driver `driver`."""
    runner = Runner(explored)
    monkeypatch.setattr(explore_module, "explore_spec", runner)
    if driver is not None:
        monkeypatch.setattr(explore_module, "async_playwright", driver)
    result = typer.testing.CliRunner().invoke(
        aqa, ["explore", str(spec), "--url", START, *args]
    )
    return result, runner


def ended(
    outcome: Ended,
    code: int,
    record: Path | str = "/runs/r1",
    cost: str = "0.003",
    **document: Any,
) -> Explored:
    """How a stand-in run ended: its outcome, exit code, record and cost, and
    its document's fields."""
    shaped: dict[str, object] = {
        "published": None,
        "reasons": [],
        "non_resumable": [],
        "egress": None,
    }
    return Explored(outcome, code, {**shaped, **document}, Path(record), Decimal(cost))


def signed_in(
    root: Path,
    spec_id: str = "shop",
    *,
    name: str = "TEST_PASSWORD",
    email: str | None = None,
) -> Path:
    """A project whose spec `spec_id` signs in with test secrets: its
    password the secret `name` and, when given, its email the secret
    `email`; the spec's path."""
    root.mkdir(parents=True, exist_ok=True)
    fields = {name: FIELDS["password"], **({email: FIELDS["email"]} if email else {})}
    listed = ", ".join(f"{each}: {field}" for each, field in fields.items())
    (root / "config.yaml").write_text(f"secrets: {{ {listed} }}\n")
    account = f"password: {{ secret: {name} }}"
    account += f", email: {{ secret: {email} }}" if email else ""
    path = root / f"{spec_id}.spec.md"
    path.write_text(
        f"---\nid: {json.dumps(spec_id)}\ngoal: A shopper saves their name.\n"
        f"preconditions:\n  start_url: /page/shop\n  account: {{ {account} }}\n"
        "expect:\n  - The shop says the name was saved\n---\n"
    )
    return path


def scan_of(spec: Path, spec_id: str = "shop") -> Redactor:
    """The scan a run of the spec at `spec` binds."""
    loaded = load_project(spec.parent).specs[spec_id]
    return Redactor(bound_secrets(loaded, START).values())


@pytest.mark.parametrize("published", ["confirmed", None], ids=["installed", "not"])
def test_the_line_names_the_compiled_script_whenever_it_was_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: str | None
) -> None:
    spec = shop(tmp_path / "qa")
    target = tmp_path / "qa" / ".compiled" / "shop.json"
    why = (
        "the compiled script was published, but the file system failed after it, "
        "so it may not survive a crash"
        if published
        else "the compiled script couldn't be written"
    )
    done = ended("record_error", 15, cost="0.015", published=published, reasons=[why])

    result, _ = invoke(monkeypatch, spec, done, Driver())

    assert result.exit_code == 15
    named = f"{target}, " if published else ""
    assert re.fullmatch(
        rf"record_error shop: \d+\.\ds, \$0\.015, {named}run record: /runs/r1\n  {why}\n",
        result.stderr,
    )


def test_a_bound_value_a_join_spells_is_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = ended("gave_up", 3, reasons=["the model refused to write the plan"])
    bound = shop(tmp_path / "bound", preconditions=ACCOUNT, config=SECRETS)
    unbound = shop(tmp_path / "unbound")

    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "gave_up shop")
    result, _ = invoke(monkeypatch, bound, done, Driver())
    control, _ = invoke(monkeypatch, unbound, done, Driver())

    assert result.exit_code == 3
    assert re.fullmatch(
        r"\[SECRET:TEST_PASSWORD\]: \d+\.\ds, \$0\.003, run record: /runs/r1\n"
        "  the model refused to write the plan\n",
        result.stderr,
    )
    assert copies_found("gave_up shop", texts=[result.stdout, result.stderr]) == []
    assert control.stderr.startswith("gave_up shop: ")


def test_a_bound_value_escaping_writes_is_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "x1b[31m")
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS)
    done = ended("gave_up", 3, reasons=["a1: failed \x1b[31mred"])

    result, _ = invoke(monkeypatch, spec, done, Driver())

    assert result.stderr.endswith("\n  a1: failed \\[SECRET:TEST_PASSWORD]red\n")
    assert copies_found("x1b[31m", texts=[result.stdout, result.stderr]) == []


def test_a_bound_value_in_a_printed_path_is_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "fake-root-4u49")
    spec = shop(
        tmp_path / "fake-root-4u49" / "qa", preconditions=ACCOUNT, config=SECRETS
    )
    done = ended("compiled", 0, cost="0.015", published="confirmed")

    result, _ = invoke(monkeypatch, spec, done, Driver())

    assert result.exit_code == 0
    assert re.fullmatch(
        rf"compiled shop: \d+\.\ds, \$0\.015, {re.escape(str(tmp_path))}"
        r"/\[SECRET:TEST_PASSWORD\]/qa/\.compiled/shop\.json\n",
        result.stdout,
    )


def test_a_refusal_after_binding_is_printed_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "fake-place-4u49")
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS)
    refused = SpecError(["fake-place-4u49: a bound value's spelling is in it"])

    result, _ = invoke(monkeypatch, spec, refused, Driver())

    assert (result.exit_code, result.stdout, result.stderr) == (
        5,
        "",
        (
            "spec_error shop: nothing was explored\n"
            "  [SECRET:TEST_PASSWORD]: a bound value's spelling is in it\n"
        ),
    )


def test_the_output_scan_binds_what_the_run_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "fake-pass-4u49")
    monkeypatch.setenv("AQA_SECRET_TEST_EMAIL", "fake-mail-4u49")
    spec = signed_in(tmp_path / "qa", email="TEST_EMAIL")
    made: dict[str, list[set[str]]] = {"cli": [], "runner": []}

    def recording(where: str) -> Any:
        def made_with(secrets: Any) -> Redactor:
            secrets = list(secrets)
            made[where].append({each.value.get_secret_value() for each in secrets})
            return Redactor(secrets)

        return made_with

    monkeypatch.setattr(explore_module, "Redactor", recording("cli"))
    monkeypatch.setattr(runner_explore, "Redactor", recording("runner"))

    result = explore(monkeypatch, [str(spec), "--url", START], Scripted(REFUSED))

    assert result.exit_code == 3, result.output
    [cli] = made["cli"]
    assert cli == {"fake-pass-4u49", "fake-mail-4u49"}
    assert made["runner"] == [cli]


@pytest.mark.parametrize(
    ("saved", "on_disk", "words"),
    [
        (True, True, "is"),
        (False, False, "couldn't be saved to"),
        # The run's fact decides, not what happens to be on disk.
        (False, True, "couldn't be saved to"),
    ],
    ids=["saved", "unsaved", "unsaved-file-present"],
)
def test_the_egress_record_line_says_whether_the_run_saved_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved: bool,
    on_disk: bool,
    words: str,
) -> None:
    spec = shop(tmp_path / "qa")
    record = tmp_path / "qa" / ".aqa" / "runs" / "r1"
    record.mkdir(parents=True)
    if on_disk:
        (record / "egress.json").write_text(json.dumps(WITHHELD_EGRESS) + "\n")
    reason = "the run record took no more lines, so the attempt stopped"
    done = ended(
        "egress_blocked",
        6,
        record=record,
        egress=WITHHELD_EGRESS,
        non_resumable=INTENT,
        reasons=[reason],
    )

    result, _ = invoke(
        monkeypatch, spec, dataclasses.replace(done, egress_saved=saved), Driver()
    )

    assert (result.exit_code, result.stdout) == (6, "")
    lines = result.stderr.splitlines()
    assert lines[0].startswith("egress_blocked shop: ")
    assert lines[0].endswith(f"run record: {record}, non-resumable")
    assert lines[1:] == [
        f"  {reason}",
        f"  the egress record {words} {record / 'egress.json'}",
    ]


def marker_refusal() -> str:
    """The runner's own words for markers that spell a bound value."""
    [words] = {
        text
        for text in vars(runner_explore).values()
        if isinstance(text, str) and "a marker, [SECRET:<name>]" in text
    }
    return words


def test_a_marker_admission_refusal_prints_only_the_fixed_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TEST_PASSWORD's markers, [SECRET:TEST_PASSWORD], spell its value.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", "SECRET")
    spec = signed_in(tmp_path / "qa")
    scan = scan_of(spec)
    lines = ["spec_error shop: nothing was explored", f"  {marker_refusal()}"]
    rendered = scan.redact("\n".join(scan.redact(line) for line in lines))
    monkeypatch.setattr(explore_module, "async_playwright", Driver())
    client = Scripted()

    result = explore(monkeypatch, [str(spec), "--url", START], client)

    assert scan.finds(rendered)
    assert (result.exit_code, result.stdout, result.stderr) == (
        5,
        "",
        WITHHELD.format("spec_error", "shop"),
    )
    assert not scan.finds(result.output)
    assert copies_found("SECRET", texts=[result.stdout, result.stderr]) == []
    assert client.requests == []


def test_the_run_line_falls_back_on_its_own_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TEST_DRIVER's markers spell its value, which the printed path holds.
    monkeypatch.setenv("AQA_SECRET_TEST_DRIVER", "driver")
    spec = signed_in(tmp_path / "driver" / "qa", name="TEST_DRIVER")
    done = ended("compiled", 0, cost="0.015", published="unconfirmed")

    result, _ = invoke(monkeypatch, spec, done, Driver())

    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        WITHHELD.format("compiled", "shop"),
        WITHHELD.format("compiled", "shop"),
    )


@pytest.mark.parametrize(
    ("spec_id", "stderr"),
    [
        ("dri\x1b[31mver", WITHHELD.format("unreachable", "dri\\x1b[31mver")),
        ("\u202eshop", WITHHELD.format("unreachable", "\\u202eshop")),
        # A bidi control is a gap, which the scan reads as written or
        # escaped: the escaped fallback still spells `driver`.
        ("dri\u202ever", ""),
    ],
    ids=["ansi-in-value", "bidi", "bidi-in-value"],
)
def test_a_fallback_is_written_escaped_as_it_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec_id: str, stderr: str
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_DRIVER", "driver")
    spec = signed_in(tmp_path / "qa", spec_id, name="TEST_DRIVER")
    scan = scan_of(spec, spec_id)
    failing = Driver(starts=PlaywrightError(SENTINEL))

    result, runner = invoke(monkeypatch, spec, ended("gave_up", 3), failing)

    assert scan.markers_spell_a_value
    assert (result.exit_code, result.stdout, result.stderr) == (13, "", stderr)
    assert not scan.finds(result.output)
    assert copies_found("driver", texts=[result.stdout, result.stderr]) == []
    assert RAW_CONTROL.search(result.output) is None
    assert SENTINEL not in result.output
    assert runner.calls == []


@pytest.mark.parametrize("path", ["refusal", "run-line"])
def test_a_fallback_line_its_escaping_spells_is_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    # X1B5's markers spell x1b5, so the text falls back; the fallback's
    # first line spells it only once escaped, as `shop\x1b5`. The refusal
    # has no other line; the run's line keeps its non-resumable one.
    monkeypatch.setenv("AQA_SECRET_X1B5", "x1b5")
    spec = signed_in(tmp_path / "qa", "shop\x1b5", name="X1B5")
    scan = scan_of(spec, "shop\x1b5")
    outcome = "unreachable" if path == "refusal" else "egress_blocked"
    fallback = WITHHELD.format(outcome, "shop\x1b5").rstrip("\n")
    if path == "refusal":
        failing = Driver(starts=PlaywrightError(SENTINEL))
        result, _ = invoke(monkeypatch, spec, ended("gave_up", 3), failing)
    else:
        done = ended("egress_blocked", 6, non_resumable=INTENT)
        result, _ = invoke(monkeypatch, spec, done, Driver())

    assert not scan.finds(fallback)
    assert scan.finds(fallback.replace("\x1b", "\\x1b"))
    expected = (13, "", "") if path == "refusal" else (6, "", "non-resumable\n")
    assert (result.exit_code, result.stdout, result.stderr) == expected


@pytest.mark.parametrize("intent", [True, False], ids=["unresolved-intent", "none"])
def test_a_withheld_summary_keeps_its_non_resumable_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, intent: bool
) -> None:
    # Each scan of the record path leaves one `x` fewer, and the second
    # result still spells the value.
    monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", SELF_REPRODUCING)
    spec = signed_in(tmp_path / "qa")
    scan = scan_of(spec)
    done = ended(
        "egress_blocked",
        6,
        record=f"/qa/{SELF_REPRODUCING}xx/.aqa/runs/r1",
        egress=WITHHELD_EGRESS,
        non_resumable=INTENT if intent else [],
        reasons=["the attempt asked for a host the run doesn't allow"],
    )

    result, _ = invoke(monkeypatch, spec, done, Driver())

    assert not scan.markers_spell_a_value
    expected = WITHHELD.format("egress_blocked", "shop")
    expected += "non-resumable\n" if intent else ""
    assert (result.exit_code, result.stdout, result.stderr) == (6, "", expected)
    assert not scan.finds(result.output)


def test_the_non_resumable_line_stands_alone_when_the_other_spells_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AQA_SECRET_TEST_SHOP", "shop")
    spec = signed_in(tmp_path / "qa", name="TEST_SHOP")
    done = ended("egress_blocked", 6, non_resumable=INTENT)

    result, _ = invoke(monkeypatch, spec, done, Driver())

    assert (result.exit_code, result.stdout, result.stderr) == (
        6,
        "",
        "non-resumable\n",
    )


@pytest.mark.parametrize("case", ["marker-unsafe", "marker-safe", "join-clean"])
def test_a_fallback_whose_join_spells_a_value_keeps_its_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    if case == "marker-safe":
        # Both marker-safe: the password reproduces through its own markers
        # in the record path, so the summary falls back; the email spans
        # the end of the fallback's first line and the status line's start.
        monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", SELF_REPRODUCING)
        monkeypatch.setenv("AQA_SECRET_TEST_EMAIL", "lue\nnon")
        spec = signed_in(tmp_path / "qa", email="TEST_EMAIL")
        record = f"/qa/{SELF_REPRODUCING}xx/.aqa/runs/r1"
    else:
        # ZQX_4U49's markers spell its value, which the record path holds.
        monkeypatch.setenv("AQA_SECRET_ZQX_4U49", "zqx_4u49")
        password = "lue\nnon" if case == "marker-unsafe" else "fake-pass-4u49"
        monkeypatch.setenv("AQA_SECRET_TEST_PASSWORD", password)
        spec = signed_in(tmp_path / "qa", email="ZQX_4U49")
        record = "/qa/zqx_4u49/.aqa/runs/r1"
    scan = scan_of(spec)
    first = WITHHELD.format("egress_blocked", "shop").rstrip("\n")
    joined = f"{first}\nnon-resumable"
    done = ended(
        "egress_blocked",
        6,
        record=record,
        egress=WITHHELD_EGRESS,
        non_resumable=INTENT,
        reasons=["the attempt asked for a host the run doesn't allow"],
    )

    result, _ = invoke(monkeypatch, spec, done, Driver())

    assert not scan.finds(first)
    assert not scan.finds("non-resumable")
    assert scan.finds(joined) is (case != "join-clean")
    assert scan.finds("") is False
    if case == "marker-safe":
        assert not scan.markers_spell_a_value
    expected = f"{joined}\n" if case == "join-clean" else "non-resumable\n"
    assert (result.exit_code, result.stdout, result.stderr) == (6, "", expected)
    assert not scan.finds(result.output)


def test_a_compiled_script_that_cannot_be_read_exits_15_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")
    target = kept(spec)
    target.chmod(0)
    client = Scripted()
    try:
        result = unstarted(monkeypatch, [str(spec)], client)
    finally:
        target.chmod(0o644)

    assert result.exit_code == 15, result.output
    assert result.stderr == (
        f"record_error {spec}: the compiled script could not be read; nothing was planned\n"
    )
    assert client.models == []


def test_an_up_to_date_script_is_kept_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    spec = shop(tmp_path / "qa")
    target = kept(spec)
    before = target.read_bytes()
    client = Scripted()

    result = unstarted(monkeypatch, [str(spec)], client)

    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        f"up to date shop: {target}\n",
        "",
    )
    assert target.read_bytes() == before
    assert not (tmp_path / "qa" / ".aqa").exists()
    assert client.models == []


def test_an_up_to_date_check_binds_no_test_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("AQA_SECRET_TEST_PASSWORD", raising=False)
    # A protocol log would be refused while a secret is bound.
    monkeypatch.setenv("DEBUG", "pw:protocol")
    spec = shop(tmp_path / "qa", preconditions=ACCOUNT, config=SECRETS)
    target = kept(spec)

    result = unstarted(monkeypatch, [str(spec)])

    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        f"up to date shop: {target}\n",
        "",
    )


def test_an_up_to_date_unconfirmed_script_prints_its_warning_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")
    target = kept(spec, confirmed=False)

    result = unstarted(monkeypatch, [str(spec)])

    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        f"up to date shop: {target}\n",
        UNCONFIRMED.format(target),
    )


def test_a_script_compiled_under_other_subject_contracts_is_explored_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contracted = "subjects: [{ spec: shop, expect: 0, region: main, part: p }]\n"
    spec = shop(tmp_path / "qa")
    target = kept(spec)
    (tmp_path / "qa" / "config.yaml").write_text(contracted)
    before = target.read_bytes()
    client = Scripted(REFUSED)

    result = unlaunched(
        monkeypatch, [str(spec), "--url", "http://127.0.0.1:4100"], client
    )

    assert result.exit_code == 3, result.output
    assert len(client.requests) == 1
    assert target.read_bytes() == before


def test_a_compiled_script_that_does_not_load_exits_5_unless_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")
    target = kept(spec)
    target.write_text("{}\n")
    args = [str(spec), "--url", "http://127.0.0.1:4100"]
    client = Scripted(REFUSED)

    refused = unlaunched(monkeypatch, args, client)
    forced = unlaunched(monkeypatch, [*args, "--force"], client)

    assert refused.exit_code == 5, refused.output
    assert refused.stderr.startswith(
        f"spec_error {spec}: the compiled script doesn't load, so nothing was "
        "explored: correct it, or pass --force to explore the spec again\n  "
    )
    assert forced.exit_code == 3, forced.output
    assert len(client.requests) == 1


@pytest.mark.parametrize("force", [False, True], ids=["kept", "forced"])
def test_a_compiled_directory_that_is_a_link_exits_5_even_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    spec = shop(tmp_path / "qa")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "qa" / ".compiled").symlink_to(elsewhere)
    args = [
        str(spec),
        "--url",
        "http://127.0.0.1:4100",
        *(["--force"] if force else []),
    ]
    client = Scripted(planned())

    result = unlaunched(monkeypatch, args, client)

    assert result.exit_code == 5, result.output
    assert f"{tmp_path / 'qa' / '.compiled'}: a link" in result.stderr
    assert client.requests == []


API = Path(__file__).parents[3] / "API.md"


def test_api_md_lists_every_explore_option() -> None:
    text = API.read_text()
    block = text[text.index("aqa explore <spec>") :]
    block = block[: block.index("```")]
    for option in ("--url", "--plan-only", "--force", "--budget <name>=<value>"):
        assert option in block
    for name in Budgets.model_fields:
        assert name in block
    assert re.search(r"--confirm-repeat\b.*\(planned, #56\)", block)
    assert (
        "--confirm-repeat"
        not in typer.testing.CliRunner().invoke(aqa, ["explore", "--help"]).stdout
    )


def test_explore_help_states_the_m1_rules() -> None:
    result = typer.testing.CliRunner().invoke(aqa, ["explore", "--help"])

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    words = " ".join(re.sub(r"[│╭╮╰╯─]", " ", plain).split())
    assert "In M1, a phase whose traffic used HTTPS or a WebSocket exits 14" in words
    assert "M1 makes one attempt, whatever attempts says" in words


GIVE = (
    "  --budget: give NAME=VALUE, NAME one of attempts, actions_per_attempt, "
    "model_usd, minutes, resolve_seconds"
)


@pytest.mark.parametrize(
    ("overrides", "line"),
    [
        ([SENTINEL], GIVE),
        ([f"{SENTINEL}=1", "neither"], GIVE),
        (
            [f"minutes={SENTINEL}"],
            "  --budget: minutes: Input should be a valid number",
        ),
        (
            [f'minutes="{SENTINEL}"'],
            "  --budget: minutes: Input should be a valid number",
        ),
        (["minutes=true"], "  --budget: minutes: Input should be a valid number"),
        (["minutes=1", f"minutes={SENTINEL}"], "  --budget: minutes: given twice"),
        (["minutes=0"], "  --budget: minutes: Input should be greater than 0"),
        (
            ["actions_per_attempt=1.5"],
            "  --budget: actions_per_attempt: Input should be a valid integer",
        ),
        (
            ["model_usd=Infinity"],
            "  --budget: model_usd: Input should be a finite number",
        ),
    ],
    ids=[
        "malformed",
        "unknown",
        "text",
        "json-text",
        "bool",
        "twice",
        "zero",
        "fraction",
        "infinite",
    ],
)
def test_a_budget_diagnostic_names_the_budget_and_the_rule_never_what_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: list[str], line: str
) -> None:
    spec = shop(tmp_path / "qa")
    budget = [arg for override in overrides for arg in ("--budget", override)]
    client = Scripted()

    result = unstarted(monkeypatch, [str(spec), *budget], client)

    assert result.exit_code == 5, result.output
    assert result.stderr == f"spec_error {spec}: nothing was explored\n{line}\n"
    assert SENTINEL not in result.output
    assert client.models == []


def test_plan_only_refuses_the_options_only_exploring_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")
    args = [str(spec), "--plan-only", "--force", "--budget", "minutes=1"]

    result = unstarted(monkeypatch, args)

    assert result.exit_code == 2, result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stderr)
    assert "Invalid value for '--force', '--budget'" in " ".join(
        re.sub(r"[│╭╮╰╯─]", " ", plain).split()
    )


def test_a_script_disagreeing_with_unchanged_contracts_exits_5_unless_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")
    contracted = {
        "name": {
            "semantic": "the name field",
            "locators": [{"css": "input"}],
            "contract": {"region": "section.empty", "part": "p"},
        }
    }
    kept(spec, targets=contracted)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    client = Scripted(REFUSED)

    refused = explore(monkeypatch, [str(spec)], client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    forced = explore(
        monkeypatch, [str(spec), "--url", "http://127.0.0.1:4100", "--force"], client
    )

    assert refused.exit_code == 5, refused.output
    assert refused.stderr == (
        f"spec_error {spec}: the compiled script disagrees with the project's "
        "subject contracts, so nothing was explored: correct them, or pass --force "
        "to explore the spec again\n"
        "  targets.name: carries a subject contract no row gives it\n"
    )
    assert forced.exit_code == 3, forced.output
    assert len(client.requests) == 1


def test_a_repaired_script_is_kept_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = shop(tmp_path / "qa")
    repaired = {"name": {"semantic": "the name field", "locators": [{"css": "input"}]}}
    target = kept(spec, targets=repaired)

    result = unstarted(monkeypatch, [str(spec)])

    assert (result.exit_code, result.stdout) == (0, f"up to date shop: {target}\n")
