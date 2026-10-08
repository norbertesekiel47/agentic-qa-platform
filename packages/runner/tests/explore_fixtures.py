import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from aqa_runner.browser_session import BrowserSession, open_browser_session
from aqa_runner.locator_generation import register_identity_engine
from aqa_runner.loopback_server import LoopbackServer
from playwright.async_api import async_playwright

from packages.runner.tests.egress_fixtures import egress_proxy
from packages.runner.tests.pilot_pages import _STYLESHEETS, PAGES, RENDERINGS

PARTS = '<app-favorite-button><button>Unfavorite Article <span class="counter">(1)</span></button></app-favorite-button>'
MARKUP = f'<div class="banner">{PARTS}</div><div class="article-actions">{PARTS}</div>'
MODES = {
    "clean": MARKUP,
    "banner-bug": MARKUP.replace("(1)", "(3)", 1),
    "lower-bug": MARKUP.rsplit("(1)", 1)[0] + "(3)" + MARKUP.rsplit("(1)", 1)[1],
    "flattened": MARKUP.replace('<span class="counter">(1)</span>', "bonus 1 (3)", 1),
    "decoy": MARKUP.replace(
        '<div class="banner">', '<div class="banner"><span class="decoy">1</span>'
    ),
    "wrapped-lower": f'<div class="banner">{PARTS}</div><div class="article-actions"><section>{PARTS}</section></div>',
    "published-template-inferred": '<div class="banner"><app-article-meta><div class="info"><a class="author">jake</a><span class="date">October 1, 2026</span></div></app-article-meta></div>',
}


@dataclass
class App:
    origin: str = ""
    mode: str = "clean"
    writes: int = 0
    messages: list[bytes] = field(default_factory=list)
    slow_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_slow: asyncio.Event = field(default_factory=asyncio.Event)

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[App]:
        async with LoopbackServer(self.handle) as server:
            self.origin = f"http://127.0.0.1:{server.port}"
            try:
                yield self
            finally:
                self.release_slow.set()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin1").split("\r\n")
        method, path, _ = head[0].split(" ")
        headers = {
            name.lower(): value
            for line in head[1:]
            if ": " in line
            for name, value in [line.split(": ", 1)]
        }
        length = int(headers.get("content-length", "0"))
        await reader.readexactly(length)
        if headers.get("upgrade") == "websocket":
            await self.websocket(reader, writer, headers["sec-websocket-key"])
            return
        status, body = await self.response(method, path)
        encoded = body.encode()
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: text/html\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode()
            + encoded
        )
        await writer.drain()

    async def response(self, method: str, path: str) -> tuple[str, str]:
        target = urlsplit(path)
        if method == "POST" and target.path == "/reset":
            mode = parse_qs(target.query).get("mode", ["clean"])[0]
            if mode not in MODES:
                return "400 Bad Request", "unknown fixture mode"
            self.mode, self.writes = mode, 0
            self.messages.clear()
            self.slow_started.clear()
            self.release_slow.clear()
            return "204 No Content", ""
        if method == "POST" and target.path in ("/write", "/slow-write"):
            if target.path == "/slow-write":
                self.slow_started.set()
                async with asyncio.timeout(5):
                    await self.release_slow.wait()
            self.writes += 1
            return "204 No Content", ""
        name = target.path.removeprefix("/page/")
        if method == "GET" and name in PAGES:
            return "200 OK", (RENDERINGS / f"{name}.html").read_text() + "".join(
                f"<style>{sheet.read_text()}</style>" for sheet in _STYLESHEETS
            )
        if method == "GET" and (name in MODES or target.path == "/"):
            return "200 OK", MODES[self.mode if target.path == "/" else name]
        return "404 Not Found", "no fixture page"

    async def websocket(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, key: str
    ) -> None:
        digest = hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(),
            usedforsecurity=False,
        ).digest()
        accept = base64.b64encode(digest).decode()
        writer.write(
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        await writer.drain()
        async with asyncio.timeout(5):
            frame = await reader.readexactly(2)
            length = frame[1] & 127
            if frame[0] != 129 or not frame[1] & 128 or length > 125:
                return
            mask = await reader.readexactly(4)
            payload = await reader.readexactly(length)
            message = bytes(
                value ^ mask[index % 4] for index, value in enumerate(payload)
            )
            self.messages.append(message)
            self.writes += 1
            writer.write(bytes([129, len(message)]) + message)
            await writer.drain()
            frame = await reader.readexactly(2)
            if frame[0] == 136:
                length = frame[1] & 127
                if frame[1] & 128:
                    await reader.readexactly(4)
                await reader.readexactly(length)
                writer.write(b"\x88\x00")
                await writer.drain()


def in_app[T](scenario: Callable[[App, BrowserSession], Awaitable[T]]) -> T:
    async def run() -> T:
        async with App().serving() as app, async_playwright() as playwright:
            await register_identity_engine(playwright)
            async with (
                egress_proxy(app.origin) as egress,
                open_browser_session(playwright.chromium, egress=egress) as session,
            ):
                return await scenario(app, session)

    return asyncio.run(run())
