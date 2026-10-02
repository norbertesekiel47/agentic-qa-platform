"""Which origin a document is on, and what the browser session records when
one isn't allowed (#44; ADR-0026, Two tiers of hosts, and its amendment on
document origins). Over HTTPS the egress proxy can't tell a page load from a
resource load, so the session checks the origin of every document it observes
or acts on, and a subresource host that becomes a document gains no
authority."""

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from aqa_core.schema import parse_origin

# Where a document on no allowed origin was found: the session's top-level
# page, or a popup.
type Reached = Literal["document", "popup"]

# How many of each record a session keeps; it counts them all. A page can
# open popups in a loop (Playwright launches Chromium with popup blocking
# off), and the first ones show what started it.
RECORD_LIMIT = 100


@dataclass(frozen=True)
class PolicyEvent:
    """A document from an origin the run doesn't allow, which the session
    found and never observed or acted on (ADR-0026). `url` is as Chromium
    reports it; `origin` is None when the document has none a run could
    allow, such as Chromium's error page. What it does to the run is #47's."""

    kind: Reached
    url: str
    origin: str | None


class PolicyEventError(Exception):
    """The session refused to observe or act on a document from an origin
    the run doesn't allow: a policy event, recorded in the session's
    `policy_events`. Its message names the origin only, never the URL, whose
    path and query the page chose."""

    def __init__(self, event: PolicyEvent) -> None:
        where = "no origin" if event.origin is None else event.origin
        super().__init__(
            f"the page is on {where}, which isn't one of the run's allowed origins: "
            "nothing on it is observed or acted on; navigate back to an allowed "
            "origin, or restart"
        )
        self.event = event


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
    - about:blank and about:srcdoc are on `inherited`: a frame's parent's
      origin, a popup's opener's, or None for the session's own page.
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
