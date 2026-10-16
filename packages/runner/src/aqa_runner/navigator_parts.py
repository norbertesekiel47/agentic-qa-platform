"""Parts (ADR-0025's #53 P19 amendment): elements a snapshot gives no ref of
their own, such as the count inside a button's name, described under an
element that has one so a check can bind them.

`Parts.describe`, the body of the navigator's `describe_parts` tool (#53
P19b), lists up to `PARTS` element descendants of a ref's element, in
document order and at most `DEPTH` deep, each under a ref `p<g>.<k>` (the
attempt's g-th description) with its tag, its stable classes and its
rendered text. The page chooses each of those, so each is scanned as the
page wrote it, then stripped of its gaps (`bound_secrets.GAP`) and
normalized, then scanned again, and the text is cut to `TEXT_CHARS` only
after both scans.
The list is read in the page's world, so it is the page's word. A
description's parts serve only the call of the turn after it: the end of
that turn, an action, another description, a change of the page's documents
or the end of the attempt lets them go, with their handles. A part binds
only while the utility world finds it still inside its element
(`binding.within`), then as `Seen(handle)`, with no role or name, through
its check's `TargetUses`, whose subject contract decides."""

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

from aqa_core.text import normalize
from playwright.async_api import ElementHandle, Error, JSHandle
from pydantic import Field, StrictInt, StringConstraints, TypeAdapter, ValidationError

from aqa_runner.binding import within
from aqa_runner.bound_secrets import GAP
from aqa_runner.browser_session import RENDERED_TEXT, BrowserSession, RefError
from aqa_runner.document_origins import DocumentChangedError
from aqa_runner.locator_generation import Seen, stable
from aqa_runner.redaction import Redacted, Redactor

# What one description holds at most: its parts, how deep they sit under
# the described element, and how much of each one's text it shows.
PARTS = 12
DEPTH = 4
TEXT_CHARS = 40

# A part's ref, in the one ref argument the check tools take.
PART_REF = re.compile(r"p([1-9][0-9]{0,8})\.([1-9][0-9]?)")

# The element's descendants and their facts, and its own text as it renders
# and as the page wrote it, read in one evaluation so all are of one moment.
# A descendant that isn't HTML, whose text `text_of` won't read, shows none.
_DESCENDANTS = f"""(root) => {{
    const rendered = {RENDERED_TEXT};
    const elements = [], facts = [];
    const walk = (node, depth) => {{
        for (const child of node.children) {{
            if (elements.length === {PARTS}) return;
            let text = "";
            try {{ text = rendered(child); }} catch {{}}
            elements.push(child);
            facts.push({{tag: child.localName, classes: [...child.classList], depth, text}});
            if (depth < {DEPTH}) walk(child, depth + 1);
        }}
    }};
    walk(root, 1);
    return [elements, [facts, [root.innerText ?? "", root.textContent]]];
}}"""

_Text = Annotated[str, StringConstraints(strict=True)]
_Token = Annotated[str, StringConstraints(strict=True, max_length=256)]
# A tag shows unquoted, and its scan folds whitespace into single spaces, so
# one holding whitespace could read as more parts on its line. Only a lying
# or malformed page writes whitespace or a control character in a tag.
_Tag = Annotated[_Token, StringConstraints(pattern=r"^[^\s\x00-\x1f\x7f-\x9f]+$")]


class _Fact(TypedDict):
    tag: _Tag
    classes: Annotated[list[_Token], Field(max_length=256)]
    depth: Annotated[StrictInt, Field(ge=1, le=DEPTH)]
    text: _Text


_READING: TypeAdapter[tuple[list[_Fact], tuple[str, str]]] = TypeAdapter(
    tuple[Annotated[list[_Fact], Field(max_length=PARTS)], tuple[_Text, _Text]]
)

_LIED = "the page described the element's parts as no element is"
# A part's text, in place, when the described element's own text holds a
# bound value: the snapshot joins that value and marks it, while the parts
# could show it in pieces no scan joins.
_WITHHELD = "(text withheld: the described element's text holds a test secret)"
_UNREAD = (
    "the element's parts couldn't be read: it left the page, or the page's "
    "scripts broke the reading"
)


class PartError(RefError):
    """A part's ref that names no part this call may use. Our own words, so
    it is presented as a snapshot ref's is."""


@dataclass(frozen=True)
class Described:
    """What `describe_parts` did: its result, and the parts block the next
    request carries, "" when it listed none."""

    text: Redacted
    parts: Redacted


@dataclass(frozen=True)
class _Description:
    ancestor: ElementHandle
    parts: dict[str, ElementHandle]
    changes: int


@dataclass
class Parts:
    """The attempt's last description, held, and whether its parts serve
    this turn's call: described in one turn, they serve the next."""

    described: int = field(default=0, init=False)
    _last: _Description | None = field(default=None, init=False)
    _serving: bool = field(default=False, init=False)

    async def turned(self) -> None:
        """A turn ended: parts it described serve the next turn's call, and
        parts that served it go."""
        if self._serving:
            await self.void()
        self._serving = self._last is not None

    async def void(self) -> None:
        """Let go of the last description and every handle it holds; each
        one is disposed whatever raises (`_let_go`)."""
        last, self._last, self._serving = self._last, None, False
        if last is not None:
            await _let_go([last.ancestor, *last.parts.values()])

    async def describe(self, session: BrowserSession, ref: str) -> Described:
        """List the parts of the element `ref` names, in place of any earlier
        description's. A change of the page's documents while it reads them
        discards the list (`DocumentChangedError`). Every handle it makes is
        the description's once it returns one, and let go on any other exit."""
        await self.void()
        self.described += 1
        changes = session.document_changes
        ancestor = await session.locate(ref)
        held: list[JSHandle] = [ancestor]
        try:
            elements, facts, own = await _listed(session, ancestor)
            held += elements
            if session.document_changes != changes:
                raise DocumentChangedError
            said = session.redactor.redact
            if not elements:
                return Described(said(f"{ref} has no element inside it"), said(""))
            g = self.described
            withheld = any(map(session.redactor.finds, own))
            lines = [f"{ref}'s parts, for your next call only:"]
            for k, fact in enumerate(facts, 1):
                indent = "  " * (fact["depth"] - 1)
                shown = _shown(fact, session.redactor, withheld=withheld)
                lines.append(f"{indent}- p{g}.{k} {shown}")
            self._last = _Description(
                ancestor, {f"p{g}.{k}": e for k, e in enumerate(elements, 1)}, changes
            )
            held = []
            return Described(
                said(f"describe_parts listed {ref}'s parts"), said("\n".join(lines))
            )
        finally:
            await _let_go(held)

    async def seen(self, session: BrowserSession, ref: str) -> Seen:
        """The part `ref` names, as a use offers it: while it serves this
        turn's call, the page's documents are the ones it was described in,
        and it is still inside its element. `PartError` otherwise; a moved
        count lets the description go."""
        last = self._last if self._serving else None
        element = None if last is None else last.parts.get(ref)
        if last is None or element is None:
            given = PART_REF.fullmatch(ref)
            if given is not None and int(given[1]) <= self.described:
                raise PartError(
                    f"{ref} is from an earlier describe_parts, and parts serve only "
                    "the call after it: describe the element again"
                )
            raise PartError(f"{ref[:40]!r} isn't a part describe_parts gave")
        if session.document_changes != last.changes:
            await self.void()
            raise PartError(
                f"{ref} is void: the page changed documents since describe_parts "
                "listed it, so describe the element again"
            )
        async with session.observing_page():
            inside = await within(last.ancestor, element)
        if not inside:
            raise PartError(
                f"{ref} has left the element describe_parts listed it under: "
                "describe that element again"
            )
        return Seen(element)


async def _listed(
    session: BrowserSession, ancestor: ElementHandle
) -> tuple[list[ElementHandle], list[_Fact], tuple[str, str]]:
    """The element's parts, their facts and its own text (`_DESCENDANTS`),
    read as an observation is: the facts first, bounded, then one handle per
    fact, so the page can't choose how many handles are made. The temporary
    goes before the parts are returned, and the parts go unless they are,
    whatever raises."""
    temporary: list[JSHandle] = []
    listed: list[JSHandle] = []
    returned = False
    try:
        try:
            async with session.observing_page():
                temporary.append(pair := await ancestor.evaluate_handle(_DESCENDANTS))
                facts, own = _READING.validate_python(
                    await pair.evaluate("(p) => p[1]")
                )
                for k in range(len(facts)):
                    # Kept as each is made, so a failure part way lets go of
                    # those made before it.
                    part = await pair.evaluate_handle("(p, k) => p[0][k]", k)
                    listed.append(part)
        finally:
            await _let_go(temporary)
        elements = [each for handle in listed if (each := handle.as_element())]
        if len(elements) != len(listed):
            raise PartError(_LIED)
        returned = True
    except ValidationError:
        raise PartError(_LIED) from None
    except Error:
        # Playwright's general error type: here the element left the page or
        # the page's own scripts broke the reading. A closed page is not.
        if session.page.is_closed():
            raise
        raise PartError(_UNREAD) from None
    finally:
        if not returned:
            await _let_go(listed)
    return elements, facts, own


async def _let_go(handles: list[JSHandle]) -> None:
    """Dispose every one of `handles`, emptying the list. Each is disposed in
    a `finally` of the rest's, so a failure or a cancellation still disposes
    the others; a cancellation or other non-Exception already being raised
    stays the one raised, a later failure its cause (as `locators._let_go`
    and `binding._forget` keep it)."""
    if not handles:
        return
    handle = handles.pop()
    try:
        await _let_go(handles)
    finally:
        earlier = sys.exception()
        try:
            await handle.dispose()
        except BaseException as error:
            if isinstance(earlier, Exception | None):
                raise
            raise earlier from error


def _scanned(text: str, redactor: Redactor, limit: int | None = None) -> Redacted:
    """`text` as the model reads it: scanned as the page wrote it, then
    without its gaps and normalized, so the cut counts only what shows, and
    scanned again before the cut (the consumer rule of ADR-0026's #53 ZW
    amendment)."""
    shown = normalize(GAP.sub("", redactor.redact(text)))
    return redactor.redact(shown, limit=limit)


def _shown(fact: _Fact, redactor: Redactor, *, withheld: bool) -> str:
    """A part as the model reads it: its tag and stable classes, then its
    text, or `_WITHHELD` when `withheld`, each scanned (`_scanned`) before
    the line joins them, and the text cut only after its scans, so a cut
    never leaves part of a value the whole text would show."""
    named = _scanned(
        fact["tag"] + "".join(f".{name}" for name in fact["classes"] if stable(name)),
        redactor,
    )
    if withheld:
        return f"{named} {_WITHHELD}"
    text = _scanned(fact["text"], redactor, TEXT_CHARS)
    return named + (f" {json.dumps(text, ensure_ascii=False)}" if text else "")
