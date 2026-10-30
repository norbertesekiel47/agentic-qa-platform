"""Screenshots a run may save (ADR-0034; SECURITY §5): `take_screenshot`
makes a `Screenshot`, which nothing else can make, and
`RunRecord.write_screenshot` saves one.

Until `fill_secret` hands the page a test secret's value in the browser
session, a screenshot is the live page's. From then on it is a render of
a snapshot, which the page can't steer:
- DevTools takes the page's MHTML archive and a DOM snapshot of its
  fields' values. Neither runs anything in the page's worlds or fires an
  event in it.
- Every part of the archive is scrubbed with the run's scan, raw and with
  HTML's character references decoded. Each field's value is dropped when
  the scan finds a bound value in it or it shares `SHARED_RUN` characters
  with one, and every editable element is emptied.
- What is left is rendered in a page of its own, with JavaScript off and
  every request answered from the archive or refused, at the session's
  viewport, scale and colour scheme and the live page's scroll. Its fields,
  frames, options and editable elements must be the snapshot's, so a scrub
  that cut markup is caught, and the field values and text it then shows
  are scanned again before that page is captured.
The render has no canvas, web fonts, adopted style sheets, shadow-root
`<style>`, inner scroll or element with a `hidden` attribute, and a value
the page draws into an image is drawn there too: ADR-0034's residuals, the
last #250's OCR check."""

import asyncio
import html
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import InitVar, dataclass, field
from email import message_from_bytes
from email.message import Message
from functools import partial
from itertools import chain
from typing import Annotated, NotRequired, TypedDict, cast
from urllib.parse import urldefrag, urlsplit

from playwright.async_api import CDPSession, Error, Page, Route
from pydantic import AfterValidator, Base64Bytes, TypeAdapter, ValidationError

from aqa_runner.browser_session import BrowserSession
from aqa_runner.redaction import Redactor

# How long the live page's snapshot, and then its scrub and render, may
# take: a page that keeps its main thread busy holds a snapshot back
# (ADR-0034).
CAPTURE_SECONDS = 5.0

# The largest archive a capture scrubs. On CI's runner, the slowest host
# measured, a page of text this size takes up to 2.4 s to snapshot, scrub
# and render, half of CAPTURE_SECONDS (ADR-0034). A larger one is withheld
# rather than left scrubbing on in a thread the bound has cut off.
ARCHIVE_CHARS = 130_000

# A field's value that shares this many consecutive characters with a bound
# value, or all of a shorter one, is dropped from a render. A test secret
# has at least 4 bytes, so a longer run would let a short one show whole
# (ADR-0034).
SHARED_RUN = 4

# The computed style a DOM snapshot reports editing by.
USER_MODIFY = "-webkit-user-modify"

# Where a render serves the archive's frames, by Content-ID, on the page's
# own origin, so the render's script reaches into each.
ARCHIVE_PATH = "/.aqa-archive/"

# An HTML character reference, as html.unescape finds one (CPython 3.14's
# `html._charref`).
_REFERENCE = re.compile(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
# The encoding Blink names in each HTML part's <meta>.
_DECLARED = re.compile(rb"""charset=["']?([A-Za-z0-9._:-]+)""")
# A reference to another part of the archive, as Blink writes one in an
# attribute or a style sheet's url(): quoted, and to one of its own
# Content-IDs. Text that only reads like one is left alone.
_REFERENCED = re.compile(r'(?<=")cid:(?=[^"\s]*@mhtml\.blink")')
# A shadow root as Blink writes one into an archive. A render's parser
# attaches it, open, so the render's script reaches into it.
_SHADOW = re.compile(r'<template shadowmode="(?:open|closed)"')

# In the render, before its capture: walks each document's flat tree, as a
# DOM snapshot lists it (each shadow root's content, then what each slot
# shows), and each frame's document; drops <noscript>, which a page with
# script never showed; checks that the kinds of its fields, its frames, its
# options (with their select and whether it takes several) and its elements
# with a `contenteditable` attribute are the snapshot's, in order, and gives
# each field its value or its checked state and each option its chosen
# state; walks again, without choosing, since a choice can copy an option's
# content; hides each frame it can't reach; empties each such element and
# every editing host; checks each option's chosen state; and scrolls to the
# live page's place. Null when they don't match; otherwise the values of the
# fields and the text left.
RESTORE = """({plan, scroll}) => {
    const fields = [], texts = [], emptied = [], chosen = [];
    const walk = (document, plan, choosing) => {
        const kinds = [], inputs = [], frames = [], options = [];
        let selects = 0;
        // `owner`: the kind of an option in `node`, as `_plan` gives it.
        const visit = (node, owner) => {
            if (node.nodeType === Node.TEXT_NODE) texts.push(node);
            if (node.nodeType === Node.ELEMENT_NODE) {
                const name = node.localName.toLowerCase();
                if (name === "noscript") return node.remove();
                if (name === "template") return;
                if (name === "select") {
                    owner = `${selects++}${node.hasAttribute("multiple") ? " multiple" : ""}`;
                }
                if (name === "input" || name === "textarea") {
                    inputs.push(node);
                    kinds.push([name, name === "input" ? node.getAttribute("type") : null]);
                } else if (name === "iframe" || name === "frame") {
                    frames.push(node);
                    kinds.push([name, null]);
                } else if (node.hasAttribute("contenteditable")) {
                    // An option among them isn't chosen: a customizable
                    // select would copy its content where it isn't editable.
                    emptied.push(node);
                    kinds.push([name, "contenteditable"]);
                } else if (name === "option") {
                    options.push(node);
                    kinds.push([name, owner]);
                }
                if (node.isContentEditable && !node.parentElement?.isContentEditable) {
                    emptied.push(node);
                }
            }
            let kids = node.shadowRoot?.childNodes ?? node.childNodes;
            if (node.localName === "slot" && node.getRootNode() instanceof ShadowRoot) {
                const assigned = node.assignedNodes();
                if (assigned.length > 0) kids = assigned;
            }
            for (const kid of [...kids]) visit(kid, owner);
        };
        visit(document, null);
        if (plan && JSON.stringify(kinds) !== JSON.stringify(plan.kinds)) return false;
        if (plan && choosing) {
            // Any other input's `value` is its attribute, which the archive
            // holds, or a file's name.
            const kept = ["hidden", "submit", "image", "reset", "button", "file"];
            inputs.forEach((input, i) => {
                if (input.type === "checkbox" || input.type === "radio") {
                    input.checked = plan.checked[i];
                } else if (!kept.includes(input.type)) input.value = plan.values[i];
            });
            options.forEach((option, i) => { option.selected = plan.selected[i]; });
            // A select whose every option is unchosen picks its first, so
            // one the page left blank is made blank again.
            options.forEach((option, i) => {
                if (option.selected && !plan.selected[i]) {
                    option.closest("select").selectedIndex = -1;
                }
            });
        } else if (plan) {
            options.forEach((option, i) => chosen.push([option, plan.selected[i]]));
        }
        fields.push(...inputs);
        return frames.every((frame, i) => {
            const inner = frame.contentDocument;
            if (inner) return walk(inner, plan ? plan.frames[i] : null, choosing);
            // On an opaque origin (sandboxed): nothing in it can be checked
            // or emptied, so it isn't shown.
            frame.style.setProperty("visibility", "hidden", "important");
            return true;
        });
    };
    if (!walk(document, plan, true)) return null;
    // A choice the walk restored copies the option's content, as new nodes,
    // into a <selectedcontent> it had passed, so a second walk checks, lists,
    // empties and scans the final DOM. It chooses nothing: in a select that
    // takes the choices otherwise than the page's did, each copies anew.
    fields.length = texts.length = emptied.length = 0;
    if (!walk(document, plan, false)) return null;
    for (const element of emptied) element.replaceChildren();
    // Emptying an element that holds an option takes the option out, and its
    // select chooses again.
    if (chosen.some(([option, planned]) => option.selected !== planned)) return null;
    window.scrollTo({left: scroll[0], top: scroll[1], behavior: "instant"});
    const shown = (nodes) => nodes.filter((node) => node.isConnected);
    // For DROP: nothing but our own scripts runs in a render.
    window.renderedFields = shown(fields);
    return {
        values: renderedFields.map((field) => field.value),
        text: shown(texts).map((text) => text.data).join(""),
    };
}"""

# Empties the fields RESTORE listed at these places, in the render.
DROP = """(dropped) => {
    for (const place of dropped) renderedFields[place].value = "";
}"""

_CAPTURED = object()

# A render's archive parts, by URL: each one's content type and body.
type _Parts = dict[str, tuple[str, bytes]]


class _Document(TypedDict):
    """What a render must find of a document, in walk order: the kind of
    each field (its element and an input's `type` attribute), frame, option
    (its element and its select's place among the document's, with
    ` multiple` when that select has the attribute) and element with a
    `contenteditable` attribute (its element and that name); each field's
    value and checked state; whether each option is chosen; and each frame's
    document."""

    kinds: list[tuple[str, str | None]]
    values: list[str]
    checked: list[bool]
    selected: list[bool]
    frames: list[_Document | None]


# The parts of DevTools' replies a capture reads, checked as they arrive: a
# reply in any other shape withholds the screenshot.
# https://chromedevtools.github.io/devtools-protocol/tot/DOMSnapshot/#type-DocumentSnapshot
class _Data(TypedDict):
    data: str


class _Shot(TypedDict):
    data: Base64Bytes


class _Viewport(TypedDict):
    pageX: float
    pageY: float


class _Metrics(TypedDict):
    cssVisualViewport: _Viewport


class _Flags(TypedDict):
    """The nodes that are so: a checkbox or radio button that is checked,
    or an option that is chosen."""

    index: list[int]


class _Rare(_Flags):
    """Values of the nodes at `index`, one each."""

    value: list[int]


class _Nodes(TypedDict):
    """Each node's name, parent and attributes (name and value by turns)."""

    nodeName: list[int]
    parentIndex: list[int]
    attributes: list[list[int]]
    inputValue: NotRequired[_Rare]
    textValue: NotRequired[_Rare]
    contentDocumentIndex: NotRequired[_Rare]
    inputChecked: NotRequired[_Flags]
    optionSelected: NotRequired[_Flags]


class _Layout(TypedDict):
    nodeIndex: list[int]
    styles: list[list[int]]


class _DocumentSnapshot(TypedDict):
    nodes: _Nodes
    layout: _Layout


class _Snapshot(TypedDict):
    strings: list[str]
    documents: list[_DocumentSnapshot]


class _Shown(TypedDict):
    """What RESTORE reports of a render."""

    values: list[str]
    text: str


def _aligned(snapshot: _Snapshot) -> _Snapshot:
    """`snapshot`, when its arrays of a node each are in step and each index
    in it is to something it holds: a string (-1 for the empty one), a node,
    a node's parent listed before it, and a frame's document listed after
    the frame's own."""
    strings, documents = len(snapshot["strings"]), len(snapshot["documents"])
    for place, document in enumerate(snapshot["documents"]):
        nodes, layout = document["nodes"], document["layout"]
        count = len(nodes["nodeName"])
        values, texts, frames = (
            nodes.get("inputValue"),
            nodes.get("textValue"),
            nodes.get("contentDocumentIndex"),
        )
        lengths = [
            (len(nodes["parentIndex"]), count),
            (len(nodes["attributes"]), count),
            (len(layout["styles"]), len(layout["nodeIndex"])),
            *(
                (len(data["value"]), len(data["index"]))
                for data in (values, texts, frames)
                if data
            ),
        ]
        if any(one != other for one, other in lengths) or any(
            len(attributes) % 2 for attributes in nodes["attributes"]
        ):
            raise ValueError("a DOM snapshot's arrays are out of step")
        named = chain(
            *nodes["attributes"],
            *layout["styles"],
            *(data["value"] for data in (values, texts) if data),
        )
        if not (
            all(0 <= name < strings for name in nodes["nodeName"])
            and all(-1 <= index < strings for index in named)
            and all(-1 <= up < node for node, up in enumerate(nodes["parentIndex"]))
            and all(0 <= node < count for node in layout["nodeIndex"])
            and all(place < inner < documents for inner in _rare(frames).values())
        ):
            raise ValueError("a DOM snapshot's index is out of range")
    return snapshot


_DATA = TypeAdapter(_Data)
_SHOT = TypeAdapter(_Shot)
_METRICS = TypeAdapter(_Metrics)
_SNAPSHOT: TypeAdapter[_Snapshot] = TypeAdapter(
    Annotated[_Snapshot, AfterValidator(_aligned)]
)
_SHOWN: TypeAdapter[_Shown | None] = TypeAdapter(_Shown | None)


class ScreenshotWithheldError(Exception):
    """A screenshot that wasn't taken, and why, in words of ours that
    never quote the page."""


@dataclass(frozen=True)
class Screenshot:
    """A PNG `take_screenshot` made, and the scan of the session it made it
    in, which a record that saves it must share. Only `take_screenshot`
    makes one, so a record never holds an image no capture scrubbed."""

    png: bytes = field(repr=False)
    redactor: Redactor = field(repr=False)
    _key: InitVar[object] = None

    def __post_init__(self, _key: object) -> None:
        if _key is not _CAPTURED:
            raise TypeError("only take_screenshot makes a Screenshot")


async def take_screenshot(session: BrowserSession) -> Screenshot:
    """A screenshot of the session's page, at its viewport in CSS pixels:
    the live page's until the session's first `fill_secret`, a render of
    its scrubbed snapshot from then on. On a page off the run's allowed
    origins it raises `PolicyEventError`, and after a frame change during
    the snapshot `DocumentChangedError`, as every observation does.

    It is withheld (`ScreenshotWithheldError`, with a reason that never
    quotes the page, so nothing is saved) when the snapshot, its scrub or
    its render takes over `CAPTURE_SECONDS`; a DevTools call or the render
    fails; DevTools replies in a shape this module doesn't read; the archive
    is over `ARCHIVE_CHARS`, isn't 7-bit MIME, names no page, or has a part
    in an encoding Python can't decode; a part spells a bound value after
    the scrub; an element is editable in a way the snapshot doesn't hold;
    or the render's fields, frames, options and editable elements don't
    match the snapshot's, or it spells a bound value."""
    try:
        png = await _captured(session)
    except TimeoutError:
        reason = f"the page's snapshot, its scrub or its render took over {CAPTURE_SECONDS} s"
    except Error:
        # Playwright's general error type: a DevTools call, or the render's
        # load or capture, failed. Its message can hold the page's URL,
        # which can spell a value.
        reason = "a DevTools call or the render failed"
    except ValidationError:
        # Its message quotes the reply, which holds the page's text.
        reason = "DevTools replied in a shape the capture doesn't read"
    else:
        return Screenshot(png, session.redactor, _key=_CAPTURED)
    # Raised outside the handlers, so the error behind it isn't even its
    # context.
    raise ScreenshotWithheldError(reason)


async def _captured(session: BrowserSession) -> bytes:
    # The bound covers the page's checks too, which a busy page holds back.
    async with (
        asyncio.timeout(CAPTURE_SECONDS),
        session.observing_page() as page,
        _devtools(page) as devtools,
    ):
        # Read under the session's lock, which `fill_secret` holds from its
        # checks through its fill, so a capture queued behind the first fill
        # is a render.
        if not session.secret_filled:
            # https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-captureScreenshot
            shot = await devtools.send("Page.captureScreenshot", {"format": "png"})
            return _SHOT.validate_python(shot)["data"]
        # https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-getLayoutMetrics
        metrics = await devtools.send("Page.getLayoutMetrics")
        # https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-captureSnapshot
        archive = await devtools.send("Page.captureSnapshot", {"format": "mhtml"})
        # https://chromedevtools.github.io/devtools-protocol/tot/DOMSnapshot/#method-captureSnapshot
        dom = await devtools.send(
            "DOMSnapshot.captureSnapshot", {"computedStyles": [USER_MODIFY]}
        )
    mhtml = _DATA.validate_python(archive)["data"]
    if len(mhtml) > ARCHIVE_CHARS:
        raise ScreenshotWithheldError(f"the archive is over {ARCHIVE_CHARS} characters")
    viewport = _METRICS.validate_python(metrics)["cssVisualViewport"]
    async with asyncio.timeout(CAPTURE_SECONDS):
        # In a thread, so a long scrub holds up neither the event loop nor
        # the egress proxy that runs on it. A Redactor's scans change
        # nothing it holds, so one runs in any thread.
        parts, main, plan = await asyncio.to_thread(
            _prepared, mhtml, dom, session.redactor
        )
        return await _render(
            session, parts, main, plan, (viewport["pageX"], viewport["pageY"])
        )


def _prepared(
    archive: str, reply: object, redactor: Redactor
) -> tuple[_Parts, str, _Document]:
    """The render's archive parts, its page's URL and its plan, from the
    archive and DevTools' DOM snapshot `reply`."""
    dom = _SNAPSHOT.validate_python(reply)
    if _editing_unmarked(dom):
        raise ScreenshotWithheldError(
            "an element is editable in a way the snapshot doesn't hold "
            "(designMode, or a style it leaves out)"
        )
    parts, main = _scrubbed(archive, redactor)
    return parts, main, _plan(dom, 0, redactor)


@asynccontextmanager
async def _devtools(page: Page) -> AsyncIterator[CDPSession]:
    """A DevTools session of `page`'s own, which the page can't see."""
    devtools = await page.context.new_cdp_session(page)
    try:
        yield devtools
    finally:
        await devtools.detach()


def _scrubbed(archive: str, redactor: Redactor) -> tuple[_Parts, str]:
    """`archive`'s parts, each scrubbed and rewritten for a render, by the
    URLs a render asks for them by, and the page's own URL."""
    if not archive.isascii():
        raise ScreenshotWithheldError("the archive isn't 7-bit MIME")
    message = message_from_bytes(archive.encode("ascii"))
    parts = [part for part in message.walk() if not part.is_multipart()]
    main = _header(parts[0], "Content-Location") if parts else None
    if main is None:
        raise ScreenshotWithheldError("the archive names no page")
    place = urlsplit(main)
    prefix = f"{place.scheme}://{place.netloc}{ARCHIVE_PATH}"
    served: _Parts = {}
    for part in parts:
        kind, body = _scrubbed_part(part, redactor, prefix)
        named = _header(part, "Content-ID")
        # A frame is named by its Content-ID, a <style> by a `cid:` URL, and
        # each is asked for as its text names it once `cid:` is rewritten.
        for url in (
            named and f"cid:{named.strip('<>')}",
            _header(part, "Content-Location"),
        ):
            if url:
                asked = prefix + url[4:] if url.startswith("cid:") else url
                served.setdefault(urldefrag(asked).url, (kind, body))
    return served, urldefrag(main).url


def _header(part: Message, name: str) -> str | None:
    """A part's header, any folding taken out: Blink's are URLs and IDs."""
    value = part[name]
    return None if value is None else re.sub(r"\s+", "", str(value))


def _scrubbed_part(part: Message, redactor: Redactor, prefix: str) -> tuple[str, bytes]:
    """A part's content type and body as a render gets them. Text is
    served as UTF-8 of exactly what was scanned, whatever its encoding
    was, so the render reads nothing the scan didn't."""
    kind = part.get_content_type()
    # A part that isn't multipart decodes to bytes, its transfer encoding
    # undone.
    body = cast("bytes", part.get_payload(decode=True))
    if not kind.startswith("text/") and not kind.endswith(
        ("/xml", "+xml", "/javascript", "/json")
    ):
        if redactor.finds(body.decode("latin-1")):
            raise ScreenshotWithheldError("a binary part spells a bound value")
        return kind, body
    text = _scrubbed_text(_text(part, body, kind), redactor)
    text = _SHADOW.sub('<template shadowrootmode="open"', _REFERENCED.sub(prefix, text))
    if redactor.finds(text) or redactor.finds(html.unescape(text)):
        raise ScreenshotWithheldError(
            "a bound value's spelling is left in a part after the scrub"
        )
    return f"{kind}; charset=utf-8", text.encode()


def _text(part: Message, body: bytes, kind: str) -> str:
    declared = part.get_content_charset()
    if declared is None and kind in ("text/html", "application/xhtml+xml"):
        named = _DECLARED.search(body[:1024])
        declared = None if named is None else named[1].decode("ascii")
    # An encoding Python doesn't have, one that isn't text's, or bytes it
    # doesn't hold. The error is dropped, not chained: it holds the part.
    with suppress(LookupError, UnicodeDecodeError):
        return body.decode(declared or "utf-8")
    raise ScreenshotWithheldError("a part is in an encoding the scrub can't read")


def _scrubbed_text(text: str, redactor: Redactor) -> str:
    """`text` with each span the scan finds cut out, raw, then with HTML's
    character references decoded, where a span takes every reference it
    touches."""
    text = _cut(text, redactor.spans(text))
    if not redactor.finds(html.unescape(text)):
        return text
    pieces: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    cursor = 0
    for found in (*_REFERENCE.finditer(text), None):
        stop = len(text) if found is None else found.start()
        pieces.append(text[cursor:stop])
        starts.extend(range(cursor, stop))
        ends.extend(range(cursor + 1, stop + 1))
        if found is not None:
            decoded = html.unescape(found[0])
            pieces.append(decoded)
            starts.extend([found.start()] * len(decoded))
            ends.extend([found.end()] * len(decoded))
            cursor = found.end()
    spans = redactor.spans("".join(pieces))
    return _cut(text, [(starts[start], ends[end - 1]) for start, end in spans])


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor : max(cursor, start)])
        cursor = max(cursor, end)
    pieces.append(text[cursor:])
    return "".join(pieces)


def _plan(dom: _Snapshot, document: int, redactor: Redactor) -> _Document:
    """What a render must find of document `document`, taken before the
    scrub, in the order a DOM snapshot lists it, its flat tree's, leaving
    out what the archive leaves out. Each field's value is dropped (made
    empty) when the scan finds a bound value in it or it shares
    `SHARED_RUN` characters with one; a frame whose document isn't in the
    snapshot is None."""
    strings = dom["strings"]
    nodes = dom["documents"][document]["nodes"]
    inputs = _rare(nodes.get("inputValue"))
    texts = _rare(nodes.get("textValue"))
    documents = _rare(nodes.get("contentDocumentIndex"))
    checked = set(nodes.get("inputChecked", {"index": []})["index"])
    selected = set(nodes.get("optionSelected", {"index": []})["index"])
    plan = _Document(kinds=[], values=[], checked=[], selected=[], frames=[])
    heads: set[int] = set()
    omitted: set[int] = set()
    # The kind of an option in each node under a select, by the flat tree.
    owners: dict[int, str] = {}
    selects = 0
    for node, name in enumerate(nodes["nodeName"]):
        element = strings[name].lower()
        named = _attributes(nodes, strings, node)
        # A snapshot lists a node after its parent.
        parent = nodes["parentIndex"][node]
        if element == "head" or parent in heads:
            heads.add(node)
        # What Blink's archive leaves out, with all it holds: a <noscript>,
        # and what MHTMLFrameSerializerDelegate::ShouldIgnoreHiddenElement
        # does (#171's probes fix-q2, fix-q3).
        if parent in omitted or (
            "hidden" in named
            or element == "noscript"
            or (element == "input" and named.get("type", "").lower() == "hidden")
            or (element == "iframe" and node in heads)
        ):
            omitted.add(node)
            continue
        if element == "select":
            owners[node] = f"{selects}{' multiple' if 'multiple' in named else ''}"
            selects += 1
        elif parent in owners:
            owners[node] = owners[parent]
        match element:
            # https://chromedevtools.github.io/devtools-protocol/tot/DOMSnapshot/#type-NodeTreeSnapshot
            # A textarea's value is its textValue, not its inputValue.
            case "input" | "textarea":
                kind = named.get("type") if element == "input" else None
                plan["kinds"].append((element, kind))
                held = (inputs if element == "input" else texts).get(node, -1)
                value = strings[held] if held >= 0 else ""
                shown = not (
                    redactor.finds(value) or redactor.overlaps(value, SHARED_RUN)
                )
                plan["values"].append(value if shown else "")
                plan["checked"].append(node in checked)
            case "iframe" | "frame":
                plan["kinds"].append((element, None))
                inner = documents.get(node)
                plan["frames"].append(
                    None if inner is None else _plan(dom, inner, redactor)
                )
            case _ if "contenteditable" in named:
                plan["kinds"].append((element, "contenteditable"))
            case "option":
                plan["kinds"].append((element, owners.get(node)))
                plan["selected"].append(node in selected)
    return plan


def _rare(data: _Rare | None) -> dict[int, int]:
    return {} if data is None else dict(zip(data["index"], data["value"], strict=True))


def _editing_unmarked(dom: _Snapshot) -> bool:
    """Whether a node the page draws is editable with no `contenteditable`
    attribute on it or an element around it: a designMode document, or
    editing a style the archive leaves out turned on. A render couldn't
    tell to empty it."""
    strings = dom["strings"]
    for document in dom["documents"]:
        nodes = document["nodes"]
        layout = document["layout"]
        for node, styles in zip(layout["nodeIndex"], layout["styles"], strict=True):
            editable = any(strings[style].startswith("read-write") for style in styles)
            if editable and not _marked(nodes, strings, node):
                return True
    return False


def _marked(nodes: _Nodes, strings: list[str], node: int) -> bool:
    """Whether the nearest `contenteditable` attribute on `node` or around
    it turns editing on."""
    while node >= 0:
        named = _attributes(nodes, strings, node)
        if "contenteditable" in named:
            return named["contenteditable"].casefold() != "false"
        node = nodes["parentIndex"][node]
    return False


def _attributes(nodes: _Nodes, strings: list[str], node: int) -> dict[str, str]:
    """`node`'s attributes by name, as the render's DOM names them."""
    attributes = nodes["attributes"][node]
    return {
        strings[name]: strings[value]
        for name, value in zip(attributes[::2], attributes[1::2], strict=True)
    }


async def _render(
    session: BrowserSession,
    parts: _Parts,
    main: str,
    plan: _Document,
    scroll: tuple[float, float],
) -> bytes:
    settings = session.settings
    width, height = settings.viewport
    # A context that names no proxy takes the launch's fail-closed one
    # (`aqa_runner.sandbox.FAIL_CLOSED_PROXY`), and every request is answered
    # from the archive or refused: nothing leaves the render.
    # https://playwright.dev/python/docs/api/class-browser#browser-new-context
    context = await session.browser.new_context(
        java_script_enabled=False,
        service_workers="block",
        accept_downloads=False,
        viewport={"width": width, "height": height},
        device_scale_factor=settings.device_scale_factor,
        color_scheme=settings.color_scheme,
        locale=settings.locale,
    )
    try:
        await context.route("**/*", partial(_answer, parts))
        page = await context.new_page()
        await page.goto(main, wait_until="load")
        shown = _SHOWN.validate_python(
            await page.evaluate(RESTORE, {"plan": plan, "scroll": scroll})
        )
        if shown is None:
            raise ScreenshotWithheldError(
                "the snapshot's fields, frames, options and editable elements "
                "don't match its render's"
            )
        # What the render's parser and fields made of the scrubbed text: a
        # NUL it drops, or a newline a text field takes out, can join a value.
        if any(
            session.redactor.finds(text) for text in (shown["text"], *shown["values"])
        ):
            raise ScreenshotWithheldError("the render spells a bound value")
        # A value the snapshot didn't hold, from a frame it left out, or a
        # field's `value` attribute, is dropped by the same rule.
        await page.evaluate(
            DROP,
            [
                place
                for place, value in enumerate(shown["values"])
                if session.redactor.overlaps(value, SHARED_RUN)
            ],
        )
        # In CSS pixels, as the live page's capture is.
        return await page.screenshot(
            type="png", animations="disabled", caret="hide", scale="css"
        )
    finally:
        await context.close()


async def _answer(parts: _Parts, route: Route) -> None:
    found = parts.get(urldefrag(route.request.url).url)
    if found is None:
        await route.abort()
    else:
        kind, body = found
        await route.fulfill(body=body, content_type=kind)
