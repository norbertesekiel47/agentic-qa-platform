"""What `test_report_page.py` and `test_report_browser.py` share (#249): real runs
on #240's drift pages, one per outcome the report pins and one that binds a test
secret, each made once per module and copied for a test, which may edit its copy
as a person could; `aqa report` of a copy; and the page read back as a tree."""

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import langsmith
import pytest
import typer.testing
from aqa_cli import run as run_module
from aqa_cli.main import app as aqa
from aqa_core.compiled import CompiledScript

from packages.cli.tests.test_replay import committed
from packages.cli.tests.test_run import KEY, order_at
from packages.runner.tests.drift_fixtures import order_spec, page, serving
from packages.runner.tests.explore_graph_fixtures import Scripted
from packages.runner.tests.heal_fixtures import rebinding
from packages.runner.tests.secret_fixtures import BOUND_AT_START, OTHER_FAKE_VALUE

# The goal every drift page's spec has.
GOAL = "A customer finds out when their order ships."
# How a run's line gives its outcome, time and cost.
_LINE = re.compile(
    r"(?P<outcome>\S+) order: (?P<seconds>\d+\.\ds), (?P<cost>\$[0-9.]+)"
)


@dataclass(frozen=True)
class Ran:
    """A finished run: its spec root, its record, and what it printed."""

    root: Path
    record: Path
    stdout: str
    stderr: str

    @property
    def seconds(self) -> str:
        """The time its line gave, as `<s>s`."""
        return self._line()["seconds"]

    @property
    def cost(self) -> str:
        """The cost its line gave, as `$<cost>`."""
        return self._line()["cost"]

    @property
    def diff(self) -> list[str]:
        """The heal proposal's diff it printed, as `test_run.printed_diff` reads
        it."""
        lines = [line.removeprefix("  ") for line in self.stderr.splitlines()]
        return lines[lines.index("--- a/.compiled/order.json") :]

    def _line(self) -> dict[str, str]:
        found = _LINE.match(self.stdout or self.stderr)
        assert found is not None, self.stdout + self.stderr
        return found.groupdict()

    def replay_json(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self.record / "replay.json").read_text())
        return loaded

    def edit_replay_json(self, edit: Callable[[dict[str, Any]], object]) -> None:
        document = self.replay_json()
        edit(document)
        (self.record / "replay.json").write_text(json.dumps(document, indent=2))


def _the_record(root: Path) -> Path:
    [record] = (root / ".aqa" / "runs").iterdir()
    return record


def replayed(root: Path, html: str, script: CompiledScript, expectation: str) -> Ran:
    """`aqa replay` of the order spec under `root`, expecting `expectation`, with
    `script` committed, against the drift page `html`."""
    spec = committed(order_spec(root, expectation), script)
    with serving(html) as app:
        result = typer.testing.CliRunner().invoke(
            aqa, ["replay", str(spec), "--url", app.origin]
        )
    return Ran(root, _the_record(root), result.stdout, result.stderr)


def replayed_with_a_secret(root: Path, script: CompiledScript, expectation: str) -> Ran:
    """`replayed` of the clean order page, with the spec binding a test secret, so
    its evidence takes the withheld form (ADR-0026's #47 amendment)."""
    spec = order_spec(root, expectation)
    account = "{ email: reader@example.test, password: { secret: TEST_PASSWORD } }"
    spec.write_text(
        spec.read_text().replace(
            "preconditions:\n", f"preconditions:\n  account: {account}\n"
        )
    )
    config = root / "config.yaml"
    config.write_text(f"{config.read_text()}secrets: {{ {BOUND_AT_START} }}\n")
    committed(spec, script)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("AQA_SECRET_TEST_PASSWORD", OTHER_FAKE_VALUE)
        with serving(page("order")) as app:
            result = typer.testing.CliRunner().invoke(
                aqa, ["replay", str(spec), "--url", app.origin]
            )
    return Ran(root, _the_record(root), result.stdout, result.stderr)


def healed(root: Path) -> Ran:
    """`aqa run` of the order spec under `root` on the moved page, whose scripted
    healer rebinds the toggle: a run that ends `drift_consistent`."""
    spec = order_at(root)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("ANTHROPIC_API_KEY", KEY)
            client = Scripted(rebinding("button", "Show details"))
            patch.setattr(run_module, "AnthropicClient", client)
            with serving(page("moved")) as app:
                result = typer.testing.CliRunner().invoke(
                    aqa, ["run", str(spec), "--url", app.origin]
                )
    finally:
        # A router switches LangSmith's tracing off for the whole process.
        langsmith.configure(enabled=None, client=None)
    return Ran(root, _the_record(root), result.stdout, result.stderr)


def copied(ran: Ran, to: Path) -> Ran:
    """`ran`'s spec root copied under `to`, links as they are."""
    root = shutil.copytree(ran.root, to / "qa", symlinks=True)
    return Ran(root, root / ".aqa" / "runs" / ran.record.name, ran.stdout, ran.stderr)


def reported(ran: Ran, monkeypatch: pytest.MonkeyPatch) -> typer.testing.Result:
    """`aqa report <id>` of `ran`'s record, from its spec root."""
    monkeypatch.chdir(ran.root)
    return typer.testing.CliRunner().invoke(aqa, ["report", ran.record.name])


@dataclass
class Element:
    """An element of a page, its attributes, and what it holds."""

    tag: str
    attrs: dict[str, str | None]
    children: list[Element | str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The text it holds, its descendants' included, as a browser lays it
        out: a block apart from what is around it, spaces collapsed."""
        parts = [
            c if isinstance(c, str) else f" {c.text} " if c.tag in _BLOCKS else c.text
            for c in self.children
        ]
        return " ".join("".join(parts).split())

    @property
    def raw_text(self) -> str:
        """The text it holds, as it is."""
        return "".join(c if isinstance(c, str) else c.raw_text for c in self.children)

    def all(self, test: Callable[[Element], bool]) -> list[Element]:
        """Every element below it that `test` takes, in document order."""
        found = []
        for child in self.children:
            if isinstance(child, Element):
                if test(child):
                    found.append(child)
                found.extend(child.all(test))
        return found

    def one(self, test: Callable[[Element], bool]) -> Element:
        [found] = self.all(test)
        return found


def having(name: str, value: str | None = None) -> Callable[[Element], bool]:
    """A test for an element with the attribute `name`, of `value` when given."""
    return lambda e: name in e.attrs and (value is None or e.attrs[name] == value)


def tagged(tag: str) -> Callable[[Element], bool]:
    return lambda e: e.tag == tag


_VOID = frozenset({"meta", "br", "hr", "img", "input", "link", "wbr", "source"})
# The elements the page lays out as blocks, its flex and grid items included.
_BLOCKS = frozenset(
    {
        "blockquote",
        "button",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "header",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "ul",
    }
)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {})
        self.open = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, dict(attrs))
        self.open[-1].children.append(element)
        if tag not in _VOID:
            self.open.append(element)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        while self.open[-1].tag != tag:
            self.open.pop()
        self.open.pop()

    def handle_data(self, data: str) -> None:
        self.open[-1].children.append(data)


def tree(html: str) -> Element:
    """`html` as a tree of elements."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root
