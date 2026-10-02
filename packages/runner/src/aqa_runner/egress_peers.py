"""The sides of an HTTP/1.1 exchange through the run's egress gate, framed
with h11: https://h11.readthedocs.io/en/v0.16.0/api.html. The egress proxy
has both, the browser's and the upstream's; a runner-side request has the
upstream's (ADR-0026 and its 2026-10-01 amendment on the egress proxy)."""

import asyncio
import contextlib
import ssl
from collections.abc import Iterator
from dataclasses import dataclass

import h11

from aqa_runner.egress import EgressGate

# How much one read takes.
CHUNK = 65536


@dataclass(frozen=True)
class Peer:
    """One side of an exchange; this one is the browser's, in the egress
    proxy, whose failures end the exchange and nothing more."""

    http: h11.Connection
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def next(self) -> h11.Event:
        """The peer's next event, reading as much as it needs. The end of the
        stream is h11's to judge: ConnectionClosed between messages, a
        RemoteProtocolError within one."""
        while True:
            event = self.http.next_event()
            if isinstance(event, h11.Event):
                return event
            if event is h11.PAUSED:
                # A peer is read only when it owes an event.
                raise RuntimeError("a peer that waits on its reader was read")
            self.http.receive_data(await self.read())

    async def send(self, event: h11.Event) -> None:
        if data := self.http.send(event):
            await self.write(data)

    async def read(self) -> bytes:
        return await self.reader.read(CHUNK)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()


@dataclass(frozen=True)
class Upstream(Peer):
    """The upstream server's side: any failure is an infrastructure event,
    recorded with the gate."""

    gate: EgressGate
    host: str
    port: int

    async def read(self) -> bytes:
        with self._failures():
            return await super().read()

    async def write(self, data: bytes) -> None:
        with self._failures():
            await super().write(data)

    async def next(self) -> h11.Event:
        with self._failures():
            return await super().next()

    async def start_tls(self, context: ssl.SSLContext, server_hostname: str) -> None:
        """Speak TLS from here on, on the same connection, verifying the
        upstream's certificate for `server_hostname`. A handshake that fails,
        a certificate that doesn't verify included, is a failure like any
        other: https://docs.python.org/3.14/library/asyncio-stream.html#asyncio.StreamWriter.start_tls
        """
        with self._failures():
            await self.writer.start_tls(context, server_hostname=server_hostname)

    @contextlib.contextmanager
    def _failures(self) -> Iterator[None]:
        try:
            yield
        # A reset, an unreachable host, a timeout, or a message cut short.
        except (h11.RemoteProtocolError, OSError) as error:
            raise self.gate.record_failure(
                self.host, self.port, f"the exchange broke off: {error}"
            ) from error
