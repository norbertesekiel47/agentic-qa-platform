"""Fixtures the executor's tests share (#46): a fixture app whose requests
note the steps record as it stood when each arrived, hand-written compiled
scripts, and a replay against the app. Imported by its path, as pytest
names the runner's test modules (TESTING.md §1, Shared egress fixtures)."""

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aqa_core.compiled import CompiledScript
from aqa_core.config import ProjectConfig
from aqa_core.project import load_spec
from aqa_core.spec import Spec
from aqa_runner.egress_proxy import EgressProxy
from aqa_runner.executor import RunResult, RunSetup, replay
from aqa_runner.run_record import RunRecord
from aqa_runner.settling import Window
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import gate

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
    # A page that leaves for a host the run doesn't allow 1.5 s after it loads.
    "leaves": """<script>
        setTimeout(() => {
            location = location.origin.replace("127.0.0.1", "localhost") + "/page/form";
        }, 1500);
    </script>""",
    # A popup to such a host 0.8 s after the page loads, and a pay button
    # that appears 2 s after it.
    "popup-then-pay": """<script>
        const elsewhere = location.origin.replace("127.0.0.1", "localhost") + "/page/form";
        setTimeout(() => window.open(elsewhere), 800);
        setTimeout(() => {
            const pay = document.createElement("button");
            pay.textContent = "Pay";
            pay.addEventListener("click", () => fetch("/write/pay", {method: "POST"}));
            document.body.append(pay);
        }, 2000);
    </script>""",
    # A field whose focus handler never returns, and one whose page makes
    # filling throw a long message with a terminal escape in it.
    "stall": """<label>Name <input onfocus="for (;;) {}"></label>""",
    "throws": """<label>Name <input></label><script>
        document.execCommand = () => {
            throw new Error("\\x1b[31m" + "x".repeat(100000));
        };
    </script>""",
    # A page that fetches the URL its `to` parameter names, as it loads.
    "fetches": """<script>fetch(new URLSearchParams(location.search).get("to"))</script>""",
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


@contextmanager
def serving_app() -> Iterator[App]:
    """The fixture app, serving until the block ends."""
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
    origins: tuple[str, ...] | None = None,
) -> Run:
    """Replay `script` against the app, or against the first of `origins`
    when given, which the run allows with the rest, with a record under
    `tmp_path`, for `spec` (by default one whose start_url is the form)."""
    settings = config or ProjectConfig()
    allowed = origins or (app.origin,)
    origin = allowed[0]
    # Declared private only so they may be loopback here.
    run_gate = gate(allowed=allowed, private=allowed[1:])
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
    {
        "seq": 3,
        "action": "press",
        "key": "Enter",
        "side_effect": True,
        "side_effect_basis": "network: GET /did/press",
    },
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


def paths(window: Window | None) -> list[tuple[str, str]]:
    """Each request in a step's window: its method and its URL's path."""
    assert window is not None
    return [
        (request.method, urlsplit(request.url).path) for request in window.requests.kept
    ]
