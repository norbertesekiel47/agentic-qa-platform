"""Which origin a document is on, and what the browser session records when
one isn't allowed (#44; ADR-0026, Two tiers of hosts, and its amendment on
document origins). Over HTTPS the egress proxy can't tell a page load from a
resource load, so the session checks the origin of every document it observes
or acts on, and a subresource host that becomes a document gains no
authority."""

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from aqa_core.schema import parse_origin
from playwright.async_api import Frame

# Where a document on no allowed origin was found: the session's top-level
# page, the frame of an element the session was about to give out or act on,
# or a popup; or a URL navigate refused before it left.
type PolicyEventKind = Literal["document", "frame", "popup", "navigation"]

# What each kind of policy event refused, as its message says it.
REFUSED_BY_KIND: dict[PolicyEventKind, str] = {
    "document": "the page is",
    "frame": "the element's frame is",
    "popup": "a popup is",
    "navigation": "the URL to navigate to is",
}

# Characters a browser drops or reads differently from Python's urlsplit,
# which no URL to navigate to needs as themselves: whitespace and control
# characters, which WHATWG URL parsing strips, and a backslash, which it
# reads as a slash after an http(s) scheme.
UNREAD = re.compile(r"[\s\x00-\x1f\x7f\\]")

# How many of each record a session keeps; it counts them all. A page can
# open popups in a loop (Playwright launches Chromium with popup blocking
# off), and the first ones show what started it.
RECORD_LIMIT = 100


@dataclass(frozen=True)
class PolicyEvent:
    """A document from an origin the run doesn't allow, which the session
    found and never observed or acted on, or a URL `navigate` refused
    (ADR-0026). `url` is the document's as Chromium reports it, the URL
    `navigate` was given, or empty for an element in no frame. `origin` is
    None when there is none a run could allow, as for Chromium's error page.
    What it does to the run is #47's."""

    kind: PolicyEventKind
    url: str
    origin: str | None


@dataclass(frozen=True)
class Popup:
    """A page another page opened, which the session recorded and closed.
    `url` is its first URL as Playwright reports it once it has navigated
    there; `opener` is the opener's URL, None when Playwright reports none,
    as once the opener has closed."""

    url: str
    opener: str | None


class PolicyEventError(Exception):
    """The session refused to observe or act on a document from an origin
    the run doesn't allow: a policy event, recorded in the session's
    `policy_events`. Its message names the origin only, never the URL, whose
    path and query the page chose."""

    def __init__(self, event: PolicyEvent) -> None:
        if event.origin is None:
            where = "on no origin a run could allow"
        else:
            where = f"on {event.origin}, which isn't one of the run's allowed origins"
        super().__init__(
            f"{REFUSED_BY_KIND[event.kind]} {where}: nothing there is observed or acted on; "
            "navigate to an allowed origin, or restart"
        )
        self.event = event


class DocumentChangedError(Exception):
    """A frame of the page navigated or was removed while the session took a
    snapshot or looked a target up, so what it saw could come from a
    document it never checked: that is discarded, and the caller looks
    again."""

    def __init__(self) -> None:
        super().__init__(
            "the page changed while the session looked at it, so what it saw was "
            "discarded: take a new snapshot, or look the target up again"
        )


@dataclass
class Records[T]:
    """The first `RECORD_LIMIT` of something the session records, and how
    many there were in all."""

    kept: list[T] = field(default_factory=list)
    total: int = 0

    def add(self, entry: T) -> None:
        self.total += 1
        if len(self.kept) < RECORD_LIMIT:
            self.kept.append(entry)


def document_origin(url: str, inherited: str | None) -> str | None:
    """The origin of a document at `url`, as Chromium reports a frame's URL,
    written as an origin writes it; None when it has none a run could allow.

    - An http(s) document is on its URL's origin, and a blob on its maker's.
    - about:blank and about:srcdoc are on `inherited`: for a frame, its
      parent's origin when `frame_origin` finds the parent's is theirs, and
      None for a top-level page.
    - Any other document, such as data:, Chromium's error page or a file, is
      on none."""
    parts = urlsplit(url)
    match parts.scheme:
        case "http" | "https":
            # A user and password before the host aren't part of the origin.
            host = parts.netloc.rpartition("@")[2]
            try:
                return parse_origin(f"{parts.scheme}://{host}")
            except ValueError:  # a host no origin can name, such as `app.test.`
                return None
        case "blob":
            inner = url.removeprefix("blob:")
            return (
                document_origin(inner, None)
                if urlsplit(inner).scheme in ("http", "https")
                else None
            )
        case "about" if parts.path in ("blank", "srcdoc"):
            return inherited
        case _:
            return None


async def frame_origin(frame: Frame) -> str | None:
    """The origin of `frame`'s document, from the URL Chromium reports for it
    (https://playwright.dev/python/docs/api/class-frame#frame-url), which the
    page's scripts can't forge.

    about:srcdoc is on its parent frame's origin: only the parent's srcdoc
    attribute makes one, and Chromium refuses a navigation to it. about:blank
    is on its parent's only when the parent can reach its document. A
    document reached by navigating to about:blank is on the origin of the
    frame that navigated it there, which may be a subresource host's that
    then writes into it; the parent reaching it is the browser's own
    same-origin check, run in the parent's world, where that host can't."""
    parent = frame.parent_frame
    if parent is None:
        return document_origin(frame.url, None)
    inherited = await frame_origin(parent)
    if inherited is not None and is_blank(frame.url) and not await reaches(frame):
        inherited = None
    return document_origin(frame.url, inherited)


def is_blank(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "about" and parts.path == "blank"


async def reaches(frame: Frame) -> bool:
    """Whether `frame`'s parent can reach its document: whether they are on
    one origin."""
    owner = await frame.frame_element()
    try:
        # contentDocument is null for a frame on another origin:
        # https://html.spec.whatwg.org/multipage/iframe-embed-object.html#dom-iframe-contentdocument
        # An <embed> has none at all (undefined), so its frame never counts
        # as reached: `!= null` refuses both.
        return bool(await owner.evaluate("(owner) => owner.contentDocument != null"))
    finally:
        await owner.dispose()


def navigable_origin(url: str) -> str | None:
    """The origin `navigate` would go to at `url`: an absolute http(s) URL's,
    written as an origin writes it; None for any other URL, and for one with
    whitespace, a control character or a backslash anywhere, which a browser
    reads differently from Python's parser."""
    if UNREAD.search(url):
        return None
    try:
        scheme = urlsplit(url).scheme
    except ValueError:  # brackets that don't close
        return None
    return document_origin(url, None) if scheme in ("http", "https") else None
