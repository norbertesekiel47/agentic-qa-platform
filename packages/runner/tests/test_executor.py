"""The M1 strict executor (#46; ADR-0024's Consequences and its #46
amendment): a compiled script's steps run in a fresh session, each intent on
disk before its action and its completion after, with no model involved. The
scripts are written by hand, and the browser tests launch real Chromium on
the OS that runs them: Linux in CI, macOS locally."""

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anthropic
import pytest
from aqa_core.compiled import CompiledScript, Target
from aqa_core.config import ProjectConfig
from aqa_core.project import SpecError, load_spec
from aqa_core.spec import Spec
from aqa_runner import executor, settling
from aqa_runner.anthropic_client import AnthropicClient
from aqa_runner.browser_session import BrowserSession
from aqa_runner.document_origins import DocumentChangedError, PolicyEvent
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import RunResult, RunSetup, replay
from aqa_runner.locators import Use
from aqa_runner.model_router import ModelRouter
from aqa_runner.run_record import RunRecord
from aqa_runner.settling import Window
from langchain_anthropic import ChatAnthropic
from playwright.async_api import ElementHandle, async_playwright

from packages.runner.tests.egress_fixtures import gate, unused_port

# The fixture app's pages, by name.
PAGES = {
    "start": """<a href="/page/form">Form</a>""",
    # A write 1 s after a click, and a button that appears 1.5 s after it.
    "late": """<button id="save">Save</button><script>
        document.querySelector("#save").addEventListener("click", () => {
            setTimeout(() => fetch("/write/late", {method: "POST"}), 1000);
            setTimeout(() => {
                const next = document.createElement("button");
                next.textContent = "Next";
                next.addEventListener("click", () => fetch("/did/next"));
                document.body.append(next);
            }, 1500);
        });
    </script>""",
    # A link to the app under another name, which the run doesn't allow, and
    # a button that opens a popup there.
    "away": """<a id="away">Away</a><button id="pop">Pop</button><script>
        const elsewhere = location.origin.replace("127.0.0.1", "localhost") + "/page/form";
        document.querySelector("#away").href = elsewhere;
        document.querySelector("#pop").onclick = () => window.open(elsewhere);
    </script>""",
    # A request the app holds until the test lets it go, and a payment the
    # app holds the same way.
    "wait": """<button onclick="fetch('/held/long')">Wait</button>""",
    "pay": """<button onclick="fetch('/held/pay', {method: 'POST'})">Pay</button>""",
    # The settings the page runs under, sent to the app.
    "env": """<script>
        const settings = new URLSearchParams({
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            locale: navigator.language,
            viewport: `${innerWidth}x${innerHeight}`,
            scale: String(devicePixelRatio),
            scheme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
        });
        fetch("/did/env?" + settings);
    </script>""",
    "form": """<label>Name <input oninput="fetch('/did/fill')"></label>
        <label>Size <select onchange="fetch('/did/select')">
            <option>S</option><option>M</option></select></label>
        <button class="save" onclick="fetch('/write/save', {method: 'POST'})">Save</button>
        <script>
            addEventListener("keydown", (event) => {
                if (event.key === "Enter") fetch("/did/press");
            });
        </script>""",
}

# What every hand-written script's settings are unless a test says otherwise.
PINNED = {
    "timezone": "UTC",
    "locale": "en-US",
    "viewport": [1280, 800],
    "device_scale_factor": 1,
    "color_scheme": "light",
}


@dataclass
class App:
    """The fixture app's origin, each request's method and raw path, in
    order, the steps record as it stood when each write arrived, and the
    responses it holds until the test lets them go."""

    origin: str
    seen: list[tuple[str, str]] = field(default_factory=list)
    arrived: threading.Condition = field(default_factory=threading.Condition)
    held: dict[str, threading.Event] = field(default_factory=dict)
    record: Path | None = None
    # Each request's method and path, and the steps record's lines when it
    # arrived.
    records: list[tuple[str, str, list[dict[str, Any]]]] = field(default_factory=list)

    def hold(self, key: str) -> threading.Event:
        with self.arrived:
            return self.held.setdefault(key, threading.Event())

    def release(self, key: str) -> None:
        self.hold(key).set()

    def paths(self) -> list[str]:
        with self.arrived:
            return [path for _, path in self.seen]


class _Handler(BaseHTTPRequestHandler):
    """Serves `/page/<name>`, `/raw/…` (a page, whatever its path), `/did/…`
    and `/write/…` (204), and `/held/<key>` (204 once released), noting the
    steps record as it stood when each request arrived."""

    app: App

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._answer()

    def do_GET(self) -> None:
        self._answer()

    def _answer(self) -> None:
        record = self.app.record
        steps = [] if record is None else read_steps(record)
        with self.app.arrived:
            self.app.seen.append((self.command, self.path))
            self.app.records.append((self.command, urlsplit(self.path).path, steps))
            self.app.arrived.notify_all()
        kind, _, name = urlsplit(self.path).path.removeprefix("/").partition("/")
        if kind == "page" and name in PAGES:
            self._page(PAGES[name])
        elif kind == "raw":
            self._page("<p>Raw</p>")
        elif kind == "held":
            self.app.hold(name).wait(30)
            self._empty(HTTPStatus.NO_CONTENT)
        elif kind in ("did", "write"):
            self._empty(HTTPStatus.NO_CONTENT)
        else:
            self._empty(HTTPStatus.NOT_FOUND)

    def _page(self, body: str) -> None:
        data = f"<!doctype html><title>app</title>{body}".encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: object) -> None:
        pass  # the test reads `seen`


@pytest.fixture
def app() -> Iterator[App]:
    served = App("")
    handler = type("Handler", (_Handler,), {"app": served})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    served.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield served
    finally:
        with served.arrived:
            keys = list(served.held)
        for key in keys:
            served.release(key)
        server.shutdown()
        thread.join()
        server.server_close()


def read_steps(record: Path) -> list[dict[str, Any]]:
    """The steps record's lines, each without its time."""
    steps = record / "steps.jsonl"
    if not steps.exists():
        return []
    return [
        {key: value for key, value in json.loads(line).items() if key != "at"}
        for line in steps.read_text(encoding="utf-8").splitlines()
    ]


def compiled(
    steps: list[dict[str, Any]],
    *,
    targets: dict[str, Any] | None = None,
    browser: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
) -> CompiledScript:
    """A hand-written compiled script with these steps, targets and
    assertions; one `url_matches` assertion when it names none."""
    checks = assertions or [
        {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"}
    ]
    return CompiledScript.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "spec_id": "replay",
                "spec_hash": "sha256:" + "0" * 64,
                "compiled_at": "2026-10-02T00:00:00Z",
                "compiled_by": {
                    "mode": "explore",
                    "models": {"navigator": "a-model"},
                    "price_map": "a-commit",
                },
                "confirmed": True,
                "browser": browser or PINNED,
                "coverage": {
                    "plan_hash": "sha256:" + "0" * 64,
                    "expectations": [
                        {
                            "expect_index": 0,
                            "subject": "the form",
                            "claim": "is saved",
                            "assertions": [check["id"] for check in checks],
                        }
                    ],
                    "requires": [],
                },
                "targets": targets or {},
                "probe_baselines": {},
                "steps": steps,
                "assertions": checks,
            }
        )
    )


def a_spec(
    tmp_path: Path, config: ProjectConfig, *, start_url: str, extra: str = ""
) -> Spec:
    """A spec whose start_url is `start_url`, read as the loader reads one."""
    path = tmp_path / "qa" / "replay.spec.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "id: replay\n"
        "goal: A reader saves the form.\n"
        f"preconditions:\n  start_url: '{start_url}'\n"
        "expect:\n  - The form is saved\n"
        f"{extra}---\n"
    )
    return load_spec(path, config)


@dataclass
class Run:
    """A replay's result, its record, and the app it ran against."""

    result: RunResult
    record: RunRecord


def run(
    app: App,
    tmp_path: Path,
    script: CompiledScript,
    *,
    config: ProjectConfig | None = None,
    spec: Spec | None = None,
    start: str | None = None,
) -> Run:
    """Replay `script` against the app, or against `start` when given, with
    a record under `tmp_path`, for `spec` (by default one whose start_url
    is the form)."""
    settings = config or ProjectConfig()
    origin = start or app.origin
    run_gate = gate(allowed=(origin,))
    record = RunRecord.create(tmp_path)
    app.record = record.path
    setup = RunSetup(
        spec or a_spec(tmp_path, settings, start_url="/page/form"),
        settings,
        origin,
        record,
    )

    async def scenario() -> RunResult:
        async with async_playwright() as playwright, EgressProxy(run_gate) as proxy:
            # No executor test may hang the suite.
            async with asyncio.timeout(60):
                return await replay(
                    script,
                    setup,
                    chromium=playwright.chromium,
                    proxy=proxy,
                    gate=run_gate,
                )

    return Run(asyncio.run(scenario()), record)


def by_role(role: str, name: str) -> dict[str, Any]:
    return {"role": role, "name": name}


FORM_TARGETS = {
    "name": {"semantic": "the name field", "locators": [{"css": "input"}]},
    "size": {"semantic": "the size list", "locators": [{"css": "select"}]},
    "save": {
        "semantic": "the save button",
        # The first locator finds nothing, so the second is the one used.
        "locators": [by_role("button", "Store"), by_role("button", "Save")],
    },
}

# Every M1 action, after the start URL.
FORM_STEPS = [
    {
        "seq": 1,
        "action": "fill",
        "target": "name",
        "value": "Ada",
        "side_effect": False,
    },
    {
        "seq": 2,
        "action": "select",
        "target": "size",
        "option": "M",
        "side_effect": False,
    },
    {"seq": 3, "action": "press", "key": "Enter", "side_effect": False},
    {
        "seq": 4,
        "action": "click",
        "target": "save",
        "side_effect": True,
        "side_effect_basis": "network: POST /write/save",
    },
    {"seq": 5, "action": "reload", "side_effect": False},
    {"seq": 6, "action": "navigate", "url": "/page/start", "side_effect": False},
]


def test_the_first_navigation_goes_to_the_start_url_and_navigate_paths_join_as_written(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [
            {
                "seq": 1,
                "action": "navigate",
                "url": "/raw/%2f%2fwhere",
                "side_effect": False,
            },
            {
                "seq": 2,
                "action": "navigate",
                "url": "/raw/a?b=%41#c",
                "side_effect": False,
            },
        ]
    )

    spec = a_spec(tmp_path, ProjectConfig(), start_url="/raw/start?at=%2F")
    result = run(app, tmp_path, script, spec=spec).result

    # Each path as written, never decoded or resolved, on the start origin.
    assert app.paths() == ["/raw/start?at=%2F", "/raw/%2f%2fwhere", "/raw/a?b=%41"]
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
        (2, "completed"),
    ]


def test_each_step_is_completed_after_its_action_with_the_locator_used(
    app: App, tmp_path: Path
) -> None:
    done = run(app, tmp_path, compiled(FORM_STEPS, targets=FORM_TARGETS))

    lines = read_steps(done.record.path)
    assert [(line["seq"], line["state"]) for line in lines] == [
        (seq, state) for seq in range(7) for state in ("intent", "completed")
    ]
    intents = [line for line in lines if line["state"] == "intent"]
    assert intents[0] == {
        "seq": 0,
        "state": "intent",
        "action": {"action": "navigate", "url": "/page/form"},
        "side_effect": False,
        "target_used": None,
    }
    assert intents[4] == {
        "seq": 4,
        "state": "intent",
        "action": {"action": "click", "target": "save"},
        "side_effect": True,
        "target_used": "save",
    }
    completions = [line for line in lines if line["state"] == "completed"]
    assert [line["locator_used"] for line in completions] == [
        None,
        0,
        0,
        None,
        1,
        None,
        None,
    ]
    assert {line["settled"] for line in completions} == {"idle"}
    assert done.result.run_id == done.record.run_id


def test_unsupported_steps_and_checks_are_refused_by_name_before_the_browser_opens(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            },
            {
                "seq": 2,
                "action": "fill_secret",
                "target": "name",
                "secret": "TEST_PASSWORD",
                "side_effect": False,
            },
        ],
        targets=FORM_TARGETS,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"},
            {
                "id": "a2",
                "expect_index": 0,
                "check": "network_none",
                "method": "POST",
                "url_pattern": "/write",
                "status_class": "2xx",
            },
            {
                "id": "a3",
                "expect_index": 0,
                "check": "network_seen",
                "method": "GET",
                "url_pattern": "/did",
                "status_class": "2xx",
            },
            {
                "id": "a4",
                "expect_index": 0,
                "check": "probe_equals_baseline",
                "probe": "count",
            },
            {
                "id": "a5",
                "expect_index": 0,
                "check": "visible_unoccluded",
                "target": "save",
                "min_size_px": [44, 24],
                "in_viewport": True,
            },
        ],
    )
    opened: list[object] = []

    def no_browser(*args: object, **options: object) -> None:
        opened.append((args, options))
        raise AssertionError("the browser was opened")

    monkeypatch.setattr(executor, "open_browser_session", no_browser)

    with pytest.raises(SpecError) as refused:
        run(app, tmp_path, script)

    assert refused.value.problems == (
        "steps[1] (seq 2): fill_secret is not run until #49",
        "assertions[1] (a2): network_none is not evaluated until #48",
        "assertions[2] (a3): network_seen is not evaluated until #48",
        "assertions[3] (a4): probe_equals_baseline is not evaluated until #48",
        "assertions[4] (a5): visible_unoccluded is not evaluated until #48",
    )
    assert opened == []
    assert app.paths() == []


def paths(window: Window | None) -> list[tuple[str, str]]:
    """Each request in a step's window: its method and its URL's path."""
    assert window is not None
    return [
        (request.method, urlsplit(request.url).path) for request in window.requests.kept
    ]


def test_a_write_after_the_dom_settles_belongs_to_the_step_before_the_next_action(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "save": {
            "semantic": "the save button",
            "locators": [by_role("button", "Save")],
        },
        "next": {
            "semantic": "the next button",
            "locators": [by_role("button", "Next")],
        },
    }
    script = compiled(
        [
            {
                "seq": 1,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/late",
            },
            {"seq": 2, "action": "click", "target": "next", "side_effect": False},
        ],
        targets=targets,
    )

    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/late")
    result = run(app, tmp_path, script, spec=spec).result

    save, following = result.steps[1], result.steps[2]
    # Settling said idle before the write, and the next button appeared
    # later still, within resolve_seconds: the write is the click's.
    assert (save.outcome, save.settled) == ("completed", "idle")
    assert paths(save.window) == [("POST", "/write/late")]
    assert following.outcome == "completed"
    assert paths(following.window) == [("GET", "/did/next")]


def test_a_step_whose_target_never_resolves_stops_the_run_at_that_step(
    app: App, tmp_path: Path
) -> None:
    targets = FORM_TARGETS | {
        "gone": {
            "semantic": "a button the form doesn't have",
            "locators": [by_role("button", "Gone"), {"css": "#gone"}],
        }
    }
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            },
            {"seq": 2, "action": "click", "target": "gone", "side_effect": False},
            {
                "seq": 3,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/save",
            },
        ],
        targets=targets,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "url_matches", "pattern": "/"},
            {"id": "a2", "expect_index": 0, "check": "text_visible", "text": "Saved"},
        ],
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    done = run(app, tmp_path, script, config=config)

    result = done.result
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
        (2, "drifted"),
    ]
    assert result.steps[2].misses == ("no match", "no match")
    # Nothing was dispatched for it: no intent, and the save never reached the app.
    assert [line["seq"] for line in read_steps(done.record.path)] == [0, 0, 1, 1]
    assert ("POST", "/write/save") not in app.seen
    assert [(a.id, a.outcome, a.stopped_at) for a in result.assertions] == [
        ("a1", "not_evaluated", 2),
        ("a2", "not_evaluated", 2),
    ]
    assert result.outcome == "failed"


def test_a_lookup_that_hangs_ends_with_the_budget(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As is_enabled can for an element moved into another document
    # (ADR-0025's #52 amendment).
    async def hangs(_: ElementHandle) -> bool:
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(ElementHandle, "is_enabled", hangs)
    script = compiled(
        [
            {
                "seq": 1,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/save",
            }
        ],
        targets=FORM_TARGETS,
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    started = time.monotonic()

    result = run(app, tmp_path, script, config=config).result

    # The look was cut off at the budget, before any locator's miss was known.
    assert [(step.seq, step.outcome, step.misses) for step in result.steps] == [
        (0, "completed", ()),
        (1, "drifted", ()),
    ]
    assert time.monotonic() - started < 20


def test_a_lookup_the_page_changed_under_is_made_again(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve: Callable[..., Awaitable[Any]] = BrowserSession.resolve
    looks: list[str] = []

    async def changed_once(session: BrowserSession, target: Target, use: Use) -> Any:
        looks.append(target.semantic)
        if len(looks) == 1:
            raise DocumentChangedError
        return await resolve(session, target, use)

    monkeypatch.setattr(BrowserSession, "resolve", changed_once)
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )

    result = run(app, tmp_path, script).result

    assert looks == ["the name field", "the name field"]
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]


def test_a_dispatch_that_raises_stops_the_run_and_leaves_its_intent_unresolved(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        [
            # A button takes no text: the session's fill raises.
            {
                "seq": 1,
                "action": "fill",
                "target": "save",
                "value": "Ada",
                "side_effect": False,
            },
            {
                "seq": 2,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/save",
            },
        ],
        targets=FORM_TARGETS,
    )

    done = run(app, tmp_path, script)

    result = done.result
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "failed"),
    ]
    assert "didn't take the value" in (result.steps[1].error or "")
    # Its outcome is unknown: an intent, and no completion after it.
    assert [(line["seq"], line["state"]) for line in read_steps(done.record.path)] == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
    ]
    assert [(a.outcome, a.stopped_at) for a in result.assertions] == [
        ("not_evaluated", 1)
    ]
    assert result.outcome == "errored"


def test_a_step_that_lands_off_the_allowed_origins_stops_the_run_errored(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "away": {"semantic": "the away link", "locators": [by_role("link", "Away")]}
    }
    script = compiled(
        [
            {"seq": 1, "action": "click", "target": "away", "side_effect": False},
            {"seq": 2, "action": "reload", "side_effect": False},
        ],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/away")

    done = run(app, tmp_path, script, spec=spec)

    result = done.result
    # Routing refused the host, so the page became the browser's error page,
    # which settling refused to look at.
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "failed"),
    ]
    assert result.policy_events == (
        PolicyEvent("document", "chrome-error://chromewebdata/", None),
    )
    assert result.outcome == "errored"


def test_a_popup_off_the_allowed_origins_makes_the_run_errored(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "pop": {"semantic": "the pop button", "locators": [by_role("button", "Pop")]}
    }
    script = compiled(
        [{"seq": 1, "action": "click", "target": "pop", "side_effect": False}],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/away")

    result = run(app, tmp_path, script, spec=spec).result

    # The click itself completed; the popup it opened is a policy event.
    assert [(step.seq, step.outcome) for step in result.steps] == [
        (0, "completed"),
        (1, "completed"),
    ]
    assert [event.kind for event in result.policy_events] == ["popup"]
    assert result.outcome == "errored"


def test_an_unreachable_start_origin_ends_the_run_errored_before_any_step(
    app: App, tmp_path: Path
) -> None:
    nowhere = f"http://127.0.0.1:{unused_port()}"
    script = compiled(
        [
            {
                "seq": 1,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )

    result = run(app, tmp_path, script, start=nowhere).result

    assert [step.seq for step in result.steps] == [0]
    assert [(event.host, event.port) for event in result.infrastructure_events] == [
        ("127.0.0.1", int(nowhere.rsplit(":", 1)[1]))
    ]
    assert [(a.outcome, a.stopped_at) for a in result.assertions] == [
        ("not_evaluated", 0)
    ]
    assert result.outcome == "errored"


def test_an_action_waits_no_longer_than_resolve_seconds(
    app: App, tmp_path: Path
) -> None:
    script = compiled(
        # Playwright waits for an option to appear; this one never does.
        [
            {
                "seq": 1,
                "action": "select",
                "target": "size",
                "option": "XL",
                "side_effect": False,
            }
        ],
        targets=FORM_TARGETS,
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})
    started = time.monotonic()

    result = run(app, tmp_path, script, config=config).result

    step = result.steps[1]
    assert step.outcome == "failed"
    assert "Timeout 1000ms exceeded" in (step.error or "")
    assert time.monotonic() - started < 20
    assert result.outcome == "errored"


def test_a_navigation_waits_up_to_its_own_bound_not_resolve_seconds(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Playwright's default, made explicit (the coordinator's ruling on #46).
    assert executor.NAVIGATION_SECONDS == 30
    monkeypatch.setattr(executor, "NAVIGATION_SECONDS", 2)
    script = compiled(
        # The app holds this document until the test ends.
        [
            {
                "seq": 1,
                "action": "navigate",
                "url": "/held/document",
                "side_effect": False,
            }
        ]
    )
    config = ProjectConfig.model_validate({"budgets": {"resolve_seconds": 1}})

    result = run(app, tmp_path, script, config=config).result

    step = result.steps[1]
    assert step.outcome == "failed"
    assert "Timeout 2000ms exceeded" in (step.error or "")


def test_a_step_whose_requests_never_finish_is_recorded_as_a_timeout(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shorter than settling's 10 s, which test_settling measures.
    monkeypatch.setattr(settling, "SETTLE_SECONDS", 2)
    targets = {
        "wait": {"semantic": "the wait button", "locators": [by_role("button", "Wait")]}
    }
    script = compiled(
        [{"seq": 1, "action": "click", "target": "wait", "side_effect": False}],
        targets=targets,
    )
    spec = a_spec(tmp_path, ProjectConfig(), start_url="/page/wait")

    done = run(app, tmp_path, script, spec=spec)

    step = done.result.steps[1]
    assert (step.outcome, step.settled) == ("completed", "timeout")
    assert read_steps(done.record.path)[-1] == {
        "seq": 1,
        "state": "completed",
        "locator_used": 0,
        "settled": "timeout",
    }


def test_each_intent_is_on_disk_before_its_action_reaches_the_app(
    app: App, tmp_path: Path
) -> None:
    run(app, tmp_path, compiled(FORM_STEPS, targets=FORM_TARGETS))

    # The record as the app found it when each step's request arrived: the
    # navigation's while `navigate` was still waiting for the page, and the
    # click's write.
    found = {(method, path): lines for method, path, lines in app.records}
    assert found["GET", "/page/start"][-1] == {
        "seq": 6,
        "state": "intent",
        "action": {"action": "navigate", "url": "/page/start"},
        "side_effect": False,
        "target_used": None,
    }
    assert found["POST", "/write/save"][-1] == {
        "seq": 4,
        "state": "intent",
        "action": {"action": "click", "target": "save"},
        "side_effect": True,
        "target_used": "save",
    }


def test_a_crash_between_intent_and_completion_leaves_the_intent_unresolved(
    app: App, tmp_path: Path
) -> None:
    targets = {
        "pay": {"semantic": "the pay button", "locators": [by_role("button", "Pay")]}
    }
    script = compiled(
        [
            {
                "seq": 1,
                "action": "click",
                "target": "pay",
                "side_effect": True,
                "side_effect_basis": "network: POST /held/pay",
            }
        ],
        targets=targets,
    )
    config = ProjectConfig()
    spec = a_spec(tmp_path, config, start_url="/page/pay")
    record = RunRecord.create(tmp_path)
    arguments = tmp_path / "arguments.json"
    arguments.write_text(
        json.dumps(
            {
                "config": config.model_dump(mode="json"),
                "script": script.model_dump_json(),
                "spec": str(spec.path),
                "origin": app.origin,
                "run_id": record.run_id,
                "record": str(record.path),
            }
        )
    )
    root = Path(__file__).resolve().parents[3]
    with subprocess.Popen(
        [sys.executable, "-m", "packages.runner.tests.executor_crash", str(arguments)],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as child:
        # The payment reached the app, which holds it: the run is settling
        # the click, between its intent and its completion.
        with app.arrived:
            paid = app.arrived.wait_for(lambda: ("POST", "/held/pay") in app.seen, 60)
        child.kill()
        output, _ = child.communicate()
    assert paid, output.decode(errors="replace")

    assert [(line["seq"], line["state"]) for line in read_steps(record.path)] == [
        (0, "intent"),
        (0, "completed"),
        (1, "intent"),
    ]


def test_a_run_constructs_no_model_client(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    result = run(app, tmp_path, compiled(FORM_STEPS, targets=FORM_TARGETS)).result

    assert [step.outcome for step in result.steps] == ["completed"] * 7
    assert built == []


def test_the_run_uses_the_scripts_browser_settings_not_the_project_or_spec_overrides(
    app: App, tmp_path: Path
) -> None:
    recorded = {
        "timezone": "Asia/Tokyo",
        "locale": "fr-FR",
        "viewport": [900, 700],
        "device_scale_factor": 2,
        "color_scheme": "dark",
    }
    config = ProjectConfig.model_validate(
        {
            "browser": {
                "timezone": "America/New_York",
                "locale": "de-DE",
                "viewport": [1000, 800],
                "device_scale_factor": 1,
                "color_scheme": "light",
            }
        }
    )
    spec = a_spec(
        tmp_path,
        config,
        start_url="/page/env",
        extra=(
            "browser: { timezone: Europe/Paris, locale: es-ES, viewport: [1100, 900],"
            " device_scale_factor: 3, color_scheme: light }\n"
        ),
    )

    run(app, tmp_path, compiled([], browser=recorded), config=config, spec=spec)

    [env] = [path for path in app.paths() if path.startswith("/did/env?")]
    assert parse_qs(urlsplit(env).query) == {
        "timezone": ["Asia/Tokyo"],
        "locale": ["fr-FR"],
        "viewport": ["900x700"],
        "scale": ["2"],
        "scheme": ["dark"],
    }
