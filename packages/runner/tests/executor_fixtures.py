"""Fixtures the executor's tests share (#46), and the invariants' (#47): a
fixture app whose requests note the steps record as it stood when each
arrived, hand-written compiled scripts, and a replay against the app.
Imported by its path, as pytest names the runner's test modules (TESTING.md
§1, Shared egress fixtures)."""

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
from urllib.parse import parse_qs, urlsplit

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

# The shop page, whose save shows what was saved in #status, as `said`
# does it: the clean page's, the wrong page's, and the page that loses
# #status instead.
SHOP = """<label>Name <input id="name" oninput="fetch('/did/fill')"></label>
    <label>Size <select id="size"><option>S</option><option>M</option></select></label>
    <button id="save">Save</button>
    <section id="status-area"><p id="status" data-testid="status">Not saved</p></section>
    <script>
        addEventListener("keydown", (event) => {
            if (event.key === "Enter") fetch("/did/press");
        });
        document.querySelector("#save").addEventListener("click", () => {
            const name = document.querySelector("#name").value;
            const size = document.querySelector("#size").value;
            const status = document.querySelector("#status");
            fetch("/write/save", {method: "POST"}).then(() => { SAID; });
        });
    </script>"""
SHOPS = {
    "shop": SHOP.replace(
        "SAID", "status.textContent = `Saved ${name} in size ${size}`"
    ),
    "shop-wrong": SHOP.replace("SAID", 'status.textContent = "Saved nobody"'),
    "shop-gone": SHOP.replace("SAID", "status.remove()"),
}

# The fixture app's pages, by name.
PAGES = SHOPS | {
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
    # Rendered text no regex can search in time: forty a's, then a b.
    "catastrophic": """<p>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab</p>""",
    # An error toast that shows on save and fades 3 s later.
    "toast": """<button id="save">Save</button><section id="status-area"></section><script>
        document.querySelector("#save").addEventListener("click", () => {
            const area = document.querySelector("#status-area");
            area.innerHTML = '<p class="error">Card declined</p>';
            setTimeout(() => { area.innerHTML = ""; }, 3000);
        });
    </script>""",
    # A success message rendered ahead and hidden, beside one that shows.
    "pre-rendered": """<p id="confirmed" hidden>Order confirmed</p>
        <div style="display: none"><p id="inner">Order confirmed</p></div>
        <p id="pending">Order pending</p>""",
    # A page whose scripts stop answering 200 ms after it loads.
    "busy": """<p>Busy</p><script>setTimeout(() => { for (;;) {} }, 200);</script>""",
    # An error in the status area that the page never shows.
    "hidden-error": """<section id="status-area"><p class="error" hidden>Card declined</p></section>""",
    # The status area, without an error in it, 1.5 s after the page loads.
    "late-area": """<script>
        setTimeout(() => {
            document.body.insertAdjacentHTML("beforeend", '<section id="status-area"></section>');
        }, 1500);
    </script>""",
    # Sign-in pages for fill_secret steps (#49): a form that posts its
    # password to the app; a field whose page throws back the text it is
    # handed; one that, once filled, makes the check before a later action on
    # it throw what it holds (the frame beside it, on no origin and making no
    # request, makes every action ask); and one that, once filled, makes
    # reading its text throw what it holds.
    "signin": """<form method="post" action="/write/signin" accept-charset="utf-8">
            <label>Password <input type="password" name="password"></label>
            <label>Name <input name="name"></label>
            <button>Sign in</button>
        </form>""",
    "signin-throws": """<label>Password <input type="password"></label><script>
        document.execCommand = (command, ui, text) => { throw new Error(text); };
    </script>""",
    "signin-rethrows": """<label>Password <input type="password"
            oninput="this.matches = () => { throw new Error(this.value); }"></label>
        <iframe src="data:text/html,<p>Frame</p>"></iframe>""",
    "signin-hides": """<label>Password <input type="password"
            oninput="this.getBoundingClientRect = () => { throw new Error(this.value); }">
        </label>""",
    "form": """<label>Name <input oninput="fetch('/did/fill')"></label>
        <label>Size <select onchange="fetch('/did/select')">
            <option>S</option><option>M</option></select></label>
        <button class="save" onclick="fetch('/write/save', {method: 'POST'})">Save</button>
        <script>
            addEventListener("keydown", (event) => {
                if (event.key === "Enter") fetch("/did/press");
            });
        </script>""",
    # Each invariant's own trigger (#47): each fires that invariant and no
    # other. A dedicated worker's 5xx has no console entry, and an image whose
    # 200 response isn't an image fails without one.
    "console-error": """<script>
        console.warn("warning");
        console.log("log");
        console.error("console-trigger");
    </script>""",
    "exception": """<script>
        Promise.reject(new Error("rejection-trigger"));
        throw new Error("exception-trigger");
    </script>""",
    "worker-5xx": """<script>new Worker("/worker/fetch-500.js")</script>""",
    "worker-599": """<script>new Worker("/worker/fetch-599.js")</script>""",
    "broken-image": """<img src="/image/not-an-image">""",
    "broken-data-image": """<img src="data:image/png;base64,AAAA">""",
    # The page's own 5xx and a missing image, each with Chromium's own console
    # entry for the failed load.
    "page-5xx": """<script>fetch("/status/500")</script>""",
    "missing-image": """<img src="/status/404">""",
    # A load that fails without reaching any network: a revoked blob. The
    # fetch's rejection is caught, so only the console entry fires.
    "revoked-blob": """<script>
        const blob = URL.createObjectURL(new Blob(["gone"]));
        URL.revokeObjectURL(blob);
        fetch(blob).catch(() => {});
    </script>""",
    # More console errors than an invariant keeps, each longer than it keeps.
    "many-console-errors": """<script>
        for (let i = 0; i < 150; i++) console.error(i + ":" + "x".repeat(1000));
    </script>""",
    # Every trigger, telling a first load from a reload.
    "every-trigger": """<img src="/image/not-an-image"><script>
        const load = performance.getEntriesByType("navigation")[0].type;
        new Worker("/worker/fetch-500.js");
        console.error("console-" + load);
        throw new Error("thrown-" + load);
    </script>""",
    # A page that tries each way its scripts have to stop a broken image's
    # report, then breaks an image.
    "tampering": """<script>
        delete globalThis.__playwright__binding__;
        globalThis.__playwright__binding__ = () => {};
        globalThis.__playwright__binding__controller__ = undefined;
        globalThis.aqaBrokenImage = () => {};
        JSON.stringify = () => "";
        Map.prototype.get = () => undefined;
        Object.defineProperty(HTMLImageElement.prototype, "currentSrc", {get: () => "/lie"});
        addEventListener("error", (event) => event.stopImmediatePropagation(), true);
    </script><img src="/image/not-an-image">""",
    # An image that loads, then an error event the page makes up for it.
    "synthetic-error": """<img id="ok" src="/image/ok"><script>
        onload = () => ok.dispatchEvent(new Event("error"));
    </script>""",
    # What a request the run refuses leaves behind: its console entry and a
    # broken image, straight and through one or two redirect hops routing
    # can't see, beside a console error of the page's own.
    "refused-symptoms": """<img src="http://analytics.example.test/pixel.png">
        <img src="/redirect?to=http%3A%2F%2Fanalytics.example.test%2Fhop.png#hop">
        <img src="/redirect?to=%2Fredirect%3Fto%3Dhttp%253A%252F%252Fanalytics.example.test%252Ftwo.png">
        <script>console.error("unrelated")</script>""",
    # A script the run refuses, which the page's next script needs.
    "refused-script": """<script src="http://analytics.example.test/lib.js"></script>
        <script>analytics.track()</script>""",
    # An image whose first load redirects to a host the run refuses, and
    # whose load after a reload isn't an image.
    "once-refused": """<img src="/image/once-refused">""",
    # Three images of one URL redirected to a refused host: Blink loads it
    # once for all three.
    "same-image-refused": """
        <img src="/redirect?to=http%3A%2F%2Fanalytics.example.test%2Fsame.png">
        <img src="/redirect?to=http%3A%2F%2Fanalytics.example.test%2Fsame.png">
        <img src="/redirect?to=http%3A%2F%2Fanalytics.example.test%2Fsame.png">""",
    # More images at a refused host than any record of them keeps.
    "many-refused-images": """<body><script>
        for (let i = 0; i < 1001; i++) {
            const image = new Image();
            image.src = "http://analytics.example.test/pixel.png?" + i;
            document.body.append(image);
        }
    </script>""",
    # A CSS image whose load is redirected to a refused host, which no
    # element reports; then an <img> redirected there; then an <img> at the
    # CSS image's URL, whose second load isn't an image.
    "background-then-images": """<body>
        <div style="width: 1px; height: 1px; background-image: url('/image/once-refused')"></div>
        <script>
            const image = (src, after) => setTimeout(() => {
                const shown = new Image();
                shown.src = src;
                document.body.append(shown);
            }, after);
            image("/redirect?to=http%3A%2F%2Fanalytics.example.test%2Fimage.png", 500);
            image("/image/once-refused", 1000);
        </script>""",
    # A frame's document, and the page's own after it loads, opened and written
    # anew with a broken image: opening erases every listener they had.
    "rewritten-frame": """<iframe></iframe><script>
        const written = frames[0].document;
        written.open();
        written.write("<img src='/image/not-an-image'>");
        written.close();
    </script>""",
    "rewritten-page": """<script>
        onload = () => {
            document.open();
            document.write("<img src='/image/not-an-image'>");
            document.close();
        };
    </script>""",
    # A page that calls the reporter's binding, which its world doesn't have.
    "forging-binding": """<script>
        try {
            aqaBrokenImage(location.origin + "\\n" + location.origin + "/forged");
        } catch (error) {}
    </script>""",
    # Console calls with no arguments, one naming a refused URL.
    "zero-arg-console": """<script>
        console.error();
        eval("console.error()\\n//# sourceURL=http://analytics.example.test/zero.js");
    </script>""",
    # A console error of the page's own that says it came from a refused URL.
    "forged-source": """<script>
        eval("console.error('forged')\\n//# sourceURL=http://analytics.example.test/forged.js");
    </script>""",
    # A broken image in a frame on no origin a run could allow (data:), and a
    # frame at the URL the `to` parameter names.
    "framed": """<iframe src="data:text/html,<img src='data:image/png;base64,AAAA'>"></iframe>
        <script>
            const frame = document.createElement("iframe");
            frame.src = new URLSearchParams(location.search).get("to");
            document.body.append(frame);
        </script>""",
}

# A 1x1 GIF: an image that loads.
GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020244"
    "01003b"
)

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
    # Each POST's path and body, in order.
    bodies: list[tuple[str, bytes]] = field(default_factory=list)

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
    and `/write/…` (204), `/held/<key>` (204 once released), `/status/<code>`
    (that status, empty), `/image/ok` (a GIF), `/image/once-refused` (302 to
    a refused host the first time), any other `/image/…` (a page, not an
    image), `/worker/fetch-<code>.js` (a worker that fetches `/status/<code>`)
    and `/redirect?to=<url>` (302 there), noting the steps record as it stood
    when each request arrived."""

    app: App

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.app.arrived:
            self.app.bodies.append((self.path, body))
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
        elif kind == "status":
            self._empty(int(name))
        elif kind == "image":
            self._image(name)
        elif kind == "worker":
            status = name.removeprefix("fetch-").removesuffix(".js")
            self._send("text/javascript", f'fetch("/status/{status}");'.encode())
        elif kind == "redirect":
            self._redirect(parse_qs(urlsplit(self.path).query)["to"][0])
        else:
            self._empty(HTTPStatus.NOT_FOUND)

    def _image(self, name: str) -> None:
        if name == "ok":
            self._send("image/gif", GIF)
        elif name == "once-refused" and self.app.paths().count(self.path) == 1:
            self._redirect("http://analytics.example.test/once.png")
        else:
            self._page("<p>Not an image</p>")

    def _page(self, body: str) -> None:
        self._send("text/html", f"<!doctype html><title>app</title>{body}".encode())

    def _send(self, content_type: str, data: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _empty(self, status: int) -> None:
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


# Targets on the shop page, its error included: a not_visible target's
# locators are scoped (DATA_MODEL §7, "Checked by the loader").
SHOP_TARGETS = {
    "name": {"semantic": "the name field", "locators": [{"css": "#name"}]},
    "size": {"semantic": "the size list", "locators": [{"css": "#size"}]},
    "save": {"semantic": "the save button", "locators": [by_role("button", "Save")]},
    "status": {
        "semantic": "the save status",
        "locators": [{"css": "#status"}, {"testid": "status"}],
    },
    "error": {
        "semantic": "the error message",
        "locators": [{"css": ".error", "scope": {"css": "#status-area"}}],
    },
}


def shop_script(page: str) -> CompiledScript:
    """Every M1 action on the shop page called `page`, then one assertion
    of each M1 check."""
    return compiled(
        [
            {
                "seq": 1,
                "action": "navigate",
                "url": f"/page/{page}",
                "side_effect": False,
            },
            {"seq": 2, "action": "reload", "side_effect": False},
            {
                "seq": 3,
                "action": "fill",
                "target": "name",
                "value": "Ada",
                "side_effect": False,
            },
            {
                "seq": 4,
                "action": "select",
                "target": "size",
                "option": "M",
                "side_effect": False,
            },
            {"seq": 5, "action": "press", "key": "Enter", "side_effect": False},
            {
                "seq": 6,
                "action": "click",
                "target": "save",
                "side_effect": True,
                "side_effect_basis": "network: POST /write/save",
            },
        ],
        targets=SHOP_TARGETS,
        assertions=[
            {"id": "a1", "expect_index": 0, "check": "text_visible", "text": "saved"},
            {
                "id": "a2",
                "expect_index": 0,
                "check": "text_in_target",
                "target": "status",
                "pattern": r"Saved Ada in size M\b",
            },
            {"id": "a3", "expect_index": 0, "check": "not_visible", "target": "error"},
            {
                "id": "a4",
                "expect_index": 0,
                "check": "url_matches",
                "pattern": "/page/shop",
            },
        ],
    )
