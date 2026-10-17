"""The read-article fixture and its proof (#51, ADR-0023): seven establishing
checks replayed by the real adapter, then reports judged against the source."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from itertools import count
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch

import aqa_core.project as core
import flags
import pilot
import pilot_read_article
import pilot_replay
from aqa_core.compiled import ByCss, CompiledScript, Target
from aqa_core.compiler import assertion_for, contracts_by_meaning
from aqa_core.coverage_plan import CoveragePlan, misfits, plan_hash, planned_checks
from aqa_core.schema import Contract
from aqa_core.spec import canonical_hash
from aqa_runner.egress_proxy import EgressBlocks
from aqa_runner.evaluation import AssertionOutcome, AssertionResult
from aqa_runner.executor import RunResult, StepResult
from aqa_runner.invariants import INVARIANTS, InvariantResult
from pilot_inputs import PilotInput, load_pilots, validate_pilot
from pilot_rebinding import apply_rebinding
from pilot_replay import AttemptHealth, ObservedAttempt, replay_pilot
from pilot_results import observe
from test_flags import FakeDocker
from test_pilot_replay import CLIENTS

ROOT = Path(__file__).resolve().parents[2]
QA, FIXTURES = ROOT / "bench/apps/conduit/qa", ROOT / "bench/harness/fixtures"
ORIGIN, COLUMN = "http://127.0.0.1:4100", "div.col-xs-12.col-md-8.offset-md-2"
AUTHOR = Contract(region="div.banner", part="a.author", leaf=True)
DATE = Contract(region="div.banner", part="span.date", leaf=True)
TITLE, BODY = "Testing without flakes", "Most flaky tests are really flaky data."
QUOTE = "Deterministic data helped us most."
ASK = "Sign in or sign up to add comments on this article"
CHECKS = [  # id | target | css | text, for expectations 0 to 6
    line.split(" | ")
    for line in f"""a1 | banner_title | div.banner h1 | {TITLE}
a2 | banner_author | a.author | anna
a3 | banner_date | span.date | January 4, 2026
a4 | article_body | div.article-content > div.col-md-12 > div | {BODY}
a5 | article_tags | div.article-content ul.tag-list | testing
a6 | comment_list | {COLUMN} > div.comment-list | {QUOTE}
a7 | comment_prompt | {COLUMN} > div:not(.comment-list) | {ASK}""".splitlines()
]
PREVIEW = 'a.preview-link[href="/article/testing-without-flakes"]'
HEALTHY = AttemptHealth(
    reset_completed=True, infrastructure=False, egress_blocked=False
)
F: Final = "failed"
U: Final = "binding_unresolved"
# Conduit's signed-out article page (article.component.html), and its feed.
FEED = """<a class="preview-link" href="/article/how-to-train-your-dragon">Dragons</a>
<a class="preview-link" href="/article/testing-without-flakes"><h1>Testing</h1></a>"""
PAGE = """<!doctype html><nav><a href="/login">Sign in</a> <a href="/register">Sign up</a>
{header}</nav><div class="article-page"><div class="banner"><h1>{title}</h1>
<div class="article-meta"><a class="author" href="/a">{author}</a>
<span class="date">{date}</span></div></div><div class="container page">
<div class="row article-content"><div class="col-md-12"><div>{body}</div>
<ul class="tag-list">{tags}</ul></div></div><hr><div class="article-actions">
<div class="article-meta"><a class="author" href="/a">anna</a>
<span class="date">January 4, 2026</span></div>{footer}</div><div class="row">
<div class="col-xs-12 col-md-8 offset-md-2">{prompt}<app-list-errors>{errors}
</app-list-errors>{list}</div></div></div></div>"""
CARD = """<app-article-comment><div class="card"><p class="card-text">{}</p>
<a class="comment-author" href="/r">reader</a> <span>January 4, 2026</span></div>
</app-article-comment>"""
PROMPT = '<div><a href="/login">Sign in</a> or <a href="/register">sign up</a> to add comments on this article.</div>'
MINE, OTHER, BOTH = (CARD.format(t) for t in (QUOTE, "Other", f"{QUOTE} {BODY}"))
LIST, BLANK = '<div class="comment-list">{}</div>', "<div></div>"
FORM = f"<div><app-list-errors>{QUOTE}</app-list-errors><form></form></div>"
DECOYS = {
    "prompt": PROMPT.replace("</div>", f" {QUOTE}</div>"),
    "body": f"{BODY} {QUOTE}",
    "footer": QUOTE,
    "errors": QUOTE,
}
PROMPTS = {
    "no Sign in link": PROMPT.replace('<a href="/login">Sign in</a>', ""),
    "no sign up link": PROMPT.replace('<a href="/register">sign up</a>', ""),
    "another instruction": '<div><a href="/login">Read</a> this article.</div>',
    "blank prompt": BLANK,
}
# Each row: a change to the page, and every check's outcome other than a pass.
ROWS: list[tuple[str, dict[str, str], dict[str, str]]] = [
    ("clean", {}, {}),
    ("wrong banner title", {"title": "Other", "header": TITLE}, {"a1": F}),
    ("wrong banner author, anna in the footer", {"author": "jake"}, {"a2": F}),
    ("January 3 banner, January 4 comment", {"date": "January 3, 2026"}, {"a3": F}),
    ("body only in a comment", {"body": "Other", "comments": BOTH}, {"a4": F}),
    ("no testing tag", {"tags": "<li>flakes</li>"}, {"a5": F}),
    ("no comments", {"comments": ""}, {"a6": F}),
    ("quote in the first of two", {"comments": MINE + OTHER}, {}),
    ("quote in the second of two", {"comments": OTHER + MINE}, {}),
    ("quote in neither of two", {"comments": OTHER + OTHER}, {"a6": F}),
    *(
        (f"quote only in the {k}", {"comments": OTHER, k: v}, {"a6": F})
        for k, v in DECOYS.items()
    ),
    ("signed-in form", {"comments": OTHER, "prompt": FORM}, {"a6": F, "a7": F}),
    *((label, {"prompt": prompt}, {"a7": F}) for label, prompt in PROMPTS.items()),
    ("instruction in the header", {"prompt": BLANK, "header": ASK}, {"a7": F}),
    ("no comment-list wrapper", {"list": "{}"}, {"a6": U}),
    ("two comment-list wrappers", {"list": LIST + LIST.format("")}, {"a6": U}),
]
FAKE, Z = "fake-sensitive-canary", "sha256:" + "0" * 64
BUGS = [f"conduit-bug-00{n}" for n in range(1, 6)]
DEV = ["conduit-benign-001", "conduit-benign-002", *BUGS]
DROP = object()
MALFORMED = "error: the report or the fixture's inputs can't be read\n"


def article(**changes: str) -> str:
    parts = {"header": "", "title": TITLE, "author": "anna", "date": "January 4, 2026"}
    parts |= {"body": f"<p>{BODY}</p>", "tags": "<li>testing</li>", "footer": ""}
    parts |= {"prompt": PROMPT, "errors": "", "list": LIST, "comments": MINE}
    parts |= changes
    return PAGE.format_map(parts | {"list": parts["list"].format(parts["comments"])})


def trapped(test: unittest.TestCase, owners: list[Any]) -> list[str]:
    """Calls to each trapped constructor or (owner, function), each shown to fire."""
    calls: list[str] = []

    def refuse(*_: object, **__: object) -> None:
        calls.append("called")
        raise RuntimeError

    for owner in owners:
        held, name = owner if isinstance(owner, tuple) else (owner, "__init__")
        test.enterContext(patch.object(held, name, refuse))
        test.assertRaises(
            RuntimeError, held if name == "__init__" else getattr(held, name)
        )
    test.assertEqual(len(calls), len(owners))
    calls.clear()
    return calls


def result(
    *, js: int = 0, console: int = 0, index: int = 0, **checks: AssertionOutcome
) -> RunResult:
    """A replay's result: both steps settled, each check as given or passed,
    on its first locator unless unresolved."""
    steps = tuple(
        StepResult(
            n, "completed", locator_index=None if n == 0 else index, settled="idle"
        )
        for n in (0, 1)
    )
    totals = {"js_exceptions": js, "console_errors": console}
    seen = tuple(
        InvariantResult(
            i, "violated" if totals.get(i) else "held", (), totals.get(i, 0)
        )
        for i in INVARIANTS
    )
    outcomes = [checks.get(f"a{n}", "pass") for n in range(1, 8)]
    done = tuple(
        AssertionResult(f"a{n}", o, locator_index=None if o == U else 0)
        for n, o in enumerate(outcomes, 1)
    )
    return RunResult(
        "r",
        "failed" if js or console or checks else "passed",
        steps,
        done,
        (),
        (),
        seen,
        EgressBlocks((), False),
        None,
        0,
    )


def edited(report: object, path: str, value: object) -> Any:
    """A copy of `report` whose value at the dotted `path` is set, appended or dropped."""
    changed: Any = json.loads(json.dumps(report))
    *parents, last = [int(p) if p.isdigit() else p for p in path.split(".")]
    held = changed
    for key in parents:
        held = held[key]
    if value is DROP:
        del held[last]
    elif isinstance(held, list) and last == len(held):
        held.append(value)
    else:
        held[last] = value
    return changed


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        project = core.load_project(QA)
        self.spec, self.config = project.specs["read-article"], project.config
        self.script = core.load_compiled(FIXTURES / "read-article.json", self.config)
        plan = (FIXTURES / "read-article.plan.json").read_text()
        self.plan = CoveragePlan.model_validate_json(plan)

    def test_fixture_records_current_spec_plan_and_browser(self) -> None:
        script, plan, spec = self.script, self.plan, self.spec
        checks = [a.model_dump(exclude_none=True) for a in script.assertions]
        texts = [
            {"id": i, "check": "text_in_target", "target": t, "text": x}
            for i, t, _, x in CHECKS
        ]
        self.assertEqual(checks, [c | {"expect_index": n} for n, c in enumerate(texts)])
        contracted = {"banner_author": AUTHOR, "banner_date": DATE}
        bound = {t: ((ByCss(css=c),), contracted.get(t)) for _, t, c, _ in CHECKS}
        bound["preview_link"] = ((ByCss(css=PREVIEW),), None)
        self.assertEqual(
            {n: (t.locators, t.contract) for n, t in script.targets.items()}, bound
        )
        hashes = (
            script.spec_hash,
            script.coverage.plan_hash,
            misfits(plan, spec.frontmatter),
        )
        self.assertEqual(hashes, (spec.spec_hash, plan_hash(plan), ()))
        browser: dict[str, object] = {
            "timezone": "UTC",
            "locale": "en-US",
            "viewport": [1280, 800],
        }
        browser |= {"device_scale_factor": 1, "color_scheme": "light"}
        self.assertEqual(script.browser.model_dump(mode="json"), browser)
        self.assertEqual(script.browser, core.effective_browser(spec, self.config))
        # The plan's checks compile to exactly these assertions, meanings and rows.
        rows = core.subject_contracts(self.config, "read-article")
        contracts, planned = contracts_by_meaning(plan, rows), planned_checks(plan)
        for (name, (index, check)), done in zip(
            planned.items(), script.assertions, strict=True
        ):
            target = getattr(done, "target", "")
            meaning, held = check.target_meaning or "", script.targets[target]
            built = assertion_for(name, index, check, target=target)
            self.assertEqual(
                (done, held.semantic, held.contract),
                (built, meaning, contracts[meaning]),
            )
        covered = [
            (c.expect_index, c.subject, c.claim, c.assertions)
            for c in script.coverage.expectations
        ]
        planned_rows = [
            (p.expect_index, p.subject, p.claim, (f"a{p.expect_index + 1}",))
            for p in plan.expectations
        ]
        self.assertEqual((covered, script.coverage.requires), (planned_rows, ()))
        # Handwritten: no model call, no compiler run, never confirmed.
        by, step = script.compiled_by, script.steps[0].model_dump(mode="json")
        metadata = (
            by.mode,
            by.models,
            by.price_map,
            script.confirmed,
            len(script.steps),
        )
        self.assertEqual(
            metadata, ("explore", {}, "none: handwritten fixture", False, 1)
        )
        click = {
            "seq": 1,
            "action": "click",
            "target": "preview_link",
            "side_effect": True,
        }
        self.assertEqual(
            step,
            click | {"side_effect_basis": step["side_effect_basis"], "satisfies": []},
        )

    def test_conduit_wraps_only_its_comment_loop(self) -> None:
        pages = ROOT / "bench/apps/conduit/frontend/src/app/features/article/pages"
        html = (pages / "article/article.component.html").read_text()
        loop = r"<div class=\"comment-list\">\s*@for \(comment of comments\(\); track"
        loop += r" comment\) \{\s*<app-article-comment [^>]*/>\s*\}\s*</div>"
        found = (html.count("comment-list"), html.count("<app-article-comment"))
        self.assertEqual((*found, len(re.findall(loop, html))), (1, 1, 1))

    def test_fixture_carries_only_current_row_contracts(self) -> None:
        config, script = self.config, self.script
        rows = dict(core.subject_contracts(config, "read-article"))
        fingerprint = core.contracts_fingerprint(config, "read-article")
        agreed = (
            rows,
            script.compiled_by.subject_contracts,
            core.contract_problems(script, config),
        )
        self.assertEqual(agreed, ({1: AUTHOR, 2: DATE}, fingerprint, []))

        def bound(name: str, like: str, contract: Contract | None) -> CompiledScript:
            target: Target = script.targets[like].model_copy(
                update={"contract": contract}
            )
            return script.model_copy(
                update={"targets": script.targets | {name: target}}
            )

        stale = script.compiled_by.model_copy(update={"subject_contracts": Z})
        lacks = "targets.{}: lacks subject contract for read-article expect {}"
        for candidate, problem in [
            (
                script.model_copy(update={"compiled_by": stale}),
                "compiled under other subject contracts: explore it again",
            ),
            (
                bound("banner_author", "banner_author", None),
                lacks.format("banner_author", 1),
            ),
            (
                bound(
                    "banner_date",
                    "banner_date",
                    DATE.model_copy(update={"leaf": False}),
                ),
                lacks.format("banner_date", 2),
            ),
            (
                bound("footer_author", "banner_author", None),
                lacks.format("footer_author", 1),
            ),
            (
                bound("article_body", "article_body", AUTHOR),
                "targets.article_body: carries a subject contract no row gives it",
            ),
        ]:
            with self.subTest(problem):
                self.assertEqual(core.contract_problems(candidate, config), [problem])
                with self.assertRaisesRegex(ValueError, "^x: invalid pilot input$"):
                    validate_pilot(
                        self.spec, config, candidate, source=Path("x"), origin=ORIGIN
                    )

    def test_fixture_original_and_rebound_admission_stay_confined(self) -> None:
        (admitted,) = load_pilots(QA, FIXTURES, ("read-article",), origin=ORIGIN)
        found = (admitted.script, admitted.start, admitted.reset and admitted.reset.url)
        self.assertEqual(
            found, (self.script, ORIGIN, f"{ORIGIN}/test-api/reset?fixture=seed")
        )
        base = canonical_hash(self.script.model_dump(mode="json"))
        same = {
            "op": "replace",
            "path": "/targets/comment_list/locators",
            "value": [{"css": CHECKS[5][2]}],
        }
        text = json.dumps({"base_hash": base, "operations": [same]})
        candidate = apply_rebinding(self.script, text, self.config, source=Path("x"))
        rebound = validate_pilot(
            self.spec, self.config, candidate, source=Path("x"), origin=ORIGIN
        )
        self.assertEqual((candidate, rebound.script), (self.script, self.script))
        # B3c: every extra-origin declaration refuses, the stack's own included.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        qa = Path(directory.name)
        for name, old, new in [
            (
                "config.yaml",
                "subjects:",
                f"egress: {{private_origins: ['{ORIGIN}']}}\nsubjects:",
            ),
            (
                "config.yaml",
                "subjects:",
                "egress: {subresource_hosts: ['127.0.0.1']}\nsubjects:",
            ),
            ("read-article.spec.md", "tags:", f"allowed_origins: ['{ORIGIN}']\ntags:"),
        ]:
            with self.subTest(new):
                shutil.copytree(QA, qa, dirs_exist_ok=True)
                (qa / name).write_text((QA / name).read_text().replace(old, new, 1))
                self.assertIn(new, (qa / name).read_text())
                project = core.load_project(qa)
                fresh = project.specs["read-article"]
                script = self.script.model_copy(update={"spec_hash": fresh.spec_hash})
                with self.assertRaisesRegex(
                    ValueError, "^x: declares an origin beyond the stack's$"
                ):
                    validate_pilot(
                        fresh, project.config, script, source=Path("x"), origin=ORIGIN
                    )


class Pages(ThreadingHTTPServer):
    daemon_threads = True
    article = article()

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), PageHandler)


class PageHandler(BaseHTTPRequestHandler):
    server: Pages

    def do_GET(self) -> None:
        self.answer(self.server.article if self.path.startswith("/article/") else FEED)

    def do_POST(self) -> None:
        self.answer("")  # the spec's reset hook

    def answer(self, page: str) -> None:
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        """Quiet: each row asserts what its replay saw."""


class AdapterTests(unittest.TestCase):
    """The fixture through the real adapter, on a loopback copy of the pages."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root, self.server = Path(directory.name), Pages()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        shutil.copytree(QA, self.root / "qa")
        config = (QA / "config.yaml").read_text().replace(ORIGIN, origin)
        (self.root / "qa/config.yaml").write_text(
            config + "budgets: {resolve_seconds: 2}\n"
        )
        qa = self.root / "qa"
        (self.pilot,) = load_pilots(qa, FIXTURES, ("read-article",), origin=origin)

    def replay(self, page: str) -> ObservedAttempt:
        self.server.article = page

        async def bounded() -> object:
            async with asyncio.timeout(60):
                return await replay_pilot(self.pilot, self.root)

        attempt = asyncio.run(bounded())
        if not isinstance(attempt, ObservedAttempt):
            self.fail(f"not observed: {attempt}")
        return attempt

    def test_each_read_article_check_establishes_its_claim(self) -> None:
        passing = {f"a{n}": "pass" for n in range(1, 8)}
        for label, changes, outcomes in ROWS:
            with self.subTest(label):
                seen = self.replay(article(**changes)).observation
                steps = [(s.seq, s.outcome, s.locator_index) for s in seen.steps]
                checks = {a.id: a.outcome for a in seen.assertions}
                found = (steps, checks, seen.violated_invariants)
                wanted = (
                    [(0, "completed", None), (1, "completed", 0)],
                    passing | outcomes,
                )
                self.assertEqual(found, (*wanted, frozenset()))

    def test_fixture_replay_constructs_no_model_client(self) -> None:
        built = trapped(self, CLIENTS)
        attempt = self.replay(article())
        found = (attempt.observation.outcome, attempt.health, built)
        self.assertEqual(found, ("passed", HEALTHY, []))


class ProofTests(unittest.TestCase):
    """The proof, read from the pilot command's own report in a scratch repo."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base, self.ids = Path(directory.name), count(1)
        self.root = self.base / "repo"
        for part in ("bench/apps/conduit/qa", "bench/harness/fixtures"):
            shutil.copytree(ROOT / part, self.root / part)
        shutil.copy(ROOT / "bench/manifest.v1.json", self.root / "bench")
        self.git("init", "-q")
        self.commit()

    def git(self, *args: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        who = [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.test",
            "-c",
            "commit.gpgsign=false",
        ]
        run = subprocess.run(
            ["git", *who, *args],
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return run.stdout

    def commit(self) -> None:
        self.git("add", "bench")
        self.git("commit", "--no-verify", "-qm", "fixture")

    def pilot(self, answers: dict[str, list[RunResult]] | None = None) -> Any:
        """The pilot command's report, Docker and the replay faked by the flag on."""
        docker, queues, out = (
            FakeDocker(),
            answers or {},
            self.base / f"out-{next(self.ids)}",
        )
        usual = {"4o6x": result(a3=F), "66jd": result(js=2)}

        async def replay(pilot_input: PilotInput, _: Path) -> ObservedAttempt:
            queue = queues.get(docker.bench_flags, [])
            run = queue.pop(0) if queue else usual.get(docker.bench_flags, result())
            settings = pilot_input.spec.frontmatter.invariants
            seen = observe(pilot_input.script, run, invariants=settings)
            return ObservedAttempt(f"{FAKE}-{next(self.ids)}", Path(), seen, HEALTHY)

        fixtures = str(self.root / "bench/harness/fixtures")
        argv = [
            "--root",
            str(self.root),
            "conduit",
            "--spec",
            "read-article",
            "--out",
            str(out),
        ]
        with redirect_stdout(StringIO()):
            pilot.main(
                [*argv, "--compiled-dir", fixtures], docker=docker, replay=replay
            )
        return json.loads((out / "report.json").read_text())

    def prove(self, report: object, path: Path | None = None) -> tuple[int, Any, str]:
        """The proof's exit, receipt and stderr; the report's bytes never change."""
        number = next(self.ids)
        path, out = (
            path or self.base / f"report-{number}.json",
            self.base / f"proof-{number}.json",
        )
        path.write_text(report if isinstance(report, str) else json.dumps(report))
        before, stderr = path.read_bytes(), StringIO()
        with redirect_stderr(stderr), redirect_stdout(StringIO()):
            code = pilot_read_article.main(
                [str(path), "--report", str(out), "--root", str(self.root)]
            )
        written = out.read_text() if out.exists() else ""
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(FAKE, stderr.getvalue() + written)
        return code, json.loads(written) if written else None, stderr.getvalue()

    def verdict(self, report: object) -> tuple[int, list[str], list[str]]:
        code, receipt, _ = self.prove(report)
        failed = [case for case, found in receipt["cases"].items() if found != "passed"]
        return code, receipt["problems"], failed

    def test_proof_accepts_the_pilot_commands_report(self) -> None:
        report = self.pilot()
        owners = [
            (flags.LocalDocker, "compose"),
            (pilot_replay, "replay"),
            (pilot_replay, "runner_request"),
        ]
        calls = trapped(self, [*owners, *CLIENTS])
        code, receipt, stderr = self.prove(report)
        commit, tree = self.git("rev-parse", "HEAD", "HEAD^{tree}").split()
        script = json.loads((FIXTURES / "read-article.json").read_text())
        self.assertEqual((code, calls, stderr, report["exit"]), (0, [], "", 0))
        self.assertEqual(
            receipt,
            {
                "spec": "read-article",
                "source": {"commit": commit, "tree": tree},
                "report": "sha256:"
                + hashlib.sha256(json.dumps(report).encode()).hexdigest(),
                "hashes": report["hashes"]
                | {"plan:read-article": script["coverage"]["plan_hash"]},
                "cases": dict.fromkeys(["clean", *DEV], "passed"),
                "problems": [],
                "proven": True,
            },
        )

    def test_proof_requires_the_issues_outcome_for_every_case(self) -> None:
        rows: list[tuple[str, dict[str, list[RunResult]], int, list[str]]] = [
            (
                "another locator on one clean repeat",
                {"": [result(), result(), result(index=1)]},
                1,
                ["clean"],
            ),
            (
                "a failed check on one clean repeat",
                {"": [result(), result(a1=F)]},
                1,
                ["clean"],
            ),
            ("bug-001 also fails a4", {"4o6x": [result(a3=F, a4=F)]}, 1, BUGS[:1]),
            ("bug-001 fails nothing", {"4o6x": [result()]}, 1, BUGS[:1]),
            (
                "bug-002 also logs a console error",
                {"66jd": [result(js=2, console=1)]},
                1,
                BUGS[1:2],
            ),
            ("bug-002 throws nothing", {"66jd": [result()]}, 1, BUGS[1:2]),
            (
                "bug outcomes swapped",
                {"4o6x": [result(js=2)], "66jd": [result(a3=F)]},
                1,
                BUGS[:2],
            ),
            ("an unscored failure", {"7ozy": [result(a6=F)]}, 0, BUGS[2:3]),
            ("an unscored binding block", {"zhwm": [result(a6=U)]}, 0, DEV[1:2]),
        ]
        for label, answers, generic, failed in rows:
            with self.subTest(label):
                report = self.pilot(answers)
                found = (report["exit"], *self.verdict(report))
                self.assertEqual(found, (generic, 1, ["report.exit"] * generic, failed))

    def test_proof_requires_each_check_on_its_first_locator(self) -> None:
        def second(run: RunResult, n: int) -> RunResult:
            """`run`, with check `a<n>` resolved by its second locator."""
            done = list(run.assertions)
            done[n - 1] = replace(done[n - 1], locator_index=1)
            return replace(run, assertions=tuple(done))

        rows: list[tuple[dict[str, list[RunResult]], list[str]]] = [
            ({"": [second(result(), 1)] * 3}, ["clean"]),
            ({"4o6x": [second(result(a3=F), 3)]}, BUGS[:1]),
        ]
        for answers, failed in rows:
            with self.subTest(failed=failed):
                report = self.pilot(answers)
                found = (report["exit"], *self.verdict(report))
                self.assertEqual(found, (0, 1, [], failed))
        path = "pairs.3.attempts.0.assertions.2.locator_index"
        dropped = edited(self.pilot(), path, DROP)
        self.assertEqual(self.verdict(dropped), (1, [], BUGS[:1]))

    def test_proof_refuses_a_report_that_disagrees_with_the_source(self) -> None:
        report = self.pilot()
        self.assertEqual(self.verdict(report), (0, [], []))
        first, bug = (report["pairs"][n]["attempts"][0] for n in (0, 3))
        # (dotted path, value): the problems it must raise and the cases it must fail.
        edits: list[tuple[str, object, list[str], list[str]]] = [
            ("pairs.0.attempts", [], ["report.exit"], ["clean"]),
            ("pairs.0.attempts.2", DROP, [], ["clean"]),
            ("pairs.0.attempts.3", first | {"run_id": "x"}, [], ["clean"]),
            ("pairs.0.attempts.1.run_id", first["run_id"], [], ["clean"]),
            ("pairs.3.attempts.0.run_id", first["run_id"], [], ["clean", BUGS[0]]),
            ("pairs.4.attempts.0.run_id", bug["run_id"], [], BUGS[:2]),
            ("pairs.3.attempts.1", bug | {"run_id": "y"}, [], BUGS[:1]),
            ("pairs.0.attempts.0.eligible", False, [], ["clean"]),
            ("pairs.0.attempts.0.kind", "binding_only", [], ["clean"]),
            ("pairs.0.attempts.0.health", DROP, [], ["clean"]),
            ("pairs.0.attempts.0.steps.0.error", 1, [], ["clean"]),
            ("pairs.0.attempts.0.steps.1.seq", 2, [], ["clean"]),
            ("pairs.0.attempts.0.invariants.0.total", -1, [], ["clean"]),
            ("pairs.3.attempts.0.failed_expectations", [], [], BUGS[:1]),
            ("pairs.3.attempts.0.assertions.6", DROP, [], BUGS[:1]),
            ("pairs.3.status", "mismatch", [], BUGS[:1]),
            ("pairs.4.attempts.0.invariants.1.total", 0, [], BUGS[1:2]),
            ("pairs.4.attempts.0.invariants.3", DROP, [], BUGS[1:2]),
            ("pairs.5", DROP, ["inventory"], BUGS[2:3]),
            ("pairs.5.case", "conduit-bug-099", ["inventory"], BUGS[2:3]),
            ("pairs.5.spec", "login", ["inventory"], BUGS[2:3]),
            ("pairs.8", report["pairs"][5], ["inventory"], BUGS[2:3]),
            *(
                (path, "0" * 40, ["report.source"], [])
                for path in ("source.commit", "source.tree")
            ),
            *(
                (f"hashes.{k}", Z, ["report.hashes"], [])
                for k in [*report["hashes"], "patch:x"]
            ),
            *(
                (k, v, [f"report.{k}"], [])
                for k, v in [("halt", "unexpected"), ("halt", DROP)]
            ),
            ("reservation_release", "forbidden", ["report.reservation_release"], []),
            ("switched_back_clean", False, ["report.switched_back_clean"], []),
            ("source_unchanged", False, ["report.source_unchanged"], []),
            ("exit", 3, ["report.exit"], []),
            ("repeat", 2, ["report.repeat"], []),
            ("diagnostic", True, ["report.diagnostic"], []),
            ("assertion_provenance", "pending", ["report.extra"], []),
            ("note", FAKE, ["report.extra"], []),
        ]
        for path, value, problems, failed in edits:
            with self.subTest(path=path, value=value):
                self.assertEqual(
                    self.verdict(edited(report, path, value)), (1, problems, failed)
                )
        unreadable: list[tuple[str, object]] = [
            ("pairs", {}),
            ("pairs.0.attempts.0.run_id", None),
        ]
        unreadable += [
            ("pairs.0.attempts.0.invariants.0.total", v) for v in (True, "0")
        ]
        for path, value in unreadable:
            with self.subTest(path=path, value=value):
                self.assertEqual(
                    self.prove(edited(report, path, value))[::2], (2, MALFORMED)
                )

    def test_proof_refuses_unreadable_reports_with_a_fixed_message(self) -> None:
        text = json.dumps(self.pilot())
        swaps = ["NaN", "1e400", "0.0"]
        broken = [text[:-1], '{"exit": 0, ' + text[1:], "[]", "[" * 1_000_000]
        for case in [
            *broken,
            *(text.replace('"exit": 0', f'"exit": {s}') for s in swaps),
        ]:
            with self.subTest(case[:20]):
                self.assertEqual(self.prove(case)[::2], (2, MALFORMED))
        self.git("update-ref", "-d", "HEAD")
        self.assertEqual(self.prove(text)[::2], (2, MALFORMED))

    def test_proof_refuses_a_changed_source(self) -> None:
        report, review = self.pilot(), self.root / "bench/apps/conduit/qa/REVIEW.md"
        review.write_text(review.read_text() + "\n")
        inside = review.parent / "report.json"
        self.assertEqual(self.prove(report, inside)[1]["problems"], ["source"])
        inside.unlink()
        review.write_text(review.read_text()[:-1])
        self.assertEqual(self.verdict(report), (0, [], []))
        now = pilot.git_source(self.root, self.root.parent)
        moved = [now, now._replace(commit="0" * 40)]
        with patch.object(pilot_read_article, "git_source", side_effect=moved):
            self.assertEqual(self.verdict(report), (1, ["source"], []))
        plan = self.root / "bench/harness/fixtures/read-article.plan.json"
        plan.write_text(plan.read_text().replace("is shown as anna", "is anna"))
        self.commit()
        self.assertEqual(self.prove(report)[::2], (2, MALFORMED))
        short = json.loads((FIXTURES / "read-article.plan.json").read_text())
        short["expectations"].pop()
        plan.write_text(json.dumps(short))
        fixture = plan.with_name("read-article.json")
        script = json.loads(fixture.read_text())
        script["coverage"]["plan_hash"] = plan_hash(
            CoveragePlan.model_validate_json(plan.read_text())
        )
        fixture.write_text(json.dumps(script))
        self.commit()
        self.assertEqual(self.prove(self.pilot())[::2], (2, MALFORMED))

    def test_proof_requires_every_current_dev_case_once(self) -> None:
        listed = self.root / "bench/manifest.v1.json"
        manifest = json.loads(listed.read_text())
        case = manifest["cases"]["conduit-bug-005"]
        added = {"family": "conduit-f6", "flag": "zz06"}
        frozen = {"family": "conduit-f7", "flag": "zz07", "split": "test"}
        manifest["cases"] |= {
            "conduit-bug-006": case | added,
            "conduit-bug-007": case | frozen,
        }
        listed.write_text(json.dumps(manifest))
        self.commit()
        code, receipt, _ = self.prove(self.pilot())
        self.assertEqual(
            (code, list(receipt["cases"])), (0, ["clean", *DEV, "conduit-bug-006"])
        )
        failing = self.pilot({"zz06": [result(a6=F)]})
        self.assertEqual(self.verdict(failing), (1, [], ["conduit-bug-006"]))
        manifest["cases"]["conduit-bug-006"]["expected"][0]["spec"] = "read-article"
        listed.write_text(json.dumps(manifest))
        self.commit()
        self.assertEqual(self.verdict(self.pilot())[:2], (1, ["contract"]))

    def test_proof_preserves_existing_output(self) -> None:
        report, taken = self.pilot(), self.base / "proof-taken.json"
        taken.write_bytes(b"first")
        path = self.base / "report.json"
        path.write_text(json.dumps(report))
        stderr = StringIO()
        with redirect_stderr(stderr), redirect_stdout(StringIO()):
            code = pilot_read_article.main(
                [str(path), "--report", str(taken), "--root", str(self.root)]
            )
        found = (code, stderr.getvalue(), taken.read_bytes())
        self.assertEqual(
            found, (2, "error: the proof can't be written as a new file\n", b"first")
        )
        self.assertEqual(self.prove(report)[0], 0)
