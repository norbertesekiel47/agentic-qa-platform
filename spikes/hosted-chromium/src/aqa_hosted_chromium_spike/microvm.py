"""The trial in a Lambda MicroVM: an HTTP server that answers Lambda's hooks
and runs one trial for each `POST /trial`.

Lambda snapshots a MicroVM image once `/ready` answers 200, with every running
process, and restores that snapshot into each new MicroVM. So the server
starts no browser before a request: a browser in the snapshot would be one
browser, its memory and random state included, cloned into every MicroVM.
https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html#microvms-build-process
"""

import json
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer

from aqa_hosted_chromium_spike.trial import Report, measure

# Lambda sends a MicroVM's inbound traffic, and its hooks, to this port.
# https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html#microvms-launching-port-routing
PORT = 8080
# https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html#microvms-launching-lifecycle-hooks
HOOKS = "/aws/lambda-microvms/runtime/v1/"


def server(address: tuple[str, int], trial: Callable[[], Report]) -> HTTPServer:
    """The MicroVM's server on `address`, which runs `trial` per request."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            # /ready tells Lambda to take the snapshot; /run, that this MicroVM
            # may receive traffic.
            if self.path in {f"{HOOKS}ready", f"{HOOKS}run"}:
                self._reply(HTTPStatus.OK, b"")
            elif self.path == "/trial":
                self._reply(HTTPStatus.OK, json.dumps(trial()).encode())
            else:
                self._reply(HTTPStatus.NOT_FOUND, b"")

        def _reply(self, status: HTTPStatus, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return HTTPServer(address, Handler)


if __name__ == "__main__":
    # Lambda's proxy reaches the server through the MicroVM's network
    # interface, so it listens on every interface. Only requests carrying a
    # token from create-microvm-auth-token get through the proxy.
    server(("", PORT), measure).serve_forever()
